"""Tests for new autofin_signal deal-id tracking logic."""
from __future__ import annotations

import sys
sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

import vietnam_algo.strategies_impl.autofin_signal as mod
from vietnam_algo.strategies_impl.autofin_signal import _deal_id, _plan_close_orders, _plan_orders_for_new_positions


def _make_signal(
    symbol: str,
    rate_for_nav: float,
    deal_id: str = "12345",
    last_index: int = 1,
    supply_to: float = 10000.0,
    tp_from: float = 12000.0,
    sl_to: float = 9000.0,
) -> dict:
    raw = {
        "_id": int(deal_id) if deal_id.isdigit() else deal_id,
        "lastIndex": last_index,
        "symbol": symbol,
        "companyName": symbol,
        "statusDeal": {"market_TypePrice": 1},
        "supply_F": (supply_to - 100) / 1000,
        "supply_T": supply_to / 1000,
        "tP_F": tp_from / 1000,
        "tP_T": (tp_from + 100) / 1000,
        "sL_F": (sl_to - 100) / 1000,
        "sL_T": sl_to / 1000,
        "rateForNav": rate_for_nav,
        "lastRecomment": "test",
        "isWaittingBuy": False,
        "isWaittingSell": False,
        "numberOfProfit": 0,
        "rateIncome": 0,
        "rateIncome_TypePrice": 3,
    }
    return raw


def test_plan_new_positions_emits_buy_tp_sl_bracket():
    """New entry must come with TP and SL exit orders."""
    sig = mod.parse_signal(_make_signal("APH", 0.15, deal_id="111", supply_to=6100, tp_from=6500, sl_to=5320))
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=1_000_000_000, our_positions={}
    )
    sides = [r["side"] for r in rows]
    assert sides.count("NB") == 1
    assert sides.count("NS") == 2  # TP + SL
    buy = next(r for r in rows if r["side"] == "NB")
    assert "deal#111" in buy["reason"]
    assert buy["price"] == 6100


def test_plan_new_positions_skips_when_target_too_small():
    sig = mod.parse_signal(_make_signal("APH", 0.05, deal_id="222", supply_to=6100))
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=10_000_000, our_positions={}
    )
    # target = 0.05 * 10M = 500k < 1M min → skip
    assert rows == []


def test_plan_new_positions_no_op_when_match():
    sig = mod.parse_signal(_make_signal("APH", 0.10, deal_id="333", supply_to=6100))
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=610_000, our_positions={"APH": 100}
    )
    # 0.10 * 610k = 61k < 1M min → skip
    assert rows == []


def test_plan_close_orders_only_closes_what_we_hold():
    """SELL only deals we hold that autofin no longer holds."""
    our_deals = {
        "111": {"symbol": "APH", "_our_qty": 200},
        "222": {"symbol": "VHM", "_our_qty": 100},
        "333": {"symbol": "BAF", "_our_qty": 50},
    }
    autofin_holding = [
        # APH still held
        {"_id": 111, "symbol": "APH", "statusDeal": {"isClosed": False}},
        # VHM exited
        # BAF still held
        {"_id": 333, "symbol": "BAF", "statusDeal": {"isClosed": False}},
    ]
    rows = _plan_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "VHM"
    assert rows[0]["reason"] == "autofin_closed:deal#222"


def test_deal_id_extraction_handles_underscore_or_code():
    raw1 = {"_id": 12345, "code": 99999, "symbol": "A"}
    raw2 = {"_id": None, "code": 88888, "symbol": "B"}
    raw3 = {"symbol": "C"}  # no id
    assert _deal_id(raw1) == "12345"
    assert _deal_id(raw2) == "88888"
    assert _deal_id(raw3) == ""
