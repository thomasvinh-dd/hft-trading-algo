"""Regression tests for the automatic marketable-SELL fallback.

A discretionary SELL rejected by DNSE for a price error (for example
``INVALID_PRICE_LOT``) is retried once using the live best bid and the
symbol's correct exchange price lot. Protective STOP legs are never
converted into a market sell.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.bots.runner import (
    _is_price_rejection,
    retry_price_rejected_sell_orders,
)
from vietnam_algo.libs.order_ledger import LedgerOrder, save_ledger


def _row(**overrides) -> LedgerOrder:
    data = {
        "runId": "2026-11-16",
        "sourceOrderId": 123,
        "sourceDate": "2026-11-16",
        "symbol": "CTP",
        "side": "NS",
        "quantity": 200,
        "price": 6630,
        "reason": "autofin_partial:deal#1793664000:CTP:reduce_lap:lap1:pct50:test",
        "status": "rejected",
        "dnseOrderId": "",
        "error": 'HTTP 400 {"code":"INVALID_PRICE_LOT","message":"Invalid price lot"}',
        "orderCategory": "NORMAL",
        "strategy": "autofin_signal",
    }
    data.update(overrides)
    return LedgerOrder(**data)


def _client(*, market_id="STX", position_qty=300, bid=6.7):
    client = MagicMock()
    client.get_market_id.return_value = market_id
    client.get_latest_quote.return_value = {
        "quotes": [{"boardId": "G1", "bid": [{"price": bid}]}]
    }
    client.get_positions.return_value = {
        "positions": [{"symbol": "CTP", "openQuantity": position_qty}]
    }
    client.get_cash_loan_package_id.return_value = 1775
    client.place_stock_order.return_value = {"id": "dnse-retry-1", "orderStatus": "New"}
    client.order_detail.return_value = {"id": "dnse-retry-1", "orderStatus": "New"}
    return client


def test_price_rejection_classification() -> None:
    assert _is_price_rejection("INVALID_PRICE_LOT")
    assert _is_price_rejection("INVALID_PRICE")
    assert _is_price_rejection("INVALID_PRICE_FOR_SYMBOL")
    assert not _is_price_rejection("TRADE_QUANTITY_NOT_ENOUGH")
    assert not _is_price_rejection("INVALID_TRADING_TOKEN")
    assert not _is_price_rejection("")


def test_ctp_price_rejected_sell_is_retried_at_hnx_market_price(tmp_path) -> None:
    save_ledger({"old": _row()}, tmp_path)
    client = _client()
    result = retry_price_rejected_sell_orders(
        client, "0003937868", "token", tmp_path, "2026-11-16",
        loan_package_id=1775,
    )
    assert len(result["retried"]) == 1
    assert result["retried"][0]["price"] == 6600
    call = client.place_stock_order.call_args.kwargs
    assert call["price"] == 6600
    assert call["side"] == "NS"
    assert call["order_type"] == "LO"


def test_unknown_market_uses_conservative_100_vnd_ladder(tmp_path) -> None:
    save_ledger({"old": _row()}, tmp_path)
    client = _client()
    client.get_market_id.side_effect = RuntimeError("lookup failed")
    result = retry_price_rejected_sell_orders(
        client, "0003937868", "token", tmp_path, "2026-11-16",
        loan_package_id=1775,
    )
    assert len(result["retried"]) == 1
    assert result["retried"][0]["price"] == 6600


def test_protective_stop_is_never_retried_as_market_sell(tmp_path) -> None:
    save_ledger({
        "old": _row(
            reason="autofin_user_sl:deal#123:CTP:10%:6660",
            orderCategory="STOP",
            stopPrice=6660,
        )
    }, tmp_path)
    client = _client()
    result = retry_price_rejected_sell_orders(
        client, "0003937868", "token", tmp_path, "2026-11-16",
        loan_package_id=1775,
    )
    assert result["retried"] == []
    client.place_stock_order.assert_not_called()


def test_retry_is_idempotent(tmp_path) -> None:
    save_ledger({"old": _row()}, tmp_path)
    client = _client()
    first = retry_price_rejected_sell_orders(
        client, "0003937868", "token", tmp_path, "2026-11-16",
        loan_package_id=1775,
    )
    second = retry_price_rejected_sell_orders(
        client, "0003937868", "token", tmp_path, "2026-11-16",
        loan_package_id=1775,
    )
    assert len(first["retried"]) == 1
    assert second["retried"] == []


def test_buy_and_non_price_rejections_are_not_retried(tmp_path) -> None:
    save_ledger({
        "buy": _row(side="NB"),
        "other": _row(error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH"),
    }, tmp_path)
    client = _client()
    result = retry_price_rejected_sell_orders(
        client, "0003937868", "token", tmp_path, "2026-11-16",
        loan_package_id=1775,
    )
    assert result["retried"] == []
    client.place_stock_order.assert_not_called()
