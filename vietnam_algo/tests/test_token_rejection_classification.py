"""Regression tests for INVALID_TRADING_TOKEN order classification (2026-10-25).

An order rejected with ``INVALID_TRADING_TOKEN`` is NOT a permanent
rejection — the same payload succeeds once a fresh token is used. But
it must ALSO not be allowed to wedge the submit pipeline.

Two failure modes we must avoid:

1. Classified as ``rejected`` (terminal) → the ledger's dedup key
   (run_id, sourceOrderId, symbol, side) matches forever and the
   order is NEVER retried. This is what silently lost the KDC BUY on
   2026-10-22.

2. Classified as ``unknown`` → the row lands in
   ``UNSAFE_LEDGER_STATUSES``, so ``unsafe_existing_orders`` returns
   non-empty and ``execute_orders_with_ledger`` short-circuits with
   ``blocked_by_unsafe_existing_orders`` — blocking EVERY subsequent
   submit for that run, not just the failed order.

The correct behaviour is a dedicated ``token_rejected`` status that is
neither terminal nor unsafe: the order is re-attemptable, and it does
not block other orders.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.daily_engine import execute_orders_with_ledger  # noqa: E402
from vietnam_algo.libs.order_ledger import (  # noqa: E402
    TERMINAL_LEDGER_STATUSES,
    UNSAFE_LEDGER_STATUSES,
    load_ledger,
)


def _make_order(symbol: str, side: str, qty: int, price: int, source_order_id: int):
    from vietnam_algo.libs.trading_engine import DnseOrder

    return DnseOrder(
        symbol=symbol, side=side, quantity=qty, price=price,
        loanPackageId=1775, orderType="LO",
        sourceOrderId=source_order_id, sourceDate="2026-10-25",
        reason=f"autofin_buy:deal#test:{symbol}",
    )


def _client_rejecting_with(code: str) -> MagicMock:
    client = MagicMock()
    client.place_stock_order.side_effect = RuntimeError(
        'DNSE POST /accounts/0003937868/orders -> HTTP 400 '
        f'{{"status":400,"code":"{code}","message":"invalid trading token"}}'
    )
    client.order_detail.return_value = {"id": "x", "orderStatus": "New"}
    return client


def test_token_rejection_is_not_terminal() -> None:
    """``token_rejected`` must NOT be in TERMINAL_LEDGER_STATUSES.

    If it were, the ledger dedup would match forever and the order
    would never be retried.
    """
    assert "token_rejected" not in TERMINAL_LEDGER_STATUSES


def test_token_rejection_is_not_unsafe() -> None:
    """``token_rejected`` must NOT be in UNSAFE_LEDGER_STATUSES.

    If it were, ``unsafe_existing_orders`` would block every
    subsequent submit for the run.
    """
    assert "token_rejected" not in UNSAFE_LEDGER_STATUSES


def test_invalid_trading_token_is_classified_as_token_rejected(tmp_path) -> None:
    """The submit path must classify INVALID_TRADING_TOKEN distinctly."""
    client = _client_rejecting_with("INVALID_TRADING_TOKEN")
    order = _make_order("KDC", "NB", 100, 50900, source_order_id=11111)

    results, _recon, _complete = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-25",
    )

    assert results[0]["status"] == "token_rejected", (
        f"expected token_rejected, got {results[0]['status']}"
    )


def test_other_400_still_classified_as_rejected(tmp_path) -> None:
    """A genuine permanent 4xx must remain terminal ``rejected``."""
    client = _client_rejecting_with("TRADE_QUANTITY_NOT_ENOUGH")
    order = _make_order("BAF", "NS", 200, 27900, source_order_id=11111)

    results, _recon, _complete = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-25",
    )

    assert results[0]["status"] == "rejected"


def test_token_rejection_does_not_block_other_orders(tmp_path) -> None:
    """A token-rejected order must not wedge the whole submit run."""
    client = _client_rejecting_with("INVALID_TRADING_TOKEN")
    order1 = _make_order("KDC", "NB", 100, 50900, source_order_id=11111)
    order2 = _make_order("MSB", "NB", 100, 12000, source_order_id=22222)

    results, _recon, _complete = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order1, order2], data_dir=tmp_path, run_id="2026-10-25",
    )

    statuses = [r["status"] for r in results]
    assert "token_rejected" in statuses
    # The second order must still have been attempted — not blocked.
    assert len(results) == 2, results


def test_token_rejected_order_can_be_retried_with_a_fresh_token(tmp_path) -> None:
    """The core regression: the order must be re-attemptable.

    Scenario: the first attempt is rejected with
    INVALID_TRADING_TOKEN. The cron then refreshes the token. The
    next cycle must actually submit the order — it must NOT be
    skipped as an existing ledger row.
    """
    client = _client_rejecting_with("INVALID_TRADING_TOKEN")
    order = _make_order("KDC", "NB", 100, 50900, source_order_id=11111)

    results1, _r1, _c1 = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="bad",
        orders=[order], data_dir=tmp_path, run_id="2026-10-25",
    )
    assert results1[0]["status"] == "token_rejected"

    # Token refreshed -> the retry must go through.
    client.place_stock_order.side_effect = None
    client.place_stock_order.return_value = {"id": "dnse-kdc-1"}
    client.order_detail.return_value = {
        "id": "dnse-kdc-1", "orderStatus": "New", "fillQuantity": 0,
    }

    results2, _r2, _c2 = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="good",
        orders=[order], data_dir=tmp_path, run_id="2026-10-25",
    )

    assert results2[0]["status"] == "submitted", (
        f"the order must be retried after the token was refreshed, "
        f"got {results2[0]['status']}"
    )
    client.place_stock_order.assert_called()


def test_token_rejection_preserves_error_text(tmp_path) -> None:
    """The DNSE error must remain visible in the ledger for audit."""
    client = _client_rejecting_with("INVALID_TRADING_TOKEN")
    order = _make_order("KDC", "NB", 100, 50900, source_order_id=11111)

    execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-25",
    )

    ledger = load_ledger(tmp_path)
    row = [o for o in ledger.values() if o.symbol == "KDC"][0]
    assert "INVALID_TRADING_TOKEN" in (row.error or "")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
