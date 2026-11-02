"""Tests for the TVN stop-loss rejection incident (2026-11-02).

What happened
-------------
The TVN protective stop was rejected by DNSE at 09:19:29 with::

    INVALID_PRICE_LOT|Invalid price lot

Three separate defects let that happen silently, and the position was
left with NO stop-loss at all:

1. The price ladder is BOARD-dependent. UPCOM (UPX) stocks use a flat
   100-VND tick at every price level — the HOSE "below 10,000 → 10 VND"
   band does NOT apply. TVN is a UPCOM stock, so the generated limit of
   9,310 (a multiple of 10) was invalid; the correct limit is 9,300.

2. The duplicate-STOP guard counted a merely ``submitted`` stop as
   "already protected". But DNSE had already rejected it — the ledger
   row was simply not updated yet. The next retry pass concluded "300
   shares already protected — skipping" and the position was left
   unprotected for the rest of the day.

3. A SYNCHRONOUS rejection never notified. The submit path writes
   ``rejected`` (terminal) straight to the ledger, and the order
   monitor skips terminal rows — so it never polled the order and
   never sent a Telegram message. The operator heard nothing.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.bots.runner import (
    _hose_price_tick,
    _stop_is_confirmed_live,
)


# -----------------------------------------------------------------------------
# 1. the price ladder is board-dependent
# -----------------------------------------------------------------------------

def test_upcom_uses_a_flat_100_vnd_tick() -> None:
    """UPCOM (UPX) — and HNX — use 100 VND at EVERY price level."""
    # TVN: the stop at 9,360 -> 9,313 -> 9,310 with the HOSE ladder.
    # That is a multiple of 10 but NOT of 100 → INVALID_PRICE_LOT.
    assert _hose_price_tick(9_313, "UPX") == 9_300
    assert _hose_price_tick(9_313, "upx") == 9_300
    assert _hose_price_tick(9_313, "HNX") == 9_300

    # The HOSE ladder would have produced the invalid 9,310.
    assert _hose_price_tick(9_313) == 9_310
    assert _hose_price_tick(9_313, "STO") == 9_310


def test_the_hose_ladder_is_unchanged() -> None:
    """The HOSE (STO) banded ladder must not regress."""
    assert _hose_price_tick(9_995, "STO") == 9_990
    assert _hose_price_tick(5_123, "STO") == 5_120
    assert _hose_price_tick(30_950, "STO") == 30_950
    assert _hose_price_tick(30_640, "STO") == 30_600
    assert _hose_price_tick(49_999, "STO") == 49_950
    assert _hose_price_tick(57_777, "STO") == 57_700
    assert _hose_price_tick(100_000, "STO") == 100_000


def test_the_tvn_stop_limit_is_now_valid() -> None:
    """The exact TVN case: stop 9,360 → limit must be 9,300."""
    stop_level = 9_360
    raw = int(round(stop_level * 0.995))
    assert raw == 9_313
    limit = _hose_price_tick(raw, "UPX")
    assert limit == 9_300
    assert limit % 100 == 0, "a UPCOM price must be a multiple of 100"


def test_the_abb_stop_limit_is_now_upcom_correct() -> None:
    """ABB is also UPX. Its 15,850 limit was accepted only by luck.

    15,850 is a multiple of 50 (HOSE ladder) and of 10, but NOT of
    100 — so it was one bad price away from being rejected.
    """
    assert _hose_price_tick(15_761, "UPX") == 15_700
    assert _hose_price_tick(15_761, "STO") == 15_750


def test_an_unknown_market_falls_back_to_the_hose_ladder() -> None:
    """A failed market lookup must not produce a wild price."""
    assert _hose_price_tick(9_313, "") == 9_310
    assert _hose_price_tick(9_313, None) == 9_310
    assert _hose_price_tick(0, "UPX") == 0


# -----------------------------------------------------------------------------
# 2. the duplicate-STOP guard needs broker confirmation
# -----------------------------------------------------------------------------

def _stop_row(status="submitted", detail=None, symbol="TVN", qty=300):
    from vietnam_algo.libs.order_ledger import LedgerOrder

    return LedgerOrder(
        runId="2026-11-02", sourceOrderId=1, sourceDate="2026-11-02",
        symbol=symbol, side="NS", quantity=qty, price=9300,
        reason="autofin_user_sl:deal#1792713600:TVN:10%:9360",
        status=status, orderCategory="STOP", stopPrice=9360,
        dnseOrderId="dbjv6cb6tmgc73bddbag",
        createdAt="2026-11-02T02:19:29Z", updatedAt="2026-11-02T02:19:29Z",
        lastDetail=detail,
    )


def test_a_just_submitted_stop_is_not_confirmed() -> None:
    """The TVN case: ``submitted`` but DNSE has not confirmed it.

    The guard must NOT treat this as "already protected" — DNSE can
    still reject it (and did, with INVALID_PRICE_LOT).
    """
    assert _stop_is_confirmed_live(_stop_row(status="submitted")) is False


def test_a_stop_confirmed_new_by_the_broker_is_protected() -> None:
    """Once the monitor polls the order and sees it live, it counts."""
    detail = {"id": "dbheejb6tmgc73bdauj0", "orderStatus": "New",
              "price": 15850, "stopPrice": 15930}
    assert _stop_is_confirmed_live(_stop_row(status="submitted", detail=detail)) is True


def test_a_stop_confirmed_rejected_is_not_protected() -> None:
    """A rejected detail must never count as protection."""
    detail = {"id": "dbjv6cb6tmgc73bddbag", "orderStatus": "Rejected",
              "errorMessage": "INVALID_PRICE_LOT|Invalid price lot"}
    assert _stop_is_confirmed_live(_stop_row(status="submitted", detail=detail)) is False


def test_an_open_or_partial_row_is_trusted() -> None:
    """``open``/``partial`` are only written by the monitor from a live
    detail, so they count without re-reading it."""
    assert _stop_is_confirmed_live(_stop_row(status="open")) is True
    assert _stop_is_confirmed_live(_stop_row(status="partial")) is True


def test_pending_statuses_count_as_live() -> None:
    for status in ("PendingNew", "pendingCancel", "PartiallyFilled"):
        detail = {"orderStatus": status}
        assert _stop_is_confirmed_live(
            _stop_row(status="submitted", detail=detail)) is True


def test_a_rejected_ledger_row_is_never_protected() -> None:
    assert _stop_is_confirmed_live(_stop_row(status="rejected")) is False


# -----------------------------------------------------------------------------
# 3. a synchronous rejection notifies
# -----------------------------------------------------------------------------

def test_a_rejected_protective_stop_notifies_telegram(tmp_path, monkeypatch) -> None:
    """A 4xx rejection of a protective STOP must reach Telegram.

    2026-11-02 (TVN): the submit path writes ``rejected`` straight to
    the ledger, and the order monitor SKIPS terminal rows — so it
    never polled the order and never notified. The operator heard
    nothing while the position sat unprotected.
    """
    from vietnam_algo.libs.daily_engine import execute_orders_with_ledger
    from vietnam_algo.libs.order_ledger import save_ledger
    from vietnam_algo.libs.trading_engine import DnseOrder

    messages: list[str] = []
    import vietnam_algo.libs.daily_engine as de
    monkeypatch.setattr(de, "notify", lambda m: messages.append(m))

    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    save_ledger({}, tmp_path)

    client = MagicMock()
    client.place_stock_order.side_effect = RuntimeError(
        'DNSE POST /accounts/0003937868/orders -> HTTP 400 '
        '{"status":400,"code":"INVALID_PRICE_LOT","message":"Invalid price lot"}'
    )

    order = DnseOrder(
        symbol="TVN", side="NS", quantity=300, price=9300,
        loanPackageId=1775, orderType="LO",
        orderCategory="STOP", stopPrice=9360,
        conditionOperator="<=", durationType="GTD",
        sourceOrderId=118262733281696, sourceDate="2026-11-02",
        reason="autofin_user_sl:deal#1792713600:TVN:10%:9360 [t2-retry from 2026-10-28]",
    )

    results, _recon, _complete = execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-11-02",
        strategy="autofin_signal",
    )

    assert results[0]["status"] == "rejected"
    assert len(messages) == 1, f"the rejection must notify: {messages}"
    msg = messages[0]
    assert "UNPROTECTED" in msg
    assert "TVN" in msg
    assert "INVALID_PRICE_LOT" in msg


def test_a_rejected_normal_order_does_not_notify(tmp_path, monkeypatch) -> None:
    """A plain BUY rejection is reported by the submit summary — the
    protective-stop alert is for stops only."""
    from vietnam_algo.libs.daily_engine import execute_orders_with_ledger
    from vietnam_algo.libs.order_ledger import save_ledger
    from vietnam_algo.libs.trading_engine import DnseOrder

    messages: list[str] = []
    import vietnam_algo.libs.daily_engine as de
    monkeypatch.setattr(de, "notify", lambda m: messages.append(m))

    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    save_ledger({}, tmp_path)

    client = MagicMock()
    client.place_stock_order.side_effect = RuntimeError(
        'DNSE POST /accounts/0003937868/orders -> HTTP 400 '
        '{"status":400,"code":"INVALID_SYMBOL","message":"bad symbol"}'
    )

    order = DnseOrder(
        symbol="ZZZ", side="NB", quantity=100, price=10000,
        loanPackageId=1775, orderType="LO",
        sourceOrderId=1, sourceDate="2026-11-02",
        reason="autofin_buy:deal#1:ZZZ:nav15pct:x",
    )

    execute_orders_with_ledger(
        client=client, account_no="0003937868", trading_token="tok",
        orders=[order], data_dir=tmp_path, run_id="2026-11-02",
    )

    assert messages == [], "a non-stop rejection must not raise the stop alert"


# -----------------------------------------------------------------------------
# the monitor's message must carry the reason
# -----------------------------------------------------------------------------

def test_the_status_change_message_carries_the_error_message() -> None:
    """DNSE puts the reason in ``errorMessage``, not ``rejectReason``."""
    from vietnam_algo.bots.runner import _format_status_change_message
    from vietnam_algo.libs.order_ledger import LedgerOrder

    item = LedgerOrder(
        runId="2026-11-02", sourceOrderId=1, sourceDate="2026-11-02",
        symbol="TVN", side="NS", quantity=300, price=9310,
        reason="autofin_user_sl:deal#1792713600:TVN:10%:9360",
        status="submitted", dnseOrderId="dbjv6cb6tmgc73bddbag",
        createdAt="2026-11-02T02:19:29Z", updatedAt="2026-11-02T02:19:29Z",
    )
    detail = {
        "orderStatus": "Rejected",
        "errorMessage": "INVALID_PRICE_LOT|Invalid price lot",
    }
    msg = _format_status_change_message(item, "submitted", "rejected", detail)
    assert "INVALID_PRICE_LOT" in msg
    assert "REJECTED" in msg
