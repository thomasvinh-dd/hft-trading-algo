"""Tests for the autofin live-submit wiring in bots/runner.py.

The autofin adapter produces ``deals_actioned`` with ``dnse_orders`` dicts
of shape ``{symbol, side, price, quantity, kind, reason}``. The runner
funnels these through ``_submit_autofin_deals_to_dnse`` which converts
to ``DnseOrder`` and submits via ``execute_orders_with_ledger``.

These tests pin the conversion rules so a future change can't silently
break the live-submit path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

from vietnam_algo.bots.runner import _submit_autofin_deals_to_dnse


class _StubLedger:
    """Minimal stand-in for execute_orders_with_ledger; returns its inputs."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *, client, account_no, trading_token, orders, data_dir, run_id):
        from vietnam_algo.libs.trading_engine import DnseOrder
        self.calls.append(
            dict(
                client=client, account_no=account_no, trading_token=trading_token,
                orders=list(orders), data_dir=data_dir, run_id=run_id,
            )
        )
        # Echo back each order as a "submitted" result with a synthetic id.
        return [
            {
                "status": "submitted",
                "order": o,
                "response": {"id": f"DNSE-{i:06d}"},
                "detail": {"id": f"DNSE-{i:06d}", "status": "NEW"},
            }
            for i, o in enumerate(orders)
        ], [], True


class _StubDnseClient:
    """Stand-in for ``DnseClient`` that returns a per-symbol cash package id.

    2026-10-12: the runner now calls ``client.get_cash_loan_package_id(
    account_no, sym)`` for every unique symbol in the submit batch
    (DNSE requires ``loanPackageId`` to be a real package id, not 0).
    The default mapping below mimics the real DNSE account
    ``0003937868``: ABW=1036, HDB=1036 (cash), VND=1036, etc.

    Tests that want a different per-symbol map can set
    ``stub_client.cash_pkg_by_symbol = {...}`` before the call.
    """

    def __init__(self) -> None:
        self.cash_pkg_by_symbol: dict[str, int] = {
            "ABW": 1036,
            "HDB": 1036,
            "ABB": 1036,
            "VCB": 1036,
        }
        self.get_latest_quote_calls: list[str] = []

    def get_cash_loan_package_id(self, account_no: str, symbol: str) -> int:
        sym = symbol.upper()
        if sym not in self.cash_pkg_by_symbol:
            raise RuntimeError(
                f"StubDnseClient: no cash package for {sym}"
            )
        return self.cash_pkg_by_symbol[sym]

    def get_latest_quote(self, symbol: str) -> dict:
        self.get_latest_quote_calls.append(symbol.upper())
        # Return a mid-zone price so the live-quote gate doesn't
        # drop the deal in tests that aren't exercising that path.
        return {"sym": symbol.upper(), "g1": {"askPrice": 20000}}


@pytest.fixture
def stub_ledger(monkeypatch):
    """Replace execute_orders_with_ledger with a stub that records its call.

    The helper imports ``execute_orders_with_ledger`` from
    ``vietnam_algo.libs.daily_engine`` at call time, so we patch the
    source module (not the runner module).
    """
    from vietnam_algo.libs import daily_engine
    stub = _StubLedger()
    monkeypatch.setattr(daily_engine, "execute_orders_with_ledger", stub)
    return stub


@pytest.fixture
def stub_client() -> _StubDnseClient:
    """A stub DNSE client that returns per-symbol cash package ids.

    2026-10-12: ``_submit_autofin_deals_to_dnse`` now calls
    ``client.get_cash_loan_package_id(account_no, sym)`` for every
    symbol in the submit batch. Tests that pre-date this fix pass
    ``client=None`` and relied on the default ``loan_package_id=0``;
    the new tests (and migrated ones) use this stub to provide
    real-looking package ids.
    """
    return _StubDnseClient()


@pytest.fixture
def in_continuous_session(monkeypatch):
    """Pin ``vietnam_now()`` to a continuous-trading moment so the
    session-aware submit path in ``_submit_autofin_deals_to_dnse`` does
    not return the off-hours short-circuit. Required because the
    tests don't run in the morning, and 2026-10-08's
    session-detection extension rejects orders placed during
    11:30-13:00, 15:00-09:00 next day, weekends, and holidays.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    fixed = datetime(2026, 10, 8, 10, 0, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    from vietnam_algo.bots import runner
    monkeypatch.setattr(runner, "vietnam_now", lambda: fixed)
    return fixed


def test_empty_deals_actioned_short_circuits(stub_ledger):
    """An empty deals_actioned list should NOT call the ledger."""
    result = _submit_autofin_deals_to_dnse(
        [],
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=None,
        data_dir=Path("/tmp"),
        trade_date="2026-10-06",
    )
    assert result == {"submitted": [], "skipped_existing": 0, "unknown": 0, "total": 0}
    assert stub_ledger.calls == []


def test_buy_order_converted_with_default_loan_package(stub_ledger, stub_client, in_continuous_session):
    """A single entry BUY maps to one DnseOrder with the configured loan package."""
    deals = [
        {
            "action": "new_entry",
            "symbol": "ABW",
            "deal_id": "1785456000:ABW",
            "dnse_orders": [
                {
                    "symbol": "ABW",
                    "side": "NB",
                    "price": 14100,
                    "quantity": 1000,
                    "kind": "entry",
                    "reason": "autofin_buy:deal#1785456000:ABW:nav15pct:Khuyến nghị đầu tư",
                }
            ],
        }
    ]
    result = _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=stub_client,
        data_dir=Path("/tmp"),
        trade_date="2026-10-06",
    )
    assert result["total"] == 1
    assert result["submitted"] == ["DNSE-000000"]
    # Verify the DnseOrder was built correctly
    call = stub_ledger.calls[0]
    assert call["account_no"] == "0003937868"
    assert call["trading_token"] == "tok"
    assert call["run_id"] == "2026-10-06"
    o = call["orders"][0]
    assert o.symbol == "ABW"
    assert o.side == "NB"
    assert o.price == 14100
    assert o.quantity == 1000
    # 2026-10-12: the runner now looks up the cash package id per
    # symbol. For ABW on account 0003937868 the cash package is
    # ``1036`` (GD tiền mặt / cash transaction). This replaces the
    # earlier 2026-10-09 behaviour of ``loanPackageId=0`` which the
    # DNSE server rejects with HTTP 400 INVALID_INPUT.
    assert o.loanPackageId == 1036
    assert o.orderType == "LO"
    assert o.sourceDate == "2026-10-06"
    assert isinstance(o.sourceOrderId, int)
    # Reason must round-trip
    assert o.reason.startswith("autofin_buy:deal#")


def test_partial_close_sell_converted_correctly(stub_ledger, stub_client, in_continuous_session):
    """A partial close SELL is a NS side order."""
    deals = [
        {
            "action": "partial_close",
            "symbol": "HDB",
            "deal_id": "1786492800:HDB",
            "dnse_orders": [
                {
                    "symbol": "HDB",
                    "side": "NS",
                    "price": 28800,
                    "quantity": 100,
                    "kind": "partial_close",
                    "reason": "autofin_tp:deal#1786492800:HDB:28800",
                }
            ],
        }
    ]
    result = _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=stub_client,
        data_dir=Path("/tmp"),
        trade_date="2026-10-06",
    )
    assert result["total"] == 1
    o = stub_ledger.calls[0]["orders"][0]
    assert o.symbol == "HDB"
    assert o.side == "NS"
    assert o.price == 28800
    assert o.quantity == 100


def test_source_order_id_stable_across_calls(stub_ledger, stub_client, in_continuous_session):
    """The same deal+side+price+kind must produce the same sourceOrderId
    so the ledger dedup prevents double-submit on bot restart."""
    deals = [
        {
            "action": "new_entry",
            "symbol": "ABW",
            "deal_id": "1785456000:ABW",
            "dnse_orders": [
                {
                    "symbol": "ABW",
                    "side": "NB",
                    "price": 14100,
                    "quantity": 1000,
                    "kind": "entry",
                    "reason": "autofin_buy:deal#1785456000:ABW:nav15pct:Khuyến nghị đầu tư",
                }
            ],
        }
    ]
    _submit_autofin_deals_to_dnse(
        deals, account_no="A", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-06",
    )
    _submit_autofin_deals_to_dnse(
        deals, account_no="A", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-06",
    )
    id_a = stub_ledger.calls[0]["orders"][0].sourceOrderId
    id_b = stub_ledger.calls[1]["orders"][0].sourceOrderId
    assert id_a == id_b, (
        f"sourceOrderId must be stable across calls for ledger dedup; "
        f"got {id_a} vs {id_b}"
    )


def test_different_kind_produces_different_id(stub_ledger, in_continuous_session):
    """A SELL (partial_close) for the same deal must NOT collide with the BUY."""
    entry_deal = {
        "action": "new_entry", "symbol": "ABW", "deal_id": "1785456000:ABW",
        "dnse_orders": [{
            "symbol": "ABW", "side": "NB", "price": 14100, "quantity": 1000,
            "kind": "entry", "reason": "autofin_buy:deal#1785456000:ABW:nav15pct",
        }],
    }
    close_deal = {
        "action": "partial_close", "symbol": "ABW", "deal_id": "1785456000:ABW",
        "dnse_orders": [{
            "symbol": "ABW", "side": "NS", "price": 17100, "quantity": 300,
            "kind": "partial_close", "reason": "autofin_tp:deal#1785456000:ABW:17100",
        }],
    }
    _submit_autofin_deals_to_dnse(
        [entry_deal], account_no="A", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-06",
    )
    _submit_autofin_deals_to_dnse(
        [close_deal], account_no="A", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-06",
    )
    id_entry = stub_ledger.calls[0]["orders"][0].sourceOrderId
    id_close = stub_ledger.calls[1]["orders"][0].sourceOrderId
    assert id_entry != id_close, (
        "ENTRY and PARTIAL_CLOSE for the same deal must have different sourceOrderIds"
    )


def test_per_symbol_loan_package_override(stub_ledger, stub_client, in_continuous_session):
    """When loan_package_id_by_symbol is provided, the matching symbol uses
    its override; others fall back to the per-symbol lookup."""
    deals = [
        {"action": "new_entry", "symbol": "ABW", "deal_id": "1:ABW", "dnse_orders": [{
            "symbol": "ABW", "side": "NB", "price": 14100, "quantity": 1000,
            "kind": "entry", "reason": "x",
        }]},
        {"action": "new_entry", "symbol": "HDB", "deal_id": "1:HDB", "dnse_orders": [{
            "symbol": "HDB", "side": "NB", "price": 27100, "quantity": 1100,
            "kind": "entry", "reason": "y",
        }]},
    ]
    _submit_autofin_deals_to_dnse(
        deals, account_no="A", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-06",
        loan_package_id_by_symbol={"HDB": 9999},
    )
    orders = {o.symbol: o for o in stub_ledger.calls[0]["orders"]}
    # 2026-10-12: the per-symbol override wins BEFORE the cash-package
    # lookup runs. ABW has no override, so the runner falls back to
    # ``client.get_cash_loan_package_id("A", "ABW")`` which the stub
    # returns as ``1036`` (the real DNSE cash package id).
    assert orders["ABW"].loanPackageId == 1036
    assert orders["HDB"].loanPackageId == 9999


def test_invalid_orders_skipped(stub_ledger, stub_client, in_continuous_session):
    """Orders with missing/zero price or qty are silently skipped (no submit)."""
    # Add W to the stub so the cash-pkg lookup succeeds
    stub_client.cash_pkg_by_symbol.update({"X": 1036, "Y": 1036, "W": 1036, "Z": 1036})
    deals = [
        {"action": "new_entry", "symbol": "X", "deal_id": "1:X", "dnse_orders": [
            {"symbol": "X", "side": "NB", "price": 0, "quantity": 100, "kind": "entry", "reason": "r"},
        ]},
        {"action": "new_entry", "symbol": "Y", "deal_id": "1:Y", "dnse_orders": [
            {"symbol": "Y", "side": "NB", "price": 1000, "quantity": 0, "kind": "entry", "reason": "r"},
        ]},
        {"action": "new_entry", "symbol": "Z", "deal_id": "1:Z", "dnse_orders": [
            {"symbol": "Z", "side": "ZZ", "price": 1000, "quantity": 100, "kind": "entry", "reason": "r"},
        ]},
        {"action": "new_entry", "symbol": "W", "deal_id": "1:W", "dnse_orders": [
            {"symbol": "W", "side": "NB", "price": 1000, "quantity": 100, "kind": "entry", "reason": "r"},
        ]},
    ]
    result = _submit_autofin_deals_to_dnse(
        deals, account_no="A", loan_package_id=1775,
        trading_token="t", client=stub_client, data_dir=Path("/tmp"),
        trade_date="2026-10-06",
    )
    assert result["total"] == 1  # only the W order
    assert stub_ledger.calls[0]["orders"][0].symbol == "W"


def test_skipped_existing_and_unknown_counts(monkeypatch, in_continuous_session):
    """The result dict must surface skipped_existing_ledger and unknown counts."""

    def stub_ledger(client, account_no, trading_token, orders, data_dir, run_id):
        return [
            {"status": "submitted", "response": {"id": "A1"}, "detail": {"id": "A1"}},
            {"status": "skipped_existing_ledger", "ledgerStatus": "open"},
            {"status": "skipped_existing_ledger", "ledgerStatus": "submitted"},
            {"status": "unknown", "error": "timeout"},
        ], [], False

    from vietnam_algo.libs import daily_engine
    monkeypatch.setattr(daily_engine, "execute_orders_with_ledger", stub_ledger)
    deals = [{"action": "new_entry", "symbol": "X", "deal_id": "1:X", "dnse_orders": [
        {"symbol": "X", "side": "NB", "price": 100, "quantity": 1, "kind": "entry", "reason": "r"},
    ]}]
    result = _submit_autofin_deals_to_dnse(
        deals, account_no="A", loan_package_id=1775,
        trading_token="t", client=None, data_dir=Path("/tmp"),
        trade_date="2026-10-06",
    )
    assert result["submitted"] == ["A1"]
    assert result["skipped_existing"] == 2
    assert result["unknown"] == 1
    assert result["total"] == 1  # input had 1 order, ledger reported per-order status


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))