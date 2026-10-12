"""Tests for the order monitor's re-pricing logic.

2026-10-13: the autofin bot placed ABB BUY 100 @ 17,500 on
2026-10-12 14:48 ICT. Over the next 12 minutes the ask drifted
up to 17,600 and stayed there; the order never filled and was
expired at EOD. The user asked for a way to chase a rising
market without runaway behaviour.

The fix is ``_maybe_reprice_stale_order`` in
``vietnam_algo/bots/runner.py``: if a BUY limit order has been
open for >= 15 min (configurable via ORDER_REPRICE_AFTER_SECONDS)
and the live best-ask is above the limit but below the cap
(``original_price * (1 + ORDER_REPRICE_TOLERANCE_PCT)``), cancel
the old order and place a new one at the new ask. If the ask
is above the cap, abandon (cancel and skip). At most
``ORDER_MAX_REPRICES`` re-prices per order (default 1, the
kill-switch against runaway chasing).

These tests pin the eligibility / staleness / cap / direction /
count rules. The Telegram notifier and the live-quote fetch are
mocked so the tests are pure logic.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.bots.runner import _maybe_reprice_stale_order
from vietnam_algo.libs.order_ledger import LedgerOrder


# Real DNSE /price/{sym}/quotes/latest shape. ``price`` is in
# THOUSANDS of VND (17.6 = 17,600 VND) per _extract_best_ask_in_vnd.
QUOTE_ABOVE_17500 = {
    "quotes": [
        {"boardId": "G1", "offer": [{"price": 17.6}]}
    ]
}
QUOTE_BELOW_17500 = {
    "quotes": [
        {"boardId": "G1", "offer": [{"price": 17.0}]}
    ]
}
QUOTE_WAY_ABOVE = {
    "quotes": [
        {"boardId": "G1", "offer": [{"price": 20.6}]}
    ]
}
QUOTE_AT_18100 = {
    "quotes": [
        {"boardId": "G1", "offer": [{"price": 18.1}]}
    ]
}
QUOTE_NO_OFFER = {
    "quotes": [
        {"boardId": "G1", "offer": []}
    ]
}


class _StubClient:
    """Records calls; returns canned responses for cancel/place/quote."""

    def __init__(
        self,
        latest_quote: dict | Exception = None,
        cancel_error: Exception | None = None,
        place_response: dict | None = None,
        place_error: Exception | None = None,
        cash_pkg_id: int = 1036,
    ) -> None:
        self._latest_quote = latest_quote if latest_quote is not None else QUOTE_ABOVE_17500
        self._cancel_error = cancel_error
        self._place_response = place_response if place_response is not None else {"id": "new-123", "orderStatus": "New"}
        self._place_error = place_error
        self._cash_pkg_id = cash_pkg_id
        self.calls: list[dict] = []

    def get_latest_quote(self, symbol: str) -> dict:
        self.calls.append({"method": "get_latest_quote", "symbol": symbol})
        if isinstance(self._latest_quote, Exception):
            raise self._latest_quote
        return self._latest_quote

    def cancel_order(self, *args, **kwargs):
        self.calls.append({"method": "cancel_order", "args": args, "kwargs": kwargs})
        if self._cancel_error:
            raise self._cancel_error
        return {"id": args[2] if len(args) > 2 else "cancelled", "orderStatus": "PendingCancel"}

    def place_stock_order(self, *args, **kwargs):
        self.calls.append({"method": "place_stock_order", "args": args, "kwargs": kwargs})
        if self._place_error:
            raise self._place_error
        return self._place_response

    def get_cash_loan_package_id(self, *args, **kwargs):
        self.calls.append({"method": "get_cash_loan_package_id", "args": args, "kwargs": kwargs})
        return self._cash_pkg_id


def _make_order(
    *,
    side: str = "NB",
    status: str = "open",
    price: int = 17500,
    original_price: int = 0,
    dnse_order_id: str = "old-123",
    created_at: str = "",
    reprice_count: int = 0,
) -> LedgerOrder:
    if not created_at:
        ts = datetime.now(timezone.utc) - timedelta(minutes=16)
        created_at = ts.isoformat().replace("+00:00", "Z")
    return LedgerOrder(
        runId="2026-10-13",
        sourceOrderId=42,
        sourceDate="2026-10-13",
        symbol="ABB",
        side=side,
        quantity=100,
        price=price,
        reason="autofin_buy:test",
        status=status,
        dnseOrderId=dnse_order_id,
        createdAt=created_at,
        originalPrice=original_price if original_price > 0 else price,
        repriceCount=reprice_count,
    )


# -----------------------------------------------------------------------------
# Eligibility
# -----------------------------------------------------------------------------


def test_skips_sell_orders() -> None:
    """SELL orders should NOT be re-priced."""
    order = _make_order(side="NS")
    client = _StubClient()
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert summary["action"] == "skipped"
    assert "side is not BUY" in summary["reason"]


def test_skips_non_open_status() -> None:
    """Only ``open`` orders are eligible."""
    for status in ("partial", "filled", "cancelled", "rejected", "expired", "pending_cancel"):
        order = _make_order(status=status)
        client = _StubClient()
        summary = _maybe_reprice_stale_order(
            client, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
        )
        assert summary["action"] == "skipped", f"status={status!r} should skip"


def test_skips_when_no_dnse_order_id() -> None:
    """Orders not yet placed have no ``dnseOrderId`` and can't be re-priced."""
    order = _make_order(dnse_order_id="")
    client = _StubClient()
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert summary["action"] == "skipped"
    assert "no dnseOrderId" in summary["reason"]


def test_skips_when_max_reprices_reached() -> None:
    """The kill-switch: at most ``max_reprices`` re-prices per order."""
    order = _make_order(reprice_count=1)
    client = _StubClient()
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order,
        now_epoch=datetime.now(timezone.utc).timestamp(),
        max_reprices=1,
    )
    assert summary["action"] == "skipped"
    assert "re-price cap reached" in summary["reason"]


def test_skips_when_disabled_by_max_reprices_zero() -> None:
    """``max_reprices=0`` disables re-pricing entirely (opt-out)."""
    order = _make_order()
    client = _StubClient()
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order,
        now_epoch=datetime.now(timezone.utc).timestamp(),
        max_reprices=0,
    )
    assert summary["action"] == "skipped"
    assert "disabled" in summary["reason"]


def test_skips_when_no_client() -> None:
    order = _make_order()
    summary = _maybe_reprice_stale_order(
        None, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert summary["action"] == "skipped"
    assert "no client" in summary["reason"]


def test_skips_when_no_trading_token() -> None:
    order = _make_order()
    summary = _maybe_reprice_stale_order(
        _StubClient(), "A", None, order,
        now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert summary["action"] == "skipped"
    assert "no trading token" in summary["reason"]


# -----------------------------------------------------------------------------
# Staleness
# -----------------------------------------------------------------------------


def test_skips_when_order_too_young() -> None:
    """An order must be stale (>= ``reprice_after_seconds`` old)."""
    young = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
    order = _make_order(created_at=young)
    client = _StubClient()
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order,
        now_epoch=datetime.now(timezone.utc).timestamp(),
        reprice_after_seconds=900,
    )
    assert summary["action"] == "skipped"
    assert "too young" in summary["reason"]


def test_skips_when_created_at_unparseable() -> None:
    order = _make_order(created_at="not-a-timestamp")
    client = _StubClient()
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert summary["action"] == "skipped"
    assert "unparseable" in summary["reason"]


# -----------------------------------------------------------------------------
# Ask direction + cap
# -----------------------------------------------------------------------------


def test_skips_when_best_ask_below_current_limit() -> None:
    """If the ask is at or below the current limit, the order
    has a chance to fill naturally — don't re-price.
    """
    order = _make_order(price=17500)
    client = _StubClient(latest_quote=QUOTE_BELOW_17500)
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert summary["action"] == "skipped"
    assert "not above current limit" in summary["reason"]


def test_skips_when_no_best_ask_in_quote() -> None:
    """If the quote response has no best-ask, skip — don't crash."""
    order = _make_order()
    client = _StubClient(latest_quote=QUOTE_NO_OFFER)
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert summary["action"] == "skipped"
    assert "no best-ask" in summary["reason"]


def test_skips_when_get_latest_quote_raises() -> None:
    """Transient API failure must not crash the monitor."""
    order = _make_order()
    client = _StubClient(latest_quote=ConnectionError("timeout"))
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert summary["action"] == "skipped"
    assert "get_latest_quote failed" in summary["reason"]


def test_abandons_when_ask_above_cap() -> None:
    """If the new ask is above the cap, we cancel the order and
    ABANDON. This is the kill-switch: don't chase a runaway market.
    """
    order = _make_order(price=17500, original_price=17500)
    # 20.6 VND ask → 20,600 VND, way above the 3% cap (17,500 * 1.03 = 18,025)
    client = _StubClient(latest_quote=QUOTE_WAY_ABOVE)
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order,
        now_epoch=datetime.now(timezone.utc).timestamp(),
        reprice_tolerance_pct=0.03,
    )
    assert summary["action"] == "abandoned"
    # Cancel was called on the old order
    cancel_calls = [c for c in client.calls if c["method"] == "cancel_order"]
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["args"][2] == "old-123"
    # No place_stock_order was called (we abandoned)
    place_calls = [c for c in client.calls if c["method"] == "place_stock_order"]
    assert place_calls == []


# -----------------------------------------------------------------------------
# Happy path: re-price succeeds
# -----------------------------------------------------------------------------


def test_replaces_when_ask_within_cap() -> None:
    """The happy path: ask above limit, below cap, order is stale.
    Cancel old, place new at the new ask.
    """
    order = _make_order(price=17500, original_price=17500)
    # 17.6 VND ask → 17,600 VND, within 3% cap of 17,500
    client = _StubClient(
        latest_quote=QUOTE_ABOVE_17500,
        place_response={"id": "new-456", "orderStatus": "New"},
    )
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order,
        now_epoch=datetime.now(timezone.utc).timestamp(),
        reprice_tolerance_pct=0.03,
    )
    assert summary["action"] == "replaced"
    assert summary["new_price"] == 17600  # the new ask, in VND
    assert summary["best_ask"] == 17600
    assert summary["cap"] == int(17500 * 1.03)  # 18,025
    assert summary["cancelled_dnse_order_id"] == "old-123"
    assert summary["new_dnse_order_id"] == "new-456"
    # cancel + place both called
    cancel_calls = [c for c in client.calls if c["method"] == "cancel_order"]
    place_calls = [c for c in client.calls if c["method"] == "place_stock_order"]
    assert len(cancel_calls) == 1
    assert len(place_calls) == 1
    # place_stock_order was called with the new ask
    place_call = place_calls[0]
    place_args = place_call["args"]
    place_kwargs = place_call["kwargs"]
    if place_args:
        assert place_args[0] == "A"  # account_no
        assert place_args[2] == "ABB"  # symbol
        assert place_args[3] == "NB"  # side
        assert place_args[4] == 100  # quantity
        assert place_args[5] == 17600  # price (the new ask)
    else:
        assert place_kwargs["price"] == 17600
        assert place_kwargs["symbol"] == "ABB"
        assert place_kwargs["side"] == "NB"


def test_uses_original_price_for_cap_when_present() -> None:
    """If the order has been re-priced before, ``originalPrice``
    holds the FIRST price. The cap must be computed from the
    original price, not the current (re-priced) one.
    """
    order = _make_order(price=17600, original_price=17500)  # repriced once already
    # Ask at 17,800 VND: above 17,600 (current limit, already
    # re-priced once) but within 3% of 17,500 (original price =
    # 17,500 * 1.03 = 18,025). So this should be REPLACED at 17,800.
    client = _StubClient(
        latest_quote={"quotes": [{"boardId": "G1", "offer": [{"price": 17.8}]}]}
    )
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order,
        now_epoch=datetime.now(timezone.utc).timestamp(),
        reprice_tolerance_pct=0.03,
    )
    assert summary["action"] == "replaced"
    assert summary["new_price"] == 17800
    assert summary["cap"] == int(17500 * 1.03)


def test_cancel_failure_does_not_place() -> None:
    """If the cancel call fails, do NOT place a new order — we
    would have BOTH the old and the new order live.
    """
    order = _make_order()
    client = _StubClient(
        latest_quote=QUOTE_ABOVE_17500,
        cancel_error=RuntimeError("cancel failed"),
    )
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert summary["action"] == "skipped"
    assert "cancel failed" in summary["reason"]
    place_calls = [c for c in client.calls if c["method"] == "place_stock_order"]
    assert place_calls == []


def test_place_failure_keeps_old_order_cancelled() -> None:
    """If the cancel succeeds but the place fails, the order is
    gone from DNSE. The summary reflects the failure so the
    operator can re-place manually.
    """
    order = _make_order()
    client = _StubClient(
        latest_quote=QUOTE_ABOVE_17500,
        place_error=RuntimeError("place failed: INVALID_INPUT"),
    )
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert summary["action"] == "skipped"
    assert "place_stock_order failed" in summary["reason"]
    assert "INVALID_INPUT" in summary["reason"]


def test_cash_pkg_lookup_failure_still_attempts_place() -> None:
    """If ``get_cash_loan_package_id`` raises, the place still
    goes through with ``loan_package_id=0``. Better to fail
    loudly than to silently skip.
    """

    class _BadCashPkgClient(_StubClient):
        def get_cash_loan_package_id(self, *args, **kwargs):
            raise RuntimeError("loan-packages API down")

    order = _make_order()
    client = _BadCashPkgClient(latest_quote=QUOTE_ABOVE_17500)
    summary = _maybe_reprice_stale_order(
        client, "A", "tok", order, now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    # The place was attempted; the response from place_stock_order
    # is the stub's default, so the action is "replaced".
    assert summary["action"] == "replaced"
    # The place was called with loan_package_id=0
    place_calls = [c for c in client.calls if c["method"] == "place_stock_order"]
    assert len(place_calls) == 1
    place_args = place_calls[0]["args"]
    place_kwargs = place_calls[0]["kwargs"]
    if place_args:
        assert place_args[6] == 0  # loan_package_id positional
    else:
        assert place_kwargs.get("loan_package_id") == 0
