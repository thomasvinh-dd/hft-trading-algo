"""Tests for ``normalize_ledger_status`` and ``order_status_from_detail``.

These two helpers are the core of the order-status polling pipeline
(see ``vietnam_algo/bots/runner.py:_monitor_order_statuses``). The
monitor polls DNSE every 60 s, parses the response through these
helpers, and fires a Telegram notification when the normalized
status changes from one cycle to the next. Any silent change to
the mapping (e.g. an unmapped wire-format string returning
``"unknown"`` instead of a meaningful label) is a regression:
operators will lose visibility on order state transitions.

The 2026-10-12 fix added ``pendingCancel`` and ``pendingReplace``
as first-class non-terminal states so the monitor keeps polling
them and the Telegram message clearly says "cancel requested",
not "unknown".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.order_ledger import (
    TERMINAL_LEDGER_STATUSES,
    UNSAFE_LEDGER_STATUSES,
    normalize_ledger_status,
    order_status_from_detail,
)


# ---------------------------------------------------------------------------
# normalize_ledger_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Filled (terminal). The function is normally called with the
        # DNSE wire format (camelCase) OR an already-uppercase string
        # from ``order_status_from_detail``. We test both shapes plus
        # the lowercase form for symmetry.
        ("filled", "filled"),
        ("Filled", "filled"),
        ("FILLED", "filled"),
        ("matched", "filled"),
        ("Matched", "filled"),
        # Partial fill (non-terminal) — must be in DNSE wire format
        # (camelCase). The bare string "partial" doesn't go through
        # the camelCase → snake_case step and would map to "unknown".
        ("partiallyFilled", "partial"),
        ("partiallyMatched", "partial"),
        ("PARTIALLY_FILLED", "partial"),
        # Cancelled (terminal — American + British spelling)
        ("cancelled", "cancelled"),
        ("canceled", "cancelled"),
        ("cancelledByRightsEvent", "cancelled"),
        # Rejected (terminal)
        ("rejected", "rejected"),
        ("Rejected", "rejected"),
        # Expired (terminal)
        ("expired", "expired"),
        # Closed / done for day (terminal)
        ("closed", "closed"),
        ("doneForDay", "closed"),
        # 2026-10-12 fix: pendingCancel and pendingReplace get a
        # distinct non-terminal label (not "unknown").
        ("pendingCancel", "pending_cancel"),
        ("PendingCancel", "pending_cancel"),
        ("pendingReplace", "pending_replace"),
        # Open / pending / new / queued
        ("new", "open"),
        ("open", "open"),
        ("pending", "open"),
        ("pendingNew", "open"),
        ("waiting", "open"),
        ("queued", "open"),
        # Empty / unknown
        ("", "unknown"),
        (None, "unknown"),
        ("definitelyNotAStatus", "unknown"),
    ],
)
def test_normalize_ledger_status(raw, expected) -> None:
    assert normalize_ledger_status(raw) == expected


def test_pending_cancel_is_not_terminal() -> None:
    """``pendingCancel`` is non-terminal — the cancel is in flight.

    If the monitor treated it as terminal, it would stop polling
    and the operator would never see the final ``cancelled`` (or
    the rare ``new`` reprieve when the cancel is rejected).
    """
    assert normalize_ledger_status("pendingCancel") not in TERMINAL_LEDGER_STATUSES
    assert normalize_ledger_status("pendingReplace") not in TERMINAL_LEDGER_STATUSES


def test_filled_is_terminal() -> None:
    """Sanity check the other side: filled is terminal so we stop polling."""
    assert normalize_ledger_status("filled") in TERMINAL_LEDGER_STATUSES
    assert normalize_ledger_status("cancelled") in TERMINAL_LEDGER_STATUSES
    assert normalize_ledger_status("rejected") in TERMINAL_LEDGER_STATUSES


# ---------------------------------------------------------------------------
# order_status_from_detail
# ---------------------------------------------------------------------------


def test_order_status_from_detail_top_level_orderStatus() -> None:
    """The happy path: DNSE returns a top-level ``orderStatus`` field."""
    detail = {"orderStatus": "PartiallyFilled", "id": "123"}
    assert order_status_from_detail(detail) == "PARTIALLY_FILLED"


def test_order_status_from_detail_top_level_status() -> None:
    """Some endpoints use ``status`` (not ``orderStatus``)."""
    detail = {"status": "Filled", "id": "123"}
    assert order_status_from_detail(detail) == "FILLED"


def test_order_status_from_detail_top_level_state() -> None:
    """And some use ``state``."""
    detail = {"state": "PendingCancel", "id": "123"}
    assert order_status_from_detail(detail) == "PENDING_CANCEL"


def test_order_status_from_detail_nested_dict() -> None:
    """Some responses nest the order under ``order`` or ``data``."""
    detail = {"order": {"orderStatus": "New"}, "id": "123"}
    assert order_status_from_detail(detail) == "NEW"


def test_order_status_from_detail_list_response() -> None:
    """The list-orders endpoint returns a list of orders; take the first."""
    detail = [
        {"orderStatus": "Filled", "id": "456"},
    ]
    assert order_status_from_detail(detail) == "FILLED"


def test_order_status_from_detail_empty() -> None:
    """No status field anywhere returns empty string (NOT "unknown").
    The caller (``_monitor_order_statuses``) is expected to handle
    the empty case separately.
    """
    assert order_status_from_detail({"id": "123"}) == ""
    assert order_status_from_detail({}) == ""
    assert order_status_from_detail([]) == ""


def test_order_status_from_detail_prefers_status_over_orderStatus() -> None:
    """When multiple keys are present, ``status`` wins (it comes first
    in the priority list in the source). The helper is designed to
    be flexible about which key holds the status; the priority order
    is ``status`` → ``orderStatus`` → ``state`` → recurse-into-values.
    """
    detail = {"orderStatus": "New", "status": "Filled", "state": "Cancelled"}
    assert order_status_from_detail(detail) == "FILLED"


# ---------------------------------------------------------------------------
# End-to-end: status transition scenarios used by the monitor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "previous,current,expected_change",
    [
        # New -> open (submit succeeds): same status, no notify
        ("open", normalize_ledger_status("New"), False),
        # Open -> partial fill
        ("open", normalize_ledger_status("PartiallyFilled"), True),
        # Partial -> full fill
        ("partial", normalize_ledger_status("Filled"), True),
        # Open -> pendingCancel (operator pressed cancel)
        ("open", normalize_ledger_status("pendingCancel"), True),
        # pendingCancel -> cancelled (cancel confirmed)
        ("pending_cancel", normalize_ledger_status("cancelled"), True),
        # Open -> filled (instant fill, e.g. ATC matched)
        ("open", normalize_ledger_status("Filled"), True),
        # Open -> rejected
        ("open", normalize_ledger_status("Rejected"), True),
    ],
)
def test_status_transition_changes(previous, current, expected_change) -> None:
    """Verify the monitor would (or would not) fire a Telegram
    notification for each realistic transition."""
    assert (current != previous) is expected_change
