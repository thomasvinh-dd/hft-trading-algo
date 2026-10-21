"""Tests for the orphan-deal augmentation added to autofin_signal
in response to the 2026-10-20 BAF incident.

The /signal-static-holding endpoint sometimes drops deals that
autofin still publishes recomments for (e.g. when the operator
manually closes the deal in the autofin UI). Without the
``_augment_orphan_deals`` step the partial-close path silently
skipped those deals (``if did not in autofin_by_id: continue``)
and the bot reported ``0 orders planned``.

These tests exercise ``AutofinSignalAdapter._augment_orphan_deals``
with a stubbed autofin client to verify:

  1. A tracked deal missing from ``/signal-static-holding`` is
     augmented with the fresh ``lastRecomment`` and a synthetic
     holding entry tagged ``_orphan_injected``.
  2. Deals already in ``holding`` are NOT augmented (idempotency).
  3. ``our_deals`` entries with no live DNSE position are still
     augmented (the downstream ``_plan_partial_close_orders`` has
     the final guard; the orphan augment just provides the
     recomment text).
  4. When the fresh ``/signal-deals`` response has multiple deals
     for a symbol, the one whose ``_id`` matches our tracked
     ``did`` is selected (NOT just the most-recent-for-symbol
     fallback).
  5. When the autofin /signal-deals fetch fails, the helper
     swallows the exception and leaves ``holding`` unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter, _plan_partial_close_orders


def _make_adapter(our_deals: dict[str, dict[str, Any]]) -> AutofinSignalAdapter:
    """Bypass __init__ to avoid the env-cred load; we only need
    the data attributes the helper reads."""
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._our_deals = our_deals
    a._last_poll_at = None
    a._notified_event_ids = set()
    a._notified_order_keys = set()
    a._last_notification_id = ""
    a._last_notification_poll_at = None
    a._positions = None
    return a


def _stub_client(deals_by_symbol: dict[str, list[dict[str, Any]]]):
    """Return a MagicMock autofin client whose fetch_deals(symbol=...)
    returns the matching list from ``deals_by_symbol``.

    A symbol not in the dict raises a generic Exception (used to
    exercise the failure-handling branch).
    """

    def fetch_deals(*, symbol: str, page_size: int = 5, **_kw):
        if symbol not in deals_by_symbol:
            raise RuntimeError(f"simulated autofin 500 for {symbol}")
        # mirror the real client: page_size is honoured upstream,
        # but the stub returns everything.
        return list(deals_by_symbol[symbol])

    c = MagicMock()
    c.fetch_deals.side_effect = fetch_deals
    return c


def test_augment_injects_missing_deal_into_holding():
    """2026-10-20 BAF incident: our_tracked deal is gone from
    /signal-static-holding, but /signal-deals?symbol=BAF has the
    fresh '🛡️ Chốt lãi -1.75 %' recomment. The helper must
    inject the synthetic holding entry so the partial-close path
    sees the recomment."""
    our_deals = {
        "1785888000:BAF": {
            "_id": 1785888000,
            "symbol": "BAF",
            "_our_qty": 0,  # stale; live DNSE position is 200
            "lastRecomment": "stale old text",
        },
    }
    holding = [
        # No BAF here — autofin's /signal-static-holding dropped it.
        {"_id": 1786579200, "symbol": "TLG",
         "numberOfProfit": 1, "lastRecomment": "💰 Chốt lãi lần 1 mức giá 57,200"},
    ]
    client = _stub_client({
        "BAF": [{
            "_id": 1785888000,
            "symbol": "BAF",
            "numberOfProfit": 0,
            "lastRecomment": "🛡️ Chốt lãi -1.75 % bảo toàn vốn tại 30,950",
        }],
    })
    a = _make_adapter(our_deals)

    a._augment_orphan_deals(client, holding)

    # The helper must have appended one synthetic entry for BAF.
    baf_entries = [h for h in holding if h.get("symbol") == "BAF"]
    assert len(baf_entries) == 1, holding
    inj = baf_entries[0]
    assert inj["_orphan_injected"] is True
    assert inj["_id"] == 1785888000
    assert inj["lastRecomment"] == \
        "🛡️ Chốt lãi -1.75 % bảo toàn vốn tại 30,950"
    # And it must have repaired the cached recomment on our_deals
    # so next cycle's read sees the fresh text without a re-fetch.
    assert "🛡️ Chốt lãi -1.75 %" in our_deals["1785888000:BAF"]["lastRecomment"]


def test_augment_is_noop_when_all_deals_in_holding():
    """If every tracked deal is already in /signal-static-holding,
    no synthetic entries should be appended."""
    our_deals = {
        "1786579200:TLG": {"_id": 1786579200, "symbol": "TLG", "_our_qty": 100},
    }
    holding = [
        {"_id": 1786579200, "symbol": "TLG",
         "numberOfProfit": 1, "lastRecomment": "💰 Chốt lãi lần 1 mức giá 57,200"},
    ]
    client = _stub_client({})  # never called
    a = _make_adapter(our_deals)

    a._augment_orphan_deals(client, holding)

    assert len(holding) == 1  # unchanged
    client.fetch_deals.assert_not_called()


def test_augment_picks_matching_id_when_multiple_deals_for_symbol():
    """When /signal-deals returns several deals for the same
    symbol (different time windows), the helper must pick the one
    matching our tracked ``_id`` — NOT just ``deals[0]`` (which
    might be a different, older window)."""
    our_deals = {
        "1785888000:BAF": {"_id": 1785888000, "symbol": "BAF", "_our_qty": 0},
    }
    holding: list[dict[str, Any]] = []  # BAF missing entirely
    client = _stub_client({
        "BAF": [
            # An OLDER deal for BAF (window 2025-05) — must NOT match.
            {"_id": 1746403200, "symbol": "BAF", "numberOfProfit": 0,
             "lastRecomment": "[BAF] 🛡️ Chốt lãi -2.15 % bảo toàn vốn tại 28,399"},
            # The CURRENT deal for BAF (window 2026-08) — this one matches.
            {"_id": 1785888000, "symbol": "BAF", "numberOfProfit": 0,
             "lastRecomment": "[BAF] 🛡️ Chốt lãi -1.75 % bảo toàn vốn tại 30,950"},
        ],
    })
    a = _make_adapter(our_deals)

    a._augment_orphan_deals(client, holding)

    assert len(holding) == 1
    inj = holding[0]
    assert inj["_id"] == 1785888000  # matched our tracked id
    assert "1.75" in inj["lastRecomment"]


def test_augment_swallows_fetch_failures():
    """If autofin's /signal-deals call raises (e.g. transient
    network error after the retry budget is exhausted), the
    helper must NOT raise — it should log and move on so the
    rest of the cycle can proceed."""
    our_deals = {
        "1785888000:BAF": {"_id": 1785888000, "symbol": "BAF", "_our_qty": 0},
    }
    holding: list[dict[str, Any]] = []
    client = _stub_client({})  # stub raises for any symbol
    a = _make_adapter(our_deals)

    # Should NOT raise — augmentation is best-effort.
    a._augment_orphan_deals(client, holding)

    assert holding == []  # nothing injected


def test_orphan_fresh_recomment_fires_once_only():
    """2026-10-20 BAF incident: an orphan-deal augmentation on a
    fresh recomment must fire the partial-close on the FIRST cycle
    that sees it (not wait for the bootstrap-baseline path), and
    must NOT re-fire on subsequent cycles for the same recomment
    (the deal stays orphan in autofin's holding, so the
    augmentation fires every cycle — without a dedup the bot
    would spam SELL orders every 60 seconds). The dedup key is
    ``_last_recomment`` on the tracked deal."""
    our_deals = {
        "1785888000:BAF": {
            "_id": 1785888000,
            "symbol": "BAF",
            "_our_qty": 0,
            "lastRecomment": "stale old text",
            # No ``_last_numberOfProfit`` — first-cycle path.
        },
    }
    holding: list[dict[str, Any]] = []
    client = _stub_client({
        "BAF": [{
            "_id": 1785888000,
            "symbol": "BAF",
            "numberOfProfit": 0,
            "lastRecomment": "🛡️ Chốt lãi -1.75 % bảo toàn vốn tại 30,950",
        }],
    })
    a = _make_adapter(our_deals)

    # CYCLE 1: fresh orphan + fresh recomment → must fire.
    a._augment_orphan_deals(client, holding)
    rows1 = _plan_partial_close_orders(
        a._our_deals, holding, our_positions={"BAF": 200},
    )
    assert len(rows1) == 1
    assert rows1[0]["kind"] == "full_close"
    assert rows1[0]["quantity"] == 200
    assert rows1[0]["price"] == 30950

    # CYCLE 2: same orphan + same recomment → must NOT fire.
    holding2: list[dict[str, Any]] = []
    a._augment_orphan_deals(client, holding2)
    rows2 = _plan_partial_close_orders(
        a._our_deals, holding2, our_positions={"BAF": 200},
    )
    assert len(rows2) == 0

    # CYCLE 3 (sanity): still 0 — no spam.
    holding3: list[dict[str, Any]] = []
    a._augment_orphan_deals(client, holding3)
    rows3 = _plan_partial_close_orders(
        a._our_deals, holding3, our_positions={"BAF": 200},
    )
    assert len(rows3) == 0
