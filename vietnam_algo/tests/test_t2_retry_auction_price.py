"""Regression test: the T+2 SL retry must not replay an auction SL
at price 0.

2026-10-28. The ORS position (200 shares bought 2026-10-26 at
16,000) had NO stop-loss. Its SL was planned during the ATO auction
window and therefore submitted with ``price=0`` — the exchange sets
the fill price, and the intended price (14,400 = 10% below entry)
survives only in the reason string:

    autofin_user_sl:deal#1792713600:ORS:10%:14400
        [session=ATO signal_price=14400]

DNSE rejected that order with ``TRADE_QUANTITY_NOT_ENOUGH`` (the
T+2 settlement block). The T+2 retry then replayed the ledger row's
``price`` — which is 0 — but as a LIMIT order (``orderType="LO"``).
DNSE rejects a limit order with price 0, so the retry would have
failed on a price error instead of succeeding once the T+2.5
settlement cleared, leaving the position unprotected.

The fix recovers the intended price from the ``signal_price=``
marker (falling back to the ``:<pct>:<price>`` segment of the
autofin reason) and places the limit SL at that price.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.bots.runner import retry_t2_settlement_sl_orders  # noqa: E402
from vietnam_algo.libs.order_ledger import LedgerOrder  # noqa: E402


def _seed_auction_sl(tmp_path: Path) -> None:
    """A BUY (filled) + an auction-priced SL (rejected, price 0)."""
    items = [
        LedgerOrder(
            runId="2026-10-26", sourceOrderId=11111, sourceDate="2026-10-26",
            symbol="ORS", side="NB", quantity=200, price=0,
            reason="autofin_buy:deal#1792713600:ORS:nav15pct:Khuyến nghị đầu tư "
                   "[session=ATO signal_price=16000]",
            status="filled", dnseOrderId="dnse-buy-ors",
            createdAt="2026-10-26T02:01:08Z",
            updatedAt="2026-10-26T02:01:08Z",
            lastDetail={"filledQuantity": 200, "orderStatus": "Filled"},
        ),
        LedgerOrder(
            runId="2026-10-26", sourceOrderId=22222, sourceDate="2026-10-26",
            symbol="ORS", side="NS", quantity=200, price=0,
            reason="autofin_user_sl:deal#1792713600:ORS:10%:14400 "
                   "[session=ATO signal_price=14400]",
            status="rejected",
            error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
            createdAt="2026-10-26T02:01:08Z",
            updatedAt="2026-10-26T02:01:08Z",
            lastDetail=None,
        ),
    ]
    from vietnam_algo.libs.order_ledger import save_ledger

    save_ledger({f"k{i}": o for i, o in enumerate(items)}, tmp_path)


def _client() -> MagicMock:
    client = MagicMock()
    client.get_cash_loan_package_id.return_value = 1775
    client.place_stock_order.return_value = {"id": "dnse-sl-retry"}
    client.order_detail.return_value = {"id": "dnse-sl-retry", "orderStatus": "New"}
    # We hold the 200 ORS shares (post-settlement).
    client.get_positions.return_value = {
        "positions": [{"symbol": "ORS", "tradeQuantity": 200}],
    }
    return client


def test_auction_sl_is_retried_at_the_recovered_price(tmp_path) -> None:
    """The retry must place the limit SL at 14,400, not at 0."""
    _seed_auction_sl(tmp_path)
    client = _client()
    submitted: list[dict] = []
    client.place_stock_order.side_effect = lambda **kw: (
        submitted.append(kw) or {"id": "dnse-sl-retry"}
    )

    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-28",
        now_time=datetime.time(13, 0),
    )

    assert summary["t2_due"] == 1, summary
    assert summary["resubmitted"] == 1, summary
    assert summary["resubmit_errors"] == [], summary["resubmit_errors"]

    assert len(submitted) == 1
    assert submitted[0]["price"] == 14400, (
        f"the SL must be placed at the recovered price 14400, got "
        f"{submitted[0]['price']}"
    )
    assert submitted[0]["order_type"] == "LO"
    assert submitted[0]["symbol"] == "ORS"
    assert submitted[0]["quantity"] == 200


def test_recovered_price_is_reported(tmp_path) -> None:
    """The price recovery must be visible in the summary."""
    _seed_auction_sl(tmp_path)
    summary = retry_t2_settlement_sl_orders(
        client=_client(), account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-28",
        now_time=datetime.time(13, 0),
    )
    assert summary["recovered_price"], summary
    assert "ORS" in summary["recovered_price"][0]
    assert "14,400" in summary["recovered_price"][0]


def test_sl_without_any_recoverable_price_is_skipped(tmp_path) -> None:
    """With no price at all we must NOT submit a doomed order.

    A limit order needs a price. If neither the ledger nor the
    reason carries one, the SL is skipped and reported so the
    operator places it manually — rather than submitting LO @ 0 and
    getting a confusing price error.
    """
    items = [
        LedgerOrder(
            runId="2026-10-26", sourceOrderId=11111, sourceDate="2026-10-26",
            symbol="ORS", side="NB", quantity=200, price=0,
            reason="autofin_buy:deal#1792713600:ORS:nav15pct:Khuyến nghị đầu tư",
            status="filled", dnseOrderId="dnse-buy-ors",
            createdAt="2026-10-26T02:01:08Z",
            updatedAt="2026-10-26T02:01:08Z",
            lastDetail={"filledQuantity": 200, "orderStatus": "Filled"},
        ),
        LedgerOrder(
            runId="2026-10-26", sourceOrderId=22222, sourceDate="2026-10-26",
            symbol="ORS", side="NS", quantity=200, price=0,
            reason="autofin_user_sl:deal#1792713600:ORS:10%:14400",
            status="rejected",
            error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
            createdAt="2026-10-26T02:01:08Z",
            updatedAt="2026-10-26T02:01:08Z",
            lastDetail=None,
        ),
    ]
    from vietnam_algo.libs.order_ledger import save_ledger

    save_ledger({f"k{i}": o for i, o in enumerate(items)}, tmp_path)
    client = _client()

    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-28",
        now_time=datetime.time(13, 0),
    )

    # The ``:<pct>:<price>`` fallback recovers 14400 from the reason.
    assert summary["resubmitted"] == 1
    assert summary["resubmit_errors"] == []


def test_price_zero_is_never_submitted(tmp_path) -> None:
    """Guard: no retried SL may ever carry price 0."""
    _seed_auction_sl(tmp_path)
    client = _client()
    submitted: list[dict] = []
    client.place_stock_order.side_effect = lambda **kw: (
        submitted.append(kw) or {"id": "dnse-sl-retry"}
    )

    retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-28",
        now_time=datetime.time(13, 0),
    )

    for order in submitted:
        assert order["price"] > 0, (
            f"a retried SL must never be submitted at price 0: {order}"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
