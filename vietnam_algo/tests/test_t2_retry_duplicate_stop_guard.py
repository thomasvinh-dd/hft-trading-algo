"""Regression test: the T+2 retry must not stack duplicate STOPs.

The duplicate-STOP incident
---------------------------
The retry's own dedup is a NO-OP across days.

``retry_t2_settlement_sl_orders`` generates a fresh ``sourceOrderId``
for every retried order::

    sha1(f"t2-retry|{today_iso}|{symbol}|{stop_level}|{qty}|{base_reason}")

Because ``today_iso`` is part of the hash, the id is DIFFERENT on
every calendar day. The ledger dedup key is
``run_id:sourceOrderId:symbol:side``, so tomorrow's retry produces a
key that matches nothing, and the dedup in
``execute_orders_with_ledger`` never fires.

Meanwhile the PARENT SL row stays ``status=rejected`` forever — the
retry never marks it as consumed — so it is re-selected on every
pass.

Net effect: without an explicit guard, each daily T+2 retry pass
places ANOTHER protective STOP on a position that is already
protected. On 2026-10-29 the retry placed the ABB stop (100 shares
@ 15,930) while the parent row
``autofin_user_sl:deal#1789344000:ABB:10%:15840`` (500 shares,
status=rejected) remained scannable — so the 2026-10-30 pass would
have placed a second STOP for the same 100 shares.

These tests cover the guard:
  - a deal whose stop is already working is skipped,
  - a deal with PARTIAL protection tops up only the remainder,
  - a stop on a DIFFERENT deal of the same symbol does not block,
  - a dead/terminal stop row does not block the retry.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.bots.runner import retry_t2_settlement_sl_orders
from vietnam_algo.libs.order_ledger import LedgerOrder, save_ledger


BUY_DATE = "2026-10-13"
DEAL = "1789344000:ABB"


def _seed(tmp_path: Path, rows: list[LedgerOrder]) -> None:
    save_ledger(
        {f"{i.runId}:{i.sourceOrderId}:{i.symbol}:{i.side}": i for i in rows},
        tmp_path,
    )


def _client(positions: list[dict]) -> MagicMock:
    client = MagicMock()
    client.get_cash_loan_package_id.return_value = 1036
    client.place_stock_order.return_value = {"id": "dnse-new-sl"}
    client.order_detail.return_value = {
        "id": "dnse-new-sl", "orderStatus": "New", "filledQuantity": 0,
    }
    client.get_positions.return_value = {"positions": positions}
    return client


def _buy_row() -> LedgerOrder:
    return LedgerOrder(
        runId=BUY_DATE, sourceOrderId=1002, sourceDate=BUY_DATE,
        symbol="ABB", side="NB", quantity=100, price=17700,
        reason=f"autofin_buy:deal#{DEAL}:nav30pct:Khuyến nghị đầu tư",
        status="filled", dnseOrderId="133161",
        createdAt="2026-10-13T03:11:23Z", updatedAt="2026-10-13T03:11:23Z",
        lastDetail={"filledQuantity": 100, "orderStatus": "Filled"},
    )


def _rejected_parent(qty: int = 500) -> LedgerOrder:
    """The still-scannable parent SL row (the retry's candidate)."""
    return LedgerOrder(
        runId=BUY_DATE, sourceOrderId=2003, sourceDate=BUY_DATE,
        symbol="ABB", side="NS", quantity=qty, price=15840,
        reason=f"autofin_user_sl:deal#{DEAL}:ABB:10%:15840",
        status="rejected",
        error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
        createdAt="2026-10-13T06:58:44Z", updatedAt="2026-10-13T06:58:44Z",
    )


def _working_stop(
    qty: int = 100,
    *,
    symbol: str = "ABB",
    did: str = DEAL,
    status: str = "open",
) -> LedgerOrder:
    """A protective STOP that is already working at the broker."""
    return LedgerOrder(
        runId="2026-10-29", sourceOrderId=46993485424118,
        sourceDate="2026-10-29",
        symbol=symbol, side="NS", quantity=qty, price=15850,
        reason=f"autofin_user_sl:deal#{did}:{symbol}:10%:15840 [t2-retry from 2026-10-13]",
        status=status, orderCategory="STOP", stopPrice=15930,
        dnseOrderId="dbheejb6tmgc73bdauj0",
        createdAt="2026-10-29T06:27:24Z", updatedAt="2026-10-29T06:27:24Z",
    )


def _run(tmp_path: Path, client: MagicMock, today_iso: str = "2026-10-30"):
    return retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso=today_iso,
        now_time=datetime.time(9, 0),
    )


# -----------------------------------------------------------------------------
# The guard
# -----------------------------------------------------------------------------

def test_already_protected_deal_is_not_retried(tmp_path) -> None:
    """A deal with a working STOP must not get a second one."""
    _seed(tmp_path, [_buy_row(), _rejected_parent(), _working_stop()])
    client = _client([{"symbol": "ABB", "openQuantity": 100}])

    summary = _run(tmp_path, client)

    assert summary["resubmitted"] == 0, summary
    client.place_stock_order.assert_not_called()
    notes = summary.get("already_protected", [])
    assert notes, "the skip must be recorded in the summary"
    assert "ABB" in notes[0]


def test_todays_own_stop_does_not_block_the_first_retry(tmp_path) -> None:
    """The guard must NOT block the FIRST retry for a deal.

    On the day the stop is placed there is no working stop row yet,
    so the retry must go through. This is the regression that keeps
    the guard from disabling protection entirely.
    """
    _seed(tmp_path, [_buy_row(), _rejected_parent()])
    client = _client([{"symbol": "ABB", "openQuantity": 100}])

    summary = _run(tmp_path, client, today_iso="2026-10-29")

    assert summary["resubmitted"] == 1, summary
    client.place_stock_order.assert_called_once()


def test_partial_protection_tops_up_only_the_remainder(tmp_path) -> None:
    """If only part of the position is protected, top up the rest."""
    _seed(tmp_path, [_buy_row(), _rejected_parent(), _working_stop(qty=40)])
    client = _client([{"symbol": "ABB", "openQuantity": 100}])

    summary = _run(tmp_path, client)

    assert summary["resubmitted"] == 1, summary
    kw = client.place_stock_order.call_args_list[0].kwargs
    assert kw["quantity"] == 60, (
        f"only the unprotected 60 shares should be stopped, got "
        f"{kw['quantity']}"
    )
    notes = summary.get("partially_protected", [])
    assert notes, "the top-up must be recorded in the summary"


def test_stop_for_a_different_deal_does_not_block(tmp_path) -> None:
    """Protection is per DEAL, not per symbol.

    Two autofin deals in the same symbol are two separate positions;
    a stop on one must not suppress the other.
    """
    other_stop = _working_stop(did="1799999999:ABB")
    _seed(tmp_path, [_buy_row(), _rejected_parent(), other_stop])
    client = _client([{"symbol": "ABB", "openQuantity": 100}])

    summary = _run(tmp_path, client)

    assert summary["resubmitted"] == 1, summary
    client.place_stock_order.assert_called_once()


def test_terminal_stop_row_does_not_block(tmp_path) -> None:
    """A stop that was cancelled/rejected does NOT protect anything."""
    dead = _working_stop(status="cancelled")
    _seed(tmp_path, [_buy_row(), _rejected_parent(), dead])
    client = _client([{"symbol": "ABB", "openQuantity": 100}])

    summary = _run(tmp_path, client)

    assert summary["resubmitted"] == 1, summary
    client.place_stock_order.assert_called_once()


def test_ambiguous_stop_row_does_not_block(tmp_path) -> None:
    """An 'unknown' stop row must NOT be treated as protection.

    "unknown" means the broker state could not be read — the order
    may or may not be live. Suppressing the retry on that basis could
    leave a position unprotected, which is worse than a duplicate.
    """
    unknown = _working_stop(status="unknown")
    _seed(tmp_path, [_buy_row(), _rejected_parent(), unknown])
    client = _client([{"symbol": "ABB", "openQuantity": 100}])

    summary = _run(tmp_path, client)

    assert summary["resubmitted"] == 1, summary
    client.place_stock_order.assert_called_once()
