"""Tests for the order-status monitor in ``bots/runner.py``.

The user asked: "when we send order to dnse, let make report to telegram
channel every time the order status change". This module implements
``_monitor_order_statuses`` which:

  1. Reads the on-disk order ledger
  2. Polls DNSE for each non-terminal order
  3. Compares the new status with the persisted status
  4. Sends a Telegram notification on every change
  5. Updates the ledger with the new status + lastDetail

These tests pin:
  - Status transitions trigger a Telegram notification
  - Same status → no notification (don't spam)
  - Per-order throttle: orders polled within ``throttle_seconds`` are skipped
  - Terminal-state orders (filled/cancelled/rejected/expired) are NOT polled
  - Orders with no ``dnseOrderId`` (failed submits) are NOT polled
  - API errors don't crash; they record an error in the ledger and continue
  - The ``notify`` message includes the symbol, transition, and useful detail
    fields (filledQuantity, averagePrice, rejectReason, etc.)
  - The 2026-10-08 04:30 incident pattern (silent failure on AttributeError)
    is caught and recorded on the ledger
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.bots import runner as runner_mod
from vietnam_algo.bots.runner import (
    _format_status_change_message,
    _monitor_order_statuses,
    _ORDER_POLL_THROTTLE,
)
from vietnam_algo.libs.order_ledger import (
    LedgerOrder,
    load_ledger,
    save_ledger,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def _ledger_with(tmp_path: Path, items: list[dict]) -> Path:
    """Seed ``data/live/order_ledger.json`` with the given items."""
    data_dir = tmp_path
    (data_dir / "live").mkdir(parents=True, exist_ok=True)
    rows = [
        LedgerOrder(
            runId=item.get("runId", "2026-10-09"),
            sourceOrderId=item.get("sourceOrderId", 100 + i),
            sourceDate=item.get("sourceDate", "2026-10-09"),
            symbol=item["symbol"],
            side=item.get("side", "NB"),
            quantity=item.get("quantity", 100),
            price=item.get("price", 17400),
            reason=item.get("reason", "test"),
            status=item.get("status", "submitted"),
            dnseOrderId=item.get("dnseOrderId", ""),
            createdAt=item.get("createdAt", "2026-10-09T01:00:00Z"),
            updatedAt=item.get("updatedAt", "2026-10-09T01:00:00Z"),
            lastDetail=item.get("lastDetail"),
            error=item.get("error", ""),
        )
        for i, item in enumerate(items)
    ]
    save_ledger({f"{r.runId}:{r.sourceOrderId}:{r.symbol}:{r.side}": r for r in rows}, data_dir)
    return data_dir


def _detail(order_status: str, **extras) -> dict:
    """Build a minimal DNSE order-detail response."""
    payload: dict = {
        "id": "12345",
        "symbol": "ABB",
        "side": "NB",
        "orderStatus": order_status,
        "orderType": "LO",
        "price": 17400.0,
        "quantity": 100,
    }
    payload.update(extras)
    return payload


class _StubClient:
    """Minimal stand-in for ``DnseClient``: only ``order_detail`` is called."""

    def __init__(self, responses: dict[str, dict] | list[dict] | Exception):
        if isinstance(responses, Exception):
            self._raise = responses
            self._responses: list[dict] = []
        elif isinstance(responses, dict):
            self._responses = []
            self._by_id = responses
        else:
            self._responses = list(responses)
            self._by_id = {}
        self.calls: list[tuple[str, str | None]] = []

    def order_detail(self, account_no: str, order_id: str | int, trading_token: str | None = None):
        self.calls.append((str(order_id), trading_token))
        if hasattr(self, "_raise") and self._raise:
            raise self._raise
        if self._by_id:
            return self._by_id.get(str(order_id), _detail("filled"))
        if not self._responses:
            return _detail("filled")
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _reset_throttle():
    """The throttle cache is module-level — reset between tests."""
    _ORDER_POLL_THROTTLE.clear()
    yield
    _ORDER_POLL_THROTTLE.clear()


@pytest.fixture
def notify_spy(monkeypatch):
    """Capture all ``notify()`` calls into a list."""
    calls: list[str] = []
    monkeypatch.setattr(runner_mod, "notify", lambda msg: calls.append(msg))
    return calls


# -----------------------------------------------------------------------------
# _format_status_change_message — message shape
# -----------------------------------------------------------------------------


def test_format_status_change_message_includes_symbol_and_transition() -> None:
    item = LedgerOrder(
        runId="2026-10-09", sourceOrderId=1, sourceDate="2026-10-09",
        symbol="ABB", side="NB", quantity=1700, price=17400, reason="test",
        status="open", dnseOrderId="13301",
    )
    msg = _format_status_change_message(item, "open", "filled", _detail("filled", filledQuantity=1700))
    assert "ABB" in msg
    assert "13301" in msg
    assert "OPEN" in msg
    assert "FILLED" in msg
    assert "BUY 1,700 ABB" in msg
    assert "17,400 VND" in msg


def test_format_status_change_message_rejected_includes_reason() -> None:
    item = LedgerOrder(
        runId="2026-10-09", sourceOrderId=1, sourceDate="2026-10-09",
        symbol="VCB", side="NB", quantity=100, price=100, reason="test",
        status="open", dnseOrderId="12345",
    )
    msg = _format_status_change_message(
        item, "open", "rejected",
        _detail("rejected", rejectReason="INSUFFICIENT_BALANCE"),
    )
    assert "rejected" in msg.lower()
    assert "rejectReason" in msg or "INSUFFICIENT" in msg
    assert "❌" in msg  # the icon for rejected


def test_format_status_change_message_buy_sell_label() -> None:
    """NB → BUY, NS → SELL."""
    buy = LedgerOrder(runId="d", sourceOrderId=1, sourceDate="d", symbol="X", side="NB",
                      quantity=10, price=100, reason="r", status="open", dnseOrderId="1")
    sell = LedgerOrder(runId="d", sourceOrderId=2, sourceDate="d", symbol="X", side="NS",
                       quantity=10, price=100, reason="r", status="open", dnseOrderId="2")
    assert "BUY 10 X" in _format_status_change_message(buy, "open", "filled", _detail("filled"))
    assert "SELL 10 X" in _format_status_change_message(sell, "open", "filled", _detail("filled"))


# -----------------------------------------------------------------------------
# _monitor_order_statuses — no orders
# -----------------------------------------------------------------------------


def test_monitor_no_orders_returns_empty_summary(tmp_path, notify_spy) -> None:
    """Empty ledger → no polling, no notify, no errors."""
    _ledger_with(tmp_path, [])
    client = _StubClient([])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path)
    assert summary == {
        "polled": 0, "skipped_throttle": 0,
        # 2026-10-29: orders skipped because they belong to another
        # strategy's bot.
        "skipped_other_strategy": 0,
        "changes": [],
        "errors": [], "terminal_transitions": 0,
        "repriced": [], "abandoned": [],
        "force_replan": False, "force_replan_symbols": [],
    }
    assert client.calls == []
    assert notify_spy == []


# -----------------------------------------------------------------------------
# _monitor_order_statuses — status change
# -----------------------------------------------------------------------------


def test_monitor_status_change_sends_telegram(tmp_path, notify_spy) -> None:
    """Order went from open → filled → notify fires."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "quantity": 1700, "price": 17400,
         "status": "open", "dnseOrderId": "13301", "sourceOrderId": 52114748960652},
    ])
    client = _StubClient([_detail("filled", id="13301", filledQuantity=1700)])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["polled"] == 1
    assert summary["changes"] == [{
        "key": "2026-10-09:52114748960652:ABB:NB",
        "symbol": "ABB", "previous": "open", "current": "filled",
    }]
    assert summary["terminal_transitions"] == 1
    assert len(notify_spy) == 1
    assert "FILLED" in notify_spy[0]
    assert "ABB" in notify_spy[0]
    assert "13301" in notify_spy[0]


def test_monitor_no_change_no_notify(tmp_path, notify_spy) -> None:
    """If status is unchanged, no notify (don't spam Telegram)."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "13301"},
    ])
    client = _StubClient([_detail("open", id="13301")])  # still open
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["polled"] == 1
    assert summary["changes"] == []
    assert summary["terminal_transitions"] == 0
    assert notify_spy == []
    # The ledger IS updated (lastDetail + updatedAt) but no notify.


def test_monitor_terminal_orders_skipped(tmp_path, notify_spy) -> None:
    """filled/cancelled/rejected/expired orders are NOT polled (nothing to do)."""
    for terminal in ("filled", "cancelled", "rejected", "expired", "closed"):
        _ORDER_POLL_THROTTLE.clear()
        _ledger_with(tmp_path, [
            {"symbol": "ABB", "side": "NB", "status": terminal, "dnseOrderId": "1"},
        ])
        client = _StubClient([_detail("filled")])
        summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
        assert summary["polled"] == 0, f"terminal status {terminal!r} should be skipped"
        assert client.calls == []


def test_monitor_orders_without_dnse_id_skipped(tmp_path, notify_spy) -> None:
    """Orders that never made it to DNSE (failed submit) are skipped."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "unknown", "dnseOrderId": ""},
    ])
    client = _StubClient([_detail("filled")])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["polled"] == 0
    assert client.calls == []


# -----------------------------------------------------------------------------
# _monitor_order_statuses — throttle
# -----------------------------------------------------------------------------


def test_monitor_throttles_per_order(tmp_path, notify_spy) -> None:
    """Same order polled twice within throttle_seconds is skipped the 2nd time."""
    # Use a non-terminal status throughout (open → open) so the 2nd
    # call is throttled, not skipped for being terminal.
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1"},
    ])
    client = _StubClient([
        _detail("open", id="1"),  # 1st call: still open, no notify
        _detail("open", id="1"),  # 2nd call: would notify, but throttled
    ])
    s1 = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=60)
    assert s1["polled"] == 1
    assert s1["skipped_throttle"] == 0
    assert notify_spy == []  # status didn't change
    # Second call: same order, within throttle. Skipped.
    s2 = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=60)
    assert s2["skipped_throttle"] == 1
    assert s2["polled"] == 0
    assert notify_spy == []


def test_monitor_zero_throttle_always_polls(tmp_path, notify_spy) -> None:
    """throttle_seconds=0 disables throttling for tests."""
    # Use non-terminal status so the order is polled each time.
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1"},
    ])
    client = _StubClient([
        _detail("open", id="1"),
        _detail("open", id="1"),
    ])
    s1 = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    s2 = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert s1["polled"] == 1
    assert s2["polled"] == 1
    assert notify_spy == []  # no status change → no notify either way


# -----------------------------------------------------------------------------
# _monitor_order_statuses — error handling
# -----------------------------------------------------------------------------


def test_monitor_api_error_records_on_ledger_does_not_crash(tmp_path, notify_spy) -> None:
    """Network/DNSE blip during one order's poll must not stop the cycle."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1"},
        {"symbol": "VCB", "side": "NB", "status": "open", "dnseOrderId": "2"},
    ])

    class _SelectiveClient(_StubClient):
        def order_detail(self, account_no, order_id, trading_token=None):
            if order_id == "1":
                raise RuntimeError("connection reset")
            return _detail("filled", id=order_id, filledQuantity=100)

    client = _SelectiveClient([])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    # ABB errored, VCB succeeded.
    assert summary["polled"] == 1
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["key"].endswith(":ABB:NB")
    assert "connection reset" in summary["errors"][0]["err"]
    assert len(notify_spy) == 1
    assert "VCB" in notify_spy[0]
    # ABB ledger entry was updated to record the error.
    ab = load_ledger(tmp_path)
    abb_entry = next(e for k, e in ab.items() if k.endswith(":ABB:NB"))
    assert "connection reset" in abb_entry.error


def test_monitor_handles_attribute_error_pattern(tmp_path, notify_spy) -> None:
    """The 2026-10-08 04:30 AttributeError pattern: caught, recorded, continues."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "submitted", "dnseOrderId": "1"},
    ])

    class _BadClient(_StubClient):
        def order_detail(self, *args, **kwargs):
            raise AttributeError("'DnseClient' object has no attribute 'order_detail'")

    summary = _monitor_order_statuses(_BadClient([]), "A", "tok", tmp_path, throttle_seconds=0)
    assert len(summary["errors"]) == 1
    assert "AttributeError" in summary["errors"][0]["err"]
    # The error is recorded on the ledger, not in Telegram (we don't
    # want to spam Telegram on every transient error).
    assert notify_spy == []


# -----------------------------------------------------------------------------
# _monitor_order_statuses — multiple orders, mixed transitions
# -----------------------------------------------------------------------------


def test_monitor_multiple_orders_mixed(tmp_path, notify_spy) -> None:
    """Two orders, both change status → two notifications in one cycle."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1", "sourceOrderId": 1},
        {"symbol": "VCB", "side": "NB", "status": "open", "dnseOrderId": "2", "sourceOrderId": 2},
    ])
    client = _StubClient([
        _detail("filled", id="1", filledQuantity=100),  # ABB → filled
        _detail("cancelled", id="2"),  # VCB → cancelled
    ])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["polled"] == 2
    assert summary["terminal_transitions"] == 2
    assert len(summary["changes"]) == 2
    assert len(notify_spy) == 2
    syms = {n.split("→")[0] for n in notify_spy}
    assert any("ABB" in s for s in syms)
    assert any("VCB" in s for s in syms)


def test_monitor_partial_fill_notifies_with_filled_qty(tmp_path, notify_spy) -> None:
    """partial fill: the operator wants to see how much got matched."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1"},
    ])
    client = _StubClient([
        _detail("partiallyFilled", id="1", filledQuantity=500),
    ])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["changes"][0]["current"] == "partial"
    assert summary["terminal_transitions"] == 0  # not yet terminal
    assert "filledQuantity" in notify_spy[0] or "500" in notify_spy[0]


# -----------------------------------------------------------------------------
# _monitor_order_statuses — None client (dry-run mode)
# -----------------------------------------------------------------------------


def test_monitor_no_client_short_circuits(tmp_path, notify_spy) -> None:
    """In dry-run mode there is no DnseClient — monitor must not crash."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1"},
    ])
    summary = _monitor_order_statuses(None, "A", "tok", tmp_path)
    assert summary["polled"] == 0
    assert notify_spy == []


def test_monitor_no_token_still_polls(tmp_path, notify_spy) -> None:
    """trading_token=None is allowed (some DNSE sessions accept it on
    GETs). Monitor passes it through to order_detail unchanged."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1"},
    ])
    client = _StubClient([_detail("filled", id="1")])
    _monitor_order_statuses(client, "A", None, tmp_path, throttle_seconds=0)
    assert client.calls[0][1] is None


def test_monitor_token_loaded_from_shared_file(tmp_path, monkeypatch, notify_spy) -> None:
    """The main-loop integration loads the trading token from
    data/live/dnse_trading_token.json (the file the get_otp cron
    writes). If the file is missing, the monitor still runs with
    token=None — DNSE accepts GETs without it on some sessions.
    """
    # The 2026-10-08 token-less crash: the monitor was called with the
    # variable ``token`` which is only assigned inside the
    # should_submit_orders block. The fix: load from
    # load_shared_trading_token() at the top of the loop, BEFORE the
    # token is assigned by the submit path. If the file is missing
    # (e.g. fresh VPS reboot), pass None and let DNSE accept the GET
    # without it.
    from vietnam_algo.libs.dnse.auth import load_shared_trading_token
    shared = tmp_path / "live" / "dnse_trading_token.json"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(json.dumps({
        "token": "uuid-from-get-otp-cron",
        "account_no": "0003937868",
        "issued_at": "2026-10-08T15:43:01Z",
    }))

    # Verify the shared token loader returns the expected UUID.
    payload = load_shared_trading_token(tmp_path)
    assert payload["token"] == "uuid-from-get-otp-cron"

    # And the monitor uses it on order_detail.
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1"},
    ])
    client = _StubClient([_detail("filled", id="1")])
    _monitor_order_statuses(client, "A", payload["token"], tmp_path, throttle_seconds=0)
    assert client.calls[0][1] == "uuid-from-get-otp-cron"


def test_monitor_with_missing_shared_token_falls_back_to_none(tmp_path, notify_spy) -> None:
    """If the shared token file is missing, the monitor still runs
    with token=None rather than crashing."""
    # No dnse_trading_token.json file.
    from vietnam_algo.libs.dnse.auth import load_shared_trading_token
    payload = load_shared_trading_token(tmp_path)
    assert payload is None

    # The monitor accepts None and falls through.
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1"},
    ])
    client = _StubClient([_detail("filled", id="1")])
    summary = _monitor_order_statuses(client, "A", None, tmp_path, throttle_seconds=0)
    assert summary["polled"] == 1
    assert client.calls[0][1] is None


# -----------------------------------------------------------------------------
# _monitor_order_statuses — ledger persistence
# -----------------------------------------------------------------------------


def test_monitor_persists_updated_ledger_to_disk(tmp_path, notify_spy) -> None:
    """After polling, the on-disk ledger has the new status + lastDetail."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1"},
    ])
    client = _StubClient([_detail("filled", id="1", filledQuantity=1700)])
    _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    ledger = load_ledger(tmp_path)
    abb = next(e for e in ledger.values() if e.symbol == "ABB")
    assert abb.status == "filled"
    assert abb.lastDetail is not None
    assert abb.lastDetail["filledQuantity"] == 1700
    assert abb.updatedAt != "2026-10-09T01:00:00Z"  # updated


# -----------------------------------------------------------------------------
# _monitor_order_statuses — force_replan flag
#
# 2026-10-13: when an order transitions to a terminal-without-fill state
# (cancelled, rejected, expired), the strategy should re-plan and re-submit
# on the next cycle rather than waiting for the 1-hour mid-day-poll throttle.
# The order monitor sets ``summary["force_replan"] = True`` and lists the
# affected symbols in ``summary["force_replan_symbols"]``. The main loop
# then clears the strategy's ``last_poll_at`` so the next mid-day poll
# bypasses the throttle.
# -----------------------------------------------------------------------------


def test_monitor_cancellation_sets_force_replan(tmp_path, notify_spy) -> None:
    """Order going open → cancelled sets force_replan=True (should retry)."""
    _ledger_with(tmp_path, [
        {"symbol": "BAF", "side": "NB", "quantity": 200, "price": 30950,
         "status": "open", "dnseOrderId": "156661"},
    ])
    client = _StubClient([_detail("cancelled", id="156661", filledQuantity=0,
                                  canceledQuantity=200, leaveQuantity=0)])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["force_replan"] is True
    assert "BAF" in summary["force_replan_symbols"]
    assert summary["terminal_transitions"] == 1


def test_monitor_rejection_sets_force_replan(tmp_path, notify_spy) -> None:
    """Order going open → rejected sets force_replan=True (should retry)."""
    _ledger_with(tmp_path, [
        {"symbol": "ABW", "side": "NB", "quantity": 300, "price": 14100,
         "status": "open", "dnseOrderId": "999"},
    ])
    client = _StubClient([_detail("rejected", id="999", filledQuantity=0,
                                  rejectReason="INVALID_PRICE")])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["force_replan"] is True
    assert "ABW" in summary["force_replan_symbols"]


def test_monitor_expiration_sets_force_replan(tmp_path, notify_spy) -> None:
    """Order going open → expired sets force_replan=True (should retry)."""
    _ledger_with(tmp_path, [
        {"symbol": "TLG", "side": "NB", "quantity": 100, "price": 53500,
         "status": "open", "dnseOrderId": "888"},
    ])
    client = _StubClient([_detail("expired", id="888", filledQuantity=0)])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["force_replan"] is True
    assert "TLG" in summary["force_replan_symbols"]


def test_monitor_fill_does_not_set_force_replan(tmp_path, notify_spy) -> None:
    """Order going open → filled does NOT set force_replan (already done)."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "quantity": 100, "price": 17700,
         "status": "open", "dnseOrderId": "133161"},
    ])
    client = _StubClient([_detail("filled", id="133161", filledQuantity=100)])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["force_replan"] is False
    assert summary["force_replan_symbols"] == []


def test_monitor_partial_fill_then_cancellation_sets_force_replan(tmp_path, notify_spy) -> None:
    """Order filled partially then cancelled: filledQuantity > 0 → no force_replan.

    If a partial fill was followed by cancellation, the strategy already
    received some shares. Re-planning could double-buy. Don't force_replan
    in that case; the operator should re-evaluate the position manually.
    """
    _ledger_with(tmp_path, [
        {"symbol": "BAF", "side": "NB", "quantity": 200, "price": 30950,
         "status": "open", "dnseOrderId": "156661"},
    ])
    client = _StubClient([_detail("cancelled", id="156661", filledQuantity=100,
                                  canceledQuantity=100, leaveQuantity=0)])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["force_replan"] is False  # partial fill → no retry


def test_monitor_cancellation_collects_multiple_symbols(tmp_path, notify_spy) -> None:
    """Multiple cancellations in the same cycle all set force_replan=True
    and are accumulated in force_replan_symbols."""
    _ledger_with(tmp_path, [
        {"symbol": "BAF", "side": "NB", "quantity": 200, "price": 30950,
         "status": "open", "dnseOrderId": "111"},
        {"symbol": "ABW", "side": "NB", "quantity": 300, "price": 14100,
         "status": "open", "dnseOrderId": "222"},
    ])
    client = _StubClient([
        _detail("cancelled", id="111", filledQuantity=0),
        _detail("cancelled", id="222", filledQuantity=0),
    ])
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["force_replan"] is True
    assert sorted(summary["force_replan_symbols"]) == ["ABW", "BAF"]
    assert summary["terminal_transitions"] == 2


def test_monitor_no_change_does_not_set_force_replan(tmp_path, notify_spy) -> None:
    """If status is unchanged (still open), no force_replan even with open order."""
    _ledger_with(tmp_path, [
        {"symbol": "ABB", "side": "NB", "status": "open", "dnseOrderId": "1"},
    ])
    client = _StubClient([_detail("open", id="1")])  # still open
    summary = _monitor_order_statuses(client, "A", "tok", tmp_path, throttle_seconds=0)
    assert summary["force_replan"] is False
    assert summary["force_replan_symbols"] == []

