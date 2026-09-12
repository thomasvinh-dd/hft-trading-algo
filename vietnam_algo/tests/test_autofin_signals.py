"""Tests for autofin_signals parsing + signal-to-order logic."""
from __future__ import annotations

from vietnam_algo.libs.autofin_signals import AutofinSignal, parse_signal


def test_parse_signal_basic():
    raw = {
        "_id": 123,
        "symbol": "APH",
        "companyName": "An Phat Holdings",
        "statusDeal": {"market_TypePrice": 1},
        "supply_F": 5910,
        "supply_T": 6100,
        "tP_F": 6500,
        "tP_T": 7000,
        "sL_F": 5320,
        "sL_T": 5610,
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
    # Autofin API now returns RAW VND (e.g. supply_F=5910 means 5,910 VND).
    # We do not multiply by 1000 — the price is ready to send to DNSE.
    assert s.supply_from == 5910
    assert s.supply_to == 6100
    assert s.tp_from == 6500
    assert s.tp_to == 7000
    assert s.sl_from == 5320
    assert s.sl_to == 5610
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
