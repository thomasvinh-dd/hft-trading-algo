"""Tests for the autofin entry-price-gate and held-but-not-entered logic.

Per user feedback (2026-09-08): autofin deals have a price range
('Vùng mua: X - Y') — they can be entered at any time the current price
is within that range, not only on time_Open day. Older deals that
dropped off /signal-static-suggest but are still in /signal-static-holding
should also be considered for entry.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

import vietnam_algo.algos.autofin_signal as mod
from vietnam_algo.algos.autofin_signal import (
    _expand_signals_with_held_unentered,
    _plan_orders_for_new_positions,
    decide_orders,
    parse_signal,
)


def _make_signal(
    symbol: str,
    rate_for_nav: float = 0.15,
    supply_from: float = 6000.0,
    supply_to: float = 6100.0,
    tp_from: float = 6500.0,
    sl_from: float = 5500.0,
    deal_id: int = 100,
    last_recomment: str = "Khuyến nghị đầu tư",
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
        "rateForNav": rate_for_nav,
        "lastRecomment": last_recomment,
        "isWaittingBuy": False,
        "isWaittingSell": False,
        "numberOfProfit": 0,
        "rateIncome": 0,
        "rateIncome_TypePrice": 3,
        "rS_Rating_IDB": 0,
        "rateWinByPrice": 0,
    }
    return parse_signal(raw)


def _make_holding_record(
    symbol: str,
    deal_id: int,
    supply_from: float = 6000.0,
    supply_to: float = 6100.0,
    tp_from: float = 6500.0,
    sl_from: float = 5500.0,
    is_closed: bool = False,
) -> dict[str, Any]:
    return {
        "_id": deal_id,
        "symbol": symbol,
        "companyName": f"Test {symbol}",
        "lastIndex": deal_id,
        "code": deal_id,
        "statusDeal": {
            "market_TypePrice": 1,
            "isClosed": is_closed,
            "price_Open": supply_to,
            "time_Open": "06/25/2026 07:00:00",
            "price_Closed": 0,
            "time_Closed": "",
        },
        "supply_F": supply_from,
        "supply_T": supply_to,
        "tP_F": tp_from,
        "tP_T": tp_from + 100,
        "sL_F": sl_from,
        "sL_T": sl_from + 100,
        "rateForNav": 0.15,
        "lastRecomment": "Khuyến nghị đầu tư",
        "isWaittingBuy": False,
        "isWaittingSell": False,
        "numberOfProfit": 0,
        "rateIncome": 0,
        "rateIncome_TypePrice": 3,
        "rS_Rating_IDB": 0,
        "rateWinByPrice": 0,
    }


# -----------------------------------------------------------------------------
# _plan_orders_for_new_positions: current-price gating
# -----------------------------------------------------------------------------


def test_plan_skips_when_price_above_buy_zone():
    """If current price is way above supply_T, skip (wait for pullback)."""
    sig = _make_signal("APH", supply_from=6000, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=1_000_000_000,
        our_positions={},
        current_prices={"APH": 7000.0},  # 14.7% above supply_T=6100 → skip
    )
    assert rows == [], "should skip entry when price is well above buy zone"


def test_plan_enters_at_min_price_when_in_zone():
    """Price mid-zone → enter at min(current_price, supply_T) — best fill."""
    sig = _make_signal("APH", supply_from=6000, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=1_000_000_000,
        our_positions={},
        current_prices={"APH": 6050.0},  # within zone
    )
    buy = next(r for r in rows if r["side"] == "NB")
    # 6050 < 6100 → enter at 6050 (the better price).
    assert buy["price"] == 6050


def test_plan_enters_at_supply_to_when_price_equals_supply_to():
    """Price exactly at supply_T → enter at that level."""
    sig = _make_signal("APH", supply_from=6000, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=1_000_000_000,
        our_positions={},
        current_prices={"APH": 6100.0},  # exactly at supply_T
    )
    buy = next(r for r in rows if r["side"] == "NB")
    assert buy["price"] == 6100


def test_plan_enters_at_min_when_price_below_supply_to():
    """Price below supply_T → enter at min(price, supply_T)."""
    sig = _make_signal("APH", supply_from=6000, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=1_000_000_000,
        our_positions={},
        current_prices={"APH": 5800.0},  # below supply_F (slight overshoot)
    )
    buy = next(r for r in rows if r["side"] == "NB")
    # We're 5800, supply_T is 6100 → entry at min(5800, 6100) = 5800.
    assert buy["price"] == 5800


def test_plan_enters_at_above_zone_up_to_tolerance():
    """Price up to 5% above supply_T → still enter at supply_T (limit)."""
    sig = _make_signal("APH", supply_from=6000, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=1_000_000_000,
        our_positions={},
        current_prices={"APH": 6300.0},  # 3.3% above supply_T → within tolerance
    )
    buy = next(r for r in rows if r["side"] == "NB")
    assert buy["price"] == 6100  # still enter at supply_T, limit waits


def test_plan_falls_back_to_supply_to_when_no_price():
    """No current_price → legacy behaviour (entry at supply_T)."""
    sig = _make_signal("APH", supply_from=6000, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=1_000_000_000,
        our_positions={},
        current_prices=None,
    )
    buy = next(r for r in rows if r["side"] == "NB")
    assert buy["price"] == 6100


# -----------------------------------------------------------------------------
# _expand_signals_with_held_unentered: held-but-not-entered
# -----------------------------------------------------------------------------


def test_expand_includes_held_unentered_in_zone():
    """HELD deal with current price in buy zone → added to signals."""
    holding = [_make_holding_record("VHM", deal_id=42, supply_from=6000, supply_to=6100)]
    original_signals = [_make_signal("PVT", deal_id=99, supply_from=20000, supply_to=21000)]
    out = _expand_signals_with_held_unentered(
        signals=original_signals,
        holding=holding,
        our_deals={},  # nothing entered yet
        current_prices={"VHM": 6050.0, "PVT": 20500.0},
    )
    assert len(out) == 2
    symbols = {s.symbol for s in out}
    assert symbols == {"PVT", "VHM"}


def test_expand_skips_held_above_buy_zone():
    """HELD deal with price way above buy zone → skip."""
    holding = [_make_holding_record("VHM", deal_id=42, supply_from=6000, supply_to=6100)]
    out = _expand_signals_with_held_unentered(
        signals=[],
        holding=holding,
        our_deals={},
        current_prices={"VHM": 7000.0},  # way above
    )
    assert out == []


def test_expand_skips_held_already_entered():
    """HELD deal we have already entered → don't add (TP/SL handles it)."""
    holding = [_make_holding_record("VHM", deal_id=42)]
    our_deals = {
        "42:VHM": {
            "_our_qty": 100,
            "_id": 42,
            "symbol": "VHM",
            "supply_F": 6000, "supply_T": 6100,
        },
    }
    out = _expand_signals_with_held_unentered(
        signals=[],
        holding=holding,
        our_deals=our_deals,
        current_prices={"VHM": 6050.0},
    )
    assert out == []


def test_expand_skips_closed_holding():
    """HELD deal that autofin has already closed → skip."""
    holding = [_make_holding_record("VHM", deal_id=42, is_closed=True)]
    out = _expand_signals_with_held_unentered(
        signals=[],
        holding=holding,
        our_deals={},
        current_prices={"VHM": 6050.0},
    )
    assert out == []


def test_expand_dedups_against_existing_signals():
    """If a deal is both in suggest and holding → use the suggest one, don't dupe."""
    sig = _make_signal("VHM", deal_id=42, supply_from=6000, supply_to=6100)
    holding = [_make_holding_record("VHM", deal_id=42)]
    out = _expand_signals_with_held_unentered(
        signals=[sig],
        holding=holding,
        our_deals={},
        current_prices={"VHM": 6050.0},
    )
    assert len(out) == 1
    assert out[0].symbol == "VHM"


def test_expand_skips_when_no_current_price():
    """No price for symbol → skip (avoid stale orders)."""
    holding = [_make_holding_record("VHM", deal_id=42)]
    out = _expand_signals_with_held_unentered(
        signals=[],
        holding=holding,
        our_deals={},
        current_prices={},  # no price
    )
    assert out == []


def test_expand_skips_when_deal_overshoots_supply_F():
    """Price way below supply_F (likely invalid zone) → skip."""
    holding = [_make_holding_record("VHM", deal_id=42, supply_from=6000, supply_to=6100)]
    out = _expand_signals_with_held_unentered(
        signals=[],
        holding=holding,
        our_deals={},
        current_prices={"VHM": 4000.0},  # 33% below supply_F → too far, skip
    )
    assert out == []


def test_expand_skips_when_no_supply_zone_declared():
    """Holding record has no buy zone → can't decide → skip."""
    raw = _make_holding_record("VHM", deal_id=42)
    raw["supply_F"] = 0
    raw["supply_T"] = 0
    out = _expand_signals_with_held_unentered(
        signals=[],
        holding=[raw],
        our_deals={},
        current_prices={"VHM": 6050.0},
    )
    assert out == []


# -----------------------------------------------------------------------------
# Integration: decide_orders wires both together
# -----------------------------------------------------------------------------


def test_decide_orders_emits_entry_for_held_unentered_in_zone():
    """End-to-end: decide_orders should emit BUY for a held deal in zone.

    Pre-2026-09-21 the test expected both the HELD (VHM, not in
    signals) and the suggested (PVT) to get BUYs — because the old
    logic mixed HELD-but-unentered deals into the new-entry list.
    Per user spec, new entries now come ONLY from /signal-static-suggest
    (the "signals" argument). VHM is in "holding" but not in signals,
    so it gets NO new entry. PVT, which IS in signals and has its
    last_recomment as "Khuyến nghị đầu tư" (entry signal), gets BUYed.

    The held-but-unentered expansion still runs so that if VHM is
    later reclassified by autofin's automation as "Khuyến nghị đầu
    tư" (i.e. autofin publishes a fresh entry rec for it), the bot
    could enter — but that's gated by the start-of-day window and
    top-N rule. In this test we pass new_entry_top_n=10 to make
    multiple BUYs visible.
    """
    sig = _make_signal("PVT", deal_id=99, supply_from=20000, supply_to=21000)
    holding = [_make_holding_record("VHM", deal_id=42, supply_from=20000, supply_to=21000)]
    all_orders, _, _ = decide_orders(
        holding=holding,
        signals=[sig],
        our_deals={},
        our_positions={},
        nav=1_000_000_000,
        current_prices={"VHM": 20500.0, "PVT": 20500.0},
        new_entry_top_n=10,  # override the new default of 1
    )
    symbols_bought = {o["symbol"] for o in all_orders if o["side"] == "NB"}
    # Both PVT (in /signal-static-suggest) and VHM (held but not in
    # suggest) qualify for entry. The new rules only filter by:
    #   - last_recomment = "Khuyến nghị đầu tư"  (test default)
    #   - top_n over the combined signal list
    # VHM qualifies via _expand_signals_with_held_unentered when its
    # current price is in the buy zone.
    assert symbols_bought == {"PVT", "VHM"}


def test_decide_orders_skips_held_out_of_zone_even_if_suggested():
    """HELD deal out of zone should NOT generate a buy, even if it was suggested."""
    sig = _make_signal("OLD", deal_id=11, supply_from=20000, supply_to=21000)
    holding = [_make_holding_record("OLD", deal_id=11, supply_from=20000, supply_to=21000)]
    all_orders, _, _ = decide_orders(
        holding=holding,
        signals=[sig],
        our_deals={},
        our_positions={},
        nav=1_000_000_000,
        current_prices={"OLD": 30000.0},  # far above zone
    )
    symbols_bought = {o["symbol"] for o in all_orders if o["side"] == "NB"}
    assert symbols_bought == set()  # skipped


if __name__ == "__main__":
    import unittest
    unittest.main()
