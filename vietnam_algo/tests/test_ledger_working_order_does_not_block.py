"""Regression test: a resting stop-loss must not block other orders.

2026-10-28. The retried ORS stop-loss was correctly placed (DNSE
order 216241) and recorded in the ledger with status "open".

``execute_orders_with_ledger`` calls ``unsafe_existing_orders`` BEFORE
submitting anything, and if that returns a non-empty list it
short-circuits the ENTIRE run:

    unsafe = unsafe_existing_orders(ledger, run_id)
    if unsafe:
        return [], [...], False

With "open" in UNSAFE_LEDGER_STATUSES, one resting stop-loss therefore
prevented every other order from being submitted — the MSB entry
planned at 11:02 ICT could not go through, and the log showed
``execute_orders_with_ledger returned 0 results: []`` on every cycle.

The fix: only AMBIGUOUS statuses ("prepared" / "submitting" /
"unknown") block the run. A CONFIRMED working order ("submitted" /
"open" / "partial") is skipped per-order by the existing dedup and
lets the rest of the run through.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.daily_engine import execute_orders_with_ledger  # noqa: E402
from vietnam_algo.libs.order_ledger import (  # noqa: E402
    AMBIGUOUS_LEDGER_STATUSES,
    UNSAFE_LEDGER_STATUSES,
    LedgerOrder,
    load_ledger,
    save_ledger,
    unsafe_existing_orders,
)


def _make_order(symbol: str, side: str, qty: int, price: int, source_order_id: int):
    from vietnam_algo.libs.trading_engine import DnseOrder

    return DnseOrder(
        symbol=symbol, side=side, quantity=qty, price=price,
        loanPackageId=1775, orderType="LO",
        sourceOrderId=source_order_id, sourceDate="2026-10-28",
        reason=f"autofin_buy:deal#1:{symbol}:test",
    )


def _seed_working_sl(tmp_path: Path) -> None:
    """A confirmed live stop-loss (status 'open') for today."""
    save_ledger({
        "2026-10-28:999:ORS:NS": LedgerOrder(
            runId="2026-10-28", sourceOrderId=999, sourceDate="2026-10-28",
            symbol="ORS", side="NS", quantity=200, price=14400,
            reason="autofin_user_sl:deal#1:ORS:10%:14400",
            status="open", dnseOrderId="216241",
            createdAt="2026-10-28T05:12:31Z", updatedAt="2026-10-28T05:12:31Z",
        ),
    }, tmp_path)


def test_open_order_does_not_block_the_submit_run(tmp_path) -> None:
    """A resting stop-loss must not prevent other orders submitting."""
    _seed_working_sl(tmp_path)
    client = MagicMock()
    client.place_stock_order.return_value = {"id": "dnse-msb"}
    client.order_detail.return_value = {"id": "dnse-msb", "orderStatus": "New"}

    order = _make_order("MSB", "NB", 500, 15850, source_order_id=11111)
    results, _recon, _complete = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-28",
    )

    assert len(results) == 1, (
        f"the run was blocked by the resting stop-loss: {results}"
    )
    assert results[0]["status"] == "submitted"
    client.place_stock_order.assert_called()


def test_ambiguous_order_still_blocks_the_run(tmp_path) -> None:
    """An order whose fate we don't know MUST still block the run.

    Re-submitting it could place a duplicate order at DNSE.
    """
    save_ledger({
        "2026-10-28:999:MSB:NB": LedgerOrder(
            runId="2026-10-28", sourceOrderId=999, sourceDate="2026-10-28",
            symbol="MSB", side="NB", quantity=500, price=15850,
            reason="autofin_buy:deal#1:MSB:test",
            status="unknown", dnseOrderId="",
            createdAt="2026-10-28T05:12:31Z", updatedAt="2026-10-28T05:12:31Z",
        ),
    }, tmp_path)
    client = MagicMock()
    client.place_stock_order.return_value = {"id": "dnse-x"}
    client.order_detail.return_value = {"id": "dnse-x", "orderStatus": "New"}

    order = _make_order("AAS", "NB", 100, 10200, source_order_id=11111)
    results, _recon, _complete = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-28",
    )

    assert results == [], (
        f"an 'unknown' order must block the run, got {results}"
    )
    client.place_stock_order.assert_not_called()


def test_ambiguous_set_covers_the_right_statuses() -> None:
    """Only the genuinely-ambiguous statuses may block a run."""
    assert AMBIGUOUS_LEDGER_STATUSES == {"prepared", "submitting", "unknown"}
    # Every ambiguous status must be in the unsafe set.
    assert AMBIGUOUS_LEDGER_STATUSES <= UNSAFE_LEDGER_STATUSES
    # The confirmed-working statuses must NOT block.
    for status in ("submitted", "open", "partial"):
        assert status not in AMBIGUOUS_LEDGER_STATUSES


def test_unsafe_existing_orders_ignores_working_orders(tmp_path) -> None:
    """``unsafe_existing_orders`` must only return ambiguous rows."""
    _seed_working_sl(tmp_path)
    ledger = load_ledger(tmp_path)
    assert unsafe_existing_orders(ledger, "2026-10-28") == []


def test_token_rejected_order_does_not_block_the_run(tmp_path) -> None:
    """A token-rejected order is retryable and must not block the run."""
    save_ledger({
        "2026-10-28:999:MSB:NB": LedgerOrder(
            runId="2026-10-28", sourceOrderId=999, sourceDate="2026-10-28",
            symbol="MSB", side="NB", quantity=500, price=15850,
            reason="autofin_buy:deal#1:MSB:test",
            status="token_rejected", dnseOrderId="",
            error="HTTP 400 INVALID_TRADING_TOKEN",
            createdAt="2026-10-28T05:12:31Z", updatedAt="2026-10-28T05:12:31Z",
        ),
    }, tmp_path)
    client = MagicMock()
    client.place_stock_order.return_value = {"id": "dnse-aas"}
    client.order_detail.return_value = {"id": "dnse-aas", "orderStatus": "New"}

    order = _make_order("AAS", "NB", 100, 10200, source_order_id=11111)
    results, _recon, _complete = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-28",
    )

    assert len(results) == 1, results
    assert results[0]["status"] == "submitted"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
