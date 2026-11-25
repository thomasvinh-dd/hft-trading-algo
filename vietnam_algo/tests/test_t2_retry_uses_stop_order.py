"""Regression tests: protective SLs must be native DNSE STOP orders.

2026-10-28. The T+2 settlement retry placed the ORS stop-loss as a
plain ``NORMAL`` sell ``LO @ 14,400`` (dnseOrderId 216241). A sell
limit at 14,400 against a 15,150 bid is marketable — DNSE crossed it
and filled 200 shares instantly at 15,150. The position was
LIQUIDATED instead of protected, and no protective order ever rested
at the broker (``ORDERS[STOP]: 0``).

Two independent defects had to be fixed:

1. **Wrong order class.** A protective stop must be a native DNSE
   conditional order (``orderCategory="STOP"`` with ``stopPrice`` /
   ``conditionOperator`` / ``durationType`` / ``durationDateTime``).
   A STOP rests at the broker as a trigger; the generated NORMAL
   sell only exists once the market reaches the level. A plain sell
   LO is in the book immediately and can be repriced or crossed.

2. **Wrong level.** The ledger ``price`` is unreliable for a stop
   leg: an auction-planned SL is stored with ``price=0``, and the
   sell-at-market override rewrote TVN's intended 9,360 stop to
   10,150 ("ensures immediate fill at market"). The intended level
   is recoverable only from the reason string.

These tests pin the behaviour end-to-end so the defect cannot come
back silently.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.bots.runner import (  # noqa: E402
    _is_protective_reason,
    _maybe_reprice_stale_order,
    _sl_stop_level_from_reason,
    retry_t2_settlement_sl_orders,
)
from vietnam_algo.libs.order_ledger import (  # noqa: E402
    LedgerOrder,
    load_ledger,
    save_ledger,
)


# -----------------------------------------------------------------------------
# Reason parsing
# -----------------------------------------------------------------------------


def test_stop_level_recovered_from_every_reason_format() -> None:
    """The intended level must come from the reason, not the ledger price."""
    # The user's own stop-loss (``:pct:level`` form).
    assert _sl_stop_level_from_reason(
        "autofin_user_sl:deal#1792713600:TVN:10%:9360"
    ) == 9360
    # Same, with the auction annotation appended.
    assert _sl_stop_level_from_reason(
        "autofin_user_sl:deal#1792713600:ORS:10%:14400 "
        "[session=ATO signal_price=14400]"
    ) == 14400
    # Autofin's own stop (no pct segment) — the old ``:<pct>:<price>``
    # regex missed this one entirely.
    assert _sl_stop_level_from_reason(
        "autofin_sl:deal#1785888000:BAF:27900"
    ) == 27900
    # A BUY reason carries no stop level -> 0 means "not recoverable".
    assert _sl_stop_level_from_reason(
        "autofin_buy:deal#1792713600:TVN:nav15pct:Khuyến nghị đầu tư"
    ) == 0
    assert _sl_stop_level_from_reason("") == 0
    assert _sl_stop_level_from_reason(None) == 0


def test_protective_reasons_are_recognised() -> None:
    assert _is_protective_reason("autofin_sl:deal#1:BAF:27900")
    assert _is_protective_reason("autofin_user_sl:deal#1:ABB:10%:15930")
    # A take-profit is discretionary, not protective.
    assert not _is_protective_reason("autofin_tp:deal#1:BAF:32000")
    assert not _is_protective_reason("autofin_buy:deal#1:BAF:nav30pct:x")
    assert not _is_protective_reason(None)


# -----------------------------------------------------------------------------
# The retry must place a native STOP, never a plain limit sell
# -----------------------------------------------------------------------------


def _seed_t2_sl(tmp_path: Path) -> None:
    """A filled BUY plus its SL rejected by the T+2 settlement block."""
    items = [
        LedgerOrder(
            runId="2026-10-28", sourceOrderId=11111, sourceDate="2026-10-28",
            symbol="TVN", side="NB", quantity=300, price=10400,
            reason="autofin_buy:deal#1792713600:TVN:nav15pct:Khuyến nghị đầu tư",
            status="filled", dnseOrderId="dnse-buy-tvn",
            createdAt="2026-10-28T02:01:08Z", updatedAt="2026-10-28T02:01:08Z",
            lastDetail={"filledQuantity": 300, "orderStatus": "Filled"},
        ),
        LedgerOrder(
            runId="2026-10-28", sourceOrderId=22222, sourceDate="2026-10-28",
            symbol="TVN", side="NS", quantity=300, price=10150,
            # The INTENDED stop is 9,360 (10% below the 10,400 entry).
            # The ledger price says 10,150 because the sell-at-market
            # override rewrote it to "ensure immediate fill at market".
            reason="autofin_user_sl:deal#1792713600:TVN:10%:9360",
            status="rejected",
            error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
            createdAt="2026-10-28T02:01:08Z", updatedAt="2026-10-28T02:01:08Z",
            lastDetail=None,
        ),
    ]
    save_ledger({f"k{i}": o for i, o in enumerate(items)}, tmp_path)


def _client() -> MagicMock:
    client = MagicMock()
    client.get_cash_loan_package_id.return_value = 1775
    client.place_stock_order.return_value = {"id": "dnse-stop-retry"}
    client.order_detail.return_value = {"id": "dnse-stop-retry", "orderStatus": "New"}
    client.get_positions.return_value = {
        "positions": [{"symbol": "TVN", "openQuantity": 300, "tradeQuantity": 0}],
    }
    return client


def test_retry_places_a_native_stop_order(tmp_path) -> None:
    """The retry must submit a STOP at the intended level, not a plain LO."""
    _seed_t2_sl(tmp_path)
    client = _client()
    submitted: list[dict] = []
    client.place_stock_order.side_effect = lambda **kw: (
        submitted.append(kw) or {"id": "dnse-stop-retry"}
    )

    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-30",
        now_time=datetime.time(13, 0),
    )

    assert summary["t2_due"] == 1, summary
    assert summary["resubmitted"] == 1, summary
    assert summary["resubmit_errors"] == [], summary["resubmit_errors"]
    assert len(submitted) == 1, submitted

    order = submitted[0]
    # (a) native conditional order, at the level from the REASON.
    assert order["order_category"] == "STOP", order
    assert order["stop_price"] == 9360, (
        f"the stop level must come from the reason (9,360), got "
        f"{order['stop_price']!r} — the ledger price 10,150 is the "
        f"sell-at-market override, not the stop"
    )
    assert order["condition_operator"] == "<=", order
    assert order["duration_type"] == "GTD", order
    assert order["duration_date_time"], order
    # (b) the generated NORMAL sell needs a real limit (> 0).
    assert order["price"] > 0, order
    assert order["order_type"] == "LO", order


def test_retry_never_submits_a_plain_normal_sell(tmp_path) -> None:
    """Guard: a protective leg must never reach the broker as NORMAL."""
    _seed_t2_sl(tmp_path)
    client = _client()
    submitted: list[dict] = []
    client.place_stock_order.side_effect = lambda **kw: (
        submitted.append(kw) or {"id": "dnse-stop-retry"}
    )

    retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-30",
        now_time=datetime.time(13, 0),
    )

    assert submitted, "the retry submitted nothing"
    for order in submitted:
        assert order["order_category"] == "STOP", (
            f"a protective leg was submitted as a plain "
            f"{order['order_category']} order — it would be marketable "
            f"and liquidate the position: {order}"
        )


def test_stop_survives_a_ledger_round_trip(tmp_path) -> None:
    """``orderCategory``/``stopPrice`` must survive save -> load.

    Without this the monitor polls the STOP with ``orderCategory=NORMAL``
    (the broker cannot resolve it) and the re-pricer treats it as an
    ordinary limit order it may cancel.
    """
    item = LedgerOrder(
        runId="2026-10-30", sourceOrderId=33333, sourceDate="2026-10-30",
        symbol="TVN", side="NS", quantity=300, price=9310,
        reason="autofin_user_sl:deal#1792713600:TVN:10%:9360 [t2-retry from 2026-10-28]",
        status="submitted", dnseOrderId="dnse-stop-1",
        createdAt="2026-10-30T06:00:00Z", updatedAt="2026-10-30T06:00:00Z",
        orderCategory="STOP", stopPrice=9360,
    )
    save_ledger({"k": item}, tmp_path)

    loaded = load_ledger(tmp_path)
    restored = next(iter(loaded.values()))
    assert restored.orderCategory == "STOP", restored
    assert restored.stopPrice == 9360, restored


def test_repricer_never_touches_a_stop_order(tmp_path) -> None:
    """A re-price moves the stop, and the 3% cap can cancel it."""
    item = LedgerOrder(
        runId="2026-10-30", sourceOrderId=44444, sourceDate="2026-10-30",
        symbol="TVN", side="NS", quantity=300, price=9310,
        reason="autofin_user_sl:deal#1792713600:TVN:10%:9360",
        status="open", dnseOrderId="dnse-stop-1",
        createdAt="2026-10-30T06:00:00Z", updatedAt="2026-10-30T06:00:00Z",
        orderCategory="STOP", stopPrice=9360,
    )
    client = MagicMock()

    summary = _maybe_reprice_stale_order(
        client, "0003937868", "tok", item,
        now_epoch=10**10,  # far past any staleness threshold
    )

    assert summary.get("action") == "skipped", summary
    assert "STOP" in str(summary.get("reason", "")), summary
    client.replace_order.assert_not_called()
    client.cancel_order.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
