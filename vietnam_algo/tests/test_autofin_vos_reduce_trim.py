"""Tests for the 2026-10-30 VOS reduce incident.

The incident
------------
On 2026-10-30 the bot was holding 300 VOS shares (bought at 11,800 on
2026-10-29) and autofin had published a 50 % reduce recommendation::

    lastRecomment : "⚠️ Hạ tỷ trọng lần 2"
    hisRecomment[2]: "[VOS] ⚠️ Hạ 50 % tỉ trọng lần 2 Giá: 11,100
                      - Nắm giữ: 4 ngày Lỗ: -9.09 %"

The bot DID see the recommendation — the decision log showed
``blocked_non_entry=...VOS(reduce)`` — but it never trimmed a single
share. Every cycle reported ``partial=0``.

Two independent reasons, both fixed here:

1. **The SHORT headline is not parseable as an action.**
   ``_parse_partial_close("⚠️ Hạ tỷ trọng lần 2")`` returns
   ``unknown``: the text carries no percentage, no price and no
   "Hạ 50 % tỉ trọng" pattern. The partial-close path skipped the
   deal on the ``action.action == "unknown"`` guard.
   The FULL event text parses perfectly as ``reduce_lap`` lap 2,
   price 11,100, 50 %.

2. **The history was only fetched for HOLDING rows.**
   ``/signal-static-holding`` does not contain VOS (autofin dropped
   it from the active list), so the per-symbol ``/signal-deals``
   fetch never ran for VOS and the full event text was never even
   available.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.algos.autofin_signal import (
    _parse_partial_close,
    _plan_partial_close_orders,
)


SHORT = "⚠️ Hạ tỷ trọng lần 2"
FULL = ("[VOS] ⚠️ Hạ 50 % tỉ trọng lần 2 Giá: 11,100 - Nắm giữ: 4 ngày "
        "Lỗ: -9.09 %")


# -----------------------------------------------------------------------------
# The parser
# -----------------------------------------------------------------------------

def test_short_headline_is_not_actionable() -> None:
    """The short headline carries no percentage/price/pattern."""
    action = _parse_partial_close(SHORT)
    assert action.action == "unknown", (
        f"the short headline should not parse, got {action.action!r}"
    )


def test_full_event_text_is_actionable() -> None:
    """The full event text parses as a reduce step.

    ``percent`` is a PERCENTAGE (50.0), not a fraction — the fraction
    used for sizing comes from ``autofin_fractions`` (0.5).
    """
    action = _parse_partial_close(FULL)
    assert action.action == "reduce_lap"
    assert action.lap == 2
    assert action.price == 11100
    assert action.percent == pytest.approx(50.0)


# -----------------------------------------------------------------------------
# The partial-close planner
# -----------------------------------------------------------------------------

def _vos_state() -> dict:
    return {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": SHORT,
            "_our_qty": 0,          # stale tracking; positions is truth
            "_last_numberOfProfit": None,
            "_last_recomment": None,
            "supply_F": 11700,
            "supply_T": 12100,
        },
    }


def _vos_holding() -> list[dict]:
    """The synthetic holding row the orphan path injects for VOS."""
    return [{
        "_id": 1792627200,
        "symbol": "VOS",
        "numberOfProfit": 0,
        "lastRecomment": SHORT,
        "_orphan_injected": True,
    }]


def test_short_headline_alone_produces_no_trim() -> None:
    """Without the full event text there is nothing actionable.

    This is the pre-fix behaviour — the regression guard.
    """
    rows = _plan_partial_close_orders(
        _vos_state(), _vos_holding(),
        our_positions={"VOS": 300},
        autofin_fractions={"1792627200:VOS": 0.5},
    )
    assert rows == [], (
        "the short headline alone must not produce a trim "
        "(it carries no percentage or price)"
    )


def test_full_event_text_produces_the_50pct_trim() -> None:
    """With the full event text the trim is planned correctly.

    300 shares, 50 % → 150 shares. But HOSE trades in lots of 100, and
    150 is not a valid lot multiple — the partial-close path rounds
    the trim UP to the next valid lot (200), because rounding down
    would skip the order entirely (see ``_round_up_to_lots``).

    So the order is 200 shares at autofin's suggested 11,100, leaving
    100 shares — still protected by the 10,550 STOP.
    """
    state = _vos_state()
    rows = _plan_partial_close_orders(
        state, _vos_holding(),
        our_positions={"VOS": 300},
        autofin_fractions={"1792627200:VOS": 0.5},
        autofin_event_texts={"1792627200:VOS": FULL},
    )
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["symbol"] == "VOS"
    assert row["side"] == "NS"
    # 50 % of 300 = 150, rounded UP to the next 100-share lot = 200.
    assert row["quantity"] == 200, (
        f"the trim must be 150 rounded up to a valid lot (200), got "
        f"{row['quantity']}"
    )
    assert row["quantity"] <= 300, "must never sell more than we hold"
    assert row["price"] == 11100, (
        f"the trim must use autofin's suggested price, got {row['price']}"
    )
    assert "reduce_lap" in row["reason"]
    assert "lap2" in row["reason"]
    assert "pct50" in row["reason"]
    assert "pct5000" not in row["reason"], (
        "the reason must carry the PERCENTAGE (50), not the percentage "
        "multiplied by 100 (5000)"
    )


def test_trim_is_not_repeated_on_the_next_cycle() -> None:
    """After firing, the same event must not fire again.

    The dedup key is the text we ACTED ON, so the next cycle sees
    ``_last_recomment == FULL`` and skips.
    """
    state = _vos_state()
    holding = _vos_holding()
    kwargs = dict(
        our_positions={"VOS": 300},
        autofin_fractions={"1792627200:VOS": 0.5},
        autofin_event_texts={"1792627200:VOS": FULL},
    )
    first = _plan_partial_close_orders(state, holding, **kwargs)
    assert len(first) == 1
    # The state records the text we acted on.
    assert state["1792627200:VOS"]["_last_recomment"] == FULL

    # Second cycle, same event → no duplicate trim.
    second = _plan_partial_close_orders(state, holding, **kwargs)
    assert second == [], (
        "the same reduce event must not re-fire every cycle"
    )


def test_a_new_lap_still_fires() -> None:
    """A genuinely NEW step (lap 3) must still produce a trim."""
    state = _vos_state()
    state["1792627200:VOS"]["_last_recomment"] = FULL
    state["1792627200:VOS"]["_last_numberOfProfit"] = 0
    new_full = ("[VOS] ⚠️ Hạ 50 % tỉ trọng lần 3 Giá: 11,000 - Nắm giữ: 5 ngày "
                "Lỗ: -10.00 %")
    rows = _plan_partial_close_orders(
        state, _vos_holding(),
        our_positions={"VOS": 300},
        autofin_fractions={"1792627200:VOS": 0.5},
        autofin_event_texts={"1792627200:VOS": new_full},
    )
    assert len(rows) == 1, rows
    assert "lap3" in rows[0]["reason"]


# -----------------------------------------------------------------------------
# Re-arming an action that was planned but NOT submitted
# -----------------------------------------------------------------------------

def _adapter(state: dict):
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._our_deals = state
    a._notified_order_keys = set()
    a._notified_event_ids = set()
    a._actioned_deals = set()
    return a


def _vos_deal_actioned() -> dict:
    """The ``deals_actioned`` row the runner gets back from planning."""
    return {
        "action": "partial_close",
        "symbol": "VOS",
        "deal_id": "1792627200:VOS",
        "lastRecomment": SHORT,
        "dnse_orders": [{
            "symbol": "VOS", "side": "NS", "price": 11100.0,
            "quantity": 200, "kind": "partial_close",
            "reason": "autofin_partial:deal#1792627200:VOS:reduce_lap:"
                      "lap2:pct50:[VOS] ⚠️ Hạ 50 % tỉ trọng lần 2 ...",
        }],
    }


def test_rearm_when_the_partial_close_was_not_submitted() -> None:
    """A planned-but-not-submitted partial close must be re-armed.

    Without this the "already fired" marker swallows the trim forever
    (2026-10-30, the VOS reduce).
    """
    state = {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": SHORT,
            "_our_qty": 100,
            "_last_numberOfProfit": 0,
            "_last_recomment": FULL,
            "_last_partial_fraction": 0.5,
        },
    }
    adapter = _adapter(state)
    rearmed = adapter.rearm_unsubmitted_actions(
        [_vos_deal_actioned()], set(),   # nothing was submitted
    )
    assert rearmed == ["VOS"], rearmed
    sig = state["1792627200:VOS"]
    assert "_last_recomment" not in sig
    assert "_last_numberOfProfit" not in sig
    assert "_last_partial_fraction" not in sig


def test_rearm_does_nothing_when_the_order_was_submitted() -> None:
    """A partial close that reached DNSE keeps its fired marker."""
    state = {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": SHORT,
            "_our_qty": 100,
            "_last_numberOfProfit": 0,
            "_last_recomment": FULL,
            "_last_partial_fraction": 0.5,
        },
    }
    adapter = _adapter(state)
    rearmed = adapter.rearm_unsubmitted_actions(
        [_vos_deal_actioned()], {"VOS"},   # VOS was submitted
    )
    assert rearmed == [], rearmed
    sig = state["1792627200:VOS"]
    assert sig["_last_recomment"] == FULL
    assert sig["_last_numberOfProfit"] == 0
    assert sig["_last_partial_fraction"] == 0.5


def test_rearm_does_not_touch_a_planned_buy() -> None:
    """A planned BUY is re-planned by its own path — re-arming it could
    double-buy, so the re-arm must ignore it."""
    state = {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": SHORT,
            "_our_qty": 0,
        },
    }
    adapter = _adapter(state)
    buy_deal = {
        "action": "new_entry",
        "symbol": "VOS",
        "deal_id": "1792627200:VOS",
        "dnse_orders": [{
            "symbol": "VOS", "side": "NB", "price": 11700,
            "quantity": 300, "kind": "entry",
            "reason": "autofin_buy:deal#1792627200:VOS:nav15pct:...",
        }],
    }
    rearmed = adapter.rearm_unsubmitted_actions([buy_deal], set())
    assert rearmed == [], rearmed


# -----------------------------------------------------------------------------
# Lot rounding
# -----------------------------------------------------------------------------

def test_round_up_to_lots_keeps_exact_multiples() -> None:
    """An exact multiple of the lot must NOT be bumped up a whole lot.

    The old formula ``((q + lot - 1) // lot) * lot`` turned 100 into
    200 and 300 into 400 — doubling a 100-share trim.
    """
    from vietnam_algo.algos.autofin_signal import _round_up_to_lots

    assert _round_up_to_lots(100) == 100
    assert _round_up_to_lots(200) == 200
    assert _round_up_to_lots(300) == 300
    assert _round_up_to_lots(600) == 600
    # Non-multiples still round UP (rounding down would skip the
    # order entirely).
    assert _round_up_to_lots(1) == 100
    assert _round_up_to_lots(60) == 100
    assert _round_up_to_lots(150) == 200
    assert _round_up_to_lots(250) == 300
    assert _round_up_to_lots(0) == 0
