"""Tests for the 2026-10-08 trading-window extension.

The bot's order-placement window was 09:15-11:30 and 13:00-14:45 ICT
(continuous sub-windows only). The user wanted to extend it to
09:00-15:00 to cover the HOSE/HNX ATO opening auction (09:00-09:15)
and the post-close price-ladder window (14:45-15:00). This commit
also adds ATO/ATC support to the session detection because the
DNSE spec requires ATO/ATC orderTypes (with price=0) during the
auction sub-windows; LO orders are rejected there.

These tests pin:
  - The new TradingSchedule defaults: morningStart 09:00, afternoonEnd 15:00
  - current_session() correctly classifies every minute of the trading day
  - order_type_for_session() returns the right orderType per session
  - _submit_autofin_deals_to_dnse short-circuits off-hours with a Telegram
    notify and zero totals
  - In ATO/ATC, the submitted order's price is forced to 0 and the
    orderType is overridden to ATO/ATC
  - In continuous, the orderType stays LO and price matches the signal
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.schedule import (  # noqa: E402
    TradingSchedule,
    TradingSession,
    current_session,
    order_type_for_session,
)


# -----------------------------------------------------------------------------
# TradingSchedule defaults — the user-facing extension
# -----------------------------------------------------------------------------


def test_morning_start_extended_to_09_00() -> None:
    """morningStart 09:15 → 09:00 to cover the ATO opening auction."""
    assert TradingSchedule().morningStart == datetime.strptime("09:00", "%H:%M").time()


def test_afternoon_end_extended_to_15_00() -> None:
    """afternoonEnd 14:45 → 15:00 to cover the post-close window."""
    assert TradingSchedule().afternoonEnd == datetime.strptime("15:00", "%H:%M").time()


def test_ato_and_atc_subwindows_defined() -> None:
    """The 4 auction/post-close sub-window boundaries are present."""
    s = TradingSchedule()
    assert s.atoStart == datetime.strptime("09:00", "%H:%M").time()
    assert s.atoEnd == datetime.strptime("09:15", "%H:%M").time()
    assert s.atcStart == datetime.strptime("14:30", "%H:%M").time()
    assert s.atcEnd == datetime.strptime("14:45", "%H:%M").time()


# -----------------------------------------------------------------------------
# current_session() — every minute of the trading day, in 5-min steps
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("hour_minute, expected", [
    # Pre-open
    ((8, 30), TradingSession.OFF_HOURS),
    ((8, 59), TradingSession.OFF_HOURS),
    # ATO auction 09:00-09:15
    ((9, 0),  TradingSession.ATO),
    ((9, 5),  TradingSession.ATO),
    ((9, 10), TradingSession.ATO),
    ((9, 14), TradingSession.ATO),
    # Continuous morning 09:15-11:30
    ((9, 15), TradingSession.CONTINUOUS),
    ((9, 30), TradingSession.CONTINUOUS),
    ((10, 0), TradingSession.CONTINUOUS),
    ((11, 0), TradingSession.CONTINUOUS),
    ((11, 29), TradingSession.CONTINUOUS),
    # Lunch break 11:30-13:00
    ((11, 30), TradingSession.OFF_HOURS),
    ((12, 0),  TradingSession.OFF_HOURS),
    ((12, 59), TradingSession.OFF_HOURS),
    # Continuous afternoon 13:00-14:30
    ((13, 0),  TradingSession.CONTINUOUS),
    ((13, 30), TradingSession.CONTINUOUS),
    ((14, 0),  TradingSession.CONTINUOUS),
    ((14, 29), TradingSession.CONTINUOUS),
    # ATC closing auction 14:30-14:45
    ((14, 30), TradingSession.ATC),
    ((14, 35), TradingSession.ATC),
    ((14, 44), TradingSession.ATC),
    # Post-close 14:45-15:00
    ((14, 45), TradingSession.POST_CLOSE),
    ((14, 50), TradingSession.POST_CLOSE),
    ((14, 59), TradingSession.POST_CLOSE),
    # After hours
    ((15, 0),  TradingSession.OFF_HOURS),
    ((15, 30), TradingSession.OFF_HOURS),
    ((18, 0),  TradingSession.OFF_HOURS),
])
def test_current_session_classifies_every_minute(hour_minute, expected) -> None:
    h, m = hour_minute
    # 2026-10-08 is a Thursday
    moment = datetime(2026, 10, 8, h, m, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    assert current_session(moment) == expected


def test_current_session_weekend_is_off_hours() -> None:
    """Saturday and Sunday are always off_hours regardless of clock time."""
    # 2026-10-10 is Saturday, 2026-10-11 is Sunday.
    for day in (10, 11):
        moment = datetime(2026, 10, day, 10, 0, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
        assert current_session(moment) == TradingSession.OFF_HOURS


def test_current_session_holiday_is_off_hours() -> None:
    """A holiday in the holidays set is off_hours regardless of clock time."""
    moment = datetime(2026, 10, 8, 10, 0, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    assert current_session(moment, holidays={"2026-10-08"}) == TradingSession.OFF_HOURS


# -----------------------------------------------------------------------------
# order_type_for_session() — pick the right DNSE orderType
# -----------------------------------------------------------------------------


def test_order_type_ato_for_auction() -> None:
    """ATO auction window requires orderType='ATO' (with price=0)."""
    assert order_type_for_session(TradingSession.ATO) == "ATO"


def test_order_type_atc_for_closing_auction() -> None:
    """ATC closing auction requires orderType='ATC' (with price=0)."""
    assert order_type_for_session(TradingSession.ATC) == "ATC"


def test_order_type_lo_for_continuous() -> None:
    """Continuous trading uses LO with the signal price."""
    assert order_type_for_session(TradingSession.CONTINUOUS) == "LO"


def test_order_type_lo_for_post_close() -> None:
    """Post-close session: bot sends LO and lets DNSE route by board."""
    assert order_type_for_session(TradingSession.POST_CLOSE) == "LO"


def test_order_type_off_hours_raises() -> None:
    """off_hours is not a real trading session — caller should not submit."""
    with pytest.raises(ValueError, match="off-hours"):
        order_type_for_session(TradingSession.OFF_HOURS)


# -----------------------------------------------------------------------------
# _submit_autofin_deals_to_dnse: session-aware orderType wiring
# -----------------------------------------------------------------------------


class _StubLedger:
    """Minimal stand-in for execute_orders_with_ledger; returns its inputs."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *, client, account_no, trading_token, orders, data_dir,
                 run_id, strategy: str = ""):
        from vietnam_algo.libs.trading_engine import DnseOrder
        self.calls.append(
            dict(
                client=client, account_no=account_no, trading_token=trading_token,
                orders=list(orders), data_dir=data_dir, run_id=run_id,
                strategy=strategy,
            )
        )
        return [
            {
                "status": "submitted",
                "order": o,
                "response": {"id": f"DNSE-{i:06d}"},
                "detail": {"id": f"DNSE-{i:06d}", "status": "NEW"},
            }
            for i, o in enumerate(orders)
        ], [], True


@pytest.fixture
def stub_ledger(monkeypatch):
    from vietnam_algo.libs import daily_engine
    stub = _StubLedger()
    monkeypatch.setattr(daily_engine, "execute_orders_with_ledger", stub)
    return stub


class _StubDnseClient:
    """Stand-in for ``DnseClient`` that returns a per-symbol cash package id.

    2026-10-12: the runner now calls ``client.get_cash_loan_package_id(
    account_no, sym)`` for every symbol in the submit batch. This stub
    pretends every symbol has the real cash package id ``1036``.

    2026-10-12: the runner also calls ``client.get_market_id(sym)`` to
    determine the symbol's board (HSX/HNX/UPX) so the orderType can be
    overridden for UPCOM (always LO). The default market is ``HSX``;
    tests can populate ``market_by_symbol`` to override.
    """

    def __init__(self) -> None:
        self.cash_pkg_by_symbol: dict[str, int] = {}  # default → 1036
        self.market_by_symbol: dict[str, str] = {}  # default → HSX
        self.get_latest_quote_calls: list[str] = []

    def get_cash_loan_package_id(self, account_no: str, symbol: str) -> int:
        sym = symbol.upper()
        if sym in self.cash_pkg_by_symbol:
            return self.cash_pkg_by_symbol[sym]
        return 1036  # default cash package id for tests

    def get_market_id(self, symbol: str) -> str:
        sym = symbol.upper()
        if sym in self.market_by_symbol:
            return self.market_by_symbol[sym]
        return "HSX"  # default — accepts all order types

    def get_latest_quote(self, symbol: str) -> dict:
        self.get_latest_quote_calls.append(symbol.upper())
        # Return a mid-zone price so the live-quote gate doesn't drop
        return {"sym": symbol.upper(), "g1": {"askPrice": 20000}}


@pytest.fixture
def stub_client() -> _StubDnseClient:
    return _StubDnseClient()


def _patch_now(monkeypatch, year, month, day, hour, minute):
    """Pin vietnam_now() to a specific moment so the session classifier
    returns a deterministic answer."""
    from vietnam_algo.bots import runner
    fixed = datetime(year, month, day, hour, minute, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    monkeypatch.setattr(runner, "vietnam_now", lambda: fixed)
    return fixed


def _entry_deal(symbol="ABW", side="NB", price=14100, quantity=1000):
    return {
        "action": "new_entry",
        "symbol": symbol,
        "deal_id": f"1785456000:{symbol}",
        "dnse_orders": [{
            "symbol": symbol,
            "side": side,
            "price": price,
            "quantity": quantity,
            "kind": "entry",
            "reason": f"autofin_buy:deal#1785456000:{symbol}:nav15pct:Khuyến nghị đầu tư",
        }],
    }


def test_submit_in_continuous_uses_LO_with_signal_price(stub_ledger, stub_client, monkeypatch) -> None:
    """Continuous morning: orderType stays LO, price matches signal.
    2026-10-12: bot looks up the cash package id per symbol via
    ``client.get_cash_loan_package_id()``. The stub returns ``1036``
    for any symbol.
    """
    _patch_now(monkeypatch, 2026, 10, 8, 10, 0)  # 10:00 ICT, continuous
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
    result = _submit_autofin_deals_to_dnse(
        [_entry_deal(price=14100)],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["total"] == 1
    o = stub_ledger.calls[0]["orders"][0]
    assert o.orderType == "LO"
    assert o.price == 14100
    assert o.loanPackageId == 1036  # cash package id from stub


def test_submit_in_ato_is_blocked(stub_ledger, stub_client, monkeypatch) -> None:
    """ATO auction window: NO order is submitted at all.

    2026-10-31: the user asked for this explicitly — "we should not
    do trade in ato and atc session".

    An auction order is sent with ``price=0``: the fill price is
    decided by the auction, not by us. The live ledger shows the
    damage — the VOS BUY submitted at 2026-10-29 09:01:17 was an
    ATO order and filled at 11,800 while the continuous-market bid
    was ~10,800. On a BUY, ``price=0`` means "buy at ANY price".
    """
    _patch_now(monkeypatch, 2026, 10, 8, 9, 5)  # 09:05 ICT, ATO
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse

    notify_calls: list[str] = []
    monkeypatch.setattr(
        "vietnam_algo.bots.runner.notify", lambda msg: notify_calls.append(msg)
    )

    result = _submit_autofin_deals_to_dnse(
        [_entry_deal(price=14100)],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["total"] == 0
    assert result["submitted"] == []
    assert stub_ledger.calls == [], "the ledger must NOT be called in an auction"
    assert result.get("skipped_auction") == 1
    assert len(notify_calls) == 1
    assert "ATO" in notify_calls[0]


def test_submit_in_atc_is_blocked(stub_ledger, stub_client, monkeypatch) -> None:
    """ATC closing auction: NO order is submitted at all."""
    _patch_now(monkeypatch, 2026, 10, 8, 14, 35)  # 14:35 ICT, ATC
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse

    notify_calls: list[str] = []
    monkeypatch.setattr(
        "vietnam_algo.bots.runner.notify", lambda msg: notify_calls.append(msg)
    )

    result = _submit_autofin_deals_to_dnse(
        [_entry_deal(price=14100)],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["total"] == 0
    assert result["submitted"] == []
    assert stub_ledger.calls == [], "the ledger must NOT be called in an auction"
    assert result.get("skipped_auction") == 1
    assert len(notify_calls) == 1
    assert "ATC" in notify_calls[0]


def test_submit_in_ato_blocks_protective_stop_too(stub_ledger, stub_client, monkeypatch) -> None:
    """A protective STOP is also blocked in an auction.

    2026-10-31: the user's explicit choice — "Block ALL orders
    incl. stops". A stop planned during the window is deferred to
    the next continuous session by the re-arm logic.

    (The VOS protective stop was in fact created at 2026-10-29
    09:01:17 — in the ATO session.)
    """
    _patch_now(monkeypatch, 2026, 10, 8, 9, 5)  # 09:05 ICT, ATO
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse

    monkeypatch.setattr("vietnam_algo.bots.runner.notify", lambda msg: None)

    stop_deal = {
        "action": "partial_close",
        "symbol": "VOS",
        "deal_id": "1792627200:VOS",
        "dnse_orders": [{
            "symbol": "VOS", "side": "NS", "price": 10550,
            "quantity": 300, "kind": "sl",
            "reason": "autofin_sl:deal#1792627200:VOS:10550",
        }],
    }
    result = _submit_autofin_deals_to_dnse(
        [stop_deal],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["total"] == 0
    assert stub_ledger.calls == [], "a stop must NOT be placed in an auction"
    assert result.get("skipped_auction") == 1


def test_submit_in_post_close_uses_LO(stub_ledger, stub_client, monkeypatch) -> None:
    """Post-close window: bot sends LO with the signal price; DNSE routes by board."""
    _patch_now(monkeypatch, 2026, 10, 8, 14, 50)  # 14:50 ICT, post-close
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
    result = _submit_autofin_deals_to_dnse(
        [_entry_deal(price=14100)],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["total"] == 1
    o = stub_ledger.calls[0]["orders"][0]
    assert o.orderType == "LO"
    assert o.price == 14100
    assert o.loanPackageId == 1036


def test_submit_off_hours_short_circuits(stub_ledger, monkeypatch) -> None:
    """Off-hours: bot does NOT call the ledger, returns zero totals, notifies."""
    _patch_now(monkeypatch, 2026, 10, 8, 11, 45)  # 11:45 ICT, lunch break
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse

    notify_calls: list[list[str]] = []
    monkeypatch.setattr(
        "vietnam_algo.bots.runner.notify", lambda msg: notify_calls.append(msg)
    )

    result = _submit_autofin_deals_to_dnse(
        [_entry_deal()],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=None, data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["total"] == 0
    assert result["submitted"] == []
    assert stub_ledger.calls == [], "ledger must NOT be called off-hours"
    assert len(notify_calls) == 1
    assert "off-hours" in notify_calls[0].lower()
    assert result.get("skipped_off_hours") == 1


def test_submit_off_hours_evening_does_not_call_ledger(stub_ledger, monkeypatch) -> None:
    """After-hours (18:00) also short-circuits."""
    _patch_now(monkeypatch, 2026, 10, 8, 18, 0)
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
    result = _submit_autofin_deals_to_dnse(
        [_entry_deal()],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=None, data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["total"] == 0
    assert stub_ledger.calls == []


def test_submit_off_hours_weekend_does_not_call_ledger(stub_ledger, monkeypatch) -> None:
    """Saturday also short-circuits."""
    _patch_now(monkeypatch, 2026, 10, 10, 10, 0)  # Saturday
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
    result = _submit_autofin_deals_to_dnse(
        [_entry_deal()],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=None, data_dir=Path("/tmp"),
        trade_date="2026-10-10",
    )
    assert result["total"] == 0
    assert stub_ledger.calls == []


# -----------------------------------------------------------------------------
# DnseClient.place_stock_order: price=0 for any non-LO orderType
# -----------------------------------------------------------------------------


def test_place_stock_order_sends_price_zero_for_ATC(monkeypatch) -> None:
    """ATC orders must carry price=0 (auction determines fill)."""
    from vietnam_algo.libs.dnse.client import DnseClient
    captured: dict = {"req": None}

    def fake_urlopen(req, timeout=20):
        captured["req"] = req
        class _R:
            def read(self): return b'{"id": 1, "orderStatus": "pendingNew"}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = DnseClient(api_key="k" * 8, api_secret="s" * 16)
    client.place_stock_order(
        account_no="0003937868", trading_token="t",
        symbol="VCB", side="NB", quantity=100, price=50000,
        loan_package_id=1775, order_type="ATC",
    )
    import json
    body = json.loads(captured["req"].data)
    assert body["orderType"] == "ATC"
    assert body["price"] == 0, "ATC orders must send price=0"


def test_place_stock_order_sends_price_zero_for_ATO(monkeypatch) -> None:
    """ATO orders must carry price=0 (auction determines fill)."""
    from vietnam_algo.libs.dnse.client import DnseClient
    captured: dict = {"req": None}

    def fake_urlopen(req, timeout=20):
        captured["req"] = req
        class _R:
            def read(self): return b'{"id": 1}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = DnseClient(api_key="k" * 8, api_secret="s" * 16)
    client.place_stock_order(
        account_no="0003937868", trading_token="t",
        symbol="VCB", side="NB", quantity=100, price=50000,
        loan_package_id=1775, order_type="ATO",
    )
    import json
    body = json.loads(captured["req"].data)
    assert body["orderType"] == "ATO"
    assert body["price"] == 0


def test_place_stock_order_keeps_price_for_LO(monkeypatch) -> None:
    """LO orders keep the signal price (the previous default)."""
    from vietnam_algo.libs.dnse.client import DnseClient
    captured: dict = {"req": None}

    def fake_urlopen(req, timeout=20):
        captured["req"] = req
        class _R:
            def read(self): return b'{"id": 1}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = DnseClient(api_key="k" * 8, api_secret="s" * 16)
    client.place_stock_order(
        account_no="0003937868", trading_token="t",
        symbol="VCB", side="NB", quantity=100, price=50000,
        loan_package_id=1775, order_type="LO",
    )
    import json
    body = json.loads(captured["req"].data)
    assert body["orderType"] == "LO"
    assert body["price"] == 50000


# -----------------------------------------------------------------------------
# 2026-10-12: market-aware order type override.
#
# UPCOM (UPX) only supports LO. ATO/ATC on UPCOM returns HTTP 400
# ``INVALID_ORDER_TYPE_FOR_THE_EXCHANGE``. The runner must look up
# each symbol's marketId and override the session's default order type
# to ``LO`` for UPCOM symbols (and to ``LO`` for HNX during ATO,
# since HNX has no opening auction).
# -----------------------------------------------------------------------------


def test_submit_in_atc_is_blocked_for_upcom_symbol(stub_ledger, stub_client, monkeypatch) -> None:
    """UPCOM symbols (e.g. ABB) are blocked in the ATC window too.

    2026-10-31: the auction gate is market-agnostic — no order is
    submitted in an auction window, whatever the board.

    (Historically UPCOM needed LO during ATC because ATC is not
    supported on UPCOM — the 14:31 ICT ABB live-submit attempt
    returned HTTP 400 ``INVALID_ORDER_TYPE_FOR_THE_EXCHANGE``. Now
    the window is simply skipped entirely.)
    """
    stub_client.market_by_symbol["ABB"] = "UPX"
    _patch_now(monkeypatch, 2026, 10, 8, 14, 35)  # 14:35 ICT, ATC
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
    monkeypatch.setattr("vietnam_algo.bots.runner.notify", lambda msg: None)
    result = _submit_autofin_deals_to_dnse(
        [_entry_deal(symbol="ABB", price=17500)],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["total"] == 0
    assert stub_ledger.calls == []


def test_submit_in_ato_is_blocked_for_hnx_symbol(stub_ledger, stub_client, monkeypatch) -> None:
    """HNX symbols (e.g. SHB) are blocked in the ATO window too.

    2026-10-31: the auction gate is market-agnostic. (Historically
    HNX needed LO during ATO since HNX has no opening auction.)
    """
    stub_client.market_by_symbol["SHB"] = "HNX"
    _patch_now(monkeypatch, 2026, 10, 8, 9, 5)  # 09:05 ICT, ATO
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
    monkeypatch.setattr("vietnam_algo.bots.runner.notify", lambda msg: None)
    result = _submit_autofin_deals_to_dnse(
        [_entry_deal(symbol="SHB", price=10000)],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["total"] == 0
    assert stub_ledger.calls == []


def test_submit_in_atc_is_blocked_for_hose_symbol(stub_ledger, stub_client, monkeypatch) -> None:
    """HOSE symbols (e.g. VCB) are blocked in the ATC window too."""
    stub_client.market_by_symbol["VCB"] = "HSX"
    _patch_now(monkeypatch, 2026, 10, 8, 14, 35)  # 14:35 ICT, ATC
    from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse
    monkeypatch.setattr("vietnam_algo.bots.runner.notify", lambda msg: None)
    result = _submit_autofin_deals_to_dnse(
        [_entry_deal(symbol="VCB", price=60000)],
        account_no="0003937868", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["total"] == 0
    assert stub_ledger.calls == []
