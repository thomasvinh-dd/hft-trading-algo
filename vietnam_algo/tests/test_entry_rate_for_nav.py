"""Tests for the ``rateForNav`` reported on entry notifications.

2026-11-07. The operator reported::

    🤖 autofin_signal: new entry
    📅 2026-11-06 04:10
    • VLB  [deal#1792627200:VLB, rateForNav=0%]
      signal: Khuyến nghị đầu tư
      → DNSE: BUY 100 VLB @ 52,200 VND

The real rate was 15 %.

Why it was 0
-------------
``_build_deals_actioned`` read the rate ONLY from autofin's HOLDING
row — and autofin's holding rows carry ``rateForNav: null``. The
fallback that parsed the rate from the order reason
(``autofin_buy:...:nav15pct:...``) was gated behind ``if not rec:``,
so when the holding row DID have a recomment (the normal case) the
fallback never ran.

The SUGGEST row is where autofin actually publishes the rate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.algos.autofin_signal import (
    _build_deals_actioned,
    _format_per_action_message,
    _suggest_rows_by_deal_id,
)


def _entry_order(reason: str = "autofin_buy:deal#1792627200:VLB:nav15pct:Khuyến nghị đầu tư"):
    return [{
        "symbol": "VLB", "side": "NB", "price": 52200, "quantity": 100,
        "kind": "entry", "reason": reason,
    }]


def _holding_row(rate=None, rec="Khuyến nghị đầu tư") -> dict:
    return {
        "_id": 1792627200, "symbol": "VLB",
        "lastRecomment": rec,
        "rateForNav": rate,
        "numberOfProfit": 0,
        "supply_F": 50900, "supply_T": 51900,
    }


def _suggest_row(rate=0.15, symbol="VLB", _id=1792627200) -> dict:
    return {
        "_id": _id, "symbol": symbol,
        "lastRecomment": "Khuyến nghị đầu tư",
        "rateForNav": rate,
        "supply_F": 50900, "supply_T": 51900,
    }


class _FakeSignal:
    """Minimal stand-in for ``AutofinSignal``."""

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.symbol = raw.get("symbol", "")
        self.last_recomment = raw.get("lastRecomment", "")
        self.rate_for_nav = raw.get("rateForNav") or 0.0


# -----------------------------------------------------------------------------
# the core bug
# -----------------------------------------------------------------------------

def test_the_rate_comes_from_the_suggest_row_when_the_holding_row_is_null() -> None:
    """THE reported case: holding has ``rateForNav: null`` AND a recomment.

    Before the fix the rate stayed 0 because the reason-parsing
    fallback was gated behind ``if not rec:`` and ``rec`` was set.
    """
    deals = _build_deals_actioned(
        new_orders=_entry_order(),
        partial_orders=[],
        close_orders=[],
        autofin_holding_by_id={"1792627200:VLB": _holding_row(rate=None)},
        autofin_suggest_by_id={"1792627200:VLB": _suggest_row(rate=0.15)},
    )
    assert len(deals) == 1
    assert deals[0]["rateForNav"] == 0.15
    assert deals[0]["lastRecomment"] == "Khuyến nghị đầu tư"


def test_the_rate_falls_back_to_the_order_reason() -> None:
    """No suggest row -> parse ``nav15pct`` from the reason."""
    deals = _build_deals_actioned(
        new_orders=_entry_order(),
        partial_orders=[],
        close_orders=[],
        autofin_holding_by_id={"1792627200:VLB": _holding_row(rate=None)},
        autofin_suggest_by_id={},
    )
    assert deals[0]["rateForNav"] == 0.15


def test_the_rate_falls_back_to_the_order_reason_even_with_a_recomment() -> None:
    """The reason fallback must NOT depend on the recomment being empty."""
    deals = _build_deals_actioned(
        new_orders=_entry_order(),
        partial_orders=[],
        close_orders=[],
        autofin_holding_by_id={"1792627200:VLB": _holding_row(rate=None)},
        autofin_suggest_by_id={},
    )
    assert deals[0]["rateForNav"] == 0.15


def test_the_holding_rate_is_used_when_present() -> None:
    """A holding row WITH a rate wins over the fallback."""
    deals = _build_deals_actioned(
        new_orders=_entry_order(),
        partial_orders=[],
        close_orders=[],
        autofin_holding_by_id={"1792627200:VLB": _holding_row(rate=0.30)},
        autofin_suggest_by_id={"1792627200:VLB": _suggest_row(rate=0.15)},
    )
    assert deals[0]["rateForNav"] == 0.30


# -----------------------------------------------------------------------------
# the suggest index
# -----------------------------------------------------------------------------

def test_the_suggest_rows_are_indexed_by_deal_id() -> None:
    sigs = [_FakeSignal(_suggest_row(rate=0.15, symbol="VLB")),
            _FakeSignal(_suggest_row(rate=0.30, symbol="OCB", _id=1793750400))]
    idx = _suggest_rows_by_deal_id(sigs)
    assert idx["1792627200:VLB"]["rateForNav"] == 0.15
    assert idx["1793750400:OCB"]["rateForNav"] == 0.30


def test_the_suggest_index_tolerates_a_missing_raw() -> None:
    idx = _suggest_rows_by_deal_id([object()])
    assert idx == {}


# -----------------------------------------------------------------------------
# the message
# -----------------------------------------------------------------------------

def test_the_message_shows_the_correct_rate() -> None:
    """The user's exact complaint — the message must show 15 %, not 0 %."""
    deals = _build_deals_actioned(
        new_orders=_entry_order(),
        partial_orders=[],
        close_orders=[],
        autofin_holding_by_id={"1792627200:VLB": _holding_row(rate=None)},
        autofin_suggest_by_id={"1792627200:VLB": _suggest_row(rate=0.15)},
    )
    msg = _format_per_action_message(deals[0], "2026-11-06 04:10")
    assert "rateForNav=15%" in msg, msg
    assert "rateForNav=0%" not in msg, msg


def test_a_thirty_percent_rate_is_shown() -> None:
    deals = _build_deals_actioned(
        new_orders=[{
            "symbol": "OCB", "side": "NB", "price": 10700, "quantity": 700,
            "kind": "entry",
            "reason": "autofin_buy:deal#1793750400:OCB:nav30pct:Khuyến nghị đầu tư",
        }],
        partial_orders=[],
        close_orders=[],
        autofin_holding_by_id={},
        autofin_suggest_by_id={},
    )
    msg = _format_per_action_message(deals[0], "2026-11-06 09:36")
    assert "rateForNav=30%" in msg, msg
