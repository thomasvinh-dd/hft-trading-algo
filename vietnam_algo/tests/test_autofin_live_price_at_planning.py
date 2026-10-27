"""Tests for the 2026-10-09 buy-price + zone-tolerance + live-quote
overrides in ``AutofinSignalAdapter.generate_orders``.

User feedback (2026-10-09):
  1. Lower the upper/lower zone tolerance from 5% to 3%
  2. Use the LIVE best-ask from DNSE at planning time, not
     yesterday's close from the local price file
  3. Set the entry price to the LIVE market price, without
     capping at min(cur, s_hi). The 3% zone check is the gate.

These tests pin the new behavior so a future refactor can't
silently regress to the old 5% / local-close / capped-price
behavior.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


class _FakeBar:
    """A duck-typed PriceBar with .close / .time / etc."""
    def __init__(self, close: float, time: str = "2026-10-08",
                 high: float | None = None, low: float | None = None,
                 open_: float | None = None, volume: int = 0) -> None:
        self.close = close
        self.time = time
        self.high = high if high is not None else close
        self.low = low if low is not None else close
        self.open = open_ if open_ is not None else close
        self.volume = volume


def _make_adapter():
    """Build an AutofinSignalAdapter with stubbed autofin + DNSE."""
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    adapter = AutofinSignalAdapter()
    stub_client = MagicMock()
    stub_client.fetch_holding.return_value = []
    stub_client.fetch_signals.return_value = []
    stub_client.fetch_closing.return_value = []
    adapter._client_lazy = lambda: stub_client
    return adapter, stub_client


def _state_with_signal() -> dict:
    """Build a minimal autofin_signal_state with one ABB signal."""
    return {
        "our_deals": {},
        "notified_order_keys": [],
        "last_poll_at": "2026-10-09T01:00:00Z",
        "last_notification_id": "",  # empty means "no events seen yet"
        "last_notification_poll_at": "2026-10-09T01:00:00Z",
        "notified_event_ids": [],
        "actioned_deals": [],
    }


def _make_signal_for(supply_from: float = 17100, supply_to: float = 17400,
                     rate: float = 0.30) -> dict:
    """Build a minimal /signal-static-suggest dict for ABB."""
    return {
        "_id": 1789516800, "code": 1789516800, "symbol": "ABB",
        "companyName": "test", "statusDeal": {
            "riskVol": 0.2, "timeHolding": 14, "isWin": False, "isClosed": False,
            "price_Open": 17400, "time_Open": "09/16/2026 07:00:00",
            "price_Closed": 0, "time_Closed": "",
            "price_StopLoss": 14700, "time_StopLoss": "",
            "max_Volume_Strag": 904620, "time_TrailingStop": "",
            "activeTrailingStop": False, "rate_TrailingStop": 0,
            "price_TrailingStop": -1,
            "market_Vol": 13400, "market_Close": 17500,
            "market_Highest": 17500, "market_Lowest": 17500,
            "market_TypePrice": 4, "rS_Rating_IDB": 0,
            "rateWinByPrice": 0.017241379310344827,
            "lastTimeUpdate": "10/07/2026 08:15:00",
        },
        "numberOfProfit": 0,
        "supply_F": supply_from, "supply_T": supply_to,
        "tP_F": 18400, "tP_T": 18400,
        "sL_F": 14700, "sL_T": 15900,
        "rateForNav": rate,
        "lastRecomment": "Khuyến nghị đầu tư ABB",
        "isWaittingBuy": False, "isWaittingSell": False,
        "rateIncome": 0, "rateIncome_TypePrice": 0,
    }


def _dnse_quote_response(symbol: str, g1_offer_kvnd: float) -> dict:
    """Build a /price/{sym}/quotes/latest response with G1 best offer."""
    return {
        "quotes": [
            {
                "symbol": symbol, "boardId": "G1",
                "bid": [{"price": g1_offer_kvnd - 0.1, "quantity": 100}],
                "offer": [
                    {"price": g1_offer_kvnd, "quantity": 200},
                    {"price": g1_offer_kvnd + 0.1, "quantity": 100},
                ],
            },
        ]
    }


# -----------------------------------------------------------------------------
# Test 1: zone tolerance is 3% (was 5%)
# -----------------------------------------------------------------------------


def test_zone_tolerance_is_3_percent_in_decide_orders(monkeypatch, tmp_path) -> None:
    """``decide_orders`` must use 3% tolerance, not 5%.

    Pins the user-facing 2026-10-09 change: the entry-zone gate
    now uses 3% (both upper and lower) instead of 5%. We use
    boundary values (3% above supply_T → still enter; 3.5% above
    → skip) to pin the exact threshold.
    """
    from vietnam_algo.algos.autofin_signal import decide_orders
    from vietnam_algo.libs.autofin_signals import AutofinSignal
    from vietnam_algo.libs.dry_run_state import save_dry_run_state, DryRunState
    save_dry_run_state(
        DryRunState(initial_nav=100_000_000, cash=100_000_000),
        tmp_path, strategy_name="autofin_signal",
    )
    sig = AutofinSignal(
        symbol="ABB", company_name="test",
        last_recomment="Khuyến nghị đầu tư",
        supply_from=17100, supply_to=17400,
        tp_from=18400, tp_to=18400,
        sl_from=14700, sl_to=15900,
        rate_for_nav=0.30,
        is_waitting_buy=False, is_waitting_sell=False,
        market_type=4, raw={},
    )

    # Upper bound test
    # At exactly 3% above supply_T=17400 → 17400*1.03 = 17922.
    # This must be the inclusive limit.
    all_orders, _, _ = decide_orders(
        holding=[], signals=[sig], our_deals={},
        our_positions={}, nav=100_000_000,
        current_prices={"ABB": 17922.0},  # exactly at 3% upper edge
        data_dir=tmp_path, actioned_deals=set(),
    )
    buys = [o for o in all_orders if o["side"] == "NB"]
    assert len(buys) == 1, "at exactly 3% above supply_T, should still enter"

    # Just above 3% (17923) → skip.
    all_orders, _, _ = decide_orders(
        holding=[], signals=[sig], our_deals={},
        our_positions={}, nav=100_000_000,
        current_prices={"ABB": 17923.0},  # 3.005% above
        data_dir=tmp_path, actioned_deals=set(),
    )
    assert all_orders == [], "just above 3% tolerance, should skip"

    # Lower bound test: 17100 * 0.97 = 16587. At 16587 → enter.
    all_orders, _, _ = decide_orders(
        holding=[], signals=[sig], our_deals={},
        our_positions={}, nav=100_000_000,
        current_prices={"ABB": 16587.0},
        data_dir=tmp_path, actioned_deals=set(),
    )
    assert len([o for o in all_orders if o["side"] == "NB"]) == 1

    # Just below (16586) → skip.
    all_orders, _, _ = decide_orders(
        holding=[], signals=[sig], our_deals={},
        our_positions={}, nav=100_000_000,
        current_prices={"ABB": 16586.0},
        data_dir=tmp_path, actioned_deals=set(),
    )
    assert all_orders == [], "just below 3% lower tolerance, should skip"


# -----------------------------------------------------------------------------
# Test 2: entry price = LIVE market price (not capped at min(cur, s_hi))
# -----------------------------------------------------------------------------


def test_entry_price_is_market_price_not_capped(monkeypatch, tmp_path) -> None:
    """Entry price = live market price, NOT min(cur, s_hi).

    Pins the user-facing 2026-10-09 change: previously the entry
    price was min(cur, s_hi) — capped at the buy-zone upper bound.
    Now the entry price is whatever the market shows (within 3%
    of the zone). The zone check is the gate; the entry price
    reflects the actual market, not an artificial cap.
    """
    from vietnam_algo.algos.autofin_signal import decide_orders
    from vietnam_algo.libs.autofin_signals import AutofinSignal
    from vietnam_algo.libs.dry_run_state import save_dry_run_state, DryRunState
    save_dry_run_state(
        DryRunState(initial_nav=100_000_000, cash=100_000_000),
        tmp_path, strategy_name="autofin_signal",
    )
    sig = AutofinSignal(
        symbol="ABB", company_name="test",
        last_recomment="Khuyến nghị đầu tư",
        supply_from=17100, supply_to=17400,
        tp_from=18400, tp_to=18400,
        sl_from=14700, sl_to=15900,
        rate_for_nav=0.30,
        is_waitting_buy=False, is_waitting_sell=False,
        market_type=4, raw={},
    )

    # Market at 17,600 — 1.15% above supply_T=17,400. Within 3%
    # tolerance, so the entry happens. Previously the price was
    # capped at min(17,600, 17,400) = 17,400; now it should be
    # the market price 17,600.
    all_orders, _, _ = decide_orders(
        holding=[], signals=[sig], our_deals={},
        our_positions={}, nav=100_000_000,
        current_prices={"ABB": 17600.0},
        data_dir=tmp_path, actioned_deals=set(),
    )
    buys = [o for o in all_orders if o["side"] == "NB"]
    assert len(buys) == 1
    # 2026-10-09 fix: entry price is the live market price.
    # Previously (pre-fix) this was capped at supply_T=17400.
    assert buys[0]["price"] == 17600, (
        f"expected entry at live market price 17600, got {buys[0]['price']}. "
        f"The pre-2026-10-09 code capped the price at min(cur, s_hi)={17400}."
    )


# -----------------------------------------------------------------------------
# Test 3: live-quote fetch at planning time in live-submit mode
# -----------------------------------------------------------------------------


def test_live_quote_overrides_local_price_in_live_submit_mode(
    monkeypatch, tmp_path, capsys
) -> None:
    """In ``live_submit=True`` mode, the strategy fetches the LIVE
    best-ask from DNSE and uses it as ``current_prices[sym]``,
    overriding the local close.

    Pins the user-facing 2026-10-09 change: previously the strategy
    used yesterday's close from ``data/prices/{SYMBOL}.json`` which
    can be hours stale. A 1-2 % mid-day drift would silently shift
    the planned entry price and break the zone-tolerance gate.
    """
    from vietnam_algo.libs.dnse.client import DnseClient

    # Stub the DNSE client directly (the strategy instantiates
    # ``DnseClient`` at line ~2842 with the env-var credentials)
    stub_dnse = MagicMock(spec=DnseClient)
    stub_dnse.get_latest_quote.return_value = _dnse_quote_response("ABB", 17.8)

    # Patch the DnseClient class to return our stub
    from vietnam_algo.libs.dnse import client as dnse_client_mod
    monkeypatch.setattr(dnse_client_mod, "DnseClient", lambda *a, **kw: stub_dnse)

    adapter = _make_adapter()[0]
    state = _state_with_signal()
    state_path = tmp_path / "live" / "autofin_signal_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))

    # Build a proper AutofinSignal (not a dict)
    from vietnam_algo.libs.autofin_signals import AutofinSignal, parse_signal
    sig = parse_signal(_make_signal_for())

    # Stub the autofin client (signals)
    adapter._client_lazy = lambda: MagicMock(
        fetch_holding=MagicMock(return_value=[]),
        fetch_signals=MagicMock(return_value=[sig]),
        fetch_closing=MagicMock(return_value=[]),
    )

    # Stub the local ABB price file to return 17,500 (yesterday's close)
    from vietnam_algo.libs import universe
    monkeypatch.setattr(
        universe, "load_price_bars",
        lambda sym, data_dir: [_FakeBar(close=17500.0)],
    )

    # Stub the portfolio snapshot (live NAV = 10M)
    from vietnam_algo.libs import portfolio
    monkeypatch.setattr(
        portfolio, "build_portfolio_snapshot",
        lambda *a, **kw: {
            "account": "0003937868",
            "totals": {"stockValue": 0, "portfolioValue": 10_000_000},
            "positions": [],
            "balance": {"withdrawableCash": 10_000_000, "availableCash": 10_000_000},
            "errors": [],
        },
    )

    cfg = MagicMock()
    cfg.dataDir = tmp_path
    cfg.universe = MagicMock()
    cfg.strategy = "autofin_signal"
    cfg.loanPackageId = 1775

    sc = MagicMock()
    monkeypatch.setenv("AUTOFIN_FORCE_NEW_ENTRY", "1")
    monkeypatch.setenv("AUTOFIN_NEW_ENTRY_TOP_N", "10")

    adapter.generate_orders(
        cfg, sc, trade_date="2026-10-09", live_submit=True,
    )
    captured = capsys.readouterr()
    # The live-override log line confirms the strategy fetched DNSE
    # and used 17,800 (not the local 17,500).
    assert "live best-ask = 17,800" in captured.out, captured.out
    assert "overrides local price 17,500" in captured.out, captured.out


def test_live_quote_failure_falls_back_to_local_price(
    monkeypatch, tmp_path, capsys
) -> None:
    """If the live quote fetch fails (network blip), the strategy
    falls back to the local price — doesn't crash, doesn't block
    the cycle. Same graceful-degradation as the live-quote gate at
    submit time.
    """
    from vietnam_algo.libs.dnse.client import DnseClient

    # Stub the DNSE client to fail
    stub_dnse = MagicMock(spec=DnseClient)
    stub_dnse.get_latest_quote.side_effect = RuntimeError("DNSE unreachable")

    from vietnam_algo.libs.dnse import client as dnse_client_mod
    monkeypatch.setattr(dnse_client_mod, "DnseClient", lambda *a, **kw: stub_dnse)

    adapter = _make_adapter()[0]
    state = _state_with_signal()
    state_path = tmp_path / "live" / "autofin_signal_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))

    from vietnam_algo.libs.autofin_signals import parse_signal
    sig = parse_signal(_make_signal_for())
    adapter._client_lazy = lambda: MagicMock(
        fetch_holding=MagicMock(return_value=[]),
        fetch_signals=MagicMock(return_value=[sig]),
        fetch_closing=MagicMock(return_value=[]),
    )

    from vietnam_algo.libs import universe
    monkeypatch.setattr(
        universe, "load_price_bars",
        lambda sym, data_dir: [_FakeBar(close=17500.0)],
    )
    from vietnam_algo.libs import portfolio
    monkeypatch.setattr(
        portfolio, "build_portfolio_snapshot",
        lambda *a, **kw: {
            "account": "0003937868",
            "totals": {"stockValue": 0, "portfolioValue": 10_000_000},
            "positions": [],
            "balance": {"withdrawableCash": 10_000_000, "availableCash": 10_000_000},
            "errors": [],
        },
    )

    cfg = MagicMock()
    cfg.dataDir = tmp_path
    cfg.universe = MagicMock()
    cfg.strategy = "autofin_signal"

    sc = MagicMock()
    monkeypatch.setenv("AUTOFIN_FORCE_NEW_ENTRY", "1")

    # Should NOT raise — graceful fallback to local price
    result = adapter.generate_orders(
        cfg, sc, trade_date="2026-10-09", live_submit=True,
    )
    captured = capsys.readouterr()
    # The fallback log line is present
    assert "live quote for ABB failed" in captured.out, captured.out
    assert "using local price" in captured.out, captured.out


# -----------------------------------------------------------------------------
# Test 4: dry-run mode does NOT fetch live quotes
# -----------------------------------------------------------------------------


def test_dry_run_mode_does_not_fetch_live_quotes(
    monkeypatch, tmp_path, capsys
) -> None:
    """In dry-run mode, the strategy does NOT call
    ``client.get_latest_quote()`` (no DNSE network call). The
    backtest / dry-run uses only local price data so it can run
    offline / in CI.
    """
    from vietnam_algo.libs.dnse.client import DnseClient

    # If the strategy wrongly creates a DNSE client in dry-run,
    # this stub will be called and raise.
    stub_dnse = MagicMock(spec=DnseClient)
    stub_dnse.get_latest_quote.side_effect = RuntimeError(
        "should not be called in dry-run"
    )

    from vietnam_algo.libs.dnse import client as dnse_client_mod
    monkeypatch.setattr(dnse_client_mod, "DnseClient", lambda *a, **kw: stub_dnse)

    adapter = _make_adapter()[0]
    state = _state_with_signal()
    state_path = tmp_path / "live" / "autofin_signal_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))

    from vietnam_algo.libs.autofin_signals import parse_signal
    sig = parse_signal(_make_signal_for())
    adapter._client_lazy = lambda: MagicMock(
        fetch_holding=MagicMock(return_value=[]),
        fetch_signals=MagicMock(return_value=[sig]),
        fetch_closing=MagicMock(return_value=[]),
    )

    from vietnam_algo.libs import universe
    monkeypatch.setattr(
        universe, "load_price_bars",
        lambda sym, data_dir: [_FakeBar(close=17500.0)],
    )

    cfg = MagicMock()
    cfg.dataDir = tmp_path
    cfg.universe = MagicMock()
    cfg.strategy = "autofin_signal"

    sc = MagicMock()
    # The generate_orders call should NOT raise, even though
    # get_latest_quote would error — because the dry-run path
    # doesn't call it.
    adapter.generate_orders(
        cfg, sc, trade_date="2026-10-09", live_submit=False,
    )
    # If we got here, the dry-run path didn't construct a DnseClient
    # and therefore didn't call get_latest_quote.
    assert stub_dnse.get_latest_quote.call_count == 0, (
        "dry-run mode must NOT call client.get_latest_quote() — "
        "that would defeat the purpose of offline backtest mode"
    )
