"""Tests for ``vietnam_algo.bots.runner.retry_t2_settlement_sl_orders``.

The T+2.5 settlement problem: when the autofin adapter plans a
BUY + SL in the same submit batch, the SL is always rejected
on T+0 and T+1 because the BUY hasn't settled yet. DNSE
returns HTTP 400 ``TRADE_QUANTITY_NOT_ENOUGH`` for those
SELLs. Vietnam's settlement is T+2.5 (the user-confirmed
2026-10-13 update: "vn stock market using t+2.5") — the
settlement completes at 12:00 PM on T+2. Without this retry
job, the user has to manually re-place the SL after T+2.5
clears.

The retry function scans the order ledger for SL SELLs
rejected with the T+2 error, checks if the underlying BUY
is at least 2.5 calendar days old (with a noon cutoff on
T+2), and re-submits the SL.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.bots import runner as runner_mod
from vietnam_algo.bots.runner import (
    _buy_settled_by_t2,
    _extract_deal_id_from_reason,
    _is_t2_rejection,
    retry_t2_settlement_sl_orders,
)
from vietnam_algo.libs.order_ledger import LedgerOrder, save_ledger
from vietnam_algo.libs.trading_engine import DnseOrder


# -----------------------------------------------------------------------------
# Helper-function tests
# -----------------------------------------------------------------------------


def test_is_t2_rejection_matches_trade_quantity_not_enough() -> None:
    assert _is_t2_rejection('HTTP 400 {"code":"TRADE_QUANTITY_NOT_ENOUGH"}')
    assert _is_t2_rejection('TRADE_QUANTITY_NOT_ENOUGH deal not found')


def test_is_t2_rejection_ignores_unrelated_errors() -> None:
    assert not _is_t2_rejection("")
    assert not _is_t2_rejection("HTTP 500 INTERNAL_SERVER_ERROR")
    assert not _is_t2_rejection("HTTP 400 INVALID_PRICE")


def test_extract_deal_id_from_reason_handles_all_prefixes() -> None:
    """autofin emits 3 reason prefixes; all carry ``deal#NNNN:SYM``."""
    assert _extract_deal_id_from_reason(
        "autofin_buy:deal#1785888000:BAF:nav30pct:Khuyến nghị"
    ) == "1785888000:BAF"
    assert _extract_deal_id_from_reason(
        "autofin_sl:deal#1785888000:BAF:27900"
    ) == "1785888000:BAF"
    assert _extract_deal_id_from_reason(
        "autofin_user_sl:deal#1789344000:ABB:10%:15930"
    ) == "1789344000:ABB"
    # Edge cases.
    assert _extract_deal_id_from_reason("") is None
    assert _extract_deal_id_from_reason("no deal here") is None
    assert _extract_deal_id_from_reason("deal#12345:BAF:more:stuff") == "12345:BAF"


def test_buy_settled_by_t2_uses_2_5_calendar_days_with_noon_cutoff() -> None:
    """T+2.5: settled from 12:00 PM on T+2, or any time on T+3+.

    The 2026-10-13 user update: "vn stock market using t+2.5".
    The half-day matters: DNSE rejects the SELL on T+2 morning
    (e.g. 10:00 ICT) even though 2 calendar days have passed.
    """
    from datetime import time as _time
    # BUY on day 0, today is day 0 → not settled.
    assert not _buy_settled_by_t2("2026-10-13", "2026-10-13")
    # BUY on day 0, today is day 1 → not settled.
    assert not _buy_settled_by_t2("2026-10-13", "2026-10-14")
    # BUY on day 0, today is day 2 BEFORE noon → NOT settled.
    assert not _buy_settled_by_t2("2026-10-13", "2026-10-15", now_time=_time(10, 0))
    # BUY on day 0, today is day 2 AT noon → settled.
    assert _buy_settled_by_t2("2026-10-13", "2026-10-15", now_time=_time(12, 0))
    # BUY on day 0, today is day 2 AFTER noon → settled.
    assert _buy_settled_by_t2("2026-10-13", "2026-10-15", now_time=_time(14, 30))
    # BUY on day 0, today is day 3 → settled (any time).
    assert _buy_settled_by_t2("2026-10-13", "2026-10-16", now_time=_time(0, 0))
    # BUY on day 0, today is day 5 → settled.
    assert _buy_settled_by_t2("2026-10-13", "2026-10-18", now_time=_time(9, 0))


def test_buy_settled_by_t2_assumes_past_noon_when_time_omitted() -> None:
    """When ``now_time`` is None, we assume past noon (conservative default).

    The bot's main loop is set to call retry_t2_settlement_sl_orders
    on every cycle (60s). If the call happens to fire before noon
    on T+2, we don't want to spam DNSE with a known-pending SL.
    But when ``now_time`` is omitted (e.g. the unit test), the
    function should assume it's safe to retry.
    """
    # T+2 with no time → assumed past noon → settled.
    assert _buy_settled_by_t2("2026-10-13", "2026-10-15", now_time=None)
    # T+1 → still not settled (the time check doesn't help here).
    assert not _buy_settled_by_t2("2026-10-13", "2026-10-14", now_time=None)


def test_buy_settled_by_t2_handles_bad_dates() -> None:
    """Invalid date strings → False (don't re-submit on garbage input)."""
    from datetime import time as _time
    assert not _buy_settled_by_t2("not-a-date", "2026-10-15")
    assert not _buy_settled_by_t2("2026-10-13", "garbage")
    # Time-typed inputs are validated by the type checker; we don't
    # try to handle non-time values here.


# -----------------------------------------------------------------------------
# retry_t2_settlement_sl_orders end-to-end tests
# -----------------------------------------------------------------------------


def _make_sl_buy_pair(
    symbol: str = "BAF",
    deal_id: str = "1785888000:BAF",
    buy_date: str = "2026-10-13",
    sl_rejected: bool = True,
    sl_reject_error: str = "HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
    buy_quantity: int = 200,
    sl_quantity: int = 200,
    buy_status: str = "filled",
) -> list[LedgerOrder]:
    """Build a BUY + rejected SL pair (typical T+2 scenario)."""
    last_detail: dict | None = None
    if buy_status == "filled":
        last_detail = {"filledQuantity": buy_quantity, "orderStatus": "Filled"}
    return [
        LedgerOrder(
            runId=buy_date, sourceOrderId=11111, sourceDate=buy_date,
            symbol=symbol, side="NB", quantity=buy_quantity, price=31000,
            reason=f"autofin_buy:deal#{deal_id}:nav30pct:Khuyến nghị đầu tư",
            status=buy_status, dnseOrderId="dnse-buy-1",
            createdAt=f"{buy_date}T03:00:00Z",
            updatedAt=f"{buy_date}T03:00:00Z",
            lastDetail=last_detail,
        ),
        LedgerOrder(
            runId=buy_date, sourceOrderId=22222, sourceDate=buy_date,
            symbol=symbol, side="NS", quantity=sl_quantity, price=27900,
            reason=f"autofin_sl:deal#{deal_id}:27900",
            status="rejected" if sl_rejected else "submitted",
            error=sl_reject_error,
            dnseOrderId="dnse-sl-1",
            createdAt=f"{buy_date}T03:00:00Z",
            updatedAt=f"{buy_date}T03:00:00Z",
        ),
    ]


def _make_client() -> MagicMock:
    client = MagicMock()
    client.get_cash_loan_package_id.return_value = 1036
    client.place_stock_order.return_value = {"id": "dnse-new-sl"}
    client.order_detail.return_value = {"id": "dnse-new-sl", "orderStatus": "New"}
    return client


def test_retry_resubmits_sl_when_t2_cleared(tmp_path) -> None:
    """BUY 2 days ago + SL rejected with T+2 error → SL is re-submitted."""
    _seed_ledger(tmp_path, _make_sl_buy_pair(buy_date="2026-10-13"))
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    assert summary["t2_blocked"] == 1
    assert summary["t2_due"] == 1
    assert summary["t2_pending"] == 0
    assert summary["resubmitted"] == 1
    assert summary["resubmit_dnse_ids"] == ["dnse-new-sl"]


def test_retry_holds_off_when_t2_not_yet_cleared(tmp_path) -> None:
    """BUY today + SL rejected → T+2 not cleared, don't re-submit yet."""
    _seed_ledger(tmp_path, _make_sl_buy_pair(buy_date="2026-10-13"))
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-13",  # same day as BUY
    )
    assert summary["t2_blocked"] == 1
    assert summary["t2_pending"] == 1
    assert summary["t2_due"] == 0
    assert summary["resubmitted"] == 0
    # place_stock_order was NOT called.
    assert not client.place_stock_order.called


def test_retry_holds_off_1_day_after_buy(tmp_path) -> None:
    """BUY 1 day ago (T+1) → still blocked, don't re-submit."""
    _seed_ledger(tmp_path, _make_sl_buy_pair(buy_date="2026-10-13"))
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-14",
    )
    assert summary["t2_pending"] == 1
    assert summary["t2_due"] == 0
    assert summary["resubmitted"] == 0


def test_retry_skips_non_t2_rejections(tmp_path) -> None:
    """SL rejected for a non-T+2 reason (e.g. INVALID_PRICE) is NOT retried."""
    pair = _make_sl_buy_pair(
        sl_reject_error="HTTP 400 INVALID_PRICE code XYZ",
    )
    _seed_ledger(tmp_path, pair)
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    assert summary["t2_blocked"] == 0
    assert summary["t2_due"] == 0
    assert summary["resubmitted"] == 0


def test_retry_skips_filled_or_open_sls(tmp_path) -> None:
    """SLs that are not 'rejected' are not eligible for retry."""
    pair = _make_sl_buy_pair(sl_rejected=False)
    _seed_ledger(tmp_path, pair)
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    assert summary["scanned"] == 0
    assert summary["resubmitted"] == 0


def test_retry_handles_no_ledger(tmp_path) -> None:
    """Empty ledger → empty summary, no errors."""
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    assert summary["scanned"] == 0
    assert summary["resubmitted"] == 0
    assert summary["resubmit_errors"] == []


def test_retry_skips_sl_with_no_matching_buy(tmp_path) -> None:
    """An SL SELL whose BUY is not in the ledger is skipped (orphan)."""
    orphan_sl = LedgerOrder(
        runId="2026-10-13", sourceOrderId=99999, sourceDate="2026-10-13",
        symbol="ZZZ", side="NS", quantity=100, price=5000,
        reason="autofin_sl:deal#9999999999:ZZZ:5000",
        status="rejected",
        error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
        createdAt="2026-10-13T09:00:00Z",
        updatedAt="2026-10-13T09:00:00Z",
    )
    _seed_ledger(tmp_path, [orphan_sl])
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    assert summary["t2_blocked"] == 1
    assert summary["t2_due"] == 0
    assert summary["resubmitted"] == 0


def test_retry_resubmits_multiple_sls(tmp_path) -> None:
    """Multiple T+2-cleared SLs are all re-submitted in one call."""
    baf = _make_sl_buy_pair(symbol="BAF", deal_id="1785888000:BAF", buy_date="2026-10-13")
    abb = _make_sl_buy_pair(symbol="ABB", deal_id="1789344000:ABB", buy_date="2026-10-13")
    _seed_ledger(tmp_path, baf + abb)
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    assert summary["t2_due"] == 2
    assert summary["resubmitted"] == 2
    # place_stock_order was called twice.
    assert client.place_stock_order.call_count == 2


def test_retry_uses_latest_buy_for_settlement_date(tmp_path) -> None:
    """If the same deal has multiple BUYs, the latest one determines T+2.

    Reproduces a re-priced order: BUY 1 was placed on day 0,
    cancelled, BUY 2 was placed on day 1 (different price, same
    deal_id). The SL was rejected. T+2 should be measured from
    BUY 2's date, not BUY 1's.
    """
    pair = _make_sl_buy_pair(buy_date="2026-10-13")
    # Add an earlier BUY at a different price for the same deal.
    pair.insert(0, LedgerOrder(
        runId="2026-10-12", sourceOrderId=11110, sourceDate="2026-10-12",
        symbol="BAF", side="NB", quantity=200, price=30500,
        reason="autofin_buy:deal#1785888000:BAF:nav30pct:Khuyến nghị đầu tư",
        status="cancelled", dnseOrderId="dnse-buy-0",
        createdAt="2026-10-12T09:00:00Z",
        updatedAt="2026-10-12T09:00:00Z",
    ))
    _seed_ledger(tmp_path, pair)
    client = _make_client()
    # Today is day 1 from the latest BUY → still pending.
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-14",
    )
    assert summary["t2_due"] == 0
    assert summary["t2_pending"] == 1
    assert summary["resubmitted"] == 0


def test_retry_uses_dnse_cash_package_lookup(tmp_path) -> None:
    """The function asks DNSE for the cash package id per symbol."""
    _seed_ledger(tmp_path, _make_sl_buy_pair(symbol="ABB"))
    client = _make_client()
    client.get_cash_loan_package_id.return_value = 1036
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    assert summary["resubmitted"] == 1
    client.get_cash_loan_package_id.assert_called_with("0003937868", "ABB")


def test_retry_records_4xx_rejection_on_resubmit(tmp_path) -> None:
    """If the re-submit fails with a new 4xx, it's marked 'rejected' (not 'unknown')."""
    _seed_ledger(tmp_path, _make_sl_buy_pair())
    client = MagicMock()
    client.get_cash_loan_package_id.return_value = 1036
    client.place_stock_order.side_effect = RuntimeError(
        'HTTP 400 INVALID_PRICE_FOR_SYMBOL'
    )
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    assert summary["t2_due"] == 1
    assert summary["resubmitted"] == 0
    assert len(summary["resubmit_errors"]) == 1
    assert "INVALID_PRICE" in summary["resubmit_errors"][0]


def test_retry_skips_sl_when_buy_never_filled(tmp_path) -> None:
    """SL whose BUY never filled (expired/cancelled) is NOT retried.

    Reproduces the 2026-10-12 ABB saga: the BUY at 17,500 expired
    at EOD, the BUY at 17,700 filled. The original SL at 15,750
    (10% below 17,500) was tied to the expired BUY and rejected.
    There's no reason to retry it — we never held those shares.
    """
    items = [
        # The original BUY that expired (no fill).
        LedgerOrder(
            runId="2026-10-12", sourceOrderId=11111, sourceDate="2026-10-12",
            symbol="ABB", side="NB", quantity=100, price=17500,
            reason="autofin_buy:deal#1789344000:ABB:nav30pct:Khuyến nghị đầu tư",
            status="expired", dnseOrderId="dnse-buy-1",
            createdAt="2026-10-12T09:00:00Z",
            updatedAt="2026-10-12T15:00:00Z",
        ),
        # The SL that was rejected with T+2 error.
        LedgerOrder(
            runId="2026-10-12", sourceOrderId=22222, sourceDate="2026-10-12",
            symbol="ABB", side="NS", quantity=100, price=15750,
            reason="autofin_user_sl:deal#1789344000:ABB:10%:15750",
            status="rejected",
            error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
            createdAt="2026-10-12T09:00:00Z",
            updatedAt="2026-10-12T09:00:00Z",
        ),
    ]
    _seed_ledger(tmp_path, items)
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    # T+2 cleared but BUY never filled → don't retry.
    assert summary["t2_blocked"] == 1
    assert summary["t2_due"] == 0
    assert summary["resubmitted"] == 0


def test_retry_skips_sl_when_buy_qty_mismatch(tmp_path) -> None:
    """SL with qty != filled qty is NOT retried.

    Reproduces a re-priced scenario: BUY 100 filled, then we
    added 200 more via a new BUY (now 300 held). The original
    SL was for 100. Re-submitting it would SELL 100 of our 300
    shares, but the SL is meant to exit the WHOLE position
    (or a different qty). The autofin strategy should re-plan
    the SL with the correct size; the retry shouldn't try to
    fix that here.
    """
    items = [
        LedgerOrder(
            runId="2026-10-13", sourceOrderId=11111, sourceDate="2026-10-13",
            symbol="BAF", side="NB", quantity=200, price=31000,
            reason="autofin_buy:deal#1785888000:BAF:nav30pct:Khuyến nghị đầu tư",
            status="filled", dnseOrderId="dnse-buy-1",
            createdAt="2026-10-13T09:00:00Z",
            updatedAt="2026-10-13T09:00:00Z",
            lastDetail={"filledQuantity": 200, "orderStatus": "Filled"},
        ),
        # SL for 200 shares — matches the BUY. Should be retried.
        LedgerOrder(
            runId="2026-10-13", sourceOrderId=22222, sourceDate="2026-10-13",
            symbol="BAF", side="NS", quantity=200, price=28080,
            reason="autofin_user_sl:deal#1785888000:BAF:10%:28080",
            status="rejected",
            error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
            createdAt="2026-10-13T09:00:00Z",
            updatedAt="2026-10-13T09:00:00Z",
        ),
        # SL for 500 shares — qty mismatch with the BUY.
        LedgerOrder(
            runId="2026-10-13", sourceOrderId=33333, sourceDate="2026-10-13",
            symbol="BAF", side="NS", quantity=500, price=28080,
            reason="autofin_user_sl:deal#1785888000:BAF:10%:28080",
            status="rejected",
            error="HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
            createdAt="2026-10-13T09:00:00Z",
            updatedAt="2026-10-13T09:00:00Z",
        ),
    ]
    _seed_ledger(tmp_path, items)
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    # Only the qty-matching SL is retried.
    assert summary["t2_blocked"] == 2
    assert summary["t2_due"] == 1
    assert summary["resubmitted"] == 1


def test_retry_includes_settled_today(tmp_path) -> None:
    """T+2 settlement on day 2 (inclusive) is allowed.

    If BUY was on 2026-10-13 and today is 2026-10-15 (2 days
    later), the BUY is settled and the SL is retried. We allow
    the retry on day 2 to be safe (Vietnam's T+2 rule says
    "after 2 trading days", which can be the same day for
    some calendar configurations).
    """
    _seed_ledger(tmp_path, _make_sl_buy_pair(buy_date="2026-10-13"))
    client = _make_client()
    summary = retry_t2_settlement_sl_orders(
        client=client, account_no="0003937868", trading_token="tok",
        data_dir=tmp_path, today_iso="2026-10-15",
        now_time=datetime.time(14, 30),
    )
    assert summary["t2_due"] == 1
    assert summary["resubmitted"] == 1


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _seed_ledger(tmp_path: Path, items: list[LedgerOrder]) -> None:
    save_ledger(
        {f"{i.runId}:{i.sourceOrderId}:{i.symbol}:{i.side}": i for i in items},
        tmp_path,
    )
