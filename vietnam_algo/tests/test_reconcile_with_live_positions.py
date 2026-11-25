"""Tests for reconciling the tracked state with the LIVE positions.

2026-11-06 (the operator's request)::

    "the above issue remind me a issue, previously we entry BAF and
     ORS but the system got error when handling sell order so i do it
     manually, that action is not recorded in our ledger so may cause
     the issue, i wanna our system to automatically remove the stale
     order in our ledger in the case that we do not have the stock
     left in our portfolio"

A MANUAL sell (done in the DNSE app because the bot errored) never
reaches our ledger, so the ledger keeps claiming a position we no
longer hold. The stale claim then lands in
``already_planned_symbols`` and silently blocks a legitimate new
entry — which is exactly what happened to BAF and ORS on 2026-11-05
(three in-band candidates, ZERO orders).

The live DNSE position map is the truth, so the tracked state is
reconciled against it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter


def _adapter(our_deals: dict) -> AutofinSignalAdapter:
    a = AutofinSignalAdapter()
    a._our_deals = {
        k: {"symbol": v.get("symbol"), "_our_qty": v.get("_our_qty", 0)}
        for k, v in our_deals.items()
    }
    return a


def test_a_manually_sold_position_is_pruned() -> None:
    """We sold BAF by hand — the ledger never saw it."""
    a = _adapter({
        "1785888000:BAF": {"symbol": "BAF", "_our_qty": 200},
        "1792713600:ORS": {"symbol": "ORS", "_our_qty": 200},
        "1792627200:VOS": {"symbol": "VOS", "_our_qty": 300},
    })

    # We only hold VOS now — BAF and ORS were sold manually.
    pruned = a._reconcile_with_live_positions({"VOS": 300})

    assert a._our_deals["1785888000:BAF"]["_our_qty"] == 0
    assert a._our_deals["1792713600:ORS"]["_our_qty"] == 0
    assert a._our_deals["1792627200:VOS"]["_our_qty"] == 300

    pruned_syms = {p["symbol"] for p in pruned}
    assert pruned_syms == {"BAF", "ORS"}


def test_a_partially_sold_position_is_reconciled_down() -> None:
    """We hold fewer than we claimed — reconcile to the live count."""
    a = _adapter({
        "1792627200:VOS": {"symbol": "VOS", "_our_qty": 300},
    })

    a._reconcile_with_live_positions({"VOS": 100})

    assert a._our_deals["1792627200:VOS"]["_our_qty"] == 100


def test_a_position_we_still_hold_is_untouched() -> None:
    a = _adapter({
        "1789344000:ABB": {"symbol": "ABB", "_our_qty": 100},
    })

    pruned = a._reconcile_with_live_positions({"ABB": 100})

    assert a._our_deals["1789344000:ABB"]["_our_qty"] == 100
    assert pruned == []


def test_an_empty_live_map_prunes_nothing() -> None:
    """SAFETY: an empty map is ambiguous.

    It could mean "we hold nothing" OR "the portfolio fetch failed".
    Wiping the tracked state on a transient API failure would make
    the bot re-enter every position it owns — so an empty map means
    "no information" and nothing is pruned.
    """
    a = _adapter({
        "1785888000:BAF": {"symbol": "BAF", "_our_qty": 200},
        "1789344000:ABB": {"symbol": "ABB", "_our_qty": 100},
    })

    pruned = a._reconcile_with_live_positions({})

    assert pruned == []
    assert a._our_deals["1785888000:BAF"]["_our_qty"] == 200
    assert a._our_deals["1789344000:ABB"]["_our_qty"] == 100


def test_a_symbol_missing_from_the_live_map_is_pruned() -> None:
    """Not in the map at all == we hold none of it."""
    a = _adapter({
        "1785888000:BAF": {"symbol": "BAF", "_our_qty": 200},
        "1789344000:ABB": {"symbol": "ABB", "_our_qty": 100},
    })

    # Only ABB is in the live map.
    a._reconcile_with_live_positions({"ABB": 100})

    assert a._our_deals["1785888000:BAF"]["_our_qty"] == 0
    assert a._our_deals["1789344000:ABB"]["_our_qty"] == 100


def test_deals_we_do_not_hold_are_left_alone() -> None:
    """A deal with ``_our_qty = 0`` is not affected."""
    a = _adapter({
        "1792627200:TCB": {"symbol": "TCB", "_our_qty": 0},
    })

    pruned = a._reconcile_with_live_positions({"ABB": 100})

    assert pruned == []
    assert a._our_deals["1792627200:TCB"]["_our_qty"] == 0


def test_the_pruned_deal_is_released_from_actioned() -> None:
    """A fully-sold deal must become re-actionable.

    Otherwise the "do not re-enter a deal we already closed" guard
    keeps it out of the entry pool.
    """
    a = _adapter({
        "1785888000:BAF": {"symbol": "BAF", "_our_qty": 200},
    })
    a._actioned_deals = {"1785888000:BAF"}

    pruned = a._reconcile_with_live_positions({"ABB": 100})
    for p in pruned:
        if int(p.get("live_qty", 0) or 0) <= 0:
            a._actioned_deals.discard(p["deal_id"])

    assert "1785888000:BAF" not in a._actioned_deals
