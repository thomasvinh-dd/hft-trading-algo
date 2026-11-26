"""Tests for the signal-driven momentum engine.

2026-11-26. The momentum bot was replay-driven: every EOD cycle it
re-ran a ~6-year backtest and picked the CSV rows whose ``date`` equalled
today. The backtest's last signal was 2026-11-18, so the bot generated
``count: 0`` for every session from 2026-11-19 onward and never once
submitted an order.

These tests cover the replacement engine: the live signal computation,
the exit logic, the settlement gate and the output contract.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.momentum_signal import (
    SignalPosition,
    SignalState,
    _position_sellable,
    _round_to_board_lot,
    compute_signal_orders,
    load_signal_state,
    save_signal_state,
    write_signal_outputs,
)


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------

def _price_table(rows: dict[str, dict[str, float]]) -> dict:
    """Build a minimal price table: {symbol: {date: {close, volume}}}."""
    from vietnam_algo.libs.backtest import PriceBar

    out = {}
    for symbol, by_date in rows.items():
        out[symbol] = {}
        for date, close in by_date.items():
            out[symbol][date] = PriceBar(time=date, close=close, open=close,
                                         high=close, low=close, volume=1_000_000)
    return out


def _config(**overrides) -> StrategyConfig:
    base = dict(
        initialCash=100_000_000.0,
        maxPositions=5,
        bearMaxPositions=2,
        rebalanceDays=5,
        return6mDays=20,
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
        useVolTargeting=False,
        useCorporateActionGuard=False,
    )
    base.update(overrides)
    return StrategyConfig(**base)


# -----------------------------------------------------------------------------
# the settlement gate
# -----------------------------------------------------------------------------

class TestSettlementGate:
    """A position bought inside the settlement window cannot be sold."""

    def test_a_recently_bought_position_is_not_sellable(self) -> None:
        dates = ["2026-11-20", "2026-11-21", "2026-11-24", "2026-11-25"]
        position = SignalPosition(symbol="ABC", quantity=100,
                                  entry_date="2026-11-25", entry_index=3)
        assert not _position_sellable(position, "2026-11-25", dates, 3, _config())

    def test_a_position_past_the_window_is_sellable(self) -> None:
        dates = ["2026-11-20", "2026-11-21", "2026-11-24", "2026-11-25"]
        position = SignalPosition(symbol="ABC", quantity=100,
                                  entry_date="2026-11-20", entry_index=0)
        assert _position_sellable(position, "2026-11-25", dates, 3, _config())

    def test_an_unknown_entry_is_assumed_sellable(self) -> None:
        """A position adopted from the live portfolio has no entry date.

        Blocking it would mean the strategy could NEVER exit it.
        """
        dates = ["2026-11-20", "2026-11-25"]
        position = SignalPosition(symbol="ABC", quantity=100)
        assert _position_sellable(position, "2026-11-25", dates, 1, _config())

    def test_the_entry_index_fallback_is_used(self) -> None:
        dates = ["2026-11-20", "2026-11-25"]
        position = SignalPosition(symbol="ABC", quantity=100, entry_index=0)
        assert _position_sellable(position, "2026-11-25", dates, 1, _config())


# -----------------------------------------------------------------------------
# board lots
# -----------------------------------------------------------------------------

class TestBoardLot:

    def test_quantity_is_rounded_down_to_a_board_lot(self) -> None:
        assert _round_to_board_lot(350, 100) == 300
        assert _round_to_board_lot(399, 100) == 300
        assert _round_to_board_lot(400, 100) == 400

    def test_a_sub_lot_quantity_is_zero(self) -> None:
        assert _round_to_board_lot(99, 100) == 0

    def test_a_zero_lot_size_passes_through(self) -> None:
        assert _round_to_board_lot(123, 0) == 123


# -----------------------------------------------------------------------------
# the persisted state
# -----------------------------------------------------------------------------

class TestSignalState:

    def test_state_round_trips(self, tmp_path: Path) -> None:
        state = SignalState()
        state.positions["ABC"] = SignalPosition(
            symbol="ABC", quantity=100, entry_price=10_000.0,
            peak_price=11_000.0, entry_date="2026-11-20", entry_index=5,
        )
        state.breakout_entries_this_week["2026-W47"] = 1
        save_signal_state(tmp_path, state)

        loaded = load_signal_state(tmp_path)
        assert loaded.positions["ABC"].quantity == 100
        assert loaded.positions["ABC"].peak_price == 11_000.0
        assert loaded.positions["ABC"].entry_date == "2026-11-20"
        assert loaded.breakout_entries_this_week["2026-W47"] == 1

    def test_a_missing_state_file_loads_empty(self, tmp_path: Path) -> None:
        loaded = load_signal_state(tmp_path)
        assert loaded.positions == {}

    def test_a_corrupt_state_file_loads_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "live" / "momentum_signal_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert load_signal_state(tmp_path).positions == {}


# -----------------------------------------------------------------------------
# the output contract
# -----------------------------------------------------------------------------

class TestOutputContract:
    """The dry-run replay and the submit path read these files."""

    def test_the_csv_has_the_expected_schema(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "orders.csv"
        json_path = tmp_path / "orders.json"
        orders = [{
            "symbol": "HVN", "side": "NB", "price": 24_350.0,
            "quantity": 300, "kind": "entry",
            "reason": "momentum_signal:top_rank",
        }]
        write_signal_outputs(csv_path=csv_path, json_path=json_path,
                             orders=orders, trade_date="2026-11-26")

        with csv_path.open() as f:
            rows = list(csv.DictReader(f))
        assert list(rows[0].keys()) == [
            "orderId", "date", "symbol", "side", "price", "quantity",
            "value", "fee", "reason",
        ]
        assert rows[0]["symbol"] == "HVN"
        assert rows[0]["date"] == "2026-11-26"
        assert float(rows[0]["value"]) == pytest.approx(24_350.0 * 300)

    def test_the_json_carries_the_count_and_the_items(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "orders.csv"
        json_path = tmp_path / "orders.json"
        orders = [{"symbol": "HVN", "side": "NB", "price": 1.0,
                   "quantity": 100, "kind": "entry", "reason": "r"}]
        write_signal_outputs(csv_path=csv_path, json_path=json_path,
                             orders=orders, trade_date="2026-11-26")
        payload = json.loads(json_path.read_text())
        assert payload["count"] == 1
        assert payload["tradeDate"] == "2026-11-26"
        assert payload["items"][0]["symbol"] == "HVN"

    def test_the_backtest_shape_carries_the_replay_fields(self, tmp_path: Path) -> None:
        """The dry-run replay reads ``equityCurve[0].cash`` and ``metrics``."""
        csv_path = tmp_path / "b.csv"
        json_path = tmp_path / "b.json"
        write_signal_outputs(csv_path=csv_path, json_path=json_path,
                             orders=[], trade_date="2026-11-26",
                             as_backtest_result=True)
        payload = json.loads(json_path.read_text())
        assert "equityCurve" in payload
        assert "metrics" in payload
        assert "cash" in payload["equityCurve"][0]
        assert payload["metrics"]["tradeCount"] == 0


# -----------------------------------------------------------------------------
# the engine, end to end
# -----------------------------------------------------------------------------

class TestComputeSignalOrders:

    def test_no_price_data_returns_an_empty_plan(self, tmp_path: Path) -> None:
        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-11-26",
        )
        assert result["orders"] == []
        assert result["deals_actioned"] == []
        assert result["regime"] is None
        assert result["notes"]

    def test_the_plan_is_written_with_deals_actioned(self, tmp_path: Path) -> None:
        """``deals_actioned`` is the key the submit path reads."""
        result = compute_signal_orders(
            data_dir=tmp_path, strategy_config=_config(),
            trade_date="2026-11-26",
        )
        assert "deals_actioned" in result
        assert "orders" in result
        assert "entries" in result
        assert "exits" in result
        assert "blocked" in result
        assert "regime" in result

    def test_deals_carry_the_dnse_orders_the_submit_path_needs(self) -> None:
        from vietnam_algo.libs.momentum_signal import _build_deals_actioned

        orders = [
            {"symbol": "ABC", "side": "NB", "price": 10.0, "quantity": 100,
             "kind": "entry", "reason": "momentum_signal:top_rank"},
            {"symbol": "ABC", "side": "NS", "price": 11.0, "quantity": 100,
             "kind": "exit", "reason": "momentum_signal:trailing_stop"},
        ]
        deals = _build_deals_actioned(orders)
        assert len(deals) == 1
        deal = deals[0]
        assert deal["symbol"] == "ABC"
        assert deal["deal_id"] == "momentum:ABC"
        assert len(deal["dnse_orders"]) == 2
        assert deal["dnse_orders"][0]["side"] == "NB"
        assert deal["dnse_orders"][0]["quantity"] == 100

    def test_exits_and_entries_are_grouped_per_symbol(self) -> None:
        from vietnam_algo.libs.momentum_signal import _build_deals_actioned

        orders = [
            {"symbol": "ABC", "side": "NB", "price": 10.0, "quantity": 100,
             "kind": "entry", "reason": "r"},
            {"symbol": "XYZ", "side": "NS", "price": 11.0, "quantity": 200,
             "kind": "exit", "reason": "r"},
        ]
        deals = _build_deals_actioned(orders)
        assert {d["symbol"] for d in deals} == {"ABC", "XYZ"}
        assert deals[0]["action"] == "new_entry"
        assert deals[1]["action"] == "full_close"
