"""Regression test: the top-N entry slot must not be consumed by a
signal that then FAILS a downstream gate.

2026-10-28 incident: on 2026-10-27 the autofin bot produced ZERO new
entries even though 13 tracked buy-recommendation deals were inside
their buy zone and the account had 19.4M VND cash (NAV 29.9M, so the
5% cash gate passed with room to spare).

The cause is in ``_plan_orders_for_new_positions``:

    for s in selected:
        if considered >= top_n:
            break
        ...
        considered += 1          # <-- consumed HERE
        rec_kind = classify_recomment(s.last_recomment)
        if rec_kind != RECOMMENT_KIND_ENTRY:
            continue              # <-- but rejected HERE
        ...
        if cur > s_hi * (1.0 + above_zone_tolerance_pct):
            continue              # <-- or rejected HERE (price zone)
        ...
        if target_qty <= 0:
            continue              # <-- or rejected HERE (cash/lot size)
        rows.append({...})        # <-- only NOW is a row produced

``considered`` is incremented BEFORE any of the gates that can reject
the candidate. With ``top_n == 1`` (the default, and the value in
effect on 2026-10-27) the FIRST candidate burns the only slot, so if
it is then rejected the loop breaks on the next iteration and the
whole day produces no entry — the in-zone, affordable candidates
lower in the list are never even examined.

The fix: count a candidate toward the top-N budget only when it
actually produces an order. The gate checks still run in the same
order, so the "considered" count now means "entries made", which is
what the user's policy ("act on the top N suggestions") means.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.algos.autofin_signal import (  # noqa: E402
    _plan_orders_for_new_positions,
)
from vietnam_algo.libs.autofin_signals import parse_signal  # noqa: E402

ENTRY_REC = "Khuyến nghị đầu tư"


def _Sig(
    symbol: str,
    rec: str = ENTRY_REC,
    rate: float = 0.30,
    lo: float = 10_000.0,
    hi: float = 11_000.0,
    price: float = 10_500.0,
):
    """Build a real ``AutofinSignal`` from a raw autofin payload."""
    return parse_signal({
        "_id": 1_700_000_000 + (abs(hash(symbol)) % 1_000_000),
        "symbol": symbol,
        "companyName": symbol,
        "lastRecomment": rec,
        "supply_F": lo,
        "supply_T": hi,
        "tP_F": 0,
        "tP_T": 0,
        "sL_F": 0,
        "sL_T": 0,
        "rateForNav": rate,
        "isWaittingBuy": True,
        "isWaittingSell": False,
        "market_TypePrice": 1,
        "price": price,
    })


def _plan(selected, prices, top_n=1, cash=20_000_000.0):
    return _plan_orders_for_new_positions(
        selected=selected,
        nav_vnd=30_000_000.0,
        our_positions={},
        current_prices=prices,
        available_cash_vnd=cash,
        top_n=top_n,
        above_zone_tolerance_pct=0.03,
        below_zone_tolerance_pct=0.03,
    )


def test_zone_gate_failure_does_not_consume_the_top_slot() -> None:
    """A top candidate that is OUT OF ZONE must not block a later,
    in-zone candidate.

    This is the 2026-10-27 failure: the top-1 signal failed the zone
    gate, and because ``considered`` was incremented before the gate,
    the in-zone candidates below it were never considered.
    """
    out_of_zone = _Sig("AAA", lo=10_000, hi=11_000, price=10_500)
    good = _Sig("BBB", lo=10_000, hi=11_000, price=10_500)

    rows = _plan(
        [out_of_zone, good],
        {"AAA": 20_000.0, "BBB": 10_500.0},   # AAA is way above its band
        top_n=1,
    )

    assert len(rows) == 1, (
        f"expected the in-zone candidate to be entered, got {rows}. "
        f"The top-N slot was consumed by a candidate that then failed "
        f"the price-zone gate."
    )
    assert rows[0]["symbol"] == "BBB"


def test_non_entry_recommendation_does_not_consume_the_top_slot() -> None:
    """A TP/reduce recommend must not burn the slot either."""
    tp = _Sig("AAA", rec="💰 Chốt lãi lần 1 mức giá 11,000", price=10_500)
    good = _Sig("BBB", price=10_500)

    rows = _plan([tp, good], {"AAA": 10_500.0, "BBB": 10_500.0}, top_n=1)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BBB"


def test_top_n_still_limits_the_number_of_entries() -> None:
    """The top-N policy must still cap the number of new entries."""
    sigs = [_Sig(f"SYM{i}", price=10_500) for i in range(5)]
    prices = {f"SYM{i}": 10_500.0 for i in range(5)}

    rows = _plan(sigs, prices, top_n=2)

    assert len(rows) == 2, rows


def test_top_n_zero_disables_entries() -> None:
    """``top_n=0`` must still produce nothing (the SOD gate uses this)."""
    sigs = [_Sig("BBB", price=10_500)]
    rows = _plan(sigs, {"BBB": 10_500.0}, top_n=0)
    assert rows == []


def test_all_candidates_failing_produces_nothing() -> None:
    """If every candidate fails a gate we must still produce no orders."""
    sigs = [
        _Sig("AAA", lo=10_000, hi=11_000, price=10_500),
        _Sig("BBB", lo=10_000, hi=11_000, price=10_500),
    ]
    prices = {"AAA": 20_000.0, "BBB": 22_000.0}   # both above band

    rows = _plan(sigs, prices, top_n=1)

    assert rows == []


def test_already_held_symbol_does_not_consume_the_top_slot() -> None:
    """A symbol we already hold must not block a fresh candidate."""
    held = _Sig("AAA", price=10_500)
    good = _Sig("BBB", price=10_500)

    rows = _plan_orders_for_new_positions(
        selected=[held, good],
        nav_vnd=30_000_000.0,
        our_positions={"AAA": 100},
        current_prices={"AAA": 10_500.0, "BBB": 10_500.0},
        available_cash_vnd=20_000_000.0,
        top_n=1,
        above_zone_tolerance_pct=0.03,
        below_zone_tolerance_pct=0.03,
    )

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BBB"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
