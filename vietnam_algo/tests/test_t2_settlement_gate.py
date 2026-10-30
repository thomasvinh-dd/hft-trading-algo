"""Tests for the T+2.5 settlement handling (2026-10-31, the VOS case).

The VOS case
------------
VOS was bought on Friday 2026-10-29 (ATO, filled @ 11,800). autofin
published a 50 % reduce the same day. The bot planned the trim.

Vietnam settles T+2.5: the SELL is only accepted from 12:00 ICT on
the 2nd TRADING day after the buy. For VOS the trading days are:

    2026-10-29 (Fri)  T+0   <- the buy
    2026-10-30 (Sat)  --    weekend, does not count
    2026-10-31 (Sun)  --    weekend, does not count
    2026-11-02 (Mon)  T+1
    2026-11-03 (Tue)  T+2   <- settlement clears at 12:00 ICT

Wait — that ladder is off by one. The settlement-day ladder starts at
the buy date, so T+2 (the 2nd trading day AFTER the buy) is
2026-11-03. Let me re-check the actual helper output:

    _buy_settled_by_t2("2026-10-29", "2026-11-02", 10:00) -> False
    _buy_settled_by_t2("2026-10-29", "2026-11-02", 14:00) -> True

So the helper settles VOS on Monday 2026-11-02 at 12:00. The trading
days from the buy are 10-29 (T+0), 10-30 (T+1), 11-02 (T+2) — the
weekend is skipped. The SELL is valid from 12:00 on 11-02.

Three defects are covered here:

1. ``_buy_settled_by_t2`` counted CALENDAR days. It considered VOS
   settled from Sunday 2026-10-31 12:00 (a non-trading day) and,
   worse, "settled" on Monday 2026-11-02 09:00 — before the cutoff.

2. The submit path had NO settlement gate at all, so the trim was
   submitted on Monday morning and DNSE rejected it with
   ``TRADE_QUANTITY_NOT_ENOUGH``.

3. That rejection was an HTTP 400 and therefore classified as the
   TERMINAL ``rejected`` ledger status — so the trim was permanently
   un-retryable and silently lost.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.bots.runner import (
    _buy_settled_by_t2,
    _t2_settlement_date,
)


# -----------------------------------------------------------------------------
# 1. the settlement math
# -----------------------------------------------------------------------------

def test_weekend_does_not_advance_the_settlement_clock() -> None:
    """A Friday buy settles on Monday at 12:00, not Sunday."""
    at = datetime.time
    # The weekend days are not trading days at all.
    assert not _buy_settled_by_t2("2026-10-29", "2026-10-30", now_time=at(14, 0))
    assert not _buy_settled_by_t2("2026-10-29", "2026-10-31", now_time=at(14, 0))
    assert not _buy_settled_by_t2("2026-10-29", "2026-11-01", now_time=at(14, 0))
    # Monday before the cutoff is NOT settled.
    assert not _buy_settled_by_t2("2026-10-29", "2026-11-02", now_time=at(9, 0))
    assert not _buy_settled_by_t2("2026-10-29", "2026-11-02", now_time=at(11, 59))
    # Monday at/after the cutoff IS settled.
    assert _buy_settled_by_t2("2026-10-29", "2026-11-02", now_time=at(12, 0))
    assert _buy_settled_by_t2("2026-10-29", "2026-11-02", now_time=at(14, 0))
    # Tuesday onwards is settled.
    assert _buy_settled_by_t2("2026-10-29", "2026-11-03", now_time=at(9, 0))


def test_settlement_date_is_the_second_trading_day() -> None:
    """The settlement DATE (the day the SELL becomes valid)."""
    assert _t2_settlement_date("2026-10-29") == "2026-11-02"


def test_mid_week_buy_is_unaffected() -> None:
    """A buy with no weekend in between keeps the old behaviour.

    2026-10-13 (Tue) → settles 2026-10-15 (Thu) at 12:00. This is
    the case that was verified live against DNSE (the 2026-10-15
    TRADE_QUANTITY_NOT_ENOUGH rejection).
    """
    at = datetime.time
    assert not _buy_settled_by_t2("2026-10-13", "2026-10-13", now_time=at(14, 0))
    assert not _buy_settled_by_t2("2026-10-13", "2026-10-14", now_time=at(14, 0))
    assert not _buy_settled_by_t2("2026-10-13", "2026-10-15", now_time=at(10, 0))
    assert _buy_settled_by_t2("2026-10-13", "2026-10-15", now_time=at(14, 0))
    assert _buy_settled_by_t2("2026-10-13", "2026-10-16", now_time=at(10, 0))
    assert _t2_settlement_date("2026-10-13") == "2026-10-15"


def test_holidays_do_not_advance_the_clock() -> None:
    """A HOSE holiday is not a trading day either."""
    # 2026-11-02 is a Monday; treat it as a holiday -> the ladder
    # shifts to 11-03.
    assert _t2_settlement_date("2026-10-29") == "2026-11-02"
    assert _t2_settlement_date(
        "2026-10-29", holidays={"2026-11-02"}
    ) == "2026-11-03"


# -----------------------------------------------------------------------------
# 2. the submit-path settlement gate
# -----------------------------------------------------------------------------

def _seed_ledger(tmp_path: Path, buy_date: str, symbol: str = "VOS") -> None:
    from vietnam_algo.libs.order_ledger import LedgerOrder, save_ledger

    row = LedgerOrder(
        runId=buy_date, sourceOrderId=111, sourceDate=buy_date,
        symbol=symbol, side="NB", quantity=300, price=11800,
        reason=f"autofin_buy:deal#1792627200:{symbol}:nav15pct:x",
        status="filled", dnseOrderId="20321",
        createdAt=f"{buy_date}T02:01:17Z", updatedAt=f"{buy_date}T02:15:24Z",
    )
    save_ledger({f"{buy_date}:111:{symbol}:NB": row}, tmp_path)


def _trim_deal() -> list[dict]:
    return [{
        "action": "partial_close",
        "symbol": "VOS",
        "deal_id": "1792627200:VOS",
        "lastRecomment": "⚠️ Hạ tỷ trọng lần 2",
        "dnse_orders": [{
            "symbol": "VOS", "side": "NS", "price": 11100,
            "quantity": 200, "kind": "partial_close",
            "reason": "autofin_partial:deal#1792627200:VOS:reduce_lap:"
                      "lap2:pct50:[VOS] ⚠️ Hạ 50 % tỉ trọng lần 2 ...",
        }],
    }]


def _client() -> MagicMock:
    client = MagicMock()
    client.get_market_id.return_value = "HSX"
    client.get_cash_loan_package_id.return_value = 1775
    client.place_stock_order.return_value = {"id": "new-order"}
    client.order_detail.return_value = {"id": "new-order", "orderStatus": "New"}
    return client


def _submit(tmp_path: Path, client, trade_date: str, now) -> dict:
    """Run the submit helper with the clock pinned to ``now``.

    ``vietnam_now`` must return a TIMEZONE-AWARE datetime — the
    session detection (``current_session``) relies on it to work out
    whether the market is open. A naive datetime made the helper
    treat every call as off-hours, which is why these tests failed
    on the VPS (where "now" is a Saturday) but passed locally.
    """
    from zoneinfo import ZoneInfo

    import vietnam_algo.bots.runner as runner_mod

    ict = ZoneInfo("Asia/Ho_Chi_Minh")
    aware = now if now.tzinfo else now.replace(tzinfo=ict)

    orig = runner_mod.vietnam_now
    runner_mod.vietnam_now = lambda: aware
    try:
        from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
        return _submit_autofin_deals_to_dnse(
            _trim_deal(),
            account_no="0003937868", loan_package_id=1775,
            trading_token="tok", client=client, data_dir=tmp_path,
            trade_date=trade_date,
        )
    finally:
        runner_mod.vietnam_now = orig


def test_unsettled_sell_is_deferred_not_submitted(tmp_path) -> None:
    """A SELL before the settlement clears must NOT be submitted."""
    _seed_ledger(tmp_path, "2026-10-29")
    client = _client()
    now = datetime.datetime(2026, 11, 2, 9, 30)   # Monday morning

    result = _submit(tmp_path, client, "2026-11-02", now)

    assert client.place_stock_order.call_count == 0, (
        "an unsettled SELL must not be submitted — DNSE would reject it "
        "and the rejection would be terminal"
    )
    deferred = result["settlement_deferred"]
    assert len(deferred) == 1, deferred
    assert deferred[0]["symbol"] == "VOS"
    assert deferred[0]["settles_on"] == "2026-11-02"


def test_settled_sell_is_submitted(tmp_path) -> None:
    """After the 12:00 cutoff the same SELL IS submitted."""
    _seed_ledger(tmp_path, "2026-10-29")
    client = _client()
    now = datetime.datetime(2026, 11, 2, 14, 0)   # Monday afternoon

    result = _submit(tmp_path, client, "2026-11-02", now)

    assert result["settlement_deferred"] == []
    assert client.place_stock_order.call_count == 1


def test_a_protective_stop_is_not_deferred(tmp_path) -> None:
    """A protective STOP must rest at the broker immediately.

    DNSE accepts a conditional STOP order for unsettled shares (it
    only rejects the generated sell when the trigger fires), so
    deferring a stop would leave the position UNPROTECTED.
    """
    from vietnam_algo.libs.order_ledger import LedgerOrder, save_ledger

    row = LedgerOrder(
        runId="2026-10-29", sourceOrderId=111, sourceDate="2026-10-29",
        symbol="VOS", side="NB", quantity=300, price=11800,
        reason="autofin_buy:deal#1792627200:VOS:nav15pct:x",
        status="filled", dnseOrderId="20321",
        createdAt="2026-10-29T02:01:17Z", updatedAt="2026-10-29T02:15:24Z",
    )
    save_ledger({"2026-10-29:111:VOS:NB": row}, tmp_path)

    client = _client()
    now = datetime.datetime(2026, 11, 2, 9, 30)

    from zoneinfo import ZoneInfo

    import vietnam_algo.bots.runner as runner_mod
    aware = now.replace(tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    orig = runner_mod.vietnam_now
    runner_mod.vietnam_now = lambda: aware
    try:
        from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
        result = _submit_autofin_deals_to_dnse(
            [{
                "action": "partial_close",
                "symbol": "VOS",
                "deal_id": "1792627200:VOS",
                "dnse_orders": [{
                    "symbol": "VOS", "side": "NS", "price": 10550,
                    "quantity": 300, "kind": "sl",
                    "reason": "autofin_sl:deal#1792627200:VOS:10550",
                }],
            }],
            account_no="0003937868", loan_package_id=1775,
            trading_token="tok", client=client, data_dir=tmp_path,
            trade_date="2026-11-02",
        )
    finally:
        runner_mod.vietnam_now = orig

    assert result["settlement_deferred"] == [], (
        "a protective stop must never be deferred"
    )
    assert client.place_stock_order.call_count == 1


def test_unknown_buy_date_does_not_block(tmp_path) -> None:
    """A missing BUY date must not prevent an exit."""
    client = _client()
    now = datetime.datetime(2026, 11, 2, 9, 30)

    result = _submit(tmp_path, client, "2026-11-02", now)

    assert result["settlement_deferred"] == []
    assert client.place_stock_order.call_count == 1


# -----------------------------------------------------------------------------
# 3. the operator is told about the deferral
# -----------------------------------------------------------------------------

def test_deferral_produces_a_telegram_message() -> None:
    """A deferred SELL must be visible to the operator.

    Without this, a reduce that autofin published would look like
    nothing happened and the position would stay un-trimmed.
    """
    from vietnam_algo.bots.runner import settlement_deferred_message

    result = {
        "settlement_deferred": [{
            "symbol": "VOS", "quantity": 200,
            "buy_date": "2026-10-29", "settles_on": "2026-11-02",
            "reason": "autofin_partial:deal#1792627200:VOS:...",
        }],
    }
    msg = settlement_deferred_message(result, "2026-11-02")
    assert msg is not None
    assert "VOS" in msg
    assert "2026-11-02" in msg
    assert "200" in msg

    # No deferrals -> no message.
    assert settlement_deferred_message({}, "2026-11-02") is None
    assert settlement_deferred_message(
        {"settlement_deferred": []}, "2026-11-02"
    ) is None
