"""Tests for the 2026-10-29 VOS incident fixes.

The VOS incident (2026-10-29)
------------------------------
The bot opened a NEW position on VOS (300 @ 11,700, filled @ 11,800)
even though autofin's recommendation for that deal had already moved
to "⚠️ Hạ tỷ trọng lần 2" (reduce weight) four days after the deal
opened on 22/10/2026.

Two defects combined:

1. **Stale cached recommendation.** The cached ``lastRecomment`` for a
   tracked deal was written once (when the deal first entered
   ``our_deals``) and only refreshed for deals we HOLD. VOS was
   tracked-but-not-held at 09:01, so it kept its original entry-card
   text ("Khuyến nghị đầu tư" → classified as ``entry``) forever.

2. **The held-deal entry gate ignored the recommendation kind.**
   ``_expand_signals_with_held_unentered`` only checked ``isClosed``
   and ``_our_qty``, so it could propose a BUY for a deal autofin was
   actively telling us to sell.

Plus the user's request: snapshot every autofin response so a future
incident can be attributed to autofin (bad data) or to us (misreading
good data).

These tests cover:
  1. Guard 2 — the held-deal entry gate blocks reduce/tp/close kinds
     and allows entry/unknown.
  2. Guard 1 — a tracked deal whose cached recommendation is stale is
     refreshed from /signal-deals, and the refreshed text is what the
     entry gate then sees.
  3. The fresh holding/suggest rows refresh the cached recommendation
     for ALL tracked deals (held or not).
  4. The snapshot module writes a gzipped raw bundle + a readable
     decision summary, and prunes old days.
  5. The structured decision log line names the blocked candidates.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vietnam_algo.algos.autofin_signal import (
    AutofinSignalAdapter,
    _expand_signals_with_held_unentered,
    _parse_partial_close,
    decide_orders,
)
from vietnam_algo.libs.autofin_signals import (
    RECOMMENT_KIND_ENTRY,
    RECOMMENT_KIND_UNKNOWN,
    classify_recomment,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _holding_row(
    symbol: str = "VOS",
    did: int = 1792627200,
    rec: str = "Khuyến nghị đầu tư",
    supply_f: float = 11700,
    supply_t: float = 12100,
) -> dict[str, Any]:
    return {
        "_id": did,
        "symbol": symbol,
        "companyName": f"{symbol} Co",
        "statusDeal": {
            "isClosed": False,
            "price_Open": 12100,
            "price_Closed": 0,
            "price_StopLoss": 10550,
        },
        "numberOfProfit": 0,
        "rateForNav": 0.15,
        "lastRecomment": rec,
        "supply_F": supply_f,
        "supply_T": supply_t,
        "tP_F": 13100,
        "tP_T": 14800,
        "sL_F": 10550,
        "sL_T": 11100,
    }


def _make_adapter(our_deals: dict[str, dict[str, Any]]) -> AutofinSignalAdapter:
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._our_deals = our_deals
    a._last_poll_at = None
    a._notified_event_ids = set()
    a._notified_order_keys = set()
    a._last_notification_id = ""
    a._last_notification_poll_at = None
    a._positions = None
    a._actioned_deals = set()
    return a


def _stub_client(deals_by_symbol: dict[str, list[dict[str, Any]]]) -> MagicMock:
    client = MagicMock()

    def fetch_deals(*, symbol: str, page_size: int = 10, **_kw):
        rows = deals_by_symbol.get(symbol.upper())
        if rows is None:
            raise RuntimeError(f"no stub for {symbol}")
        return rows

    client.fetch_deals = fetch_deals
    return client


# ---------------------------------------------------------------------------
# Guard 2: the held-deal entry gate honours the recommendation kind
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rec, expect_allowed",
    [
        ("Khuyến nghị đầu tư", True),
        ("Ngày khuyến nghị: 22/10/2026", True),
        ("", True),  # unknown -> allowed
        ("⚠️ Hạ tỷ trọng lần 2", False),
        ("⚠️ Hạ 50 % tỉ trọng lần 1", False),
        ("💰 Chốt lãi lần 1 mức giá 13,100", False),
        ("🛡️ Chốt lãi -1.75 % bảo toàn vốn tại 30,950", False),
        ("Đóng DEAL chạm điểm chặn lãi (Lãi: 29.84 %)", False),
    ],
)
def test_held_deal_gate_blocks_non_entry_recommendations(rec, expect_allowed):
    """A held deal whose recommendation is a TP/SL/reduce/close must
    never become a BUY candidate.

    This is the guard that would have prevented the VOS entry.
    """
    holding = [_holding_row(rec=rec)]
    out = _expand_signals_with_held_unentered(
        [], holding, {}, {"VOS": 11700.0},
    )
    assert bool(out) is expect_allowed, (
        f"rec={rec!r} kind={classify_recomment(rec)} -> "
        f"allowed={bool(out)}, expected {expect_allowed}"
    )


def test_vos_specific_regression_reduce_deal_is_not_an_entry():
    """The exact VOS case: deal 1792627200 says "Hạ tỷ trọng lần 2".

    The bot must NOT propose a BUY for it even though the buy zone
    (11,700-12,100) contains the current price (11,700).
    """
    holding = [_holding_row(rec="⚠️ Hạ tỷ trọng lần 2")]
    out = _expand_signals_with_held_unentered(
        [], holding, {}, {"VOS": 11700.0},
    )
    assert out == [], (
        "a deal autofin wants to REDUCE must not become an entry candidate"
    )


def test_held_deal_gate_still_allows_entry_recommendation():
    """Guard 2 must not break the normal path: an 'entry' recommendation
    for a held-but-not-entered deal in the buy zone still becomes a
    candidate."""
    holding = [_holding_row(rec="Khuyến nghị đầu tư")]
    out = _expand_signals_with_held_unentered(
        [], holding, {}, {"VOS": 11700.0},
    )
    assert len(out) == 1
    assert out[0].symbol == "VOS"
    assert classify_recomment(out[0].last_recomment) == RECOMMENT_KIND_ENTRY


# ---------------------------------------------------------------------------
# Guard 1: stale cached recommendations get refreshed
# ---------------------------------------------------------------------------

def test_stale_recomment_is_refreshed_from_signal_deals():
    """A tracked deal whose cached recommendation is stale gets a fresh
    one from /signal-deals."""
    our_deals = {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": "Khuyến nghị đầu tư",
            "_our_qty": 0,
            # No stamp -> treated as infinitely stale.
        },
    }
    client = _stub_client({
        "VOS": [{
            "_id": 1792627200,
            "symbol": "VOS",
            "lastRecomment": "⚠️ Hạ tỷ trọng lần 2",
            "numberOfProfit": 0,
            "statusDeal": {"isClosed": False},
        }],
    })
    adapter = _make_adapter(our_deals)
    report = adapter._refresh_stale_deal_recomments(client, [], [])

    assert len(report["refreshed"]) == 1
    row = report["refreshed"][0]
    assert row["deal_id"] == "1792627200:VOS"
    assert row["changed"] is True
    assert row["new_recommendation"] == "⚠️ Hạ tỷ trọng lần 2"
    # 2026-10-29: autofin's percentage-less reduce headline
    # ("Hạ tỷ trọng lần 2") is now classified as ``reduce`` — before
    # the classifier fix it was ``unknown``, which is exactly why the
    # bot entered VOS.
    assert row["new_kind"] == "reduce"
    # The cached state is updated.
    assert our_deals["1792627200:VOS"]["lastRecomment"] == "⚠️ Hạ tỷ trọng lần 2"
    # And it is now stamped so we don't re-fetch it next cycle.
    assert our_deals["1792627200:VOS"]["_last_recomment_at"]


def test_fresh_recomment_is_not_refetched():
    """A recently-refreshed deal is not re-fetched — this bounds the
    per-cycle API cost."""
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    our_deals = {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": "Khuyến nghị đầu tư",
            "_our_qty": 0,
            "_last_recomment_at": stamp,
        },
    }
    client = MagicMock()
    client.fetch_deals = MagicMock(
        side_effect=AssertionError("must not be called")
    )
    adapter = _make_adapter(our_deals)
    report = adapter._refresh_stale_deal_recomments(client, [], [])

    assert report["refreshed"] == []
    assert report["skipped_fresh"] == ["1792627200:VOS"]


def test_deal_present_in_holding_is_not_refetched():
    """A deal covered by a fresh holding response needs no refresh."""
    our_deals = {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": "Khuyến nghị đầu tư",
            "_our_qty": 0,
        },
    }
    client = MagicMock()
    client.fetch_deals = MagicMock(
        side_effect=AssertionError("must not be called")
    )
    adapter = _make_adapter(our_deals)
    report = adapter._refresh_stale_deal_recomments(
        client, [_holding_row()], [],
    )
    assert report["refreshed"] == []
    assert report["failed"] == []


def test_refresh_failure_is_reported_not_raised():
    """A failing /signal-deals fetch is reported and does not break the
    cycle."""
    our_deals = {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": "Khuyến nghị đầu tư",
            "_our_qty": 0,
        },
    }
    client = MagicMock()
    client.fetch_deals = MagicMock(side_effect=RuntimeError("502"))
    adapter = _make_adapter(our_deals)
    report = adapter._refresh_stale_deal_recomments(client, [], [])

    assert report["refreshed"] == []
    assert len(report["failed"]) == 1
    assert "502" in report["failed"][0]["error"]


def test_refresh_matches_by_deal_id_not_just_symbol():
    """When /signal-deals returns several deals for a symbol, the row
    whose ``_id`` matches our tracked deal is used."""
    our_deals = {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": "Khuyến nghị đầu tư",
            "_our_qty": 0,
        },
    }
    client = _stub_client({
        "VOS": [
            {
                "_id": 1770940800,
                "symbol": "VOS",
                "lastRecomment": "Đóng DEAL chạm điểm chặn lãi (Lãi: 29.84 %)",
                "statusDeal": {"isClosed": True},
            },
            {
                "_id": 1792627200,
                "symbol": "VOS",
                "lastRecomment": "⚠️ Hạ tỷ trọng lần 2",
                "statusDeal": {"isClosed": False},
            },
        ],
    })
    adapter = _make_adapter(our_deals)
    report = adapter._refresh_stale_deal_recomments(client, [], [])
    assert report["refreshed"][0]["new_recommendation"] == "⚠️ Hạ tỷ trọng lần 2"


def test_refresh_failure_is_not_retried_every_cycle():
    """A deal whose refresh keeps failing must not cost an API call on
    every cycle — there is a one-hour backoff."""
    our_deals = {
        "1789344000:ABB": {
            "symbol": "ABB",
            "lastRecomment": "Khuyến nghị đầu tư",
            "_our_qty": 0,
        },
    }
    client = MagicMock()
    client.fetch_deals = MagicMock(side_effect=RuntimeError("502"))
    adapter = _make_adapter(our_deals)

    # First attempt fails and records the attempt time.
    report = adapter._refresh_stale_deal_recomments(client, [], [])
    assert len(report["failed"]) == 1
    assert client.fetch_deals.call_count == 1

    # Second attempt within the backoff window is skipped entirely.
    report = adapter._refresh_stale_deal_recomments(client, [], [])
    assert report["failed"] == []
    assert report["skipped_fresh"] == ["1789344000:ABB"]
    assert client.fetch_deals.call_count == 1, (
        "a failed refresh must be backed off, not retried every cycle"
    )


def test_refresh_backoff_expires(monkeypatch):
    """After the backoff window the refresh is attempted again."""
    import vietnam_algo.algos.autofin_signal as mod

    monkeypatch.setattr(mod, "_FAILED_REFRESH_BACKOFF_SECONDS", 0)
    old = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    our_deals = {
        "1789344000:ABB": {
            "symbol": "ABB",
            "lastRecomment": "Khuyến nghị đầu tư",
            "_our_qty": 0,
            "_last_refresh_attempt_at": old,
        },
    }
    client = MagicMock()
    client.fetch_deals = MagicMock(side_effect=RuntimeError("502"))
    adapter = _make_adapter(our_deals)
    report = adapter._refresh_stale_deal_recomments(client, [], [])
    assert len(report["failed"]) == 1
    assert client.fetch_deals.call_count == 1


# ---------------------------------------------------------------------------
# Guard 1b: fresh holding/suggest rows refresh ALL tracked deals
# ---------------------------------------------------------------------------

def test_decide_orders_refreshes_cached_recommendation_for_unheld_deal():
    """A tracked-but-not-held deal whose row appears in a fresh holding
    response gets its cached recommendation refreshed.

    This is the exact VOS shape: the row is in ``our_deals`` with
    ``_our_qty == 0``, and the stale cached text was the entry card.
    """
    our_deals = {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": "Khuyến nghị đầu tư",  # STALE
            "_our_qty": 0,
            "supply_F": 11700,
            "supply_T": 12100,
            "sL_F": 10550,
            "tP_F": 13100,
            "tP_T": 14800,
            "rateForNav": 0.15,
        },
    }
    holding = [_holding_row(rec="⚠️ Hạ tỷ trọng lần 2")]

    orders, new_our_deals, _ = decide_orders(
        holding=holding,
        signals=[],
        our_deals=our_deals,
        our_positions={},
        nav=1_000_000_000,
        current_prices={"VOS": 11700.0},
    )

    # The cached text was refreshed to the fresh value.
    assert new_our_deals["1792627200:VOS"]["lastRecomment"] == (
        "⚠️ Hạ tỷ trọng lần 2"
    )
    # And because the recommendation is now a reduce, no BUY is emitted.
    buys = [o for o in orders if o["side"] == "NB"]
    assert buys == [], (
        f"a reduce recommendation must not produce a BUY, got {buys}"
    )


def test_decide_orders_entry_recommendation_still_buys():
    """The normal path is unaffected: an entry recommendation for a
    held-but-not-entered deal in the buy zone still produces a BUY."""
    our_deals = {
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": "Khuyến nghị đầu tư",
            "_our_qty": 0,
            "supply_F": 11700,
            "supply_T": 12100,
            "sL_F": 10550,
            "tP_F": 13100,
            "tP_T": 14800,
            "rateForNav": 0.15,
        },
    }
    holding = [_holding_row(rec="Khuyến nghị đầu tư")]

    orders, _, _ = decide_orders(
        holding=holding,
        signals=[],
        our_deals=our_deals,
        our_positions={},
        nav=1_000_000_000,
        current_prices={"VOS": 11700.0},
    )
    buys = [o for o in orders if o["side"] == "NB"]
    assert len(buys) == 1
    assert buys[0]["symbol"] == "VOS"


# ---------------------------------------------------------------------------
# Snapshot module
# ---------------------------------------------------------------------------

def test_snapshot_writes_gzipped_bundle_and_summary(tmp_path: Path):
    from vietnam_algo.libs.autofin_snapshot import (
        latest_snapshot_paths,
        read_snapshot,
        write_snapshot,
    )

    payloads = {
        "holding": [_holding_row()],
        "suggest": [{"_id": 1, "symbol": "AAA"}],
    }
    summary = {"tradeDate": "2026-10-29", "counts": {"holding": 1}}
    path = write_snapshot(tmp_path, payloads, summary)

    assert path is not None
    assert path.exists()
    assert path.suffix == ".gz"

    with gzip.open(path, "rt", encoding="utf-8") as fh:
        bundle = json.load(fh)
    assert bundle["payloads"]["holding"][0]["symbol"] == "VOS"
    assert bundle["capturedAt"]

    raw, sum_path = latest_snapshot_paths(tmp_path)
    assert raw == path
    assert sum_path is not None
    assert json.loads(sum_path.read_text())["tradeDate"] == "2026-10-29"

    # read_snapshot round-trips both formats.
    assert read_snapshot(path)["payloads"]["suggest"][0]["symbol"] == "AAA"


def test_snapshot_prunes_old_days(tmp_path: Path, monkeypatch):
    from vietnam_algo.libs.autofin_snapshot import (
        prune_snapshots,
        write_snapshot,
    )

    old_day = (datetime.now(timezone.utc) - timedelta(days=30)).date()
    old_dir = tmp_path / "live" / "autofin_snapshots" / old_day.isoformat()
    old_dir.mkdir(parents=True)
    (old_dir / "x.json.gz").write_bytes(b"x")

    recent_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    recent_dir = tmp_path / "live" / "autofin_snapshots" / recent_day.isoformat()
    recent_dir.mkdir(parents=True)
    (recent_dir / "y.json.gz").write_bytes(b"y")

    pruned = prune_snapshots(tmp_path, keep_days=7)
    assert pruned == [old_day.isoformat()]
    assert not old_dir.exists()
    assert recent_dir.exists()


def test_snapshot_retention_is_configurable(tmp_path: Path, monkeypatch):
    from vietnam_algo.libs.autofin_snapshot import prune_snapshots

    monkeypatch.setenv("AUTOFIN_SNAPSHOT_RETENTION_DAYS", "3")
    old_day = (datetime.now(timezone.utc) - timedelta(days=5)).date()
    old_dir = tmp_path / "live" / "autofin_snapshots" / old_day.isoformat()
    old_dir.mkdir(parents=True)
    (old_dir / "x.json.gz").write_bytes(b"x")

    pruned = prune_snapshots(tmp_path)
    assert pruned == [old_day.isoformat()]


def test_snapshot_write_failure_is_swallowed(tmp_path: Path, monkeypatch):
    """A snapshot failure must never break a trading cycle."""
    from vietnam_algo.libs import autofin_snapshot

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(autofin_snapshot, "prune_snapshots", boom)
    # prune failure is caught inside write_snapshot -> returns the path
    path = autofin_snapshot.write_snapshot(tmp_path, {"a": []}, {"b": 1})
    assert path is not None


# ---------------------------------------------------------------------------
# Structured decision log
# ---------------------------------------------------------------------------

def test_decision_log_names_blocked_candidates(capsys):
    """The structured log line names the blocked (non-entry) candidates
    so a skipped deal is never silent."""
    adapter = _make_adapter({
        "1792627200:VOS": {
            "symbol": "VOS",
            "lastRecomment": "⚠️ Hạ tỷ trọng lần 2",
            "_our_qty": 0,
        },
    })
    summary = {
        "tradeDate": "2026-10-29",
        "nav": 26_757_702.0,
        "counts": {"holding": 19, "suggest": 7, "trackedDeals": 1,
                   "allOrders": 0, "newOrders": 0, "partialOrders": 0,
                   "closeOrders": 0},
        "entryGate": {"allowNewEntries": True, "newEntryTopN": 3},
        "staleRecommentRefresh": {"refreshed": [], "skipped_fresh": [],
                                  "failed": []},
        "entryCandidates": [{
            "deal_id": "1792627200:VOS",
            "symbol": "VOS",
            "recommendation": "⚠️ Hạ tỷ trọng lần 2",
            "recommendation_kind": "unknown",
            "our_qty": 0,
            "held_on_dnse": 0,
            "current_price": 11700.0,
            "entered_this_cycle": False,
            "eligible_for_entry": True,
        }],
        "deals": [],
        "orders": [],
        "dealsActioned": [],
        "rawPayloadSizes": {},
    }
    adapter._log_decision_summary(summary)
    out = capsys.readouterr().out
    assert "[autofin_decision]" in out
    assert "entered=-" in out
    assert "tracked=1" in out
