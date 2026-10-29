"""Regression test: stop-losses must NOT be re-priced to the live bid.

2026-10-29. The ``AUTOFIN_SELL_AT_MARKET`` override replaced ANY
SELL price with the live best-bid × 99%, including stop-losses.

That is right for an EXIT order (take-profit / partial close /
full close) — we want those to fill immediately so a falling market
doesn't leave them resting above the book.

It is exactly wrong for a STOP-LOSS. Two consequences, both observed:

1. **The protection was destroyed.** TVN's intended 9,360 stop
   (10% below the 10,400 entry) was rewritten to 10,150 — the live
   bid — a 2.4% stop instead of 10%.

2. **The position was liquidated instead of protected.** The 2026-10-28
   ORS incident: a sell LO @ 14,400 against a 15,150 bid is
   marketable, so DNSE crossed it and filled 200 shares instantly at
   15,150. No protective order ever rested at the broker.

So a protective leg keeps its own level, and when that level was
recorded as 0 (an auction-planned SL) it is recovered from the
reason string.
"""

from __future__ import annotations

import sys
from datetime import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.bots.runner import (  # noqa: E402
    _is_protective_reason,
    _submit_autofin_deals_to_dnse,
)


def _quote(bid_vnd: int) -> dict:
    """A DNSE quote with the best bid at ``bid_vnd`` (absolute VND)."""
    return {
        "quotes": [
            {
                "boardId": "G1",
                "bid": [{"price": bid_vnd / 1000.0, "quantity": 100}],
                "offer": [{"price": (bid_vnd + 50) / 1000.0, "quantity": 100}],
            }
        ]
    }


def _deal(symbol: str, quantity: int, price: int, reason: str) -> dict:
    return {
        "action": "sl",
        "symbol": symbol,
        "deal_id": "1792627200:VOS",
        "quantity": quantity,
        "price": price,
        "rate_for_nav": 0.15,
        "last_recomment": reason,
        "supply_F": 10000,
        "supply_T": 10300,
        "dnse_orders": [
            {
                "symbol": symbol, "side": "NS", "quantity": quantity,
                "price": price, "reason": reason,
            }
        ],
    }


def _run(tmp_path, deal, monkeypatch, env=None):
    """Run the submit helper with a stubbed executor; return the order."""
    submitted: list[dict] = []

    def _fake_execute(*, client, account_no, trading_token, orders, data_dir,
                      run_id, strategy: str = ""):
        submitted.extend(orders)
        return (
            [{"status": "submitted", "detail": {"id": "dnse-1"}} for _ in orders],
            {},
            True,
        )

    import vietnam_algo.libs.daily_engine as daily_engine
    monkeypatch.setattr(daily_engine, "execute_orders_with_ledger", _fake_execute)

    client = MagicMock()
    client.get_latest_quote.return_value = _quote(10_300)
    client.get_cash_loan_package_id.return_value = 1775

    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)

    _submit_autofin_deals_to_dnse(
        [deal],
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=client,
        data_dir=tmp_path,
        trade_date="2026-10-29",
    )
    return submitted


def test_stop_loss_keeps_its_intended_level(tmp_path, monkeypatch) -> None:
    """A protective SL must be submitted at its own level, not the bid.

    The stop is submitted as a native DNSE conditional (STOP) order:
    ``stopPrice`` carries the intended level and ``price`` sits
    slightly BELOW it (a 0.5% buffer) so the trigger rests below the
    level rather than at it.

    What matters is that the level is 9,360 — the live-bid override
    would have made it 10,150 (a 2.4% stop instead of 10%).
    """
    monkeypatch.setenv("AUTOFIN_SELL_AT_MARKET", "1")
    deal = _deal(
        "TVN", 300, 9360,
        "autofin_user_sl:deal#1792627200:TVN:10%:9360",
    )
    submitted = _run(tmp_path, deal, monkeypatch)

    assert len(submitted) == 1, submitted
    order = submitted[0]
    assert order.stopPrice == 9360, (
        f"the stop level must stay 9,360, got {order.stopPrice} "
        f"(the live-bid override would have made it 10,150 — a 2.4% "
        f"stop instead of 10%)"
    )
    # The resting trigger sits slightly below the level.
    assert order.price < 9360, order.price
    assert order.price >= 9360 * 0.99, order.price
    assert order.side == "NS"
    # It must be a native conditional order, not a plain limit sell.
    assert order.orderCategory == "STOP", order.orderCategory
    assert order.conditionOperator == "<=", order.conditionOperator


def test_exit_order_still_gets_the_live_bid_override(tmp_path, monkeypatch) -> None:
    """A take-profit / partial close must STILL be sold at the bid.

    The override exists so exits always fill. Excluding stop-losses
    must not change that behaviour.
    """
    monkeypatch.setenv("AUTOFIN_SELL_AT_MARKET", "1")
    deal = _deal(
        "VOS", 300, 11_800,
        "autofin_partial:deal#1792627200:VOS:take_profit_lap:lap1",
    )
    submitted = _run(tmp_path, deal, monkeypatch)

    assert len(submitted) == 1, submitted
    order = submitted[0]
    # The live bid is 10,300 → ×99% = 10,197 → tick-rounded 10,150.
    assert order.price == 10_150, (
        f"an exit order must still be sold at the live bid, got "
        f"{order.price}"
    )


def test_auction_stop_loss_recovers_its_level(tmp_path, monkeypatch) -> None:
    """An auction-planned SL (price=0) recovers its level from the reason."""
    monkeypatch.setenv("AUTOFIN_SELL_AT_MARKET", "1")
    deal = _deal(
        "ORS", 200, 0,
        "autofin_user_sl:deal#1792713600:ORS:10%:14400 "
        "[session=ATO signal_price=14400]",
    )
    submitted = _run(tmp_path, deal, monkeypatch)

    assert len(submitted) == 1, submitted
    order = submitted[0]
    assert order.stopPrice == 14_400, (
        f"the auction stop must recover 14,400 from the reason, got "
        f"{order.stopPrice}"
    )
    assert order.orderCategory == "STOP", order.orderCategory


def test_autofin_sl_prefix_is_protective() -> None:
    """Both protective reason prefixes are recognised."""
    assert _is_protective_reason("autofin_sl:deal#1:27900") is True
    assert _is_protective_reason("autofin_user_sl:deal#1:10%:28080") is True
    # NOT protective:
    assert _is_protective_reason("autofin_partial:deal#1:take_profit_lap") is False
    assert _is_protective_reason("autofin_buy:deal#1:nav15pct:Khuyến nghị") is False
    assert _is_protective_reason("autofin_closed:deal#1") is False
    assert _is_protective_reason(None) is False


def test_sell_at_market_kill_switch_still_works_for_exits(tmp_path, monkeypatch) -> None:
    """``AUTOFIN_SELL_AT_MARKET=0`` still disables the override for exits."""
    monkeypatch.setenv("AUTOFIN_SELL_AT_MARKET", "0")
    deal = _deal(
        "VOS", 300, 11_800,
        "autofin_partial:deal#1792627200:VOS:take_profit_lap:lap1",
    )
    submitted = _run(tmp_path, deal, monkeypatch)

    assert len(submitted) == 1, submitted
    assert submitted[0].price == 11_800


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
