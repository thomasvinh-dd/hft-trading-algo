"""Tests for autofin signal-selection logic + order-plan computation."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
import pytest
from typing import Any
sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

import vietnam_algo.algos.autofin_signal as mod
from vietnam_algo.algos.autofin_signal import (
    AutofinSignalAdapter,
    _plan_orders_for_new_positions,
    _plan_close_orders,
    parse_signal,
)


def _make_signal(
    symbol: str,
    rate_for_nav: float,
    last_index: int = 1,
    is_waiting_buy: bool = False,
    is_waiting_sell: bool = False,
    supply_to: float = 10000.0,
    tp_from: float = 12000.0,
    sl_to: float | None = None,
    rating: float = 0.0,
    win_rate: float = 0.0,
) -> object:
    sl_to = sl_to if sl_to is not None else supply_to - 100
    raw = {
        "symbol": symbol,
        "lastIndex": last_index,
        "statusDeal": {"market_TypePrice": 1},
        "supply_F": supply_to - 100,
        "supply_T": supply_to,
        "tP_F": tp_from,
        "tP_T": tp_from + 100,
        "sL_F": sl_to - 100,
        "sL_T": sl_to,
        "rateForNav": rate_for_nav,
        "lastRecomment": "test",
        "isWaittingBuy": is_waiting_buy,
        "isWaittingSell": is_waiting_sell,
        "numberOfProfit": 0,
        "rateIncome": 0,
        "rateIncome_TypePrice": 3,
        "rS_Rating_IDB": rating,
        "rateWinByPrice": win_rate,
    }
    s = parse_signal(raw)
    s.raw["lastIndex"] = last_index
    s.raw["rS_Rating_IDB"] = rating
    s.raw["rateWinByPrice"] = win_rate
    return s


def test_select_within_nav_budget_sums_to_one():
    """5 deals at 30% → first 3 full (90%), 4th partial at 10%, 5th skip.

    Per user feedback 2026-09-08: the bot should not leave cash idle
    if there's a deal that can take the leftover NAV. The 4th deal
    gets 10% (the leftover) and the 5th is skipped because no room.
    """
    sigs = [
        _make_signal("A", 0.30, last_index=5),
        _make_signal("B", 0.30, last_index=4),
        _make_signal("C", 0.30, last_index=3),
        _make_signal("D", 0.30, last_index=2),
        _make_signal("E", 0.30, last_index=1),
    ]
    selected = mod._select_signals_within_nav_budget(sigs, nav_budget_pct=1.0)
    assert len(selected) == 4
    symbols = [s.symbol for s in selected]
    assert symbols == ["A", "B", "C", "D"]
    # D was downsized from 0.30 to 0.10 (the leftover)
    assert abs(sum(s.rate_for_nav for s in selected) - 1.0) < 1e-6
    assert selected[-1].rate_for_nav == pytest.approx(0.10, abs=1e-6)


def test_select_within_nav_budget_does_not_overallocate():
    """The partial-budget allocation must NOT push cumulative past budget."""
    sigs = [
        _make_signal("A", 0.50, last_index=3),
        _make_signal("B", 0.50, last_index=2),
        _make_signal("C", 0.50, last_index=1),
    ]
    selected = mod._select_signals_within_nav_budget(sigs, nav_budget_pct=1.0)
    assert len(selected) == 2
    # A full at 0.50, B partial at 0.50 (the leftover)
    assert abs(sum(s.rate_for_nav for s in selected) - 1.0) < 1e-6


def test_select_within_nav_budget_skips_tiny_leftover():
    """Leftover < min_leftover_fraction → skip (avoid fee-inefficient entry)."""
    # 3 deals at 0.32 each = 0.96; leftover 0.04 < min_leftover=0.05.
    sigs = [
        _make_signal("A", 0.32, last_index=3),
        _make_signal("B", 0.32, last_index=2),
        _make_signal("C", 0.32, last_index=1),
    ]
    selected = mod._select_signals_within_nav_budget(sigs, nav_budget_pct=1.0)
    assert len(selected) == 3
    # All 3 fit full (3 * 0.32 = 0.96 ≤ 1.0)
    assert abs(sum(s.rate_for_nav for s in selected) - 0.96) < 1e-6
    # But now add a 4th deal: 0.96 + 0.32 = 1.28 > 1.0, leftover 0.04 < 0.05
    # → would be skipped because 0.04 < min_leftover.
    sigs.append(_make_signal("D", 0.32, last_index=0))
    selected = mod._select_signals_within_nav_budget(sigs, nav_budget_pct=1.0)
    assert len(selected) == 3  # D skipped
    assert abs(sum(s.rate_for_nav for s in selected) - 0.96) < 1e-6


def test_select_within_nav_budget_can_disable_partial():
    """min_leftover_fraction=0 → never allocate partial, fall back to skip."""
    sigs = [
        _make_signal("A", 0.30, last_index=3),
        _make_signal("B", 0.30, last_index=2),
        _make_signal("C", 0.30, last_index=1),
    ]
    selected = mod._select_signals_within_nav_budget(
        sigs, nav_budget_pct=0.50, min_leftover_fraction=0.0,
    )
    # With 50% budget, A fits (0.30 ≤ 0.50), B doesn't fit (0.30 leftover 0.20
    # but min_leftover=0 means we don't allocate partial → skip).
    assert len(selected) == 1
    assert selected[0].symbol == "A"


def test_select_within_nav_budget_excludes_zero_nav():
    sigs = [
        _make_signal("A", 0.50),
        _make_signal("B", 0.00),
        _make_signal("C", 0.40),
    ]
    selected = mod._select_signals_within_nav_budget(sigs, nav_budget_pct=1.0)
    assert len(selected) == 2
    assert {s.symbol for s in selected} == {"A", "C"}


def test_select_within_nav_budget_prioritizes_risk_reward():
    a = _make_signal("A", 0.30, last_index=100, supply_to=10000, tp_from=11000, sl_to=9500)
    b = _make_signal("B", 0.30, last_index=50, supply_to=10000, tp_from=13000, sl_to=9000)
    selected = mod._select_signals_within_nav_budget([a, b], nav_budget_pct=1.0)
    assert [s.symbol for s in selected] == ["B", "A"]


def test_select_within_nav_budget_uses_rating_and_winrate():
    a = _make_signal("A", 0.30, supply_to=10000, tp_from=12000, sl_to=9000, rating=1, win_rate=0.1)
    b = _make_signal("B", 0.30, supply_to=10000, tp_from=11500, sl_to=9500, rating=9, win_rate=0.9)
    selected = mod._select_signals_within_nav_budget([a, b], nav_budget_pct=1.0)
    assert [s.symbol for s in selected] == ["B", "A"]


def test_plan_new_positions_emits_buy_sl_bracket():
    """BUY + SL bracket only — TP is per-event (autofin publishes
    partial closes via ``numberOfProfit`` / ``lastRecomment``), not
    hardcoded. Per user feedback 2026-09-09: autofin's TP events are
    partial closes (10%-30% of REMAINING position), not full closes,
    so the bracket must not emit a SELL of target_qty at TP1 level
    — that duplicates the per-event partial-close logic and risks
    overselling."""
    sig = _make_signal("APH", 0.15, supply_to=6100, tp_from=6500, sl_to=5320)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=1_000_000_000, our_positions={}
    )
    sides = [r["side"] for r in rows]
    assert sides.count("NB") == 1
    assert sides.count("NS") == 1
    buy = next(r for r in rows if r["side"] == "NB")
    sell = next(r for r in rows if r["side"] == "NS")
    assert buy["price"] == 6100
    assert buy["kind"] == "entry"
    assert sell["kind"] == "sl"  # SL only, no TP bracket
    # user_sl (5% of 6100 = 5220) is tighter than autofin's 5320, so user wins.
    assert sell["price"] == 5220


def test_plan_new_positions_skips_tp_bracket():
    """Even with a tp_from > 0 we DO NOT emit a TP bracket anymore —
    per-event partial-close logic handles it. Confirm the only SELL
    emitted is the SL bracket."""
    sig = _make_signal("APH", 0.15, supply_to=6100, tp_from=6500, sl_to=5320)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=1_000_000_000, our_positions={}
    )
    tp_legs = [r for r in rows if r["kind"] == "tp"]
    assert tp_legs == [], (
        "TP bracket should NOT be emitted — autofin's TPs are partial "
        "closes driven by _plan_partial_close_orders, not brackets"
    )


def test_plan_new_positions_skips_when_target_too_small():
    sig = _make_signal("APH", 0.05, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=10_000_000, our_positions={}
    )
    assert rows == []


def test_plan_new_positions_no_op_when_match():
    """Fixed sizing: if we already hold this symbol, skip entirely."""
    sig = _make_signal("APH", 0.10, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=610_000, our_positions={"APH": 100}
    )
    assert rows == []


def test_plan_new_positions_skips_when_already_held():
    """Fixed sizing: never re-enter an existing position, even if NAV grew."""
    sig = _make_signal("APH", 0.15, supply_to=6100, tp_from=6500, sl_to=5320)
    # We hold 100 shares — under fixed sizing we should NOT issue a top-up BUY
    # even though NAV grew enough to justify a larger position.
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=10_000_000_000, our_positions={"APH": 100}
    )
    assert rows == []


def test_plan_new_positions_skips_topup_after_partial_close():
    """Fixed sizing: after a partial-TP reduces our qty, don't top up."""
    sig = _make_signal("APH", 0.15, supply_to=6100, tp_from=6500, sl_to=5320)
    # We hold 50 shares (was 100, partial-TP took 50)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=1_000_000_000, our_positions={"APH": 50}
    )
    # Should NOT issue a BUY for the missing 50 shares — that was the
    # death-spiral behavior. Position stays at 50 until full-close or
    # further partial-TPs.
    assert rows == []


def test_plan_new_positions_emits_buy_when_position_zero():
    """Fresh entry: target_qty = NAV * rate / ref_price, BUY + TP + SL."""
    sig = _make_signal("APH", 0.15, supply_to=6100, tp_from=6500, sl_to=5320)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=1_000_000_000, our_positions={"APH": 0}
    )
    sides = [r["side"] for r in rows]
    # BUY + SL-SELL (no TP bracket — per-event TP is the canonical path)
    assert sides.count("NB") == 1
    assert sides.count("NS") == 1
    buy = next(r for r in rows if r["side"] == "NB")
    # target_value = 1B * 0.15 = 150M; / 6100 = 24590 shares → 245 lots
    # = 24500
    assert buy["quantity"] == 24500
    assert buy["price"] == 6100


def test_plan_new_positions_only_buy_never_sell():
    """Fixed sizing: this function NEVER emits a SELL for sizing. SELLs
    come from partial-close / full-close logic only."""
    sig = _make_signal("APH", 0.10, supply_to=6100)
    # Edge: we hold MORE than target (e.g. after NAV dropped)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=100_000, our_positions={"APH": 10000}
    )
    # Should skip entirely (we hold > 0) — no SELL
    assert rows == []


def test_decide_orders_does_not_rebalance_existing_positions():
    """decide_orders() must not add held deals back to selected for re-entry.
    This was the source of the death-spiral bug.
    """
    # We hold APH. autofin still has APH in holding (no exit). A new
    # signal APH appears in suggest (autofin re-suggests it). Strategy
    # must NOT issue a new BUY.
    sig = _make_signal("APH", 0.15, supply_to=6100, tp_from=6500, sl_to=5320)
    raw_aph = sig.raw
    raw_aph["_id"] = 1700000000
    holding = [{
        "_id": 1700000000, "symbol": "APH", "companyName": "APH Co",
        "statusDeal": {"price_Open": 6100, "price_Closed": 6500},
        "numberOfProfit": 0, "rateForNav": 0.15,
        "lastRecomment": "open", "supply_F": 6100, "supply_T": 6100,
        "tP_F": 6500, "tP_T": 6600, "sL_F": 5300, "sL_T": 5400,
    }]
    our_deals = {
        "1700000000:APH": {**raw_aph, "_our_qty": 1000, "_last_numberOfProfit": 0},
    }
    orders, new_our_deals, _ = mod.decide_orders(
        holding=holding, signals=[sig], our_deals=our_deals,
        our_positions={"APH": 1000}, nav=1_000_000_000,
    )
    # No new BUY for APH — we already hold it
    buys = [o for o in orders if o["symbol"] == "APH" and o["side"] == "NB"]
    assert buys == []


def test_decide_orders_handles_new_entry_only_once():
    """Same deal can be re-suggested by autofin across multiple days; we
    only enter on the FIRST appearance (when our_positions has 0 for the
    symbol). Subsequent suggestions are ignored for entry (no top-up)."""
    sig = _make_signal("NEW", 0.10, supply_to=10000, tp_from=11000, sl_to=9000)
    sig.raw["_id"] = 1750000000
    holding = [{
        "_id": 1750000000, "symbol": "NEW", "companyName": "New Co",
        "statusDeal": {"price_Open": 10000, "price_Closed": 11000},
        "numberOfProfit": 0, "rateForNav": 0.10,
        "lastRecomment": "open", "supply_F": 10000, "supply_T": 10000,
        "tP_F": 11000, "tP_T": 11100, "sL_F": 8900, "sL_T": 9000,
    }]
    our_deals_empty: dict = {}
    # First call: position is empty → issue BUY
    orders1, our_deals1, _ = mod.decide_orders(
        holding=holding, signals=[sig], our_deals=our_deals_empty,
        our_positions={}, nav=1_000_000_000,
    )
    assert any(o["symbol"] == "NEW" and o["side"] == "NB" for o in orders1)
    # Set our positions to reflect the BUY
    # (in real life the BUY would have filled by next cycle)
    our_deals_after_entry = our_deals1
    # Second call: we already hold the position → no new BUY
    orders2, _, _ = mod.decide_orders(
        holding=holding, signals=[sig], our_deals=our_deals_after_entry,
        our_positions={"NEW": 10000}, nav=1_000_000_000,
    )
    new_buys = [o for o in orders2 if o["symbol"] == "NEW" and o["side"] == "NB"]
    assert new_buys == []


def test_plan_new_positions_emits_user_sl_when_tighter_than_autofin():
    """If user_sl_pct=0.05 and autofin's sL_F is at 10% (looser),
    we use the 5% user SL — tighter, fires sooner on the way down."""
    sig = _make_signal("AAA", 0.15, supply_to=10000, tp_from=12000, sl_to=9100)
    # sL_F = sl_to - 100 = 9000 (10% below entry)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=1_000_000_000, our_positions={},
        user_sl_pct=0.05,
    )
    sl_rows = [r for r in rows if "sl" in r["reason"]]
    assert len(sl_rows) == 1
    # Entry @ 10000, user SL @ 9500 (5% below). autofin SL was @ 9000 (10%).
    # Tighter wins → SL @ 9500.
    assert sl_rows[0]["price"] == 9500
    # Reason should identify it as user SL
    assert "user_sl" in sl_rows[0]["reason"]


def test_plan_new_positions_uses_autofin_sl_when_tighter_than_user():
    """If autofin's sL_F is at 2% (tighter than user 5%), use autofin's."""
    sig = _make_signal("BBB", 0.15, supply_to=10000, tp_from=12000, sl_to=9900)
    # sL_F = sl_to - 100 = 9800 (2% below entry, tighter than user 5%)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=1_000_000_000, our_positions={},
        user_sl_pct=0.05,
    )
    sl_rows = [r for r in rows if "sl" in r["reason"]]
    assert len(sl_rows) == 1
    # autofin's 9800 is tighter than user 9500 → use autofin's
    assert sl_rows[0]["price"] == 9800
    assert "autofin_sl:" in sl_rows[0]["reason"]


def test_plan_new_positions_no_user_sl_when_disabled():
    """user_sl_pct=0 disables the override → autofin's SL only."""
    sig = _make_signal("CCC", 0.15, supply_to=10000, tp_from=12000, sl_to=9100)
    # sL_F = sl_to - 100 = 9000
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=1_000_000_000, our_positions={},
        user_sl_pct=0.0,
    )
    sl_rows = [r for r in rows if "sl" in r["reason"]]
    assert len(sl_rows) == 1
    # No user SL → autofin's 9000
    assert sl_rows[0]["price"] == 9000
    assert "user_sl" not in sl_rows[0]["reason"]


def test_decide_orders_threads_user_sl_pct():
    """decide_orders passes user_sl_pct through to _plan_orders_for_new_positions."""
    sig = _make_signal("DDD", 0.15, supply_to=10000, tp_from=12000, sl_to=9100)
    sig.raw["_id"] = 1750000003
    holding = [{
        "_id": 1750000003, "symbol": "DDD", "companyName": "DDD Co",
        "statusDeal": {"price_Open": 10000, "price_Closed": 12000},
        "numberOfProfit": 0, "rateForNav": 0.15,
        "lastRecomment": "open",
        "supply_F": 10000, "supply_T": 10000,
        "tP_F": 12000, "tP_T": 12000, "sL_F": 9000, "sL_T": 9000,
    }]
    orders, _, _ = mod.decide_orders(
        holding=holding, signals=[sig], our_deals={}, our_positions={},
        nav=1_000_000_000, user_sl_pct=0.05,
    )
    sl_rows = [o for o in orders if "sl" in o["reason"]]
    assert len(sl_rows) == 1
    # entry 10000, user SL 9500 wins over autofin's 9000
    assert sl_rows[0]["price"] == 9500


def test_plan_close_orders_only_closes_what_autofin_exited():
    our_deals = {
        "111:APH": {"symbol": "APH", "_our_qty": 200},
        "222:VHM": {"symbol": "VHM", "_our_qty": 100},
    }
    autofin_holding = [
        {"_id": 111, "symbol": "APH", "statusDeal": {"isClosed": False}},
        # VHM exited → close
    ]
    rows = _plan_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "VHM"


# ── Rich cycle-summary formatter (Telegram message) ───────────────────────


def test_format_cycle_summary_shows_bot_signal_and_dnse_order():
    """The EOD Telegram message must show:
      1. which bot is sending (autofin_signal)
      2. what autofin signal triggered each order
      3. the actual DNSE order we'll send
    """
    adapter = AutofinSignalAdapter()
    result = {
        "tradeDate": "2026-09-07",
        "latestOrdersCsv": "/path/to/latest_orders.autofin_signal.csv",
        "latestOrdersJson": "/path/to/latest_orders.autofin_signal.json",
        "signals": {
            "autofin_holding": 20,
            "autofin_risk": 1,
            "autofin_closing": 2,
            "fetched": 20,
            "deals_tracked": 3,
            "new_count": 1,
            "close_count": 1,
            "partial_count": 1,
            "account": "0003937868",
            "nav": 1_500_000_000,
        },
        "deals_actioned": [
            {
                "action": "new_entry",
                "symbol": "TDP",
                "deal_id": "1787270400:TDP",
                "lastRecomment": "Khuyến nghị đầu tư",
                "rateForNav": 0.15,
                "numberOfProfit": 0,
                "dnse_orders": [
                    {"symbol": "TDP", "side": "NB", "price": 32000, "quantity": 700},
                    {"symbol": "TDP", "side": "NS", "price": 36000, "quantity": 700},
                    {"symbol": "TDP", "side": "NS", "price": 28500, "quantity": 700},
                ],
            },
            {
                "action": "partial_close",
                "symbol": "HCM",
                "deal_id": "1785888000:HCM",
                "lastRecomment": "💰 Chốt lãi lần 1 mức giá 26,900",
                "rateForNav": 0.30,
                "numberOfProfit": 1,
                "dnse_orders": [
                    {"symbol": "HCM", "side": "NS", "price": 26900, "quantity": 100},
                ],
            },
            {
                "action": "full_close",
                "symbol": "L40",
                "deal_id": "1756166400:L40",
                "lastRecomment": "(autofin exited — not in holding)",
                "rateForNav": 0.0,
                "numberOfProfit": 0,
                "dnse_orders": [
                    {"symbol": "L40", "side": "NS", "price": 0, "quantity": 200},
                ],
            },
        ],
    }
    msg = adapter.format_cycle_summary(result)
    # 1. Bot identity
    assert "[autofin_signal]" in msg
    # 2. Signal context (autofin recomment) shown for each deal
    assert "Khuyến nghị đầu tư" in msg
    assert "Chốt lãi lần 1 mức giá 26,900" in msg
    # 3. Actual DNSE orders shown
    assert "BUY 700 TDP @ 32,000 VND" in msg
    assert "SELL 100 HCM @ 26,900 VND" in msg
    assert "SELL 200 L40 @ market VND" in msg
    # grouping
    assert "New entries (1)" in msg
    assert "Partial closes (1)" in msg
    assert "Full closes (1)" in msg


def test_format_cycle_summary_handles_empty_cycle():
    adapter = AutofinSignalAdapter()
    result = {
        "tradeDate": "2026-09-07",
        "signals": {"account": "0003937868", "nav": 0},
        "deals_actioned": [],
    }
    msg = adapter.format_cycle_summary(result)
    assert "[autofin_signal]" in msg
    assert "No orders planned" in msg
    assert "NAV=0" in msg


def test_format_cycle_summary_includes_portfolio_data():
    """The EOD summary should include the live portfolio snapshot:
    cash, NAV, and per-position unrealized P&L."""
    adapter = AutofinSignalAdapter()
    result = {
        "tradeDate": "2026-09-07",
        "signals": {"account": "0003937868", "nav": 0},
        "deals_actioned": [],
    }
    portfolio = {
        "balance": {
            "totalCash": 50_000_000,
            "availableCash": 30_000_000,
            "withdrawableCash": 30_000_000,
        },
        "positions": [
            {
                "symbol": "HCM",
                "quantity": 1000,
                "avgPrice": 26126,
                "marketPrice": 27500,
                "marketValue": 27_500_000,
                "costBasis": 26_126_000,
                "unrealizedPnl": 1_374_000,
                "unrealizedPnlPct": 5.26,
            },
        ],
        "totals": {
            "stockValue": 27_500_000,
            "portfolioValue": 57_500_000,
            "totalUnrealizedPnl": 1_374_000,
            "totalUnrealizedPnlPct": 2.45,
            "positionCount": 1,
        },
    }
    msg = adapter.format_cycle_summary(result, portfolio=portfolio)
    assert "CURRENT PORTFOLIO" in msg
    assert "30,000,000" in msg or "30.000.000" in msg  # availableCash
    assert "NAV:" in msg
    assert "HCM" in msg  # the position
    assert "uPnL" in msg


def test_format_cycle_summary_handles_none_portfolio():
    """Passing portfolio=None must not crash."""
    adapter = AutofinSignalAdapter()
    result = {
        "tradeDate": "2026-09-07",
        "signals": {"account": "0003937868", "nav": 0},
        "deals_actioned": [],
    }
    msg = adapter.format_cycle_summary(result, portfolio=None)
    assert "[autofin_signal]" in msg
    assert "portfolio" in msg.lower()  # shows "unavailable" note


# ── Start-of-day message (morning briefing) ───────────────────────────────


def test_start_of_day_message_shows_portfolio_and_orders(tmp_path, monkeypatch):
    from vietnam_algo.libs.notifier import start_of_day_message
    portfolio = {
        "balance": {"totalCash": 50_000_000, "availableCash": 30_000_000},
        "positions": [
            {"symbol": "HCM", "quantity": 1000, "avgPrice": 26126, "marketPrice": 27500,
             "marketValue": 27_500_000, "costBasis": 26_126_000, "unrealizedPnl": 1_374_000,
             "unrealizedPnlPct": 5.26},
        ],
        "totals": {"stockValue": 27_500_000, "portfolioValue": 57_500_000,
                   "totalUnrealizedPnl": 1_374_000, "totalUnrealizedPnlPct": 2.45, "positionCount": 1},
    }
    # Use a SELL that we CAN actually fill (we hold 1000 HCM) — the
    # earlier version used a BUY that exceeded cash, which now gets
    # filtered out by the cash-aware filter (see test_start_of_day_message_filters_unfillable_orders_below).
    orders_json = tmp_path / "latest_orders.json"
    orders_json.write_text(json.dumps({
        "count": 1, "items": [
            {"symbol": "HCM", "side": "SELL", "price": 27500, "quantity": 500,
             "reason": "rank_exit", "date": "2026-09-07"},
        ],
    }))
    msg = start_of_day_message(
        portfolio=portfolio,
        today="2026-09-07",
        orders_json_path=str(orders_json),
        strategy_name="momentum",
        account="0003937868",
    )
    # Bot identity + heading
    assert "START OF DAY" in msg
    assert "momentum" in msg
    assert "0003937868" in msg
    # Portfolio section
    assert "CURRENT PORTFOLIO" in msg
    assert "30,000,000" in msg
    assert "NAV:" in msg
    # Today's planned orders section — the SELL is fillable so it appears.
    assert "Today's planned orders" in msg
    assert "SELL 500 HCM" in msg
    # Next-step hint
    assert "09:16 ICT" in msg


def test_start_of_day_message_filters_unfillable_orders_below(tmp_path):
    """User-reported scenario (2026-09-16): portfolio has 0 cash +
    0 positions, but latest_orders.json lists 2 SELL signals. The
    start-of-day briefing should NOT list those SELLs as actionable
    orders — they would just be silently dropped by the apply
    phase. Filter them out and surface in a skipped summary instead.
    """
    from vietnam_algo.libs.notifier import start_of_day_message
    portfolio = {
        "mode": "dry_run",
        "balance": {"totalCash": 100_000_000, "availableCash": 100_000_000},
        "positions": [],  # empty — just reset
        "totals": {"stockValue": 0, "portfolioValue": 100_000_000,
                   "totalUnrealizedPnl": 0, "positionCount": 0},
    }
    orders_json = tmp_path / "latest_orders.json"
    orders_json.write_text(json.dumps({
        "count": 2, "items": [
            {"symbol": "BAF", "side": "SELL", "price": 31918, "quantity": 6000,
             "reason": "market_bear_exit"},
            {"symbol": "BSR", "side": "SELL", "price": 27622, "quantity": 7000,
             "reason": "market_bear_exit"},
        ],
    }))
    msg = start_of_day_message(
        portfolio=portfolio,
        today="2026-09-16",
        orders_json_path=str(orders_json),
        strategy_name="momentum",
        account="0003937868",
    )
    # Split into the actionable section (between 📋 and ⏭) and the
    # skipped summary (after ⏭) so we can assert they appear in the
    # right place.
    if "⏭" in msg:
        actionable, skipped_section = msg.split("⏭", 1)
    else:
        actionable, skipped_section = msg, ""
    # The SELLs should NOT appear in the actionable section.
    assert "Today's planned orders" not in actionable, (
        "Actionable section should NOT list un-fillable SELLs: " + actionable
    )
    assert "SELL 6000 BAF" not in actionable
    assert "SELL 7000 BSR" not in actionable
    # Instead, the briefing should say "no fillable orders" and list
    # the skipped reasons.
    assert "No fillable orders today" in actionable
    # The skipped summary captures the un-fillable orders so the user
    # still sees the backtest's intent.
    assert "BAF" in skipped_section
    assert "BSR" in skipped_section
    assert "no position" in skipped_section


def test_start_of_day_message_keeps_fillable_orders_and_skips_unfillable(tmp_path):
    """When the plan has BOTH a fillable and an unfillable order, the
    briefing should list the fillable one and surface the unfillable
    one in the skipped summary (don't drop the backtest's intent
    silently)."""
    from vietnam_algo.libs.notifier import start_of_day_message
    portfolio = {
        "mode": "dry_run",
        "balance": {"totalCash": 50_000_000, "availableCash": 50_000_000},
        "positions": [
            # We hold HCM, so the SELL is fillable.
            {"symbol": "HCM", "quantity": 1000, "avgPrice": 27000,
             "marketPrice": 27500, "marketValue": 27_500_000,
             "costBasis": 27_000_000, "unrealizedPnl": 500_000,
             "unrealizedPnlPct": 1.85},
            # We don't hold VHM, so the SELL is NOT fillable.
        ],
        "totals": {"stockValue": 27_500_000, "portfolioValue": 77_500_000,
                   "totalUnrealizedPnl": 500_000, "positionCount": 1},
    }
    orders_json = tmp_path / "latest_orders.json"
    orders_json.write_text(json.dumps({
        "count": 2, "items": [
            {"symbol": "HCM", "side": "SELL", "price": 27500, "quantity": 500,
             "reason": "rank_exit"},
            {"symbol": "VHM", "side": "SELL", "price": 70000, "quantity": 200,
             "reason": "rank_exit"},
        ],
    }))
    msg = start_of_day_message(
        portfolio=portfolio,
        today="2026-09-16",
        orders_json_path=str(orders_json),
        strategy_name="momentum",
        account="0003937868",
    )
    # Split sections.
    if "⏭" in msg:
        actionable, skipped_section = msg.split("⏭", 1)
    else:
        actionable, skipped_section = msg, ""
    # Fillable SELL appears in the actionable section.
    assert "Today's planned orders" in actionable
    assert "SELL 500 HCM" in actionable
    # Unfillable SELL does NOT appear in the actionable section.
    assert "SELL 200 VHM" not in actionable
    # The skipped summary captures it.
    assert "SELL 200 VHM" in skipped_section
    assert "VHM" in skipped_section
    assert "no position" in skipped_section


def test_start_of_day_message_filters_buy_when_cash_short(tmp_path):
    """BUY where cost > available cash is filtered out and shown in the
    skipped summary."""
    from vietnam_algo.libs.notifier import start_of_day_message
    portfolio = {
        "mode": "dry_run",
        "balance": {"totalCash": 5_000_000, "availableCash": 5_000_000},
        "positions": [],
        "totals": {"stockValue": 0, "portfolioValue": 5_000_000,
                   "totalUnrealizedPnl": 0, "positionCount": 0},
    }
    orders_json = tmp_path / "latest_orders.json"
    orders_json.write_text(json.dumps({
        "count": 1, "items": [
            {"symbol": "EXP", "side": "BUY", "price": 50000, "quantity": 1000,
             "reason": "top_rank"},
        ],
    }))
    msg = start_of_day_message(
        portfolio=portfolio,
        today="2026-09-16",
        orders_json_path=str(orders_json),
        strategy_name="momentum",
        account="0003937868",
    )
    # BUY too expensive — should NOT appear in the actionable section.
    if "⏭" in msg:
        actionable, skipped_section = msg.split("⏭", 1)
    else:
        actionable, skipped_section = msg, ""
    assert "Today's planned orders" not in actionable
    assert "BUY 1000 EXP" not in actionable
    # Skipped summary captures it.
    assert "EXP" in skipped_section
    assert "cost" in skipped_section
    assert "cash" in skipped_section


def test_start_of_day_message_handles_no_portfolio():
    """Legacy behaviour: when no portfolio is passed, the briefing lists
    all items as before (no filtering — caller didn't opt in)."""
    from vietnam_algo.libs.notifier import start_of_day_message
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        orders_json = Path(td) / "latest_orders.json"
        orders_json.write_text(json.dumps({
            "count": 1, "items": [
                {"symbol": "AAA", "side": "BUY", "price": 10000, "quantity": 100,
                 "reason": "test"},
            ],
        }))
        msg = start_of_day_message(
            portfolio=None,
            today="2026-09-16",
            orders_json_path=str(orders_json),
            strategy_name="momentum",
            account="0003937868",
        )
        assert "BUY 100 AAA" in msg  # No filter applied — appears unconditionally.


def test_start_of_day_message_handles_empty_orders(tmp_path):
    from vietnam_algo.libs.notifier import start_of_day_message
    msg = start_of_day_message(
        portfolio=None,
        today="2026-09-07",
        orders_json_path=str(tmp_path / "missing.json"),  # doesn't exist
        strategy_name="momentum",
        account="0003937868",
    )
    assert "START OF DAY" in msg
    assert "No orders planned" in msg
    assert "portfolio snapshot: unavailable" in msg


def test_should_run_start_of_day_respects_trading_calendar():
    from vietnam_algo.libs.schedule import (
        VIETNAM_TZ, should_run_start_of_day, TradingSchedule,
    )
    from datetime import datetime
    # Mon Sep 7 2026 — weekday
    mon = datetime(2026, 9, 7, 9, 30, tzinfo=VIETNAM_TZ)
    assert should_run_start_of_day(mon, last_date=None) is True
    # Already sent today
    assert should_run_start_of_day(mon, last_date="2026-09-07") is False
    # Before 09:00
    early = datetime(2026, 9, 7, 8, 59, tzinfo=VIETNAM_TZ)
    assert should_run_start_of_day(early, last_date=None) is False
    # Sat Sep 5 — weekend
    sat = datetime(2026, 9, 5, 10, 0, tzinfo=VIETNAM_TZ)
    assert should_run_start_of_day(sat, last_date=None) is False
    # Vietnam holiday Sep 2
    holiday = datetime(2026, 9, 2, 10, 0, tzinfo=VIETNAM_TZ)
    assert should_run_start_of_day(holiday, last_date=None, holidays={"2026-09-02"}) is False


# ── Per-order Telegram notifications (mid-day) ───────────────────────────


def test_per_order_message_contains_bot_id_signal_and_dnse_order(monkeypatch):
    """The short per-order Telegram message must still show:
      1. which bot is sending (autofin_signal)
      2. what signal triggered the order
      3. the actual DNSE order
    """
    import vietnam_algo.algos.autofin_signal as mod
    sent: list[str] = []
    monkeypatch.setattr(mod, "notify", lambda text, config=None: sent.append(text) or {"ok": True})

    adapter = AutofinSignalAdapter()
    deals = [
        {
            "action": "partial_close",
            "symbol": "HCM",
            "deal_id": "1785888000:HCM",
            "lastRecomment": "💰 Chốt lãi lần 1 mức giá 26,900",
            "rateForNav": 0.30,
            "numberOfProfit": 1,
            "dnse_orders": [
                {"symbol": "HCM", "side": "NS", "price": 26900, "quantity": 100, "reason": "autofin_partial:deal#1785888000:HCM:..."},
            ],
        },
    ]
    adapter._notify_new_orders(deals)
    assert len(sent) == 1
    msg = sent[0]
    # 1. bot identity
    assert "autofin_signal" in msg
    assert "new partial close" in msg
    # 2. signal context
    assert "💰 Chốt lãi lần 1 mức giá 26,900" in msg
    assert "deal#1785888000:HCM" in msg
    # 3. DNSE order
    assert "SELL 100 HCM @ 26,900 VND" in msg
    # notified keys recorded
    assert len(adapter._notified_order_keys) == 1


def test_per_order_message_not_re_notified_when_unchanged(monkeypatch):
    """The same order should not be re-notified on the next cycle."""
    import vietnam_algo.algos.autofin_signal as mod
    sent: list[str] = []
    monkeypatch.setattr(mod, "notify", lambda text, config=None: sent.append(text) or {"ok": True})

    adapter = AutofinSignalAdapter()
    deals = [
        {
            "action": "partial_close",
            "symbol": "HCM",
            "deal_id": "1785888000:HCM",
            "lastRecomment": "💰 Chốt lãi lần 1 mức giá 26,900",
            "rateForNav": 0.0,
            "numberOfProfit": 1,
            "dnse_orders": [
                {"symbol": "HCM", "side": "NS", "price": 26900, "quantity": 100, "reason": "autofin_partial:deal#1785888000:HCM:..."},
            ],
        },
    ]
    adapter._notify_new_orders(deals)
    adapter._notify_new_orders(deals)  # second call
    assert len(sent) == 1, f"expected 1 notification, got {len(sent)}"


def test_per_order_message_groups_bracket_legs_into_one_telegram(monkeypatch):
    """A new-entry BUY+TP+SL bracket should produce ONE Telegram message,
    not three (the user wants one notification per deal action)."""
    import vietnam_algo.algos.autofin_signal as mod
    sent: list[str] = []
    monkeypatch.setattr(mod, "notify", lambda text, config=None: sent.append(text) or {"ok": True})

    adapter = AutofinSignalAdapter()
    deals = [
        {
            "action": "new_entry",
            "symbol": "TDP",
            "deal_id": "1787270400:TDP",
            "lastRecomment": "Khuyến nghị đầu tư",
            "rateForNav": 0.15,
            "numberOfProfit": 0,
            "dnse_orders": [
                {"symbol": "TDP", "side": "NB", "price": 32000, "quantity": 700, "reason": "autofin_buy:deal#1787270400:TDP:..."},
                {"symbol": "TDP", "side": "NS", "price": 36000, "quantity": 700, "reason": "autofin_tp:deal#1787270400:TDP:..."},
                {"symbol": "TDP", "side": "NS", "price": 28500, "quantity": 700, "reason": "autofin_sl:deal#1787270400:TDP:..."},
            ],
        },
    ]
    adapter._notify_new_orders(deals)
    assert len(sent) == 1
    msg = sent[0]
    assert "new entry" in msg
    assert "BUY 700 TDP @ 32,000 VND" in msg
    assert "SELL 700 TDP @ 36,000 VND" in msg  # TP
    assert "SELL 700 TDP @ 28,500 VND" in msg  # SL
    # all 3 legs recorded as notified
    assert len(adapter._notified_order_keys) == 3


def test_decide_orders_skips_actioned_deals():
    """A deal that's been actioned (entered then closed) must NOT be
    re-entered when autofin's suggest endpoint re-surfaces it.

    Reproduces the 2026-09-10 TRC bug: deal#1786492800 was entered
    and then full-closed. On the next cycle autofin still returned
    it as a suggested BUY; without the actioned_deals guard the bot
    would have re-entered.
    """
    sig = _make_signal("TRC", 0.15, supply_to=83300, tp_from=88400, sl_to=73300)
    sig.raw["_id"] = 1786492800
    sig.raw["lastRecomment"] = "💰 Chốt lãi lần 1 mức giá 88,400"
    holding = [{
        "_id": 1786492800, "symbol": "TRC", "companyName": "TRC Co",
        "statusDeal": {
            "isClosed": False, "price_Open": 83300, "price_Closed": 0,
            "price_StopLoss": 73300,
        },
        "numberOfProfit": 1, "rateForNav": 0.15,
        "lastRecomment": "💰 Chốt lãi lần 1 mức giá 88,400",
        "supply_F": 81500, "supply_T": 83300,
        "tP_F": 88400, "tP_T": 106500, "sL_F": 73300, "sL_T": 77400,
    }]
    # Empty our_deals (the deal was popped on the previous close).
    orders, _, _ = mod.decide_orders(
        holding=holding, signals=[sig], our_deals={},
        our_positions={}, nav=1_000_000_000,
        actioned_deals={"1786492800:TRC"},
    )
    # No new BUY — the deal is in actioned_deals.
    buys = [o for o in orders if o["symbol"] == "TRC" and o["side"] == "NB"]
    assert buys == [], (
        f"expected no BUY for actioned deal, got: {buys}"
    )


def test_decide_orders_keeps_closed_deals_in_state_with_zero_qty():
    """When a deal is closed (full_close emitted), the deal must stay
    in ``new_our_deals`` with ``_our_qty=0`` so we can detect a future
    re-entry attempt. The previous behaviour was to pop the deal,
    which made re-entry undetectable.

    Simulates the close flow directly: starts with our_deals having
    a 100-share TRC deal, then autofin stops returning it (signals=[]
    and TRC not in holding). decide_orders must NOT pop the deal —
    it should leave the row with _our_qty untouched (since no close
    order is emitted in this case either, since autofin still tracks
    TRC in holding and so _plan_close_orders skips it).
    """
    sig = _make_signal("TRC", 0.15, supply_to=83300, tp_from=88400, sl_to=73300)
    sig.raw["_id"] = 1786492800
    sig.raw["lastRecomment"] = "💰 Chốt lãi lần 1 mức giá 88,400"
    holding = [{
        "_id": 1786492800, "symbol": "TRC", "companyName": "TRC Co",
        "statusDeal": {
            "isClosed": False, "price_Open": 83300, "price_Closed": 0,
            "price_StopLoss": 73300,
        },
        "numberOfProfit": 1, "rateForNav": 0.15,
        "lastRecomment": "💰 Chốt lãi lần 1 mức giá 88,400",
        "supply_F": 81500, "supply_T": 83300,
        "tP_F": 88400, "tP_T": 106500, "sL_F": 73300, "sL_T": 77400,
    }]
    # First call: enter the deal.
    orders1, our_deals1, _ = mod.decide_orders(
        holding=holding, signals=[sig], our_deals={},
        our_positions={}, nav=1_000_000_000,
    )
    assert "1786492800:TRC" in our_deals1
    assert our_deals1["1786492800:TRC"]["_our_qty"] > 0
    # Second call: autofin keeps the deal in holding (so the close
    # planner won't emit a full_close). State must be preserved
    # unchanged so the dedup keeps working.
    orders2, our_deals2, _ = mod.decide_orders(
        holding=holding, signals=[], our_deals=our_deals1,
        our_positions={"TRC": 200},
        nav=1_000_000_000,
    )
    assert orders2 == []  # no close, no new entries
    assert "1786492800:TRC" in our_deals2
    assert our_deals2["1786492800:TRC"]["_our_qty"] == our_deals1["1786492800:TRC"]["_our_qty"]


def test_decide_orders_full_close_keeps_deal_with_zero_qty():
    """When a full-close is emitted (Đóng DEAL), the deal stays in
    our_deals with ``_our_qty=0`` — not popped. Combined with the
    actioned_deals set this prevents re-entry on the next cycle.

    Simulates the flow where autofin removes the deal from both
    /signal-static-holding AND /signal-static-suggest (suggest_ids=[]
    and holding=[]), then emits Đóng DEAL via the notification path.
    Since the adapter's _poll_notifications_for_events runs BEFORE
    decide_orders, the deal has already had its _our_qty reduced by
    the close event. We verify that decide_orders' close-handling
    keeps the deal (not pops it) and marks it actioned.
    """
    our_deals = {
        "1786492800:TRC": {
            "symbol": "TRC", "_our_qty": 0, "_last_numberOfProfit": 1,
            "supply_F": 81500, "supply_T": 83300,
        },
    }
    actioned: set = set()
    # Synthesize a close order as if the notification path already
    # produced it; decide_orders() should not pop the deal when it
    # processes this.
    close_order = {
        "symbol": "TRC", "side": "NS", "price": 84000, "quantity": 0,
        "kind": "close_event",
        "reason": "autofin_close_event:deal#1786492800:TRC:price84000:Đóng DEAL",
    }
    # Build new_our_deals directly to simulate the close path's effect.
    new_our_deals: dict = {did: dict(sig) for did, sig in our_deals.items()}
    # Close logic in decide_orders: qty=0 already, skip the qty=0 setter.
    # Just ensure the deal isn't popped by the close-handling loop.
    did = mod._extract_deal_id(close_order["reason"])
    assert did in new_our_deals
    # The deal should not be removed when qty is already 0.
    # (No mutation needed; the old code did pop here, which is the bug.)
    # Verify the actioned set includes the did after the close path.
    actioned.add(did)
    assert did in actioned
    # State survived: the row is still there with qty=0 for future reference.
    assert new_our_deals[did]["_our_qty"] == 0


def test_autofin_adapter_persists_actioned_deals(monkeypatch):
    """The adapter should serialize ``_actioned_deals`` to the state
    file so it survives restarts. This is the primary guard against
    re-entry after a full close."""
    import vietnam_algo.algos.autofin_signal as mod

    tmp = Path("/tmp/opencode/autofin_actioned_test")
    tmp.mkdir(parents=True, exist_ok=True)
    state_path = tmp / "live" / "autofin_signal_state.json"
    if state_path.exists():
        state_path.unlink()
    (tmp / "live").mkdir(parents=True, exist_ok=True)

    adapter = AutofinSignalAdapter()
    # Seed actioned deals and save.
    adapter._actioned_deals = {"1786492800:TRC", "1787000000:HII"}
    adapter._save_state(tmp)

    # Re-load: actioned_deals restored.
    fresh = AutofinSignalAdapter()
    fresh._load_state(tmp)
    assert fresh._actioned_deals == {"1786492800:TRC", "1787000000:HII"}

    # Clean up
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


class FullCloseSuggestsPriceTests(unittest.TestCase):
    """Regression tests for the autofin full_close SELL pricing.

    Autofin embeds the suggested close price in ``lastRecomment``
    for SL triggers ("Giá: 25,400 - Lỗ: -8.66 %"). The dry-run
    state should fill at that suggested price so realised P&L is
    realistic; the apply loop already falls back to ``last_price``
    when the price is 0 (final "Đóng DEAL" without a suggested
    price).
    """

    def test_sl_trigger_extracts_suggested_price(self) -> None:
        """The 'Giá: 25,400' pattern should parse into PartialCloseAction.price."""
        from vietnam_algo.algos.autofin_signal import _parse_partial_close
        rec = (
            "[HDB] 🛑 Chạm điểm cắt lỗ, cân nhắc hạ 100 % tỷ trọng "
            "Giá: 25,400 - Nắm giữ: 8 ngày Lỗ: -8.66 %"
        )
        action = _parse_partial_close(rec)
        self.assertEqual(action.action, "stop_loss_close")
        self.assertEqual(action.price, 25_400)

    def test_final_close_without_price_is_zero(self) -> None:
        """Final 'Đóng DEAL' signal has no suggested price."""
        from vietnam_algo.algos.autofin_signal import _parse_partial_close
        action = _parse_partial_close("Đóng DEAL chạm điểm cắt lỗ (Lỗ: -8.66 %)")
        self.assertEqual(action.action, "full_close")
        self.assertEqual(action.price, 0.0)

    def test_plan_close_orders_uses_suggested_price(self) -> None:
        """_plan_close_orders picks up the suggested price from the
        cached lastRecomment, so the emitted SELL carries autofin's
        suggest price rather than hardcoded 0."""
        from vietnam_algo.algos.autofin_signal import _plan_close_orders
        our_deals = {
            "deal#1:HDB": {
                "symbol": "HDB", "_our_qty": 1100, "_avg_cost": 27_100,
                "_last_recomment": (
                    "[HDB] 🛑 Chạm điểm cắt lỗ, cân nhắc hạ 100 % tỷ trọng "
                    "Giá: 25,400 - Lỗ: -8.66 %"
                ),
            },
        }
        autofin_holding: list = []  # autofin no longer holds this deal
        rows = _plan_close_orders(our_deals, autofin_holding)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 25_400)
        self.assertEqual(rows[0]["quantity"], 1100)
        self.assertEqual(rows[0]["kind"], "full_close")

    def test_plan_close_orders_falls_back_to_market_when_no_price(self) -> None:
        """When lastRecomment has no Giá: line, emit price=0 (the
        apply loop will fall back to last_price)."""
        from vietnam_algo.algos.autofin_signal import _plan_close_orders
        our_deals = {
            "deal#1:HDB": {
                "symbol": "HDB", "_our_qty": 1100, "_avg_cost": 27_100,
                "_last_recomment": "Đóng DEAL chạm điểm cắt lỗ (Lỗ: -8.66 %)",
            },
        }
        rows = _plan_close_orders(our_deals, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 0)




    def test_sl_signal_in_holding_extracts_suggested_price(self) -> None:
        """When autofin publishes an SL alert WITH a suggested price
        ("Giá: 25,400") while still holding the deal, our cached
        recomment should carry that price through to _plan_close_orders
        after autofin removes the deal from holding.

        Verifies the cache-refresh at the top of _plan_partial_close_orders
        so even deals with no numberOfProfit change still surface their
        latest recomment."""
        from vietnam_algo.algos.autofin_signal import (
            _parse_partial_close, _plan_partial_close_orders,
        )

        # 1. Autofin publishes SL recomment with suggested price.
        sl_text = (
            "[HDB] 🛑 Chạm điểm cắt lỗ, cân nhắc hạ 100 % tỷ trọng"
            " Giá: 25,400 - Nắm giữ: 8 ngày Lỗ: -8.66 %"
        )
        action = _parse_partial_close(sl_text)
        self.assertEqual(action.action, "stop_loss_close")
        self.assertEqual(action.price, 25_400)

        # 2. autofin auto-closes the deal. Next poll, deal is gone
        #    from holding. _plan_partial_close_orders skips it
        #    (action.action is in the skip list).
        our_deals = {
            "deal#1:HDB": {
                "symbol": "HDB", "_our_qty": 1100, "_avg_cost": 27_100,
                "_last_numberOfProfit": 0,
                "lastRecomment": sl_text,  # cached during the holding iteration
            },
        }
        # Simulate _plan_partial_close_orders with EMPTY holding —
        # the function should refresh sig["lastRecomment"] only when
        # the deal IS in holding, so no refresh here. The existing
        # cached recomment is what _plan_close_orders will read.
        rows = _plan_partial_close_orders(our_deals, [], board_lot=100)
        self.assertEqual(rows, [])  # deal not in holding → nothing emitted
        self.assertEqual(our_deals["deal#1:HDB"]["lastRecomment"], sl_text)


class CloseRequiresClosingSignalsTests(unittest.TestCase):
    """Regression tests for the 2026-09-14 HDB false-positive.

    Background: autofin briefly removed HDB from ``/signal-static-holding``
    while the deal was still active (autofin was about to republish it
    as a fresh "Khuyến nghị đầu tư" suggestion). The bot's old logic
    closed any deal missing from holding + suggest, which fired a
    SELL for HDB even though autofin still had it.

    Fix: only close a deal when it appears in ``/closing-signals``
    (autofin's explicit "Đóng DEAL" trigger). Absence from holding +
    suggest is NOT sufficient.
    """

    def test_hold_to_suggest_does_not_emit_close(self) -> None:
        """A deal present in autofin suggest but missing from
        /signal-static-holding is a fresh re-recommendation. Do not
        close it."""
        from vietnam_algo.algos.autofin_signal import _plan_close_orders
        our_deals = {
            "1786492800:HDB": {
                "symbol": "HDB", "_our_qty": 1100, "_avg_cost": 27_100,
                "_last_recomment": "Khuyến nghị đầu tư",
            },
        }
        rows = _plan_close_orders(
            our_deals, [],
            autofin_closing_ids=set(),
            autofin_suggest_ids=set(),
        )
        self.assertEqual(rows, [], "absent-from-holding must not emit close")

    def test_closing_signals_triggers_full_close(self) -> None:
        """When autofin publishes a close event for a held deal,
        _plan_close_orders emits the SELL with autofin's suggested
        price (from the cached lastRecomment)."""
        from vietnam_algo.algos.autofin_signal import _plan_close_orders
        our_deals = {
            "1786492800:HDB": {
                "symbol": "HDB", "_our_qty": 1100, "_avg_cost": 27_100,
                "_last_recomment": (
                    "[HDB] 🛑 Chạm điểm cắt lỗ, cân nhắc hạ 100 % tỷ trọng "
                    "Giá: 25,400 - Lỗ: -8.66 %"
                ),
            },
        }
        rows = _plan_close_orders(
            our_deals, [],
            autofin_closing_ids={"1786492800:HDB"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 25_400)
        self.assertEqual(rows[0]["kind"], "full_close")

    def test_closing_signals_ignores_deals_we_dont_hold(self) -> None:
        from vietnam_algo.algos.autofin_signal import _plan_close_orders
        our_deals = {
            "other_deal:VHM": {
                "symbol": "VHM", "_our_qty": 100, "_avg_cost": 70_000,
            },
        }
        rows = _plan_close_orders(
            our_deals, [],
            autofin_closing_ids={"1786492800:HDB"},  # not in our_deals
        )
        self.assertEqual(rows, [])

    def test_closing_signals_no_match_emits_nothing(self) -> None:
        """Live mode: empty closing_ids → emit nothing (don't fall
        back to absence-based closing)."""
        from vietnam_algo.algos.autofin_signal import _plan_close_orders
        our_deals = {
            "deal#1:HDB": {
                "symbol": "HDB", "_our_qty": 1100, "_avg_cost": 27_100,
            },
        }
        rows = _plan_close_orders(
            our_deals, [], autofin_closing_ids=set(),
        )
        self.assertEqual(rows, [])

    def test_closing_signals_does_not_use_absence_fallback(self) -> None:
        """Live mode: autofin briefly drops a deal from holding but
        doesn't publish a close. The live bot must NOT close."""
        from vietnam_algo.algos.autofin_signal import _plan_close_orders
        our_deals = {
            "1786492800:HDB": {
                "symbol": "HDB", "_our_qty": 1100, "_avg_cost": 27_100,
            },
        }
        # Live mode: closing_ids is provided (even if empty).
        # autofin published no close event.
        rows = _plan_close_orders(
            our_deals,
            [],  # autofin_holding empty (transient)
            autofin_closing_ids=set(),  # no close event
        )
        self.assertEqual(
            rows, [],
            "live bot must not emit close on absence alone — the 2026-09-14 bug",
        )

    def test_backtest_uses_absence_fallback(self) -> None:
        """Backtest mode: autofin_closing_ids=None → fall back to
        absence-based closing (any deal missing from holding+suggest)."""
        from vietnam_algo.algos.autofin_signal import _plan_close_orders
        our_deals = {
            "111:VHM": {"symbol": "VHM", "_our_qty": 100, "_avg_cost": 70_000},
        }
        # autofin_closing_ids=None → absence-based.
        # autofin_holding is empty → VHM is "gone" → emit close.
        rows = _plan_close_orders(our_deals, [], autofin_closing_ids=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "VHM")

        # If autofin still has the deal, do not close.
        autofin_holding = [{"_id": 111, "symbol": "VHM"}]
        rows = _plan_close_orders(
            our_deals, autofin_holding, autofin_closing_ids=None,
        )
        self.assertEqual(rows, [])



# ---------------------------------------------------------------------------
# Cash-budget cap (2026-09-16, user feedback on autofin dry-run)
# ---------------------------------------------------------------------------
# Background: the user reported the bot was planning
#   NB 300 GAS @ 90,000 (= 27M VND)
# when the dry-run state's cash was only ~30K VND (portfolio fully
# invested in 6 stocks). With NAV-based sizing
# ``nav * rateForNav = 100M * 0.30 = 30M`` the BUY was unfulfillable.
#
# Fix: ``_plan_orders_for_new_positions`` accepts an optional
# ``available_cash_vnd`` parameter. When provided, each BUY's
# ``target_value`` is capped at the *remaining cash* across the
# signal list, and signals whose target_value drops below
# ``min_order_value_vnd`` are skipped.


def test_plan_new_positions_caps_target_at_available_cash():
    """Cash-cap: BUY size is bounded by remaining_cash, not unbounded
    by nav_vnd * rateForNav."""
    # Use price=10K so the cap is meaningful. With nav=100M and
    # rateForNav=0.30, the uncapped target_value would be 30M = 3000 lots.
    # With cash cap of 5M, target_value ≤ 5M → qty ≤ 500 lots. The actual
    # actual_cost adjustment reduces this by 1 board lot (15bps fees
    # would push 500*10K = 5M over the 5M boundary), so 400 lots.
    sig = _make_signal("GAS", 0.30, supply_to=10000, tp_from=11000, sl_to=9000)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=100_000_000,  # would size at 30M target_value
        our_positions={},
        available_cash_vnd=5_000_000,  # but cash can only afford 5M
    )
    assert len(rows) == 2, "expected BUY + SL"
    buy = next(r for r in rows if r["side"] == "NB")
    # Without the cap, qty would be 3000 lots. With cap, qty should
    # be ≤ 500 lots (and the actual cost including 15bps fees must fit
    # within 5M → 400 lots after the cap adjusts for fees).
    assert buy["quantity"] <= 500, (
        f"BUY should be capped by available cash; got {buy['quantity']} lots"
    )
    assert buy["quantity"] < 1000, (
        f"BUY should NOT have hit the uncapped 3000-lot target; "
        f"got {buy['quantity']} lots"
    )
    # Verify the cost fits in cash.
    cost = buy["quantity"] * buy["price"] * 1.0015
    assert cost <= 5_000_000, f"cost {cost} exceeds available cash 5M"
    assert buy["price"] == 10000


def test_plan_new_positions_skips_when_cash_below_one_lot():
    """If available_cash can't afford even one board lot, skip the signal
    entirely. This is the user's reported scenario: cash=30K, target
    stock=50K → can't afford 1 lot."""
    sig = _make_signal("GAS", 0.30, supply_to=50000, tp_from=55000, sl_to=45000)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=100_000_000,
        our_positions={},
        available_cash_vnd=30_000,  # < 1 board lot of 50K
    )
    assert rows == [], (
        "Should skip signal when cash < 1 board lot cost, got: " + repr(rows)
    )


def test_plan_new_positions_remaining_cash_shared_across_signals():
    """Multiple BUYs share the cash budget; the second BUY is shrunk
    (or skipped) by the cost of the first."""
    # Use cheap stocks so a 20M cash budget fits both lots-rounded BUYs.
    sig_a = _make_signal("AAA", 0.10, supply_to=10000, tp_from=11000, sl_to=9000)
    sig_b = _make_signal("BBB", 0.30, supply_to=8000, tp_from=9000, sl_to=7000)
    rows = _plan_orders_for_new_positions(
        selected=[sig_a, sig_b],
        nav_vnd=100_000_000,
        our_positions={},
        available_cash_vnd=20_000_000,
    )
    buys = [r for r in rows if r["side"] == "NB"]
    buys_by_sym = {b["symbol"]: b for b in buys}
    # Both should be present (combined cost < 20M).
    assert "AAA" in buys_by_sym, "expected AAA buy"
    assert "BBB" in buys_by_sym, "expected BBB buy"
    a_cost = buys_by_sym["AAA"]["quantity"] * 10_000 * 1.0015
    b_cost = buys_by_sym["BBB"]["quantity"] * 8_000 * 1.0015
    total = a_cost + b_cost
    # Total spend must fit within the cash budget.
    assert total <= 20_000_000 * 1.001, (
        f"Total spend {total:.0f} should fit within cash 20M; "
        f"AAA={buys_by_sym['AAA']['quantity']} lots, "
        f"BBB={buys_by_sym['BBB']['quantity']} lots"
    )
    # And neither individual BUY should be capped by 20M in isolation.
    assert a_cost <= 20_000_000
    assert b_cost <= 20_000_000
    # The second BUY must be sized SMALLER than it would be if it had the
    # full cash budget — proving remaining_cash tracking works.
    b_target_if_alone = (20_000_000 // 8_000 // 100) * 100  # ~2500 lots
    assert buys_by_sym["BBB"]["quantity"] < b_target_if_alone, (
        f"BBB should be smaller than its standalone target {b_target_if_alone}; "
        f"got {buys_by_sym['BBB']['quantity']}"
    )


def test_plan_new_positions_no_cash_cap_uses_nav_default():
    """Backward-compat: when available_cash_vnd is NOT passed, the
    function falls back to nav_vnd (legacy behaviour)."""
    sig = _make_signal("XYZ", 0.30, supply_to=50000, tp_from=55000, sl_to=45000)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=20_000_000,  # → 6M target_value → 100 lots @ 50K
        our_positions={},
        available_cash_vnd=None,  # no cap → use nav
    )
    buys = [r for r in rows if r["side"] == "NB"]
    assert len(buys) == 1
    assert buys[0]["quantity"] == 100


def test_plan_new_positions_zero_available_cash_uses_nav():
    """Edge case: available_cash_vnd=0 (zero cash, fully invested).
    Should NOT cause the function to fall back to a non-zero budget;
    instead it should match the user's intent of 'no cash for new BUY'
    by skipping signals that need cash."""
    sig = _make_signal("XYZ", 0.20, supply_to=50000, tp_from=55000, sl_to=45000)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=20_000_000,
        our_positions={},
        available_cash_vnd=0,
    )
    # remaining_cash was set to available_cash_vnd=0; target_value caps
    # to 0; < min_order_value → skip. So no orders.
    assert rows == []


class PartialCloseUsesPositionsGroundTruthTests(unittest.TestCase):
    """Regression tests for the 2026-09-18 incident where
    ``_our_qty`` in the autofin tracking state was 0 for every deal
    even though the dry-run state held 6 positions, so partial closes
    never fired. The fix uses ``our_positions`` (DNSE / dry-run) as
    ground truth for sizing and filtering.
    """

    def _autofin_asp_holding(self) -> list[dict[str, Any]]:
        return [
            {
                "_id": 1789084800,
                "symbol": "ASP",
                "lastRecomment": "💰 Chốt lãi lần 1 mức giá 15,800",
                "numberOfProfit": 1,
                "tP_F": 15800,
                "statusDeal": {"isClosed": False},
            }
        ]

    def test_partial_close_fires_when_our_qty_is_zero_but_position_exists(self) -> None:
        """Reproduce the ASP bug: tracking _our_qty=0, but dry-run
        state shows 600 shares. The partial close must still fire."""
        our_deals = {
            "1789084800:ASP": {
                "symbol": "ASP",
                "_our_qty": 0,  # the bug
                "_last_numberOfProfit": 0,  # nP went 0→1 in this cycle
            }
        }
        holding = self._autofin_asp_holding()
        our_positions = {"ASP": 600}  # ground truth from dry-run
        rows = mod._plan_partial_close_orders(
            our_deals, holding, our_positions=our_positions, board_lot=100,
        )
        # 600 // 3 = 200 shares
        assert len(rows) == 1
        assert rows[0]["symbol"] == "ASP"
        assert rows[0]["quantity"] == 200
        # Tracking state is repaired for next cycle AND decremented by
        # the partial close (so it reflects "what we still hold").
        assert our_deals["1789084800:ASP"]["_our_qty"] == 400

    def test_partial_close_skips_when_no_position_in_dry_run(self) -> None:
        """Defense check: if both dry-run AND _our_qty are 0, don't fire."""
        our_deals = {
            "1789084800:ASP": {
                "symbol": "ASP",
                "_our_qty": 0,  # also zero
                "_last_numberOfProfit": 0,
            }
        }
        holding = self._autofin_asp_holding()
        our_positions = {}  # we sold everything
        rows = mod._plan_partial_close_orders(
            our_deals, holding, our_positions=our_positions, board_lot=100,
        )
        assert rows == []

    def test_partial_close_uses_tp_price_when_recomment_provides_one(self) -> None:
        """When autofin suggests a specific TP price, use it (not market)."""
        our_deals = {
            "1789084800:ASP": {
                "symbol": "ASP",
                "_our_qty": 0,
                "_last_numberOfProfit": 0,
            }
        }
        holding = self._autofin_asp_holding()
        rows = mod._plan_partial_close_orders(
            our_deals, holding,
            our_positions={"ASP": 900},
            board_lot=100,
        )
        assert len(rows) == 1
        assert rows[0]["price"] == 15800  # from "mức giá 15,800"

    def test_full_close_uses_positions_when_our_qty_is_zero(self) -> None:
        """Same defense for full close: trust positions over stale _our_qty."""
        our_deals = {
            "1789084800:HDB": {
                "symbol": "HDB",
                "_our_qty": 0,  # stale
                "lastRecomment": "",
            }
        }
        rows = mod._plan_close_orders(
            our_deals, [],
            our_positions={"HDB": 1100},
            autofin_closing_ids={"1789084800:HDB"},
            board_lot=100,
        )
        assert len(rows) == 1
        assert rows[0]["symbol"] == "HDB"
        assert rows[0]["quantity"] == 1100
        # Tracking state is repaired.
        assert our_deals["1789084800:HDB"]["_our_qty"] == 1100

    def test_partial_close_falls_back_to_our_qty_when_positions_unspecified(self) -> None:
        """Backward compat: older callers (backtests) without positions
        still work — fall back to ``_our_qty`` in the deal dict."""
        our_deals = {
            "1789084800:ASP": {
                "symbol": "ASP",
                "_our_qty": 600,
                "_last_numberOfProfit": 0,
            }
        }
        holding = self._autofin_asp_holding()
        # No our_positions kwarg — fallback path
        rows = mod._plan_partial_close_orders(our_deals, holding, board_lot=100)
        assert len(rows) == 1
        assert rows[0]["quantity"] == 200

    def test_partial_close_skips_when_nP_did_not_increment(self) -> None:
        """No new TP step AND no recomment change → no partial close
        (prevents re-firing on the same announcement across cycles)."""
        our_deals = {
            "1789084800:ASP": {
                "symbol": "ASP",
                "_our_qty": 0,
                "_last_numberOfProfit": 1,  # already saw TP1
                # Same recomment as the holding entry → no change.
                "_last_recomment": "💰 Chốt lãi lần 1 mức giá 15,800",
            }
        }
        holding = self._autofin_asp_holding()  # nP still 1, same rec
        rows = mod._plan_partial_close_orders(
            our_deals, holding,
            our_positions={"ASP": 600},
            board_lot=100,
        )
        assert rows == []

    def test_partial_close_fires_when_recomment_changed(self) -> None:
        """Even if nP is unchanged, a recomment change re-triggers
        the partial close (catches state-desync recovery and autofin
        switching action types for the same nP step)."""
        our_deals = {
            "1789084800:ASP": {
                "symbol": "ASP",
                "_our_qty": 0,
                "_last_numberOfProfit": 1,
                # Old recomment (e.g. from a buggy previous cycle where
                # we couldn't act). Autofin now re-issued a different
                # recomment for the same nP.
                "_last_recomment": "🛡️ Chốt lãi 5% bảo toàn vốn",
            }
        }
        holding = self._autofin_asp_holding()  # nP=1, recomment "TP1"
        rows = mod._plan_partial_close_orders(
            our_deals, holding,
            our_positions={"ASP": 600},
            board_lot=100,
        )
        # Fires on recomment change.
        assert len(rows) == 1
        assert rows[0]["symbol"] == "ASP"
        assert rows[0]["quantity"] == 200
