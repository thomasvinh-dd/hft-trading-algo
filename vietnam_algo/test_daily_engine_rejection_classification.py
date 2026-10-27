"""Tests for ``vietnam_algo.libs.daily_engine.execute_orders_with_ledger``.

Focuses on the 2026-10-13 fix: a 4xx HTTP rejection from DNSE must
be marked ``rejected`` (terminal) on the ledger, not ``unknown``
(unsafe). Otherwise the permanent rejection stays in the unsafe
ledger set and ``unsafe_existing_orders`` blocks all subsequent
submits for the same ``run_id`` forever.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.daily_engine import execute_orders_with_ledger
from vietnam_algo.libs.order_ledger import LedgerOrder, save_ledger


def _make_ledger(tmp_path: Path) -> Path:
    """Create a minimal empty ledger at ``tmp_path/live/order_ledger.json``."""
    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    save_ledger({}, tmp_path)
    return tmp_path


def _make_order(symbol: str, side: str, quantity: int, price: int,
                source_order_id: int, reason: str = "test"):
    from vietnam_algo.libs.trading_engine import DnseOrder
    return DnseOrder(
        symbol=symbol, side=side, quantity=quantity, price=price,
        loanPackageId=1036, orderType="LO",
        sourceOrderId=source_order_id, sourceDate="2026-10-13",
        reason=reason,
    )


def test_4xx_error_marks_ledger_as_rejected_not_unknown(tmp_path) -> None:
    """A 4xx HTTP error (e.g. TRADE_QUANTITY_NOT_ENOUGH) → status=rejected.

    This is the 2026-10-13 fix: a permanent rejection must be
    terminal so ``unsafe_existing_orders`` doesn't keep blocking
    future submits for the same run_id.
    """
    _make_ledger(tmp_path)
    client = MagicMock()
    client.place_stock_order.side_effect = RuntimeError(
        'DNSE POST /accounts/0003937868/orders -> HTTP 400 '
        '{"status":400,"code":"TRADE_QUANTITY_NOT_ENOUGH","message":"deal not found"}'
    )

    order = _make_order("BAF", "NS", 200, 27900, source_order_id=12345,
                        reason="autofin_sl:deal#1785888000:BAF:27900")
    results, _reconciliation, _complete = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-13",
    )

    # The result is terminal ("rejected"), not "unknown".
    assert len(results) == 1
    assert results[0]["status"] == "rejected"
    assert "HTTP 400" in results[0]["error"]

    # The ledger row is also "rejected" (terminal), not "unknown" (unsafe).
    ledger_path = tmp_path / "live" / "order_ledger.json"
    payload = json.loads(ledger_path.read_text())
    assert payload["items"][0]["status"] == "rejected"
    assert payload["items"][0]["error"]


def test_5xx_error_marks_ledger_as_unknown_for_retry(tmp_path) -> None:
    """A 5xx / network error → status=unknown (retry-eligible, unsafe).

    The previous bot run also marked these as "unknown", but the
    difference is: 5xx means the failure is transient and the order
    MIGHT succeed on retry, so we keep it in the unsafe set until
    the operator decides what to do. 4xx is permanent.
    """
    _make_ledger(tmp_path)
    client = MagicMock()
    client.place_stock_order.side_effect = RuntimeError(
        'DNSE POST /accounts/0003937868/orders -> HTTP 503 '
        '{"status":503,"code":"INTERNAL_SERVER_ERROR","message":"upstream busy"}'
    )

    order = _make_order("BAF", "NB", 200, 30950, source_order_id=12345)
    results, _reconciliation, _complete = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-13",
    )

    assert results[0]["status"] == "unknown"

    ledger_path = tmp_path / "live" / "order_ledger.json"
    payload = json.loads(ledger_path.read_text())
    assert payload["items"][0]["status"] == "unknown"


def test_4xx_401_403_404_422_also_marked_as_rejected(tmp_path) -> None:
    """All 4xx codes (401, 403, 404, 422) → rejected.

    These are permanent client errors. DNSE will keep returning
    the same response for the same payload, so retrying is futile.
    """
    for code in ("401", "403", "404", "422"):
        _make_ledger(tmp_path)
        client = MagicMock()
        client.place_stock_order.side_effect = RuntimeError(
            f'DNSE POST /accounts/0003937868/orders -> HTTP {code} '
            f'{{"status":{code},"code":"BAD_REQUEST","message":"x"}}'
        )

        order = _make_order("BAF", "NB", 200, 30950, source_order_id=12345)
        results, _r, _c = execute_orders_with_ledger(
            client=client, account_no="0003937868", trading_token="tok",
            orders=[order], data_dir=tmp_path, run_id="2026-10-13",
        )
        assert results[0]["status"] == "rejected", f"HTTP {code} should be rejected"


def test_4xx_rejection_unblocks_subsequent_submits(tmp_path) -> None:
    """After a 4xx rejection, the next call can submit new orders.

    The bug we're fixing: a 4xx rejection marked as "unknown" left
    the order in the unsafe set, so ``unsafe_existing_orders`` would
    return non-empty and ``execute_orders_with_ledger`` would
    short-circuit with ``return [], [...], False``. After the fix,
    the 4xx rejection is "rejected" (terminal) and the next call
    can proceed.
    """
    _make_ledger(tmp_path)
    client = MagicMock()
    # First call: 4xx rejection
    client.place_stock_order.side_effect = RuntimeError(
        'DNSE POST /accounts/0003937868/orders -> HTTP 400 '
        '{"status":400,"code":"TRADE_QUANTITY_NOT_ENOUGH","message":"deal not found"}'
    )
    order1 = _make_order("BAF", "NS", 200, 27900, source_order_id=11111)
    results1, _r1, complete1 = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order1], data_dir=tmp_path, run_id="2026-10-13",
    )
    assert results1[0]["status"] == "rejected"

    # Second call: success (use a different order id so the
    # sourceOrderId doesn't collide)
    client.place_stock_order.side_effect = None
    client.place_stock_order.return_value = {"id": "999"}
    client.order_detail.return_value = {"id": "999", "orderStatus": "New"}
    order2 = _make_order("BAF", "NB", 200, 31200, source_order_id=22222)
    results2, _r2, complete2 = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order2], data_dir=tmp_path, run_id="2026-10-13",
    )
    # Not blocked by the previous rejection.
    assert len(results2) == 1
    assert results2[0]["status"] == "submitted"


def test_existing_4xx_ledger_entry_does_not_block_new_submit(tmp_path) -> None:
    """Pre-existing 4xx-rejected orders don't block new submits.

    Simulates the 2026-10-13 case: an unknown/rejected order from
    yesterday's run sits in the ledger with status=rejected. The
    next run's ``unsafe_existing_orders`` must return empty (or
    only contain truly unsafe items), so the new order can submit.
    """
    from vietnam_algo.libs.order_ledger import ledger_key, save_ledger
    _make_ledger(tmp_path)
    # Seed a rejected order (simulating a previous 4xx).
    key = ledger_key("2026-10-13", 99999, "BAF", "NS")
    rejected = LedgerOrder(
        runId="2026-10-13", sourceOrderId=99999, sourceDate="2026-10-13",
        symbol="BAF", side="NS", quantity=200, price=27900,
        reason="autofin_sl:deal#1785888000:BAF:27900",
        status="rejected",  # terminal, not unsafe
        error='HTTP 400 TRADE_QUANTITY_NOT_ENOUGH',
        createdAt="2026-10-13T10:00:00Z",
        updatedAt="2026-10-13T10:00:00Z",
    )
    save_ledger({key: rejected}, tmp_path)

    # Now submit a new order. Should succeed (not blocked).
    client = MagicMock()
    client.place_stock_order.return_value = {"id": "1234"}
    client.order_detail.return_value = {"id": "1234", "orderStatus": "New"}
    order = _make_order("BAF", "NB", 200, 31200, source_order_id=12345)
    results, _r, _c = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-13",
    )
    assert len(results) == 1
    assert results[0]["status"] == "submitted"
