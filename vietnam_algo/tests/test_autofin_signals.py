"""Tests for autofin_signals parsing + signal-to-order logic."""
from __future__ import annotations

from vietnam_algo.autofin_signals import AutofinSignal, parse_signal


def test_parse_signal_basic():
    raw = {
        "_id": 123,
        "symbol": "APH",
        "companyName": "An Phat Holdings",
        "statusDeal": {"market_TypePrice": 1},
        "supply_F": 5.91,
        "supply_T": 6.1,
        "tP_F": 6.5,
        "tP_T": 7.0,
        "sL_F": 5.32,
        "sL_T": 5.61,
        "rateForNav": 0.15,
        "lastRecomment": "Khuyến nghị đầu tư",
        "isWaittingBuy": True,
        "isWaittingSell": False,
        "numberOfProfit": 0,
        "rateIncome": 0,
        "rateIncome_TypePrice": 3,
    }
    s = parse_signal(raw)
    assert s.symbol == "APH"
    assert s.company_name == "An Phat Holdings"
    # DNSE prices are stored as ×1/1000 of VND; autofin API gives them in
    # thousands of VND already. We multiply by 1000 so the orders land in raw VND.
    assert s.supply_from == 5.91 * 1000
    assert s.supply_to == 6.1 * 1000
    assert s.tp_from == 6.5 * 1000
    assert s.tp_to == 7.0 * 1000
    assert s.sl_from == 5.32 * 1000
    assert s.sl_to == 5.61 * 1000
    assert s.rate_for_nav == 0.15
    assert s.is_waitting_buy is True
    assert s.is_waitting_sell is False
    assert s.market_type == 1
    assert s.last_recomment == "Khuyến nghị đầu tư"


def test_parse_signal_handles_missing_fields():
    raw = {"symbol": "VCB", "statusDeal": {}}
    s = parse_signal(raw)
    assert s.symbol == "VCB"
    assert s.supply_from == 0.0
    assert s.is_waitting_buy is False


def test_parse_signal_handles_alternate_keys():
    raw = {
        "symbol": "HPG",
        "statusDeal": {"market_TypePrice": 2},
    }
    s = parse_signal(raw)
    assert s.market_type == 2
