"""Regression test: an un-actionable top candidate must not eat the top-N slot.

2026-10-27 incident (autofin bot planned 0 orders at the 09:00 ICT poll):

The autofin suggest list ranked PET first (risk-reward). PET's target
size was ``NAV * rateForNav = 4,481,655 VND`` at a live ask of
56,700 VND → ``int(4_481_655 / 56_700) = 79`` shares → rounded down to
**0 board lots** (Vietnamese board lot = 100). The old code incremented
``considered`` BEFORE the viability gates, so PET consumed the single
``top_n=1`` slot and the loop then broke at the next candidate. The
result was "poll result: 0 orders planned" even though VOS and MSB were
both fully viable.

Every gate in that path is a bare ``continue`` with no log line, so the
failure was invisible in the bot log.

The fix moves the ``considered += 1`` increment to the point where a row
is actually appended — a candidate only consumes the top-N budget once
it has survived every gate.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/root/hft-trading-algo")

import vietnam_algo.algos.autofin_signal as mod
from vietnam_algo.algos.autofin_signal import (
    _plan_orders_for_new_positions,
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
            "isClosed": False,
            "market_Close": supply_to,
            "market_Highest": supply_to,
            "market_Lowest": supply_to,
            "market_Vol": 300,
            "market_TypePrice": 3,
            "price_Open": supply_from,
            "price_StopLoss": sl_from,
            "price_TrailingStop": -1,
            "rS_Rating_IDB": 3,
            "rateWinByPrice": 0.02,
            "riskVol": 0.5,
            "timeHolding": 1,
        },
        "companyName": symbol,
        "lastRecomment": last_recomment,
        "supply_F": supply_from,
        "supply_T": supply_to,
        "tP_F": tp_from,
        "tP_T": tp_from * 1.1,
        "sL_F": sl_from,
        "sL_T": sl_from,
        "rateForNav": rate_for_nav,
        "rateIncome": 0.03,
    }
    return parse_signal(raw)


def test_unactionable_top_candidate_does_not_eat_top_n_slot() -> None:
    """PET-style candidate (0 board lots) must not block the next viable one."""
    nav = 29_877_702.0
    cash = 19_437_666.0

    # PET: 15% of NAV at 56,700 → 79 shares → 0 board lots. In-zone.
    pet = _make_signal(
        "PET",
        rate_for_nav=0.15,
        supply_from=55_500.0,
        supply_to=56_700.0,
        tp_from=62_000.0,
        sl_from=51_000.0,
        deal_id=179_271_3600,
    )
    # VOS: fully viable — 379 shares → 300 lots.
    vos = _make_signal(
        "VOS",
        rate_for_nav=0.15,
        supply_from=11_700.0,
        supply_to=12_100.0,
        tp_from=12_800.0,
        sl_from=10_600.0,
        deal_id=179_262_7200,
    )

    prices = {"PET": 56_700.0, "VOS": 11_800.0}

    orders = _plan_orders_for_new_positions(
        [pet, vos],
        nav,
        {},
        current_prices=prices,
        already_planned_symbols=set(),
        available_cash_vnd=cash,
        new_entry_min_cash_pct=0.05,
        top_n=1,
    )

    assert orders, (
        "expected the second candidate to be planned — the first candidate "
        "rounds to 0 board lots and must not consume the top-N slot"
    )
    assert orders[0]["symbol"] == "VOS"
    assert orders[0]["side"] == "NB"
    assert orders[0]["quantity"] == 300


def test_top_n_still_limits_to_one_viable_entry() -> None:
    """The fix must not weaken the one-entry-per-day policy."""
    nav = 29_877_702.0
    cash = 19_437_666.0

    vos = _make_signal(
        "VOS",
        rate_for_nav=0.15,
        supply_from=11_700.0,
        supply_to=12_100.0,
        tp_from=12_800.0,
        sl_from=10_600.0,
        deal_id=179_262_7200,
    )
    msb = _make_signal(
        "MSB",
        rate_for_nav=0.30,
        supply_from=15_100.0,
        supply_to=15_400.0,
        tp_from=17_000.0,
        sl_from=13_800.0,
        deal_id=179_262_7201,
    )

    orders = _plan_orders_for_new_positions(
        [vos, msb],
        nav,
        {},
        current_prices={"VOS": 11_800.0, "MSB": 15_550.0},
        already_planned_symbols=set(),
        available_cash_vnd=cash,
        new_entry_min_cash_pct=0.05,
        top_n=1,
    )

    buys = [o for o in orders if o.get("side") == "NB"]
    assert len(buys) == 1, f"top_n=1 must still yield exactly one BUY, got {buys}"
    assert buys[0]["symbol"] == "VOS"


def test_pet_style_candidate_alone_still_plans_nothing() -> None:
    """With only an un-actionable candidate, the plan is legitimately empty."""
    nav = 29_877_702.0
    cash = 19_437_666.0

    pet = _make_signal(
        "PET",
        rate_for_nav=0.15,
        supply_from=55_500.0,
        supply_to=56_700.0,
        tp_from=62_000.0,
        sl_from=51_000.0,
        deal_id=179_271_3600,
    )

    orders = _plan_orders_for_new_positions(
        [pet],
        nav,
        {},
        current_prices={"PET": 56_700.0},
        already_planned_symbols=set(),
        available_cash_vnd=cash,
        new_entry_min_cash_pct=0.05,
        top_n=1,
    )
    assert orders == []
