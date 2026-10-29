"""Regression test: each bot must only report ITS OWN orders.

2026-10-29. The autofin and momentum bots share one DNSE account
(0003937868) and therefore one ledger file
(``data/live/order_ledger.json``). Each bot's order monitor scanned
that whole ledger, so the momentum bot picked up the autofin bot's
orders and notified its OWN Telegram chat (6761941695):

    ✅ Vietnam Algo: order VOS → FILLED
    orderId: 20321
    transition: OPEN → FILLED
    intent: BUY 300 VOS @ 0 VND

That VOS order was placed by the AUTOFIN bot — the momentum strategy
never traded VOS — so the message was pure cross-talk in the wrong
channel.

The fix: every ledger row records the strategy that created it
(``LedgerOrder.strategy``), and the order monitor skips rows owned by
a different strategy.

Legacy rows (written before the field existed) have an empty owner
and are still monitored by every bot, so nothing goes unmonitored.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.bots.runner import _monitor_order_statuses  # noqa: E402
from vietnam_algo.libs.order_ledger import (  # noqa: E402
    LedgerOrder,
    load_ledger,
    save_ledger,
)


def _row(symbol: str, strategy: str, status: str = "open") -> LedgerOrder:
    return LedgerOrder(
        runId="2026-10-29", sourceOrderId=abs(hash(symbol)) % 100000,
        sourceDate="2026-10-29", symbol=symbol, side="NB", quantity=300,
        price=11800, reason=f"{strategy}_buy:deal#1:{symbol}", status=status,
        dnseOrderId=f"dnse-{symbol}", createdAt="2026-10-29T02:01:17Z",
        updatedAt="2026-10-29T02:01:17Z", strategy=strategy,
    )


def _client_for(rows: list[LedgerOrder]):
    client = MagicMock()
    by_symbol = {r.symbol: r for r in rows}

    def _detail(_acct, order_id, trading_token=None, order_category="NORMAL"):
        for r in rows:
            if r.dnseOrderId == order_id:
                return {
                    "id": order_id, "symbol": r.symbol,
                    "orderStatus": "Filled", "fillQuantity": r.quantity,
                    "averagePrice": r.price,
                }
        return {"id": order_id, "orderStatus": "Filled"}

    client.order_detail.side_effect = _detail
    client.get_latest_quote.return_value = {"quotes": []}
    return client


def test_monitor_skips_other_strategys_orders(tmp_path, monkeypatch) -> None:
    """The momentum bot must not poll/notify the autofin bot's orders."""
    autofin_row = _row("VOS", "autofin_signal")
    momentum_row = _row("MST", "momentum")
    save_ledger({"a": autofin_row, "b": momentum_row}, tmp_path)

    notified: list[str] = []
    monkeypatch.setattr(
        "vietnam_algo.bots.runner.notify",
        lambda text, *a, **kw: notified.append(text),
    )

    client = _client_for([autofin_row, momentum_row])
    summary = _monitor_order_statuses(
        client, "0003937868", "tok", tmp_path, throttle_seconds=0,
        strategy="momentum",
    )

    # Only the momentum order was polled.
    assert summary["polled"] == 1, summary
    assert summary["skipped_other_strategy"] == 1, summary
    # And only the momentum order was notified.
    assert len(notified) == 1, notified
    assert "MST" in notified[0]
    assert "VOS" not in notified[0]


def test_monitor_notifies_own_orders(tmp_path, monkeypatch) -> None:
    """The autofin bot must still report its own fills."""
    autofin_row = _row("VOS", "autofin_signal")
    save_ledger({"a": autofin_row}, tmp_path)

    notified: list[str] = []
    monkeypatch.setattr(
        "vietnam_algo.bots.runner.notify",
        lambda text, *a, **kw: notified.append(text),
    )

    client = _client_for([autofin_row])
    summary = _monitor_order_statuses(
        client, "0003937868", "tok", tmp_path, throttle_seconds=0,
        strategy="autofin_signal",
    )

    assert summary["polled"] == 1, summary
    assert summary["skipped_other_strategy"] == 0, summary
    assert len(notified) == 1, notified
    assert "VOS" in notified[0]


def test_legacy_rows_without_owner_are_still_monitored(tmp_path, monkeypatch) -> None:
    """Rows written before the ``strategy`` field existed have an empty
    owner. They must keep being monitored by every bot rather than
    silently going unmonitored.
    """
    legacy = LedgerOrder(
        runId="2026-10-20", sourceOrderId=1, sourceDate="2026-10-20",
        symbol="BAF", side="NS", quantity=200, price=28080,
        reason="autofin_sl:deal#1:BAF:28080", status="open",
        dnseOrderId="dnse-BAF", createdAt="2026-10-20T02:00:00Z",
        updatedAt="2026-10-20T02:00:00Z",
        # strategy defaults to "" -> legacy row
    )
    save_ledger({"legacy": legacy}, tmp_path)

    notified: list[str] = []
    monkeypatch.setattr(
        "vietnam_algo.bots.runner.notify",
        lambda text, *a, **kw: notified.append(text),
    )

    client = _client_for([legacy])
    summary = _monitor_order_statuses(
        client, "0003937868", "tok", tmp_path, throttle_seconds=0,
        strategy="momentum",
    )

    assert summary["polled"] == 1, summary
    assert summary["skipped_other_strategy"] == 0, summary
    assert len(notified) == 1


def test_no_strategy_filter_monitors_everything(tmp_path, monkeypatch) -> None:
    """With no strategy given (the default), behaviour is unchanged."""
    rows = [_row("VOS", "autofin_signal"), _row("MST", "momentum")]
    save_ledger({"a": rows[0], "b": rows[1]}, tmp_path)

    notified: list[str] = []
    monkeypatch.setattr(
        "vietnam_algo.bots.runner.notify",
        lambda text, *a, **kw: notified.append(text),
    )

    client = _client_for(rows)
    summary = _monitor_order_statuses(
        client, "0003937868", "tok", tmp_path, throttle_seconds=0,
    )

    assert summary["polled"] == 2, summary
    assert summary["skipped_other_strategy"] == 0, summary


def test_strategy_is_persisted_on_new_rows(tmp_path) -> None:
    """``execute_orders_with_ledger`` must stamp the owner on new rows."""
    from vietnam_algo.libs.daily_engine import execute_orders_with_ledger
    from vietnam_algo.libs.trading_engine import DnseOrder

    client = MagicMock()
    client.place_stock_order.return_value = {"id": "dnse-1"}
    client.order_detail.return_value = {"id": "dnse-1", "orderStatus": "New"}

    order = DnseOrder(
        symbol="VOS", side="NB", quantity=300, price=11800,
        loanPackageId=1775, orderType="LO", sourceOrderId=4242,
        sourceDate="2026-10-29", reason="autofin_buy:deal#1:VOS",
    )
    execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-10-29",
        strategy="autofin_signal",
    )

    ledger = load_ledger(tmp_path)
    assert len(ledger) == 1
    row = list(ledger.values())[0]
    assert row.strategy == "autofin_signal"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
