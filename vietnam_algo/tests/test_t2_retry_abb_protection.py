"""Tests for the 2026-10-29 ABB incident fixes in the T+2 SL retry.

The ABB incident
----------------
The ABB deal ``1789344000:ABB`` (bought 2026-10-13) had THREE BUY rows
in the ledger and THREE SL rows:

  BUY  100 @ 17,500  status=expired    (2026-10-12 08:05)
  BUY  100 @ 17,700  status=filled     (2026-10-13 03:11)  <- the real fill
  BUY  500 @ 17,600  status=cancelled  (2026-10-13 07:06)

  SL  100 @ 15,750  status=cancelled
  SL  100 @ 15,930  status=cancelled
  SL  500 @ 15,840  status=rejected   <- the only scannable row

Three bugs meant the ABB position (100 shares @ 17,700) sat with NO
protective stop for 16 days:

1. ``buy_by_did`` kept the LATEST BUY per deal — the CANCELLED
   500-share one — so the filled 100-share BUY was never used.

2. The rejected SL row asks for 500 shares, so
   ``_buy_filled_and_matches_sl(filled_buy, 500)`` was False and the
   retry skipped the deal entirely.

3. The surviving SL row's LEVEL (15,840 = 10 % below the cancelled
   17,600) did not match the shares we actually own (bought at
   17,700 → 10 % = 15,930).

These tests cover the fixes:
  - the filled BUY is preferred over the latest BUY,
  - a mis-sized SL row is clamped to the held quantity,
  - the stop LEVEL is recomputed from the filled BUY,
  - the "deliberately liquidated" case is STILL skipped.
"""

from __future__ import annotations

import dataclasses
import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.bots.runner import (
    _hose_price_tick,
    _sl_pct_from_reason,
    _sl_stop_level_from_reason,
    retry_t2_settlement_sl_orders,
)
from vietnam_algo.libs.order_ledger import LedgerOrder, save_ledger


BUY_DATE = "2026-10-13"
DEAL = "1789344000:ABB"


def _abb_ledger() -> list[LedgerOrder]:
    """The exact ABB ledger shape: 3 BUYs, 3 SLs."""
    return [
        # BUY 1 — expired
        LedgerOrder(
            runId="2026-10-12", sourceOrderId=1001, sourceDate="2026-10-12",
            symbol="ABB", side="NB", quantity=100, price=17500,
            reason=f"autofin_buy:deal#{DEAL}:nav30pct:Khuyến nghị đầu tư",
            status="expired", createdAt="2026-10-12T08:05:56Z",
            updatedAt="2026-10-12T08:05:56Z",
            lastDetail={"filledQuantity": 0, "orderStatus": "Cancelled"},
        ),
        # BUY 2 — FILLED (the real fill)
        LedgerOrder(
            runId=BUY_DATE, sourceOrderId=1002, sourceDate=BUY_DATE,
            symbol="ABB", side="NB", quantity=100, price=17700,
            reason=f"autofin_buy:deal#{DEAL}:nav30pct:Khuyến nghị đầu tư",
            status="filled", dnseOrderId="133161",
            createdAt="2026-10-13T03:11:23Z", updatedAt="2026-10-13T03:11:23Z",
            lastDetail={"filledQuantity": 100, "orderStatus": "Filled"},
        ),
        # BUY 3 — cancelled, LARGER qty (the one the old code picked)
        LedgerOrder(
            runId=BUY_DATE, sourceOrderId=1003, sourceDate=BUY_DATE,
            symbol="ABB", side="NB", quantity=500, price=17600,
            reason=f"autofin_buy:deal#{DEAL}:nav30pct:Khuyến nghị đầu tư",
            status="cancelled", dnseOrderId="277951",
            createdAt="2026-10-13T07:06:19Z", updatedAt="2026-10-13T07:06:19Z",
            lastDetail={"filledQuantity": 0, "orderStatus": "Cancelled"},
        ),
        # SL 1 — cancelled (not scannable)
        LedgerOrder(
            runId="2026-10-12", sourceOrderId=2001, sourceDate="2026-10-12",
            symbol="ABB", side="NS", quantity=100, price=15750,
            reason=f"autofin_user_sl:deal#{DEAL}:ABB:10%:15750",
            status="cancelled",
            error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
            createdAt="2026-10-12T08:05:56Z", updatedAt="2026-10-12T08:05:56Z",
        ),
        # SL 2 — cancelled (not scannable)
        LedgerOrder(
            runId=BUY_DATE, sourceOrderId=2002, sourceDate=BUY_DATE,
            symbol="ABB", side="NS", quantity=100, price=15930,
            reason=f"autofin_user_sl:deal#{DEAL}:ABB:10%:15930",
            status="cancelled",
            error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
            createdAt="2026-10-13T03:11:22Z", updatedAt="2026-10-13T03:11:22Z",
        ),
        # SL 3 — REJECTED (the only scannable row), mis-sized at 500
        LedgerOrder(
            runId=BUY_DATE, sourceOrderId=2003, sourceDate=BUY_DATE,
            symbol="ABB", side="NS", quantity=500, price=15840,
            reason=f"autofin_user_sl:deal#{DEAL}:ABB:10%:15840",
            status="rejected",
            error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
            createdAt="2026-10-13T06:58:44Z", updatedAt="2026-10-13T06:58:44Z",
        ),
    ]


def _seed(tmp_path: Path, rows: list[LedgerOrder]) -> None:
    save_ledger(
        {f"{i.runId}:{i.sourceOrderId}:{i.symbol}:{i.side}": i for i in rows},
        tmp_path,
    )


def _client(positions: list[dict]) -> MagicMock:
    client = MagicMock()
    client.get_cash_loan_package_id.return_value = 1036
    # A plain dict — the ledger serialises the order detail to JSON,
    # and a MagicMock is not serialisable.
    client.place_stock_order.return_value = {"id": "dnse-new-sl"}
    client.order_detail.return_value = {
        "id": "dnse-new-sl", "orderStatus": "New", "filledQuantity": 0,
    }
    client.get_positions.return_value = {"positions": positions}
    return client


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def test_sl_pct_from_reason_reads_the_percentage() -> None:
    assert _sl_pct_from_reason(
        "autofin_user_sl:deal#1789344000:ABB:ABB:10%:15930"
    ) == 0.10
    assert _sl_pct_from_reason(
        "autofin_user_sl:deal#1792713600:ORS:10%:14400 "
        "[session=ATO signal_price=14400]"
    ) == 0.10


def test_sl_pct_from_reason_returns_none_without_a_percentage() -> None:
    # autofin's OWN declared SL carries a level, not a percentage —
    # it must never be recomputed from the fill.
    assert _sl_pct_from_reason("autofin_sl:deal#1792627200:VOS:10550") is None
    assert _sl_pct_from_reason("autofin_sl:deal#1792627200:TCB:20400") is None
    assert _sl_pct_from_reason("") is None


# -----------------------------------------------------------------------------
# The ABB retry
# -----------------------------------------------------------------------------

def test_abb_retry_places_a_protective_stop(tmp_path) -> None:
    """The ABB position (100 shares) must get a protective STOP."""
    _seed(tmp_path, _abb_ledger())
    client = _client([{"symbol": "ABB", "openQuantity": 100}])

    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-29",
        now_time=datetime.time(14, 30),
    )

    assert summary["t2_due"] == 1, summary
    assert summary["resubmitted"] == 1, summary
    assert summary["resubmit_dnse_ids"] == ["dnse-new-sl"], summary

    # The submitted order must protect exactly what we hold.
    orders = client.place_stock_order.call_args_list
    assert len(orders) == 1
    kw = orders[0].kwargs
    assert kw["quantity"] == 100, (
        f"the stop must cover the 100 shares we hold, got {kw['quantity']}"
    )
    # ...at the level 10 % below the ACTUAL fill (17,700 → 15,930).
    assert kw["stop_price"] == 15930, (
        f"stop level must be recomputed from the filled BUY, got "
        f"{kw['stopPrice']}"
    )
    # A native STOP order, not a marketable limit sell.
    assert kw["order_category"] == "STOP"
    assert kw["condition_operator"] == "<="
    assert kw["duration_type"] == "GTD"
    # The generated limit must be > 0 and tick-aligned, and BELOW the
    # stop so it is marketable when the trigger fires.
    limit = int(kw["price"])
    assert limit > 0
    assert limit < 15930
    assert limit == _hose_price_tick(limit)
    assert limit % 50 == 0, "HOSE tick for 10k-50k VND is 50"


def test_abb_retry_clamps_and_recomputes(tmp_path) -> None:
    """The mis-sized SL row is clamped and its level recomputed."""
    _seed(tmp_path, _abb_ledger())
    client = _client([{"symbol": "ABB", "openQuantity": 100}])

    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-29",
        now_time=datetime.time(14, 30),
    )

    clamped = summary.get("clamped_sl_qty", [])
    assert clamped, "the mis-sized SL should be recorded as clamped"
    assert "ABB" in clamped[0]
    assert "500" in clamped[0] and "100" in clamped[0]

    recomputed = summary.get("recomputed_stop_level", [])
    assert recomputed, "the stop level should be recorded as recomputed"
    assert "15,930" in recomputed[0]


def test_deliberately_liquidated_position_is_still_skipped(tmp_path) -> None:
    """The 2026-10-23 guard must survive.

    When the BUY filled for MORE than we now hold, the position was
    reduced on purpose — re-submitting a partially-fillable SL would
    fail with TRADE_QUANTITY_NOT_ENOUGH forever.
    """
    rows = []
    for r in _abb_ledger():
        if r.side == "NB" and r.status == "filled":
            # The BUY filled for 500 ...
            r = dataclasses.replace(
                r,
                quantity=500,
                price=17600,
                lastDetail={"filledQuantity": 500, "orderStatus": "Filled"},
            )
        rows.append(r)
    _seed(tmp_path, rows)
    client = _client([{"symbol": "ABB", "openQuantity": 100}])  # ... we hold 100

    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-29",
        now_time=datetime.time(14, 30),
    )

    assert summary["t2_due"] == 0, summary
    assert summary["stale_no_position"] == 1, summary
    assert summary["resubmitted"] == 0, summary
    client.place_stock_order.assert_not_called()


def test_fully_closed_position_is_skipped(tmp_path) -> None:
    """Holding nothing → the SL is dead, no retry."""
    _seed(tmp_path, _abb_ledger())
    client = _client([{"symbol": "ABB", "openQuantity": 0}])

    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-29",
        now_time=datetime.time(14, 30),
    )
    assert summary["resubmitted"] == 0
    assert summary["stale_no_position"] >= 1
    client.place_stock_order.assert_not_called()


def test_filled_buy_is_preferred_over_the_latest_buy(tmp_path) -> None:
    """The filled BUY wins over a later CANCELLED BUY for the same deal."""
    _seed(tmp_path, _abb_ledger())
    client = _client([{"symbol": "ABB", "openQuantity": 100}])

    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-29",
        now_time=datetime.time(14, 30),
    )
    # If the CANCELLED 500-share BUY had been used, the settlement
    # date check would still pass (same date) but the quantity check
    # would fail → no retry at all.
    assert summary["resubmitted"] == 1, summary
    kw = client.place_stock_order.call_args_list[0].kwargs
    assert kw["quantity"] == 100
    assert kw["stop_price"] == 15930


# -----------------------------------------------------------------------------
# The reconcile function must poll with the order's own category
# -----------------------------------------------------------------------------

def test_reconcile_polls_stop_orders_with_the_stop_category(tmp_path) -> None:
    """``reconcile_ledger_orders`` must pass ``orderCategory=STOP``.

    A STOP order cannot be resolved with the default NORMAL category —
    DNSE answers HTTP 404 — and a failed reconcile forces the row back
    to ``unknown``, which is an AMBIGUOUS status that blocks the WHOLE
    submit run. That is what stopped the ABB stop from being placed
    while two perfectly healthy STOP orders (TCB, VOS) sat in the
    ledger.
    """
    from vietnam_algo.libs.daily_engine import reconcile_ledger_orders
    from vietnam_algo.libs.order_ledger import LedgerOrder

    row = LedgerOrder(
        runId="2026-10-29", sourceOrderId=12345, sourceDate="2026-10-29",
        symbol="VOS", side="NS", quantity=300, price=10450,
        reason="autofin_sl:deal#1792627200:VOS:10550",
        status="unknown", dnseOrderId="stop-order-id",
        createdAt="2026-10-29T02:01:17Z", updatedAt="2026-10-29T02:01:17Z",
        orderCategory="STOP", stopPrice=10550,
    )
    ledger = {"k": row}
    client = MagicMock()
    client.order_detail.return_value = {"id": "stop-order-id", "orderStatus": "New"}

    out, updates = reconcile_ledger_orders(
        client, "0003937868", ledger, trading_token="tok",
    )

    # The poll MUST have used the row's own category.
    kwargs = client.order_detail.call_args_list[0].kwargs
    assert kwargs["order_category"] == "STOP", (
        "the reconcile must poll with orderCategory=STOP, got "
        f"{kwargs.get('order_category')!r}"
    )
    # ...and the row must come back as a working order, not "unknown".
    assert out["k"].status == "open", out["k"].status
    assert updates[0]["status"] == "open"


def test_reconcile_keeps_the_category_on_the_error_path(tmp_path) -> None:
    """A failed poll must not drop the row's orderCategory."""
    from vietnam_algo.libs.daily_engine import reconcile_ledger_orders
    from vietnam_algo.libs.order_ledger import LedgerOrder

    row = LedgerOrder(
        runId="2026-10-29", sourceOrderId=12345, sourceDate="2026-10-29",
        symbol="VOS", side="NS", quantity=300, price=10450,
        reason="autofin_sl:deal#1792627200:VOS:10550",
        status="unknown", dnseOrderId="stop-order-id",
        createdAt="2026-10-29T02:01:17Z", updatedAt="2026-10-29T02:01:17Z",
        orderCategory="STOP", stopPrice=10550,
    )
    client = MagicMock()
    client.order_detail.side_effect = RuntimeError("HTTP 404")

    out, _ = reconcile_ledger_orders(
        client, "0003937868", {"k": row}, trading_token="tok",
    )
    assert out["k"].orderCategory == "STOP"
    assert out["k"].stopPrice == 10550


# -----------------------------------------------------------------------------
# The submit path must persist the STOP fields
# -----------------------------------------------------------------------------

def test_submit_persists_the_stop_category(tmp_path) -> None:
    """A submitted STOP order must be recorded with its category.

    Without ``orderCategory=STOP`` in the ledger row, the order monitor
    polls the order as NORMAL — DNSE answers HTTP 404 for a STOP order
    requested that way — and the row is forced back to the ambiguous
    "unknown" status, which then blocks every subsequent submit run.
    """
    from vietnam_algo.libs.daily_engine import execute_orders_with_ledger
    from vietnam_algo.libs.order_ledger import load_ledger
    from vietnam_algo.libs.trading_engine import DnseOrder

    client = MagicMock()
    client.place_stock_order.return_value = {"id": "dnse-stop-1"}
    client.order_detail.return_value = {"id": "dnse-stop-1", "orderStatus": "New"}
    client.get_cash_loan_package_id.return_value = 1036

    order = DnseOrder(
        symbol="ABB", side="NS", quantity=100, price=15850,
        loanPackageId=1036, orderType="LO",
        orderCategory="STOP", stopPrice=15930,
        conditionOperator="<=", durationType="GTD",
        durationDateTime="2026-11-28T13:27:24.369+07:00",
        sourceOrderId=46993485424118, sourceDate="2026-10-29",
        reason="autofin_user_sl:deal#1789344000:ABB:10%:15840 [t2-retry]",
    )
    execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-29",
        strategy="autofin_signal",
    )

    led = load_ledger(tmp_path)
    row = next(
        it for it in led.values()
        if it.symbol == "ABB" and it.side == "NS"
    )
    assert row.orderCategory == "STOP", (
        f"the ledger must record orderCategory=STOP, got "
        f"{row.orderCategory!r}"
    )
    assert row.stopPrice == 15930
    assert row.dnseOrderId == "dnse-stop-1"
