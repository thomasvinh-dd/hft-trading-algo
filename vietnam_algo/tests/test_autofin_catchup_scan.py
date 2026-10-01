"""Tests for the 2026-09-29 catch-up mechanism.

Background: the autofin bot died on 2026-09-25 evening during my
testing and was silent for 4 days until I noticed. The bot had no
mechanism to detect the gap and replay signals that fired during
the downtime. The fix introduces:

  1. ``_compute_poll_gap_seconds()`` — computes how long since the
     last state save.
  2. ``_catchup_threshold_seconds()`` — reads
     ``AUTOFIN_CATCHUP_THRESHOLD_HOURS`` env var (default 24h).
  3. ``_maybe_catchup_missed_signals()`` — scans every held+closed
     deal's ``hisRecomment`` history, compares against our per-deal
     state, emits catch-up orders for events we haven't applied.

The catch-up is dry-run-only by design — live mode never replays
past orders (DNSE positions already reflect whatever the operator
did manually during the gap).

These tests pin the behaviour:
  - Gap detection: ``None`` when cold-start, seconds when warm.
  - Threshold: ``AUTOFIN_CATCHUP_THRESHOLD_HOURS`` env override.
  - Catch-up fires when gap > threshold, skipped otherwise.
  - Per-deal skip: deals already up-to-date (no new events after
    our ``_last_recomment``) are not double-counted.
  - Per-deal replay: missed events emitted in chronological order
    with correct ``kind``/``side``/``fraction``.
  - Live mode no-op (alert only).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------

def _make_adapter_with_last_poll(last_poll_at: str | None) -> Any:
    """Build a minimal adapter instance with the given ``_last_poll_at``."""
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = last_poll_at
    return a


def test_gap_is_none_when_cold_start():
    """No prior ``_last_poll_at`` → ``None`` (not 0, not error)."""
    a = _make_adapter_with_last_poll(None)
    assert a._compute_poll_gap_seconds() is None


def test_gap_is_seconds_for_warm_start():
    """With ``_last_poll_at`` set, gap is wall-clock seconds since."""
    a = _make_adapter_with_last_poll(None)
    past = datetime.now(tz=timezone.utc) - timedelta(hours=25)
    a._last_poll_at = past.isoformat()
    gap = a._compute_poll_gap_seconds()
    assert gap is not None
    assert 25 * 3600 - 5 < gap < 25 * 3600 + 5, (
        f"Expected ~25h gap, got {gap/3600:.2f}h"
    )


def test_gap_handles_naive_datetime_string():
    """If ``_last_poll_at`` is a naive ISO string (no tz), treat as UTC."""
    a = _make_adapter_with_last_poll(None)
    past_utc = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    past_naive = past_utc.replace(tzinfo=None)
    a._last_poll_at = past_naive.isoformat()
    gap = a._compute_poll_gap_seconds()
    assert gap is not None
    assert abs(gap - 2 * 3600) < 30


def test_gap_handles_z_suffix():
    """ISO string with trailing 'Z' (Zulu) is parsed as UTC."""
    a = _make_adapter_with_last_poll(None)
    past = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
    a._last_poll_at = past.strftime("%Y-%m-%dT%H:%M:%SZ")
    gap = a._compute_poll_gap_seconds()
    assert gap is not None
    assert abs(gap - 30 * 60) < 30


def test_gap_returns_none_for_unparseable_string():
    """Garbage ``_last_poll_at`` returns ``None`` (don't crash)."""
    a = _make_adapter_with_last_poll("not a date")
    assert a._compute_poll_gap_seconds() is None


# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------

def test_threshold_default_is_24_hours(monkeypatch):
    """Default threshold = 24 * 3600 seconds (24 trading hours)."""
    monkeypatch.delenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", raising=False)
    from vietnam_algo.algos.autofin_signal import (
        AutofinSignalAdapter, DEFAULT_CATCHUP_THRESHOLD_SECONDS,
    )
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    assert a._catchup_threshold_seconds() == DEFAULT_CATCHUP_THRESHOLD_SECONDS
    assert a._catchup_threshold_seconds() == 24 * 3600


def test_threshold_env_override(monkeypatch):
    """``AUTOFIN_CATCHUP_THRESHOLD_HOURS=2`` → 2 hours = 7200 seconds."""
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "2")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    assert a._catchup_threshold_seconds() == 2 * 3600


def test_threshold_env_override_with_fraction(monkeypatch):
    """Fractional hours (e.g. 0.5) supported."""
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "0.5")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    assert a._catchup_threshold_seconds() == 1800


def test_threshold_invalid_env_falls_back(monkeypatch):
    """Invalid ``AUTOFIN_CATCHUP_THRESHOLD_HOURS`` falls back to 24h."""
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "not_a_number")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    assert a._catchup_threshold_seconds() == 24 * 3600


# ---------------------------------------------------------------------------
# _event_already_applied
# ---------------------------------------------------------------------------

def test_event_already_applied_short_form_in_long_form():
    """``_last_recomment='💰 Chốt lãi lần 1 mức giá 14,350'`` is contained
    in the long-form history event '... 💰 Chốt lãi lần 1 mức giá 14,350 Tỷ trọng ...'."""
    from vietnam_algo.algos.autofin_signal import _event_already_applied
    event = {
        "action": "tp",
        "raw": "[ASP] 💰 Chốt lãi lần 1 mức giá 14,350 Tỷ trọng 10 % danh mục nắm giữ Ngày mua: 11/09/2026",
    }
    assert _event_already_applied(
        event, "💰 Chốt lãi lần 1 mức giá 14,350",
    ) is True


def test_event_already_applied_unrelated_returns_false():
    """Different TP step → not yet applied."""
    from vietnam_algo.algos.autofin_signal import _event_already_applied
    event = {
        "action": "tp",
        "raw": "[ASP] 💰 Chốt lãi lần 2 mức giá 16,700 Tỷ trọng 30 %",
    }
    assert _event_already_applied(
        event, "💰 Chốt lãi lần 1 mức giá 14,350",
    ) is False


def test_event_already_applied_empty_last_returns_false():
    """Empty ``_last_recomment`` (cold start for this deal) → not applied."""
    from vietnam_algo.algos.autofin_signal import _event_already_applied
    event = {"action": "tp", "raw": "[ASP] 💰 Chốt lãi lần 1 mức giá 14,350"}
    assert _event_already_applied(event, "") is False


# ---------------------------------------------------------------------------
# _build_catchup_orders_for_deal
# ---------------------------------------------------------------------------

def test_catchup_emits_entry_for_untracked_deal():
    """First-time deal (no ``_last_recomment``): all events emitted."""
    from vietnam_algo.algos.autofin_signal import _build_catchup_orders_for_deal
    deal = {
        "_id": 1789084800,
        "symbol": "ASP",
        "statusDeal": {
            "price_Open": 14300,
            "time_Open": "11/09/2026 07:00:00",
        },
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 11/09/2026 🟢 Vùng mua: 14,000 - 14,300</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 1 mức giá 15,800 Tỷ trọng 10 % danh mục nắm giữ Ngày mua: 11/09/2026 Giá mua: 14,300 Nắm giữ: 3 ngày</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 2 mức giá 16,700 Tỷ trọng 30 % danh mục nắm giữ Ngày mua: 11/09/2026 Giá mua: 14,300 Nắm giữ: 6 ngày</p></div>",
        ],
    }
    orders = _build_catchup_orders_for_deal(deal, our_deal_state=None)
    kinds = [o["kind"] for o in orders]
    assert kinds == ["entry", "partial_close", "partial_close"], (
        f"Expected 3 catch-up orders (entry + 2 TP steps); got {kinds}"
    )
    # TP step 1 has fraction 0.10, TP step 2 has 0.30.
    assert orders[1]["fraction"] == 0.10
    assert orders[2]["fraction"] == 0.30
    assert orders[1]["side"] == "NS"
    assert orders[1]["symbol"] == "ASP"


def test_catchup_skips_already_applied_events():
    """Deal with ``_last_recomment`` set to TP1 only fires TP2."""
    from vietnam_algo.algos.autofin_signal import _build_catchup_orders_for_deal
    deal = {
        "_id": 1789084800,
        "symbol": "ASP",
        "statusDeal": {"price_Open": 14300, "time_Open": "11/09/2026"},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 11/09/2026 🟢 Vùng mua: 14,000 - 14,300</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 1 mức giá 15,800 Tỷ trọng 10 % danh mục nắm giữ Ngày mua: 11/09/2026 Giá mua: 14,300 Nắm giữ: 3 ngày</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 2 mức giá 16,700 Tỷ trọng 30 % danh mục nắm giữ Ngày mua: 11/09/2026 Giá mua: 14,300 Nắm giữ: 6 ngày</p></div>",
        ],
    }
    orders = _build_catchup_orders_for_deal(
        deal,
        our_deal_state={"_last_recomment": "💰 Chốt lãi lần 1 mức giá 15,800"},
    )
    kinds = [o["kind"] for o in orders]
    assert kinds == ["partial_close"], (
        f"Expected only TP2 (after TP1 was already applied); got {kinds}"
    )


def test_catchup_no_events_for_fully_synced_deal():
    """Deal fully synced (last event = current ``_last_recomment``)
    → no catch-up orders emitted."""
    from vietnam_algo.algos.autofin_signal import _build_catchup_orders_for_deal
    deal = {
        "_id": 1789084800,
        "symbol": "ASP",
        "statusDeal": {"price_Open": 14300},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 11/09/2026 🟢 Vùng mua: 14,000 - 14,300</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 1 mức giá 15,800 Tỷ trọng 10 %</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 2 mức giá 16,700 Tỷ trọng 30 %</p></div>",
        ],
    }
    orders = _build_catchup_orders_for_deal(
        deal,
        our_deal_state={"_last_recomment": "💰 Chốt lãi lần 2 mức giá 16,700"},
    )
    assert orders == [], (
        f"Deal fully synced should emit 0 catch-up orders; got {orders}"
    )


def test_catchup_handles_reduce_event():
    """Reduce events (⚠️ Hạ X %) emit kind=reduce_lap."""
    from vietnam_algo.algos.autofin_signal import _build_catchup_orders_for_deal
    deal = {
        "_id": 1789344000,
        "symbol": "HNM",
        "statusDeal": {"price_Open": 8600},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 14/09/2026 🟢 Vùng mua: 8,900 - 9,100</p></div>",
            "<div><p>[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1 Giá: 8,100 - Nắm giữ: 4 ngày Lỗ: -10.99 %</p></div>",
        ],
    }
    orders = _build_catchup_orders_for_deal(deal, our_deal_state=None)
    kinds = [o["kind"] for o in orders]
    assert kinds == ["entry", "reduce_lap"], (
        f"Expected entry + reduce_lap; got {kinds}"
    )
    assert orders[1]["fraction"] == 0.50


def test_catchup_handles_close_event():
    """Full close events (Đóng DEAL) emit kind=full_close."""
    from vietnam_algo.algos.autofin_signal import _build_catchup_orders_for_deal
    deal = {
        "_id": 1780000000,
        "symbol": "KLB",
        "statusDeal": {"price_Open": 12000, "price_Closed": 11500},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 01/09/2026 🟢 Vùng mua: 12,000 - 12,500</p></div>",
            "<div><p>[KLB] 💰 Chốt lãi lần 1 mức giá 13,000 Tỷ trọng 30 %</p></div>",
            "<div><p>[KLB] Đóng DEAL ... Lãi: 5.0 %</p></div>",
        ],
    }
    orders = _build_catchup_orders_for_deal(deal, our_deal_state=None)
    kinds = [o["kind"] for o in orders]
    assert kinds == ["entry", "partial_close", "full_close"], (
        f"Expected entry + TP1 + close; got {kinds}"
    )


def test_catchup_marks_orders_as_catchup():
    """All catch-up orders carry ``_catchup=True`` flag so the apply
    path can log them with a special prefix (vs regular orders)."""
    from vietnam_algo.algos.autofin_signal import _build_catchup_orders_for_deal
    deal = {
        "_id": 1789084800,
        "symbol": "ASP",
        "statusDeal": {"price_Open": 14300},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 11/09/2026 🟢 Vùng mua: 14,000 - 14,300</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 1 mức giá 15,800 Tỷ trọng 10 %</p></div>",
        ],
    }
    orders = _build_catchup_orders_for_deal(deal, our_deal_state=None)
    for o in orders:
        assert o.get("_catchup") is True, (
            f"Catch-up order missing _catchup flag: {o}"
        )
        assert o["reason"].startswith("autofin_catchup:"), (
            f"Catch-up reason must use autofin_catchup: prefix; got {o['reason']!r}"
        )


# ---------------------------------------------------------------------------
# _maybe_catchup_missed_signals (integration)
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, deals_by_symbol: dict[str, list[dict]]):
        self._deals = deals_by_symbol

    def fetch_deals(self, symbol, page_size=5):
        return self._deals.get(symbol, [])


def test_maybe_catchup_skips_when_gap_below_threshold(monkeypatch):
    """Bot was down 1h → no catch-up (threshold default 24h)."""
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=1)
    ).isoformat()
    client = _FakeClient({})
    result = a._maybe_catchup_missed_signals(
        client, [], [], live_submit=False,
    )
    assert result is None, (
        f"1h gap < 24h threshold; expected no catch-up. Got {result}"
    )


def test_maybe_catchup_skips_when_no_last_poll(monkeypatch):
    """Cold start (no ``_last_poll_at``) → no catch-up. The bot should
    process today's signals normally on the first cycle."""
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = None
    client = _FakeClient({})
    result = a._maybe_catchup_missed_signals(
        client, [], [], live_submit=False,
    )
    assert result is None


def test_maybe_catchup_live_mode_noop(monkeypatch):
    """Live mode: catch-up doesn't fire — alert only."""
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    client = _FakeClient({})
    result = a._maybe_catchup_missed_signals(
        client, [], [], live_submit=True,
    )
    assert result is not None
    assert result["live_skip"] is True
    assert result["events"] == 0


def test_maybe_catchup_emits_summary_for_missed_deals(monkeypatch):
    """Bot down 48h with 1 missed TP step → summary reflects it."""
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    # ASP currently held with TP1 already applied; TP2 fired during
    # the gap and was missed.
    holding = [{"symbol": "ASP"}]
    deal = {
        "_id": 1789084800,
        "symbol": "ASP",
        "statusDeal": {"price_Open": 14300},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 11/09/2026 🟢 Vùng mua: 14,000 - 14,300</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 1 mức giá 15,800 Tỷ trọng 10 %</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 2 mức giá 16,700 Tỷ trọng 30 %</p></div>",
        ],
    }
    a._our_deals = {
        "1789084800:ASP": {
            "_last_recomment": "💰 Chốt lãi lần 1 mức giá 15,800",
        },
    }
    client = _FakeClient({"ASP": [deal]})

    result = a._maybe_catchup_missed_signals(
        client, holding, [], live_submit=False,
    )
    assert result is not None
    assert result["live_skip"] is False
    assert len(result["deals"]) == 1
    assert result["deals"][0]["symbol"] == "ASP"
    assert result["deals"][0]["missed_events"] == 1
    assert result["events"] == 1
    assert result["gap_hours"] >= 47.9


def test_maybe_catchup_no_missed_events_returns_none(monkeypatch):
    """Bot down 48h but every deal is fully synced → no events to
    catch up. Return ``None`` (no notification needed)."""
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    a._our_deals = {
        "1789084800:ASP": {
            "_last_recomment": "💰 Chốt lãi lần 2 mức giá 16,700",
        },
    }
    deal = {
        "_id": 1789084800,
        "symbol": "ASP",
        "statusDeal": {"price_Open": 14300},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 11/09/2026 🟢 Vùng mua: 14,000 - 14,300</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 1 mức giá 15,800 Tỷ trọng 10 %</p></div>",
            "<div><p>[ASP] 💰 Chốt lãi lần 2 mức giá 16,700 Tỷ trọng 30 %</p></div>",
        ],
    }
    client = _FakeClient({"ASP": [deal]})
    result = a._maybe_catchup_missed_signals(
        client, [], [], live_submit=False,
    )
    assert result is None, (
        f"All events already applied; expected no catch-up. Got {result}"
    )


def test_maybe_catchup_includes_symbols_from_our_deals(monkeypatch):
    """2026-09-30 HNM incident: HNM was held in our dry-run state but
    autofin's ``/signal-static-holding`` no longer returned it (likely
    because the operator manually closed it in the autofin UI). The
    catchup scan only iterated ``holding + closing``, so HNM was
    skipped entirely — even though ``/signal-deals?symbol=HNM`` had
    two missed REDUCE 50% events.

    Fix: also iterate over symbols from ``self._positions`` (live
    portfolio) and ``self._our_deals`` (per-deal state). Both are
    populated from local state files which always have the symbols
    the bot actually still has a stake in.
    """
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    # HNM is in OUR state but NOT in autofin's holding or closing.
    # This is the exact scenario the bug exposed.
    a._our_deals = {
        "1789344000:HNM": {"_last_recomment": ""},  # no events applied yet
    }
    a._positions = ["HNM"]
    deal = {
        "_id": 1789344000,
        "symbol": "HNM",
        "statusDeal": {"price_Open": 9100},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 14/09/2026 🟢 Vùng mua: 8,900 - 9,100</p></div>",
            "<div><p>[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1 Giá: 8,100</p></div>",
            "<div><p>[HNM] ⚠️ Hạ 50 % tỉ trọng lần 2 Giá: 7,800</p></div>",
        ],
    }
    client = _FakeClient({"HNM": [deal]})

    # Empty holding + closing (HNM not in autofin's lists) — but
    # catchup must still visit HNM because we hold it ourselves.
    result = a._maybe_catchup_missed_signals(
        client, [], [], live_submit=False,
    )
    assert result is not None, (
        "catchup scan must visit symbols from our state even when "
        "they're absent from autofin's holding+closing lists"
    )
    hnm_deal = [d for d in result["deals"] if d["symbol"] == "HNM"]
    assert len(hnm_deal) == 1
    # 3 missed events: the entry (no applied_keys for HNM in state)
    # + 2 REDUCE events. Pre-fix the scan would skip HNM entirely.
    assert hnm_deal[0]["missed_events"] == 3, (
        "expected 3 missed events (entry + 2 REDUCE lần 1 + lần 2); "
        "pre-fix the scan would skip HNM entirely (0 missed). "
        "Got %d." % hnm_deal[0]["missed_events"]
    )


def test_maybe_catchup_includes_symbols_from_dry_run_state(
    monkeypatch, tmp_path
):
    """Even more invasive case: HNM is NOT in autofin's holding,
    NOT in autofin's closing, NOT in ``_positions``, and NOT in
    ``_our_deals`` — but IS in the dry-run state file. This is the
    most extreme orphan-position scenario the catchup scan must
    handle. Pre-fix the scan would skip HNM entirely.

    The catchup must read the dry-run state file directly as a
    last-resort fallback. ``config`` is the standard
    ``EngineConfig`` instance the runner passes in; we point its
    ``dataDir`` at a temp dir where we control the state file.
    """
    from pathlib import Path
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    a._our_deals = {}  # nothing tracked by the adapter
    a._positions = None  # nothing in live portfolio snapshot

    # Write a dry-run state file with HNM 100 shares
    state_path = tmp_path / "live" / "dry_run_state.autofin_signal.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "initial_nav": 100_000_000,
        "cash": 50_000_000,
        "positions": {
            "HNM": {
                "symbol": "HNM",
                "quantity": 100,
                "avg_price": 8600.0,
                "last_price": 8600.0,
                "cost_basis": 860000.0,
            },
        },
        "applied_keys": [],
        "closed_trades": [],
        "realized_pnl": 0.0,
    }))

    class _Cfg:
        pass
    cfg = _Cfg()
    cfg.dataDir = tmp_path

    deal = {
        "_id": 1789344000,
        "symbol": "HNM",
        "statusDeal": {"price_Open": 9100},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 14/09/2026 🟢 Vùng mua: 8,900 - 9,100</p></div>",
            "<div><p>[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1 Giá: 8,100</p></div>",
            "<div><p>[HNM] ⚠️ Hạ 50 % tỉ trọng lần 2 Giá: 7,800</p></div>",
        ],
    }
    client = _FakeClient({"HNM": [deal]})

    result = a._maybe_catchup_missed_signals(
        client, [], [], live_submit=False, config=cfg,
    )
    assert result is not None
    hnm_deal = [d for d in result["deals"] if d["symbol"] == "HNM"]
    assert len(hnm_deal) == 1, (
        "HNM must be detected via the dry-run state fallback. "
        "Pre-fix the scan would skip HNM entirely (0 missed)."
    )
    assert hnm_deal[0]["missed_events"] == 3, (
        "expected 3 missed events (entry + 2 REDUCE lần 1 + lần 2); "
        "got %d" % hnm_deal[0]["missed_events"]
    )


# ---------------------------------------------------------------------------
# Apply-to-state (2026-09-30 catch-up actually fills the dry-run state)
# ---------------------------------------------------------------------------


def test_catchup_applies_reduce_to_dry_run_state(monkeypatch, tmp_path):
    """2026-09-30 HNM incident: the catchup scan reported the missed
    events but didn't apply them. The 100 HNM shares stayed in dry-run
    state forever because HNM had been removed from autofin's
    /signal-static-holding list. This test verifies the fix: the
    catchup scan now applies the missed events to the dry-run state
    directly, so HNM ends up with 25 shares (100 × 0.5 × 0.5).

    Two missed REDUCE 50% events on HNM during the 4-day gap:
      lần 1 @ 8100  →  100 → 50
      lần 2 @ 7800  →   50 → 25
    After the catchup scan, dry_run_state should have:
      HNM qty=25, cash increased by 50*8100 + 25*7800 = 600,000 VND
      realized_pnl = (8100-8600)*50 + (7800-8600)*25 = -25000 - 20000 = -45000
    """
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    from vietnam_algo.libs.dry_run_state import (
        DryRunState,
        DryRunPosition,
        load_dry_run_state,
    )

    # Pre-existing dry-run state: HNM 100 @ 8600
    state = DryRunState(
        initial_nav=100_000_000,
        cash=50_000_000,
        positions={"HNM": DryRunPosition(
            symbol="HNM",
            quantity=100,
            avg_price=8600.0,
            last_price=8600.0,
            cost_basis=860000.0,
        )},
        realized_pnl=0.0,
        applied_keys=set(),
    )
    state_path = tmp_path / "live" / "dry_run_state.autofin_signal.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    from vietnam_algo.libs.dry_run_state import save_dry_run_state
    save_dry_run_state(state, tmp_path, strategy_name="autofin_signal")

    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    a._our_deals = {}
    a._positions = None

    deal = {
        "_id": 1789344000,
        "symbol": "HNM",
        "statusDeal": {"price_Open": 9100},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 14/09/2026 🟢 Vùng mua: 8,900 - 9,100</p></div>",
            "<div><p>[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1 Giá: 8,100</p></div>",
            "<div><p>[HNM] ⚠️ Hạ 50 % tỉ trọng lần 2 Giá: 7,800</p></div>",
        ],
    }
    client = _FakeClient({"HNM": [deal]})

    class _Cfg:
        pass
    cfg = _Cfg()
    cfg.dataDir = tmp_path

    result = a._maybe_catchup_missed_signals(
        client, [], [], live_submit=False, config=cfg,
    )
    assert result is not None
    assert result["applied"] == 2, (
        "expected 2 REDUCE orders applied to dry-run state. "
        "Pre-fix applied=0 (catchup was diagnostic only). Got %d."
        % result["applied"]
    )

    # Verify dry-run state was actually mutated
    new_state = load_dry_run_state(tmp_path, strategy_name="autofin_signal")
    assert new_state is not None
    hnm = new_state.positions.get("HNM")
    assert hnm is not None
    assert hnm.quantity == 25, (
        "HNM should be 25 shares after 2 REDUCE 50% steps "
        "(100 → 50 → 25). Got %d." % hnm.quantity
    )
    # 50 shares sold @ 8100 = 405000 cash in
    # 25 shares sold @ 7800 = 195000 cash in
    # Total cash added: 600000
    assert new_state.cash == 50_000_000 + 600_000, (
        "cash should be 50M + 600K = 50.6M. Got %.0f." % new_state.cash
    )
    # Realized P&L = (8100-8600)*50 + (7800-8600)*25 = -25000 + -20000 = -45000
    assert abs(new_state.realized_pnl - (-45000)) < 1, (
        "realized_pnl should be -45000. Got %.0f." % new_state.realized_pnl
    )


def test_catchup_applies_full_close_to_dry_run_state(monkeypatch, tmp_path):
    """Catchup scan should also apply full_close events to dry-run state."""
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    from vietnam_algo.libs.dry_run_state import (
        DryRunState,
        DryRunPosition,
        load_dry_run_state,
        save_dry_run_state,
    )

    state = DryRunState(
        initial_nav=100_000_000,
        cash=50_000_000,
        positions={"MZG": DryRunPosition(
            symbol="MZG",
            quantity=500,
            avg_price=30000.0,
            last_price=30000.0,
            cost_basis=15_000_000.0,
        )},
        realized_pnl=0.0,
        applied_keys=set(),
    )
    state_path = tmp_path / "live" / "dry_run_state.autofin_signal.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    save_dry_run_state(state, tmp_path, strategy_name="autofin_signal")

    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    a._our_deals = {}
    a._positions = None

    deal = {
        "_id": 1785888000,
        "symbol": "MZG",
        "statusDeal": {"price_Open": 30000},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 12/09/2026 🟢 Vùng mua: 29,500 - 30,500</p></div>",
            "<div><p>[MZG] 💰 Chốt lãi lần 1 mức giá 32,000 Tỷ trọng 30 %</p></div>",
            "<div><p>Đóng DEAL [MZG] Lãi: 8.00 %</p></div>",
        ],
    }
    client = _FakeClient({"MZG": [deal]})

    class _Cfg:
        pass
    cfg = _Cfg()
    cfg.dataDir = tmp_path

    result = a._maybe_catchup_missed_signals(
        client, [], [], live_submit=False, config=cfg,
    )
    assert result is not None
    # 1 partial_close (TP 30%) + 1 full_close
    assert result["applied"] == 2

    new_state = load_dry_run_state(tmp_path, strategy_name="autofin_signal")
    mzg = new_state.positions.get("MZG")
    # 500 → 350 (after 30% partial_close) → 0 (after full_close)
    assert mzg.quantity == 0, (
        "MZG should be closed after the full_close event. Got %d." % mzg.quantity
    )


def test_catchup_no_apply_when_dry_run_state_missing(
    monkeypatch, tmp_path
):
    """Cold start with no dry-run state file → catchup still surfaces
    the missed events (we passed HNM via _positions) but doesn't
    apply anything (no state to mutate)."""
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    a._our_deals = {}
    # _positions has HNM, so the scan visits the symbol
    a._positions = ["HNM"]

    deal = {
        "_id": 1789344000,
        "symbol": "HNM",
        "statusDeal": {"price_Open": 9100},
        "hisRecomment": [
            "<div><p>[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1 Giá: 8,100</p></div>",
        ],
    }
    client = _FakeClient({"HNM": [deal]})

    class _Cfg:
        pass
    cfg = _Cfg()
    # Use a tmp_path with NO dry-run state file
    subdir = tmp_path / "no_state_here"
    subdir.mkdir()
    cfg.dataDir = subdir

    result = a._maybe_catchup_missed_signals(
        client, [], [], live_submit=False, config=cfg,
    )
    assert result is not None
    assert result["applied"] == 0, (
        "no dry-run state file → no apply. Got %d." % result["applied"]
    )
    # The missed events were still surfaced (informational only)
    assert result["events"] == 1


def test_catchup_does_not_reapply_on_subsequent_runs(
    monkeypatch, tmp_path
):
    """Run the catchup twice with the same 50h gap, but on the
    SECOND run load the per-deal state file first (simulating a
    bot restart that reloaded its tracking dict from the persisted
    autofin_signal_state.json). The first run applied 2 REDUCE
    events and saved its _last_recomment. The second run should
    recognise the deals are now up-to-date and skip the re-fire.

    Pre-fix the second run would re-apply the same REDUCE events
    (HNM 25 → 12 → 6). Post-fix the second run detects 0 missed
    events because _last_recomment is set in the loaded our_deals.
    """
    monkeypatch.setenv("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "24")
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
    from vietnam_algo.libs.dry_run_state import (
        DryRunState,
        DryRunPosition,
        save_dry_run_state,
    )

    # Seed dry-run state
    state = DryRunState(
        initial_nav=100_000_000,
        cash=50_000_000,
        positions={"HNM": DryRunPosition(
            symbol="HNM",
            quantity=100,
            avg_price=8600.0,
            last_price=8600.0,
            cost_basis=860000.0,
        )},
        realized_pnl=0.0,
        applied_keys=set(),
    )
    state_path = tmp_path / "live" / "dry_run_state.autofin_signal.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    save_dry_run_state(state, tmp_path, strategy_name="autofin_signal")

    deal = {
        "_id": 1789344000,
        "symbol": "HNM",
        "statusDeal": {"price_Open": 9100},
        "hisRecomment": [
            "<div><p>Ngày khuyến nghị: 14/09/2026 🟢 Vùng mua: 8,900 - 9,100</p></div>",
            "<div><p>[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1 Giá: 8,100</p></div>",
            "<div><p>[HNM] ⚠️ Hạ 50 % tỉ trọng lần 2 Giá: 7,800</p></div>",
        ],
    }
    client = _FakeClient({"HNM": [deal]})

    class _Cfg:
        pass
    cfg = _Cfg()
    cfg.dataDir = tmp_path

    # First run — fresh adapter, _our_deals empty
    a1 = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a1._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    a1._our_deals = {}
    a1._positions = None
    r1 = a1._maybe_catchup_missed_signals(
        client, [], [], live_submit=False, config=cfg,
    )
    assert r1 is not None
    assert r1["applied"] == 2
    # a1._our_deals was updated with the orphan symbol
    assert "orphan:HNM" in a1._our_deals or any(
        v.get("symbol") == "HNM" for v in a1._our_deals.values()
    ), "first run should track HNM in our_deals"

    # Second run — simulate bot reload of state. The adapter instance
    # is fresh, but its _our_deals is restored from the first run's
    # in-memory state. This is exactly what the bot does between
    # cycles: ``_save_state`` writes our_deals to disk, ``_load_state``
    # reads it back on the next cycle. Without the reload, the
    # test would fail because the in-memory state would be lost.
    a2 = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a2._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    # Restore from first run's updates
    a2._our_deals = dict(a1._our_deals)
    a2._positions = None

    r2 = a2._maybe_catchup_missed_signals(
        client, [], [], live_submit=False, config=cfg,
    )
    # r2 might be None (no missed events after dedup) or a summary
    # with applied=0 — either way, HNM should NOT be reduced
    # below the 25 shares we landed on after the first run.
    from vietnam_algo.libs.dry_run_state import load_dry_run_state
    s2 = load_dry_run_state(tmp_path, strategy_name="autofin_signal")
    assert s2.positions["HNM"].quantity == 25, (
        "second catchup run with restored _our_deals must not "
        "re-apply reduces. Got HNM qty = %.2f." % s2.positions["HNM"].quantity
    )
