"""End-to-end tests for the signal-driven momentum engine.

These build a SYNTHETIC price dataset so the engine's decisions can be
asserted exactly, rather than depending on whatever the live market
happens to look like.

The bug this suite exists for
-----------------------------
The momentum bot was replay-driven: it re-ran a ~6-year backtest and
picked the CSV rows whose ``date`` equalled today. The backtest's last
signal was 2026-11-18, so the bot produced ``count: 0`` every session
from 2026-11-19 and never submitted an order. The replacement computes
the signal LIVE, so it must produce entries whenever the market data
says so — regardless of what a historical backtest happened to do.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.momentum_signal import compute_signal_orders


def _write_dataset(
    data_dir: Path,
    symbols: dict[str, list[tuple[str, float]]],
    *,
    universe: list[str] | None = None,
) -> None:
    """Write a minimal on-disk dataset the engine can load."""
    prices_dir = data_dir / "prices"
    prices_dir.mkdir(parents=True, exist_ok=True)

    for symbol, bars in symbols.items():
        payload = {
            "items": [
                {
                    "time": date,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1_000_000,
                }
                for date, close in bars
            ]
        }
        (prices_dir / f"{symbol}.json").write_text(
            json.dumps(payload), encoding="utf-8")

    tickers = list(symbols) if universe is None else universe
    # ``build_candidate_price_table`` filters on the exchange code, so
    # the metadata must carry one.
    (data_dir / "symbols.json").write_text(
        json.dumps({"items": [
            {"ticker": t, "exchangeCode": "HOSE"} for t in tickers
        ]}),
        encoding="utf-8",
    )


def _rising_bars(symbol: str, n: int = 120, start: float = 10_000.0,
                  step: float = 200.0) -> list[tuple[str, float]]:
    """A steadily rising series — a strong momentum candidate."""
    from datetime import date, timedelta

    out = []
    day = date(2026, 1, 1)
    price = start
    for i in range(n):
        while day.weekday() >= 5:  # skip weekends
            day += timedelta(days=1)
        out.append((day.isoformat(), price))
        price += step
        day += timedelta(days=1)
    return out


def _flat_bars(symbol: str, n: int = 120, price: float = 10_000.0
               ) -> list[tuple[str, float]]:
    from datetime import date, timedelta

    out = []
    day = date(2026, 1, 1)
    for _ in range(n):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append((day.isoformat(), price))
        day += timedelta(days=1)
    return out


def _falling_bars(symbol: str, n: int = 120, start: float = 30_000.0,
                  step: float = -200.0) -> list[tuple[str, float]]:
    """A steadily falling series."""
    from datetime import date, timedelta

    out = []
    day = date(2026, 1, 1)
    price = start
    for _ in range(n):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append((day.isoformat(), price))
        price += step
        day += timedelta(days=1)
    return out


def _config(**overrides) -> StrategyConfig:
    base = dict(
        initialCash=100_000_000.0,
        maxPositions=5,
        bearMaxPositions=2,
        rebalanceDays=1,
        return1mDays=10,
        return3mDays=15,
        return6mDays=20,
        return12mDays=20,
        smaSlowDays=20,
        smaFastDays=10,
        boardLotSize=100,
        exitRank=20,
        stopLossPercent=8.0,
        trailingStopPercent=9.0,
        neutralTrailingStopPercent=6.0,
        bearTrailingStopPercent=4.0,
        bullExposurePercent=100.0,
        neutralExposurePercent=60.0,
        bearExposurePercent=20.0,
        sellSettlementDays=3,
        maxBreakoutEntriesPerWeek=1,
        breakoutMaxRank=20,
        useVolTargeting=False,
        useCorporateActionGuard=False,
        usePointInTimeUniverse=False,
        momentum1mWeight=0.0,
        momentum3mWeight=0.6,
        momentum6mWeight=0.4,
        momentum12mWeight=0.0,
    )
    base.update(overrides)
    return StrategyConfig(**base)


class TestLiveSignalGeneration:
    """THE point of the change: a live signal produces orders."""

    def test_a_rising_market_produces_entries(self, tmp_path: Path) -> None:
        """The replay-driven bot produced 0 orders; the live one must not."""
        symbols = {
            "AAA": _rising_bars("AAA", step=300.0),
            "BBB": _rising_bars("BBB", step=200.0),
        }
        _write_dataset(tmp_path, symbols)

        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-06-30",
        )

        assert result["regime"] is not None
        assert result["entries"], (
            "the live engine must produce entries when the market data "
            "says so — this is the whole point of the change"
        )
        entry_symbols = {e["symbol"] for e in result["entries"]}
        assert "AAA" in entry_symbols, result["candidates"]

    def test_the_entries_are_sized_to_a_board_lot(self, tmp_path: Path) -> None:
        _write_dataset(tmp_path, {"AAA": _rising_bars("AAA", step=300.0)})
        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-06-30",
        )
        for entry in result["entries"]:
            assert entry["quantity"] % 100 == 0, entry
            assert entry["quantity"] > 0, entry

    def test_the_plan_is_not_empty_merely_because_a_backtest_is_stale(
        self, tmp_path: Path
    ) -> None:
        """The replay read a HISTORICAL CSV; the live engine does not.

        Even with no backtest artefacts on disk at all, the live engine
        must still produce a plan.
        """
        _write_dataset(tmp_path, {"AAA": _rising_bars("AAA", step=300.0)})
        assert not (tmp_path / "backtests").exists()

        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-06-30",
        )
        assert result["entries"], (
            "no backtest CSV exists, yet the live engine must still "
            "compute a signal from the price data"
        )


class TestRebalanceGate:
    """Top-rank entries only happen on a rebalance day.

    The backtest enters the top-ranked names only when
    ``index % rebalanceDays == 0``; on the other days it may enter
    volume-breakout names only. The live engine originally ignored
    this and bought the top rank EVERY session.
    """

    def test_a_non_rebalance_day_does_not_enter_the_top_rank(
        self, tmp_path: Path
    ) -> None:
        _write_dataset(tmp_path, {"AAA": _rising_bars("AAA", step=300.0)})
        result = compute_signal_orders(
            data_dir=tmp_path,
            strategy_config=_config(rebalanceDays=5),
            trade_date="2026-06-30",
        )
        assert not result["entries"], (
            "on a non-rebalance day the strategy must not enter the "
            "top-ranked name (it may only enter a volume breakout)"
        )

    def test_a_rebalance_day_enters_the_top_rank(self, tmp_path: Path) -> None:
        _write_dataset(tmp_path, {"AAA": _rising_bars("AAA", step=300.0)})
        result = compute_signal_orders(
            data_dir=tmp_path,
            strategy_config=_config(rebalanceDays=1),
            trade_date="2026-06-30",
        )
        assert result["entries"], result["blocked"]


class TestLiveNavSizing:
    """The order size must be based on the LIVE NAV, not the backtest's.

    The backtest sizes against ``StrategyConfig.initialCash`` = 1B VND.
    That number is FICTIONAL — the live account holds ~22M VND. Sizing
    a live order against 1B would order ~45x the money that is actually
    in the account.
    """

    def test_the_nav_falls_back_to_initial_cash_without_a_live_value(
        self, tmp_path: Path
    ) -> None:
        from vietnam_algo.libs.momentum_signal import _nav_from_positions

        nav = _nav_from_positions({}, StrategyConfig(initialCash=1_000_000_000.0))
        assert nav == pytest.approx(1_000_000_000.0)

    def test_the_live_nav_is_used_when_known(self, tmp_path: Path) -> None:
        from vietnam_algo.libs.momentum_signal import _nav_from_positions

        nav = _nav_from_positions(
            {}, StrategyConfig(initialCash=1_000_000_000.0),
            live_nav=22_159_907.0,
        )
        assert nav == pytest.approx(22_159_907.0), (
            "the live NAV must win over the backtest's fictional cash"
        )

    def test_a_missing_live_nav_is_reported(self, tmp_path: Path) -> None:
        """The failure mode must be visible, not silent."""
        _write_dataset(tmp_path, {"AAA": _rising_bars("AAA", step=300.0)})
        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(), trade_date="2026-06-30",
        )
        assert any("live NAV unavailable" in n for n in result["notes"]), (
            result["notes"]
        )

    def test_the_entry_size_scales_with_the_live_nav(self, tmp_path: Path) -> None:
        """A 45x smaller NAV must produce a ~45x smaller order."""
        from vietnam_algo.libs.momentum_signal import _entry_quantity

        cfg = _config()
        big = _entry_quantity(symbol="AAA", price=10_000.0, regime="bull",
                              strategy_config=cfg, nav=1_000_000_000.0,
                              max_positions=5)
        small = _entry_quantity(symbol="AAA", price=10_000.0, regime="bull",
                                strategy_config=cfg, nav=22_000_000.0,
                                max_positions=5)
        assert small < big
        assert small * 40 < big, (small, big)


class TestPositionCap:

    def test_the_position_cap_limits_the_entries(self, tmp_path: Path) -> None:
        symbols = {
            f"S{i}": _rising_bars(f"S{i}", step=300.0 - i * 10)
            for i in range(6)
        }
        _write_dataset(tmp_path, symbols)

        result = compute_signal_orders(
            data_dir=tmp_path,
            strategy_config=_config(maxPositions=2, bearMaxPositions=2),
            trade_date="2026-06-30",
        )
        assert len(result["entries"]) <= 2, result["entries"]
        assert result["maxPositions"] == 2

    def test_held_positions_consume_the_position_cap(self, tmp_path: Path) -> None:
        """Two positions already held + a cap of 2 -> no new entries."""
        symbols = {
            f"S{i}": _rising_bars(f"S{i}", step=300.0 - i * 10)
            for i in range(4)
        }
        _write_dataset(tmp_path, symbols)

        held = [
            {"symbol": "S0", "quantity": 100, "avgPrice": 10_000.0},
            {"symbol": "S1", "quantity": 100, "avgPrice": 10_000.0},
        ]
        result = compute_signal_orders(
            data_dir=tmp_path,
            strategy_config=_config(maxPositions=2, bearMaxPositions=2),
            trade_date="2026-06-30", live_positions=held,
        )
        assert not result["entries"], result["entries"]
        assert result["blocked"], "the surplus candidates must be reported"
        assert all(b["reason"] in ("already held", "position cap reached (2 for bull)")
                   for b in result["blocked"]), result["blocked"]


class TestHeldPositions:

    def test_a_held_symbol_is_not_re_entered(self, tmp_path: Path) -> None:
        _write_dataset(tmp_path, {"AAA": _rising_bars("AAA", step=300.0)})

        held = [{"symbol": "AAA", "quantity": 100, "avgPrice": 10_000.0}]
        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-06-30", live_positions=held,
        )
        assert "AAA" not in {e["symbol"] for e in result["entries"]}
        assert any(b["symbol"] == "AAA" for b in result["blocked"])

    def test_the_live_portfolio_is_authoritative_for_quantity(
        self, tmp_path: Path
    ) -> None:
        """A position sold manually must not linger in the tracked state."""
        _write_dataset(tmp_path, {"AAA": _rising_bars("AAA", step=300.0)})

        held = [{"symbol": "AAA", "quantity": 100, "avgPrice": 10_000.0}]
        compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-06-30", live_positions=held,
        )
        state_path = tmp_path / "live" / "momentum_signal_state.json"
        state = json.loads(state_path.read_text())
        assert state["positions"]["AAA"]["quantity"] == 100

        # The position is sold manually — the live portfolio no longer
        # has it. The strategy then re-enters it (it is the top
        # candidate and the slot is free), but the RE-ENTRY must carry
        # a FRESH entry price, not the stale one from the old position.
        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-06-30", live_positions=[],
        )
        assert result["entries"], (
            "with no live position the strategy should re-enter the "
            "top candidate"
        )
        state = json.loads(state_path.read_text())
        entry = state["positions"]["AAA"]
        assert entry["quantity"] == result["entries"][0]["quantity"]
        assert entry["entry_price"] == pytest.approx(
            result["entries"][0]["price"]
        )

    def test_a_position_that_is_no_longer_live_is_pruned(self, tmp_path: Path) -> None:
        """The tracked state must not keep a position we no longer hold.

        We check the pruning directly by making the strategy unable to
        re-enter the symbol — a position in the exit path is dropped
        from the book.
        """
        _write_dataset(tmp_path, {"AAA": _rising_bars("AAA", step=300.0)})

        held = [{"symbol": "AAA", "quantity": 100, "avgPrice": 10_000.0}]
        compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-06-30", live_positions=held,
        )
        state_path = tmp_path / "live" / "momentum_signal_state.json"
        assert "AAA" in json.loads(state_path.read_text())["positions"]

        # Now the symbol is no longer a candidate at all (the price
        # series changed), so it can neither be held nor re-entered.
        _write_dataset(tmp_path, {"BBB": _rising_bars("BBB", step=300.0)})
        compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-06-30", live_positions=[],
        )
        state = json.loads(state_path.read_text())
        assert "AAA" not in state["positions"], (
            "a position that is no longer live must be pruned from the "
            "tracked state"
        )


class TestExits:

    def test_a_position_below_its_stop_is_exited(self, tmp_path: Path) -> None:
        """A live position that fell through its stop must be sold."""
        # The price falls from 30,000 to ~6,000, so a position bought at
        # 20,000 is far below its 8 % stop.
        symbols = {"AAA": _falling_bars("AAA", start=30_000.0, step=-200.0)}
        _write_dataset(tmp_path, symbols)

        held = [{"symbol": "AAA", "quantity": 100, "avgPrice": 20_000.0}]
        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-06-30", live_positions=held,
        )
        assert result["exits"], (
            "a position below its stop-loss must produce an exit order"
        )
        exit_order = result["exits"][0]
        assert exit_order["symbol"] == "AAA"
        assert exit_order["side"] == "NS"
        assert exit_order["quantity"] == 100
        assert exit_order["exitReason"] == "stop_loss"

    def test_a_healthy_position_is_not_exited(self, tmp_path: Path) -> None:
        _write_dataset(tmp_path, {"AAA": _rising_bars("AAA", step=300.0)})
        held = [{"symbol": "AAA", "quantity": 100, "avgPrice": 10_000.0}]
        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-06-30", live_positions=held,
        )
        assert not result["exits"], result["exits"]


class TestSignalDate:

    def test_the_signal_uses_the_last_available_bar(self, tmp_path: Path) -> None:
        """Today's bar may not be in the data yet — do not go stale."""
        _write_dataset(tmp_path, {"AAA": _rising_bars("AAA", step=300.0)})
        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2027-01-01",
        )
        assert result["signalDate"] != "2027-01-01"
        assert any("last available bar" in n for n in result["notes"]), (
            result["notes"]
        )
