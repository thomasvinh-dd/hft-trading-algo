"""Tests for autofin double-entry dedup and notification key stability.

Per user feedback (2026-09-09): the autofin bot was emitting a fresh
"new entry" Telegram notification every day for the same deal because:
  1. ``_order_key`` included price + quantity, so the dedup set treated
     each day's re-plan as a different order.
  2. ``_plan_orders_for_new_positions`` only consulted DNSE positions
     (which are empty in dry-run mode), so it happily re-emitted a BUY
     for a deal it had already planned on a prior cycle.

These tests cover both fixes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

from vietnam_algo.strategies_impl.autofin_signal import (
    _order_key,
    _plan_orders_for_new_positions,
    _arm,
    decide_orders,
)
from vietnam_algo.autofin_signals import parse_signal
from typing import Any


def _make_signal(
    symbol: str,
    supply_from: float = 6000.0,
    supply_to: float = 6100.0,
    tp_from: float = 6500.0,
    sl_from: float = 5500.0,
    rate: float = 0.15,
    deal_id: int = 100,
) -> Any:
    raw = {
        "_id": deal_id,
        "symbol": symbol,
        "lastIndex": deal_id,
        "statusDeal": {
            "market_TypePrice": 1,
            "isClosed": False,
            "price_Open": supply_to,
            "time_Open": "09/08/2026 07:00:00",
        },
        "supply_F": supply_from,
        "supply_T": supply_to,
        "tP_F": tp_from,
        "tP_T": tp_from + 100,
        "sL_F": sl_from,
        "sL_T": sl_from + 100,
        "rateForNav": rate,
        "lastRecomment": "Khuyến nghị đầu tư",
        "isWaittingBuy": False,
        "isWaittingSell": False,
        "numberOfProfit": 0,
        "rateIncome": 0,
        "rateIncome_TypePrice": 3,
        "rS_Rating_IDB": 0,
        "rateWinByPrice": 0,
    }
    return parse_signal(raw)


# -----------------------------------------------------------------------------
# _order_key
# -----------------------------------------------------------------------------


def test_order_key_is_stable_across_price_moves():
    """Same deal + action, different price → same key."""
    o1 = {"reason": "autofin_buy:deal#1787616000:HII:nav15pct:...",
          "side": "NB", "symbol": "HII", "price": 9000, "quantity": 1600}
    o2 = {"reason": "autofin_buy:deal#1787616000:HII:nav15pct:...",
          "side": "NB", "symbol": "HII", "price": 9210, "quantity": 1600}
    assert _order_key(o1) == _order_key(o2)


def test_order_key_is_stable_across_partial_budget_qty_changes():
    """Same deal, different qty (partial-budget adjustment) → same key."""
    o1 = {"reason": "autofin_buy:deal#1788825600:PVT:nav25pct:...",
          "side": "NB", "symbol": "PVT", "price": 21300, "quantity": 1100}
    o2 = {"reason": "autofin_buy:deal#1788825600:PVT:nav30pct:...",
          "side": "NB", "symbol": "PVT", "price": 21300, "quantity": 1400}
    assert _order_key(o1) == _order_key(o2)


def test_order_key_distinguishes_deals():
    o_a = {"reason": "autofin_buy:deal#111:AAA:nav15pct:...",
           "side": "NB", "symbol": "AAA", "price": 100, "quantity": 100}
    o_b = {"reason": "autofin_buy:deal#222:BBB:nav15pct:...",
           "side": "NB", "symbol": "BBB", "price": 100, "quantity": 100}
    assert _order_key(o_a) != _order_key(o_b)


def test_order_key_distinguishes_actions_on_same_deal():
    """Entry vs TP vs SL on the same deal must have distinct keys."""
    entry = {"reason": "autofin_buy:deal#123:ABC:nav15pct:...",
             "side": "NB", "symbol": "ABC", "price": 100, "quantity": 100}
    tp = {"reason": "autofin_tp:deal#123:ABC:100",
          "side": "NS", "symbol": "ABC", "price": 110, "quantity": 100}
    sl = {"reason": "autofin_user_sl:deal#123:ABC:5%:95",
          "side": "NS", "symbol": "ABC", "price": 95, "quantity": 100}
    assert len({_order_key(entry), _order_key(tp), _order_key(sl)}) == 3


def test_arm_extractor():
    assert _arm("autofin_buy:deal#123:ABC:nav15pct:...") == "entry"
    assert _arm("autofin_tp:deal#123:ABC:110").startswith("tp:")
    assert _arm("autofin_user_sl:deal#123:ABC:5%:95") == "usersl"
    assert _arm("autofin_sl:deal#123:ABC:95") == "sl"
    assert _arm("autofin_partial:deal#123:ABC") == "partial"
    assert _arm("autofin_closed:deal#123:ABC") == "closed"
    assert _arm("") == ""


# -----------------------------------------------------------------------------
# already_planned_symbols — skip double-buy in dry-run
# -----------------------------------------------------------------------------


def test_plan_skips_symbol_in_already_planned_set():
    """If we've already planned a BUY for HII, don't plan another."""
    sig = _make_signal("HII", deal_id=42, supply_from=6000, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=1_000_000_000,
        our_positions={},  # DNSE empty (dry-run)
        current_prices={"HII": 6050.0},
        already_planned_symbols={"HII"},  # bot already planned it last cycle
    )
    assert rows == [], "should NOT re-buy HII since it's already planned"


def test_plan_does_not_skip_unrelated_symbol():
    """Other symbols not in the planned set should still get planned."""
    sig_hii = _make_signal("HII", deal_id=42)
    sig_pvt = _make_signal(
        "PVT", deal_id=99,
        supply_from=20000.0, supply_to=21000.0,
    )
    rows = _plan_orders_for_new_positions(
        selected=[sig_hii, sig_pvt],
        nav_vnd=1_000_000_000,
        our_positions={},
        current_prices={"HII": 6050.0, "PVT": 20500.0},
        already_planned_symbols={"HII"},  # only HII is planned
    )
    symbols = {r["symbol"] for r in rows if r["side"] == "NB"}
    assert symbols == {"PVT"}


def test_plan_uses_already_planned_alongside_dnse_position():
    """When BOTH DNSE and our state say "we already have it", skip."""
    sig = _make_signal("HII", deal_id=42)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=1_000_000_000,
        our_positions={"HII": 1600},  # DNSE knows we have it
        current_prices={"HII": 6050.0},
        already_planned_symbols={"HII"},  # and our state knows too
    )
    assert rows == []


# -----------------------------------------------------------------------------
# Integration: decide_orders
# -----------------------------------------------------------------------------


def test_decide_orders_emits_no_entry_for_already_planned_deal():
    """End-to-end: a deal already in our_deals with qty>0 should NOT emit BUY."""
    sig = _make_signal("HII", deal_id=42)
    # Set up our_deals as if we already planned+filled the position.
    our_deals = {
        "42:HII": {
            "_id": 42,
            "_our_qty": 1600,
            "symbol": "HII",
            "supply_F": 6000, "supply_T": 6100,
        },
    }
    already_planned = {"HII"}
    all_orders, _, _ = decide_orders(
        holding=[],
        signals=[sig],
        our_deals=our_deals,
        our_positions={},  # DNSE empty (dry-run)
        nav=1_000_000_000,
        current_prices={"HII": 6050.0},
        already_planned_symbols=already_planned,
    )
    buys = [o for o in all_orders if o["side"] == "NB"]
    assert buys == [], f"expected no BUY, got: {buys}"


def test_decide_orders_does_not_double_emit_on_consecutive_cycles():
    """Simulate day 1 → day 2: day 2 should not re-emit day 1's deals."""
    sig = _make_signal("HII", deal_id=42)
    # ---- Day 1: no state, no planned symbols
    day1_orders, day1_our_deals, _ = decide_orders(
        holding=[],
        signals=[sig],
        our_deals={},
        our_positions={},
        nav=1_000_000_000,
        current_prices={"HII": 6050.0},
    )
    day1_buys = [o for o in day1_orders if o["side"] == "NB"]
    assert len(day1_buys) == 1, "day 1 should emit one BUY"

    # ---- Day 2: state from day 1, planned symbols from day 1
    already_planned = {"HII"}
    day2_orders, _, _ = decide_orders(
        holding=[],
        signals=[sig],
        our_deals=day1_our_deals,
        our_positions={},  # DNSE still empty
        nav=1_000_000_000,
        current_prices={"HII": 7000.0},  # price moved up
        already_planned_symbols=already_planned,
    )
    day2_buys = [o for o in day2_orders if o["side"] == "NB"]
    assert day2_buys == [], (
        "day 2 should NOT re-buy HII even though price moved; "
        "we already have a planned position"
    )


if __name__ == "__main__":
    import unittest
    unittest.main()
