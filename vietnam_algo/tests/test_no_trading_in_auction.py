"""Tests for the ATO / ATC auction gate (2026-10-31).

The user asked for this explicitly::

    "let check again the time we create the order, i see that some
     order we create at 9AM in ATO session, that should not be, we
     should not do trade in ato and atc session"

The live ledger shows the damage::

    2026-10-26 09:01:08  ORS  BUY  price=0  [session=ATO]
    2026-10-29 09:01:17  VOS  BUY  price=0  [session=ATO]
    2026-10-29 09:01:17  VOS  STOP price=10450  [session=ATO]

Two problems with an auction order:

1. The fill price is decided by the auction, not by us. The VOS BUY
   filled at 11,800 (the ATO price) while the continuous-market bid
   was ~10,800 — we paid the auction premium and had no say in it.

2. ``price=0`` on a BUY means "buy at ANY price" to DNSE. That is an
   unbounded risk, not a bounded one.

The decision (the user's explicit choice) is to block ALL orders in
an auction — trades AND protective stops. Post-close (14:45-15:00) is
NOT an auction: it is a normal LO session on HOSE (PLO) with a real
price, so it stays tradeable.

Three submit paths are covered here:

* ``_submit_autofin_deals_to_dnse``  — the main mid-day/EOD submit
* ``retry_t2_settlement_sl_orders``   — the T+2 protective-stop retry
* the order re-pricer                 — cancel + re-place at a new price
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.schedule import (
    TradingSchedule,
    TradingSession,
    is_auction_session,
)


# -----------------------------------------------------------------------------
# the session helper
# -----------------------------------------------------------------------------

def test_is_auction_session_covers_ato_and_atc() -> None:
    assert is_auction_session(TradingSession.ATO)
    assert is_auction_session(TradingSession.ATC)
    assert not is_auction_session(TradingSession.CONTINUOUS)
    assert not is_auction_session(TradingSession.POST_CLOSE)
    assert not is_auction_session(TradingSession.OFF_HOURS)


def test_post_close_is_not_an_auction() -> None:
    """14:45-15:00 is a normal LO session (PLO) with a real price."""
    assert not is_auction_session(TradingSession.POST_CLOSE)


# -----------------------------------------------------------------------------
# 1. the main submit path
# -----------------------------------------------------------------------------

def _entry_deal(symbol: str = "VOS", price: int = 11100) -> dict:
    return {
        "action": "new_entry",
        "symbol": symbol,
        "deal_id": "1792627200:VOS",
        "dnse_orders": [{
            "symbol": symbol, "side": "NB", "price": price,
            "quantity": 300, "kind": "entry",
            "reason": f"autofin_buy:deal#1792627200:{symbol}:nav15pct:x",
        }],
    }


def _stop_deal() -> dict:
    return {
        "action": "partial_close",
        "symbol": "VOS",
        "deal_id": "1792627200:VOS",
        "dnse_orders": [{
            "symbol": "VOS", "side": "NS", "price": 10550,
            "quantity": 300, "kind": "sl",
            "reason": "autofin_sl:deal#1792627200:VOS:10550",
        }],
    }


def _client() -> MagicMock:
    client = MagicMock()
    client.get_market_id.return_value = "HSX"
    client.get_cash_loan_package_id.return_value = 1775
    client.place_stock_order.return_value = {"id": "new-order"}
    client.order_detail.return_value = {"id": "new-order", "orderStatus": "New"}
    return client


def _submit(deals, when, tmp_path: Path, monkeypatch) -> dict:
    """Run the submit helper with the ICT clock pinned to ``when``."""
    from zoneinfo import ZoneInfo

    import vietnam_algo.bots.runner as runner_mod

    ict = ZoneInfo("Asia/Ho_Chi_Minh")
    aware = when if when.tzinfo else when.replace(tzinfo=ict)
    monkeypatch.setattr(runner_mod, "vietnam_now", lambda: aware)
    monkeypatch.setattr(runner_mod, "notify", lambda msg: None)

    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
    return _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868", loan_package_id=1775,
        trading_token="tok", client=_client(), data_dir=tmp_path,
        trade_date=when.strftime("%Y-%m-%d"),
    )


@pytest.mark.parametrize(
    "hh,mm",
    [
        (9, 0),    # ATO opens
        (9, 5),    # mid-ATO
        (9, 14),   # last minute of ATO
        (14, 30),  # ATC opens
        (14, 35),  # mid-ATC
        (14, 44),  # last minute of ATC
    ],
)
def test_no_order_is_submitted_in_an_auction(hh, mm, tmp_path, monkeypatch) -> None:
    """The core rule: no order reaches DNSE in ATO or ATC."""
    when = datetime.datetime(2026, 10, 29, hh, mm)
    result = _submit([_entry_deal()], when, tmp_path, monkeypatch)

    assert result["total"] == 0, result
    assert result["submitted"] == []
    assert result.get("skipped_auction") == 1, result


@pytest.mark.parametrize(
    "hh,mm",
    [
        (9, 15),   # continuous opens
        (10, 0),
        (11, 29),
        (13, 0),
        (14, 0),
        (14, 29),  # last minute of the afternoon continuous session
        (14, 45),  # post-close (PLO) — NOT an auction
        (14, 59),
    ],
)
def test_orders_are_submitted_outside_the_auction(hh, mm, tmp_path, monkeypatch) -> None:
    """The continuous + post-close sessions still trade normally."""
    when = datetime.datetime(2026, 10, 29, hh, mm)
    result = _submit([_entry_deal()], when, tmp_path, monkeypatch)

    assert result.get("skipped_auction") is None, result
    assert result["total"] == 1, result


def test_a_protective_stop_is_blocked_in_an_auction(tmp_path, monkeypatch) -> None:
    """The user's explicit choice: block ALL orders, stops included."""
    when = datetime.datetime(2026, 10, 29, 9, 1)  # exactly the VOS case
    result = _submit([_stop_deal()], when, tmp_path, monkeypatch)

    assert result["total"] == 0
    assert result["submitted"] == []
    assert result.get("skipped_auction") == 1


def test_the_auction_gate_notifies_the_operator(tmp_path, monkeypatch) -> None:
    """A blocked submit must be visible — not silently skipped."""
    from zoneinfo import ZoneInfo

    import vietnam_algo.bots.runner as runner_mod

    ict = ZoneInfo("Asia/Ho_Chi_Minh")
    when = datetime.datetime(2026, 10, 29, 9, 1, tzinfo=ict)
    monkeypatch.setattr(runner_mod, "vietnam_now", lambda: when)

    messages: list[str] = []
    monkeypatch.setattr(runner_mod, "notify", lambda msg: messages.append(msg))

    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
    _submit_autofin_deals_to_dnse(
        [_entry_deal()],
        account_no="0003937868", loan_package_id=1775,
        trading_token="tok", client=_client(), data_dir=tmp_path,
        trade_date="2026-10-29",
    )

    assert len(messages) == 1, messages
    assert "ATO" in messages[0]
    assert "auction" in messages[0].lower()


# -----------------------------------------------------------------------------
# 2. the T+2 protective-stop retry
# -----------------------------------------------------------------------------

def test_t2_retry_is_skipped_in_an_auction(tmp_path) -> None:
    """The T+2 retry places real orders — it must respect the gate."""
    from vietnam_algo.bots.runner import retry_t2_settlement_sl_orders

    client = MagicMock()
    client.get_positions.return_value = {"positions": []}

    for hh, mm in ((9, 1), (9, 10), (14, 30), (14, 40)):
        summary = retry_t2_settlement_sl_orders(
            client=client,
            account_no="0003937868",
            trading_token="tok",
            data_dir=tmp_path,
            today_iso="2026-10-29",
            now_time=datetime.time(hh, mm),
            loan_package_id=1775,
        )
        assert summary["skipped_auction"] == 1, (hh, mm, summary)
        assert summary["resubmitted"] == 0, (hh, mm, summary)
        assert client.place_stock_order.call_count == 0, (hh, mm)


def test_t2_retry_still_runs_in_a_continuous_session(tmp_path) -> None:
    """The gate must not break the retry outside the auction windows."""
    from vietnam_algo.bots.runner import retry_t2_settlement_sl_orders

    client = MagicMock()
    client.get_positions.return_value = {"positions": []}

    summary = retry_t2_settlement_sl_orders(
        client=client,
        account_no="0003937868",
        trading_token="tok",
        data_dir=tmp_path,
        today_iso="2026-10-29",
        now_time=datetime.time(14, 0),
        loan_package_id=1775,
    )
    assert summary["skipped_auction"] == 0
    assert summary["resubmitted"] == 0  # nothing to retry in an empty ledger


# -----------------------------------------------------------------------------
# 3. the order re-pricer
# -----------------------------------------------------------------------------

def test_the_repricer_is_skipped_in_an_auction() -> None:
    """The re-pricer cancels + re-places an order — that is a submit.

    It must not run in an auction window either.
    """
    from vietnam_algo.bots.runner import _maybe_reprice_stale_order
    from vietnam_algo.libs.order_ledger import LedgerOrder

    item = LedgerOrder(
        runId="2026-10-29", sourceOrderId=111, sourceDate="2026-10-29",
        symbol="VOS", side="NB", quantity=300, price=11100,
        reason="autofin_buy:deal#1792627200:VOS:nav15pct:x",
        status="open", dnseOrderId="abc",
        createdAt="2026-10-29T02:00:00Z", updatedAt="2026-10-29T02:00:00Z",
    )

    client = MagicMock()

    # 09:05 ICT -> inside the ATO auction.
    ato_epoch = datetime.datetime(
        2026, 10, 29, 9, 5, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")
    ).timestamp()
    summary = _maybe_reprice_stale_order(
        client, "0003937868", "tok", item,
        now_epoch=ato_epoch, reprice_after_seconds=900,
    )
    assert summary["action"] == "skipped", summary
    assert "auction" in summary["reason"].lower(), summary
    assert client.place_stock_order.call_count == 0
    assert client.cancel_order.call_count == 0

    # 14:35 ICT -> inside the ATC auction.
    atc_epoch = datetime.datetime(
        2026, 10, 29, 14, 35, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")
    ).timestamp()
    summary = _maybe_reprice_stale_order(
        client, "0003937868", "tok", item,
        now_epoch=atc_epoch, reprice_after_seconds=900,
    )
    assert summary["action"] == "skipped", summary
    assert client.place_stock_order.call_count == 0


# -----------------------------------------------------------------------------
# 4. the schedule constants
# -----------------------------------------------------------------------------

def test_the_auction_windows_match_the_hose_calendar() -> None:
    """ATO 09:00-09:15 and ATC 14:30-14:45."""
    s = TradingSchedule()
    assert s.atoStart == datetime.time(9, 0)
    assert s.atoEnd == datetime.time(9, 15)
    assert s.atcStart == datetime.time(14, 30)
    assert s.atcEnd == datetime.time(14, 45)
