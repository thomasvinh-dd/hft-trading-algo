"""Tests for the daily event-driven replay backtest."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

from vietnam_algo.backtest_autofin_daily import (
    DayState,
    _interpolated_price,
    _interpolated_price_for_order,
    apply_pending_fills,
    check_conditional_triggers,
    filled_deals_in_holding_for_day,
    reconstruct_snapshot,
)


def _make_deal(
    symbol: str = "AAA",
    deal_id: str = "1700000000",
    p_open: float = 10000.0,
    p_close: float = 11000.0,
    t_open: str = "01/01/2024 07:00:00",
    t_close: str = "01/30/2024 07:00:00",
    n_profit: int = 0,
    rate: float = 0.15,
    rec: str = "Đóng DEAL chạm điểm chặn lãi",
) -> dict:
    return {
        "_id": int(deal_id),
        "symbol": symbol,
        "companyName": f"Test {symbol}",
        "statusDeal": {
            "price_Open": p_open,
            "time_Open": t_open,
            "price_Closed": p_close,
            "time_Closed": t_close,
            "timeHolding": 30,
        },
        "numberOfProfit": n_profit,
        "rateForNav": rate,
        "lastRecomment": rec,
    }


def test_interpolated_price_returns_open_before_open_date():
    """Price is price_Open for any day on or before time_Open."""
    deal = _make_deal(p_open=10000, p_close=11000,
                       t_open="01/15/2024 07:00:00", t_close="02/15/2024 07:00:00")
    assert _interpolated_price(deal, date(2024, 1, 10)) == 10000.0
    assert _interpolated_price(deal, date(2024, 1, 15)) == 10000.0


def test_interpolated_price_returns_close_after_close_date():
    """Price is price_Closed for any day at or after time_Closed."""
    deal = _make_deal(p_open=10000, p_close=11000,
                       t_open="01/15/2024 07:00:00", t_close="02/15/2024 07:00:00")
    assert _interpolated_price(deal, date(2024, 2, 15)) == 11000.0
    assert _interpolated_price(deal, date(2024, 3, 1)) == 11000.0


def test_interpolated_price_linearly_interpolates():
    """Price between open and close linearly interpolates."""
    deal = _make_deal(p_open=10000, p_close=11000,
                       t_open="01/01/2024 07:00:00", t_close="01/31/2024 07:00:00")
    # 15 days in (halfway through 30-day holding)
    p = _interpolated_price(deal, date(2024, 1, 16))
    # 15/30 = 0.5 → 10000 + 0.5 * 1000 = 10500
    assert abs(p - 10500.0) < 1


def test_reconstruct_snapshot_separates_holding_suggest_closing():
    """A deal opened on day D, closed on day E (>D+1) appears in:
    - holding for D <= day < E
    - suggest on day D only
    - closing on day E only
    """
    deals = [_make_deal(symbol="AAA", t_open="01/10/2024 07:00:00", t_close="01/20/2024 07:00:00")]
    # Day 8: before open
    snap = reconstruct_snapshot(date(2024, 1, 8), deals)
    assert snap["holding"] == []
    assert snap["suggest"] == []
    assert snap["closing"] == []
    # Day 10: open day
    snap = reconstruct_snapshot(date(2024, 1, 10), deals)
    assert len(snap["holding"]) == 1
    assert len(snap["suggest"]) == 1
    assert snap["closing"] == []
    # Day 15: mid-holding
    snap = reconstruct_snapshot(date(2024, 1, 15), deals)
    assert len(snap["holding"]) == 1
    assert snap["suggest"] == []
    assert snap["closing"] == []
    # Day 20: close day (excluded from holding)
    snap = reconstruct_snapshot(date(2024, 1, 20), deals)
    assert snap["holding"] == []
    assert snap["suggest"] == []
    assert len(snap["closing"]) == 1


def test_apply_pending_fills_buys_and_sells():
    """BUY order fills at slightly higher price (slippage), SELL at lower."""
    state = DayState(nav=100_000_000, cash=100_000_000)
    today = date(2024, 1, 10)
    state.pending_orders.append({
        "fill_date": today, "side": "NB", "symbol": "AAA",
        "qty": 1000, "price": 10000.0, "reason": "buy",
    })
    fills: list[dict] = []
    fees, slip = apply_pending_fills(state, today, 0.0, 0.001, fills)
    assert state.positions["AAA"] == 1000
    assert fills[0]["side"] == "BUY"
    assert fills[0]["price"] == 10005.0  # 10000 * 1.0005 (half of 0.1%)
    assert state.cash < 100_000_000  # cash depleted


def test_apply_pending_fills_drops_insufficient_cash_orders():
    """A BUY that exceeds cash is dropped without modifying state."""
    state = DayState(nav=1_000_000, cash=1_000)  # only 1k cash
    today = date(2024, 1, 10)
    state.pending_orders.append({
        "fill_date": today, "side": "NB", "symbol": "AAA",
        "qty": 1000, "price": 10000.0, "reason": "buy",
    })
    fills: list[dict] = []
    fees, slip = apply_pending_fills(state, today, 0.0, 0.0, fills)
    assert state.positions == {}
    assert fills == []
    assert state.cash == 1_000


def test_check_conditional_triggers_fires_tp_at_trigger():
    """A TP bracket at price 10500 fires when today's interpolated price >= 10500."""
    deals = [_make_deal(p_open=10000, p_close=12000,
                         t_open="01/01/2024 07:00:00", t_close="02/01/2024 07:00:00")]
    state = DayState(nav=1_000_000, cash=1_000_000)
    state.positions["AAA"] = 1000
    state.conditional_orders.append({
        "trigger": ">=", "price": 10500.0, "side": "NS",
        "symbol": "AAA", "qty": 1000, "reason": "TP",
    })
    fills: list[dict] = []
    fees, slip = check_conditional_triggers(
        state, date(2024, 1, 16), deals, 0.0, 0.0, fills,
    )
    assert len(fills) == 1
    assert fills[0]["side"] == "SELL"
    assert state.positions == {}
    assert state.conditional_orders == []  # consumed


def test_check_conditional_triggers_does_not_fire_below_tp():
    """A TP bracket does NOT fire when today's price < trigger level."""
    deals = [_make_deal(p_open=10000, p_close=10500,
                         t_open="01/01/2024 07:00:00", t_close="02/01/2024 07:00:00")]
    state = DayState(nav=1_000_000, cash=1_000_000)
    state.positions["AAA"] = 1000
    state.conditional_orders.append({
        "trigger": ">=", "price": 12000.0, "side": "NS",
        "symbol": "AAA", "qty": 1000, "reason": "TP",
    })
    fills: list[dict] = []
    fees, slip = check_conditional_triggers(
        state, date(2024, 1, 16), deals, 0.0, 0.0, fills,
    )
    assert fills == []
    assert state.positions["AAA"] == 1000
    assert len(state.conditional_orders) == 1  # still active


def test_check_conditional_triggers_fires_sl_at_trigger():
    """An SL bracket fires when today's price <= trigger level."""
    deals = [_make_deal(p_open=10000, p_close=8000,
                         t_open="01/01/2024 07:00:00", t_close="02/01/2024 07:00:00")]
    state = DayState(nav=1_000_000, cash=1_000_000)
    state.positions["AAA"] = 1000
    state.conditional_orders.append({
        "trigger": "<=", "price": 9500.0, "side": "NS",
        "symbol": "AAA", "qty": 1000, "reason": "SL",
    })
    fills: list[dict] = []
    fees, slip = check_conditional_triggers(
        state, date(2024, 1, 16), deals, 0.0, 0.0, fills,
    )
    assert len(fills) == 1
    assert fills[0]["side"] == "SELL"
    assert state.positions == {}


def test_filled_deals_in_holding_excludes_closed_deals():
    """Helper only returns deals still active on the given day."""
    deals = [
        _make_deal(symbol="AAA", t_open="01/01/2024 07:00:00", t_close="01/15/2024 07:00:00"),
        _make_deal(symbol="BBB", t_open="01/05/2024 07:00:00", t_close="01/25/2024 07:00:00"),
    ]
    # Day 10: AAA active, BBB active
    out = filled_deals_in_holding_for_day(deals, date(2024, 1, 10), "AAA")
    assert len(out) == 1
    # Day 16: AAA closed (not returned), BBB active
    out = filled_deals_in_holding_for_day(deals, date(2024, 1, 16), "AAA")
    assert out == []
    out = filled_deals_in_holding_for_day(deals, date(2024, 1, 16), "BBB")
    assert len(out) == 1


def test_interpolated_price_for_order_uses_close_on_close_day():
    """When the order's fill_date is the deal's close day, use price_Closed
    directly (not interpolated), since that's the actual final price the
    market saw."""
    deal = _make_deal(p_open=10000, p_close=15300,
                       t_open="01/10/2024 07:00:00", t_close="01/20/2024 07:00:00")
    # Close day → use price_Closed
    p = _interpolated_price_for_order(deal, date(2024, 1, 20))
    assert p == 15300.0
    # Mid-holding → interpolated
    p = _interpolated_price_for_order(deal, date(2024, 1, 15))
    assert 10000 < p < 15300


def test_interpolated_price_for_order_returns_zero_for_none():
    """If no deal is provided, return 0 (caller must handle)."""
    assert _interpolated_price_for_order(None, date(2024, 1, 10)) == 0.0
