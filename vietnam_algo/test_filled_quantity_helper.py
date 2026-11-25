"""Tests for order_ledger.filled_quantity() — the 2026-10-13 fix.

DNSE's order-detail response uses ``fillQuantity`` (camelCase, no
"-ed" suffix). The previous code only checked ``filledQuantity``
and silently returned 0 for every real fill, which made
``_our_qty`` reconcile to 0 even for live held positions.

These tests pin the helper's behavior so the field-name mismatch
can't silently regress again.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.order_ledger import filled_quantity


def test_dnse_actual_field_fillQuantity() -> None:
    """Real DNSE wire format — fillQuantity is the only field."""
    detail = {"fillQuantity": 200, "leaveQuantity": 0}
    assert filled_quantity(detail) == 200


def test_legacy_field_filledQuantity() -> None:
    """Older payloads used filledQuantity — still supported."""
    detail = {"filledQuantity": 100, "leaveQuantity": 100}
    assert filled_quantity(detail) == 100


def test_matchedQuantity_field() -> None:
    """Some order-status payloads use matchedQuantity."""
    detail = {"matchedQuantity": 50}
    assert filled_quantity(detail) == 50


def test_takes_fillQuantity_over_legacy_when_both_present() -> None:
    """When both are present, prefer the DNSE actual field."""
    detail = {"fillQuantity": 200, "filledQuantity": 100}
    assert filled_quantity(detail) == 200


def test_takes_filledQuantity_when_fillQuantity_missing() -> None:
    """Fallback to filledQuantity when fillQuantity is absent."""
    detail = {"filledQuantity": 75, "matchedQuantity": 50}
    assert filled_quantity(detail) == 75


def test_zero_when_no_known_field() -> None:
    """No fill field present → 0 (caller treats as 'no fill yet')."""
    detail = {"leaveQuantity": 200, "canceledQuantity": 0, "orderStatus": "Pending"}
    assert filled_quantity(detail) == 0


def test_zero_when_value_is_none() -> None:
    """Explicit None values are treated as missing."""
    detail = {"fillQuantity": None, "filledQuantity": None}
    assert filled_quantity(detail) == 0


def test_zero_when_value_is_empty_string() -> None:
    """Empty strings are treated as missing."""
    detail = {"fillQuantity": "", "filledQuantity": ""}
    assert filled_quantity(detail) == 0


def test_zero_for_non_dict() -> None:
    """None / list / str → 0 (defensive)."""
    assert filled_quantity(None) == 0
    assert filled_quantity("not a dict") == 0
    assert filled_quantity([1, 2, 3]) == 0
    assert filled_quantity([]) == 0


def test_coerces_string_numbers() -> None:
    """DNSE sometimes returns numeric fields as strings."""
    detail = {"fillQuantity": "200"}
    assert filled_quantity(detail) == 200


def test_ignores_non_numeric_strings() -> None:
    """Garbage values don't crash, fall through to next field."""
    detail = {"fillQuantity": "not-a-number", "filledQuantity": 50}
    assert filled_quantity(detail) == 50


def test_zero_when_value_is_zero() -> None:
    """Explicit zero IS a valid value (not 'missing') — return 0."""
    detail = {"fillQuantity": 0}
    assert filled_quantity(detail) == 0


def test_handles_full_dnse_response_shape() -> None:
    """The full DNSE fill payload is parseable."""
    detail = {
        "id": "277941",
        "orderStatus": "Filled",
        "fillQuantity": 200,
        "leaveQuantity": 0,
        "canceledQuantity": 0,
        "averagePrice": 31050.0,
        "lastPrice": 31050.0,
        "lastQuantity": 200,
        "price": 31200.0,
    }
    assert filled_quantity(detail) == 200


def test_abandoned_regression_test_baf_fill() -> None:
    """Reproduces the 2026-10-13 22:30 ICT silent bug.

    The BAF NB @ 31,200 was filled 200 shares on DNSE. The
    ledger's lastDetail looked exactly like the dict below.
    Before the fix, ``detail.get("filledQuantity")`` returned
    None and the bot's reconciliation step reset ``_our_qty``
    to 0 for the BAF deal — the strategy then re-planned a
    duplicate BAF BUY on the next cycle.

    After the fix, ``filled_quantity(detail)`` correctly
    returns 200 and the cleanup preserves the held position.
    """
    detail = {
        "id": "277941",
        "orderStatus": "Filled",
        "fillQuantity": 200,
        "leaveQuantity": 0,
        "canceledQuantity": 0,
        "averagePrice": 31050.0,
        "lastPrice": 31050.0,
        "lastQuantity": 200,
        "price": 31200.0,
    }
    # The wrong field name silently returned 0.
    assert detail.get("filledQuantity") in (None, "")
    # The right field name (via the helper) returns 200.
    assert filled_quantity(detail) == 200


def test_abandoned_regression_test_abb_fill() -> None:
    """Same regression test for the ABB NB @ 17,400 fill."""
    detail = {
        "id": "133161",
        "orderStatus": "Filled",
        "fillQuantity": 100,
        "leaveQuantity": 0,
        "canceledQuantity": 0,
        "averagePrice": 17700.0,
        "price": 17400.0,
    }
    assert detail.get("filledQuantity") in (None, "")
    assert filled_quantity(detail) == 100
