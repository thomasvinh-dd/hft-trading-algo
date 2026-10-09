"""Tests for the 2026-10-07 live-quote gate in
``_submit_autofin_deals_to_dnse``.

The submit helper fetches a live DNSE ``/price/{sym}/quotes/latest``
quote for each ``new_entry`` deal and re-checks the buy-zone gate
against the LIVE best-ask price. If the live ask is outside
``[supply_F * 0.95, supply_T * 1.05]`` the entire new-entry deal is
dropped and a Telegram notification is queued. Partial-close and
full-close actions are NOT gated — we always want to SELL regardless
of the live price.

These tests stub ``client.get_latest_quote`` so we can pin the gate
behavior in isolation, without hitting real DNSE.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))

from vietnam_algo.bots import runner as runner_mod
from vietnam_algo.bots.runner import (
    _extract_best_ask_in_vnd,
    _submit_autofin_deals_to_dnse,
)


@pytest.fixture
def in_continuous_session(monkeypatch):
    """Pin ``vietnam_now()`` to a continuous-trading moment so the
    session-aware submit path in ``_submit_autofin_deals_to_dnse`` does
    not return the off-hours short-circuit. Required because the
    2026-10-08 session-detection extension rejects orders placed
    during 11:30-13:00, 15:00-09:00 next day, weekends, and holidays.
    """
    fixed = datetime(2026, 10, 8, 10, 0, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    monkeypatch.setattr(runner_mod, "vietnam_now", lambda: fixed)
    return fixed


# -----------------------------------------------------------------------------
# _extract_best_ask_in_vnd: unit conversion (DNSE returns kVND, we want VND)
# -----------------------------------------------------------------------------


def test_extract_best_ask_g1_board_kvnd_to_vnd():
    """ABW live: best ask 14.5 (kVND) → 14,500 VND."""
    quote = {
        "quotes": [
            {
                "symbol": "ABW",
                "boardId": "G1",
                "bid": [{"price": 14.4, "quantity": 100}],
                "offer": [{"price": 14.5, "quantity": 40}],
            },
            {
                "symbol": "ABW",
                "boardId": "G4",
                "bid": [{"price": 14.3, "quantity": 50}],
                "offer": [{"price": 14.6, "quantity": 30}],
            },
        ]
    }
    assert _extract_best_ask_in_vnd(quote) == 14500


def test_extract_best_ask_no_g1_falls_back_to_first_offer():
    """If G1 is missing, use the first board with a non-empty offer list."""
    quote = {
        "quotes": [
            {
                "symbol": "ABW",
                "boardId": "G4",
                "offer": [{"price": 14.6, "quantity": 30}],
            },
        ]
    }
    assert _extract_best_ask_in_vnd(quote) == 14600


def test_extract_best_ask_no_boards():
    assert _extract_best_ask_in_vnd({"quotes": []}) is None
    assert _extract_best_ask_in_vnd({}) is None
    assert _extract_best_ask_in_vnd([]) is None
    assert _extract_best_ask_in_vnd(None) is None


def test_extract_best_ask_empty_offer():
    quote = {"quotes": [{"symbol": "ABW", "boardId": "G1", "offer": []}]}
    assert _extract_best_ask_in_vnd(quote) is None


# -----------------------------------------------------------------------------
# _submit_autofin_deals_to_dnse: live-quote gate
# -----------------------------------------------------------------------------


def _make_client(quote_factory=None) -> MagicMock:
    """Build a stub DnseClient. ``quote_factory`` is a callable
    ``(symbol) → quote_dict``; default returns a single G1 board with
    ask=14,500 VND.
    """
    client = MagicMock()

    def _default_factory(symbol: str) -> dict:
        return {
            "quotes": [
                {
                    "symbol": symbol,
                    "boardId": "G1",
                    "offer": [{"price": 14.5, "quantity": 100}],
                }
            ]
        }

    client.get_latest_quote.side_effect = quote_factory or _default_factory
    return client


def _make_deal(
    symbol: str,
    price: int,
    side: str = "NB",
    quantity: int = 100,
    action: str = "new_entry",
    supply_F: float = 13800,
    supply_T: float = 14100,
    deal_id: str = "1",
) -> dict:
    return {
        "action": action,
        "symbol": symbol,
        "deal_id": deal_id,
        "lastRecomment": "Khuyến nghị đầu tư",
        "rateForNav": 0.15,
        "numberOfProfit": 0,
        "supply_F": supply_F,
        "supply_T": supply_T,
        "dnse_orders": [
            {
                "symbol": symbol,
                "side": side,
                "price": price,
                "quantity": quantity,
                "kind": "entry" if side == "NB" else "partial_close",
                "reason": f"autofin_buy:deal#{deal_id}:nav15pct:test",
            }
        ],
    }


def _patch_executor(monkeypatch, captured: dict) -> None:
    """Stub execute_orders_with_ledger so the test captures the DnseOrders
    that the gate allowed through.

    The function is imported lazily inside _submit_autofin_deals_to_dnse
    (``from vietnam_algo.libs.daily_engine import execute_orders_with_ledger``)
    so we patch the source module, not the runner attribute.
    """
    def _fake_execute(*, client, account_no, trading_token, orders, data_dir, run_id):
        captured["orders"] = list(orders)
        return (
            [{"status": "submitted", "detail": {"id": f"DNSE-{o.symbol}-001"}} for o in orders],
            {},
            True,
        )
    import vietnam_algo.libs.daily_engine as daily_engine
    monkeypatch.setattr(
        daily_engine,
        "execute_orders_with_ledger",
        _fake_execute,
    )


def test_live_gate_drops_new_entry_when_ask_above_zone_tolerance(
    monkeypatch, in_continuous_session,
):
    """Ask 15,000 VND > supply_T * 1.05 = 14,805 → drop the new entry."""
    captured: dict = {}
    _patch_executor(monkeypatch, captured)

    # ABW zone [13,800, 14,100], live ask = 15,000 VND
    def _q(symbol: str) -> dict:
        return {
            "quotes": [
                {
                    "symbol": symbol,
                    "boardId": "G1",
                    "offer": [{"price": 15.0, "quantity": 100}],
                }
            ]
        }

    client = _make_client(quote_factory=_q)
    deals = [_make_deal("ABW", price=14100, supply_F=13800, supply_T=14100)]
    result = _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=client,
        data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["submitted"] == []
    assert result["total"] == 0
    assert len(result["out_of_zone"]) == 1
    oo = result["out_of_zone"][0]
    assert oo["deal"]["symbol"] == "ABW"
    assert oo["live_ask"] == 15000
    assert captured.get("orders", []) == []  # nothing reached the executor
    assert client.get_latest_quote.call_count == 1


def test_live_gate_drops_new_entry_when_ask_below_zone_tolerance(
    monkeypatch, in_continuous_session,
):
    """Ask 12,500 VND < supply_F * 0.95 = 13,110 → drop the new entry."""
    captured: dict = {}
    _patch_executor(monkeypatch, captured)

    def _q(symbol: str) -> dict:
        return {
            "quotes": [
                {
                    "symbol": symbol,
                    "boardId": "G1",
                    "offer": [{"price": 12.5, "quantity": 100}],
                }
            ]
        }

    client = _make_client(quote_factory=_q)
    deals = [_make_deal("ABW", price=14100, supply_F=13800, supply_T=14100)]
    result = _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=client,
        data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert result["submitted"] == []
    assert len(result["out_of_zone"]) == 1
    assert result["out_of_zone"][0]["live_ask"] == 12500
    assert captured.get("orders", []) == []


def test_live_gate_lets_through_when_ask_within_tolerance(
    monkeypatch, in_continuous_session,
):
    """Ask 14,500 VND is above 14,100 but within 14,100*1.05 = 14,805 → pass.
    (Same as the ABW live case at 14,500 ask — bot's LO at 14,100 still
    makes sense as a passive buy.)"""
    captured: dict = {}
    _patch_executor(monkeypatch, captured)

    def _q(symbol: str) -> dict:
        return {
            "quotes": [
                {
                    "symbol": symbol,
                    "boardId": "G1",
                    "offer": [{"price": 14.5, "quantity": 100}],
                }
            ]
        }

    client = _make_client(quote_factory=_q)
    deals = [_make_deal("ABW", price=14100, supply_F=13800, supply_T=14100)]
    result = _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=client,
        data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert len(result["submitted"]) == 1
    assert result["out_of_zone"] == []
    assert captured["orders"][0].symbol == "ABW"
    assert captured["orders"][0].price == 14100


def test_live_gate_does_not_check_partial_close_or_full_close(
    monkeypatch, in_continuous_session,
):
    """SELL orders bypass the live gate entirely (no get_latest_quote call)."""
    captured: dict = {}
    _patch_executor(monkeypatch, captured)

    def _q_should_not_be_called(symbol: str) -> dict:
        raise AssertionError("get_latest_quote should NOT be called for SELL orders")

    client = _make_client(quote_factory=_q_should_not_be_called)
    deals = [
        _make_deal(
            "ABW", price=14500, side="NS", quantity=100, action="partial_close",
        ),
    ]
    result = _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=client,
        data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert len(result["submitted"]) == 1
    assert result["out_of_zone"] == []
    assert client.get_latest_quote.call_count == 0


def test_live_gate_graceful_degradation_on_api_failure(
    monkeypatch, in_continuous_session,
):
    """If ``get_latest_quote`` raises, proceed with the planning step's
    local-cache price (don't block the submit)."""
    captured: dict = {}
    _patch_executor(monkeypatch, captured)

    def _q_fail(symbol: str) -> dict:
        raise RuntimeError("DNSE timeout")

    client = _make_client(quote_factory=_q_fail)
    deals = [_make_deal("ABW", price=14100, supply_F=13800, supply_T=14100)]
    result = _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=client,
        data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert len(result["submitted"]) == 1
    assert result["out_of_zone"] == []  # no drop, just proceed
    assert captured["orders"][0].symbol == "ABW"


def test_live_gate_disabled_via_env_var(monkeypatch, in_continuous_session):
    """Setting ``AUTOFIN_LIVE_QUOTE_GATE=0`` skips the live-quote fetch
    entirely (uses the local-cache price from planning)."""
    captured: dict = {}
    _patch_executor(monkeypatch, captured)

    def _q_should_not_be_called(symbol: str) -> dict:
        raise AssertionError("get_latest_quote should NOT be called when gate disabled")

    monkeypatch.setenv("AUTOFIN_LIVE_QUOTE_GATE", "0")
    client = _make_client(quote_factory=_q_should_not_be_called)
    deals = [_make_deal("ABW", price=14100, supply_F=13800, supply_T=14100)]
    result = _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=client,
        data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert len(result["submitted"]) == 1
    assert client.get_latest_quote.call_count == 0


def test_live_gate_skips_when_supply_zone_undeclared(monkeypatch, in_continuous_session):
    """If supply_F / supply_T are 0 (zone not in deal dict), don't gate.
    Common for HELD-but-not-entered deals where the holding record
    didn't carry the zone forward."""
    captured: dict = {}
    _patch_executor(monkeypatch, captured)

    def _q_should_not_be_called(symbol: str) -> dict:
        raise AssertionError("get_latest_quote should NOT be called when zone missing")

    client = _make_client(quote_factory=_q_should_not_be_called)
    deals = [_make_deal("ABW", price=14100, supply_F=0, supply_T=0)]
    result = _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=client,
        data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert len(result["submitted"]) == 1
    assert client.get_latest_quote.call_count == 0


def test_live_gate_partial_drop_one_through_one_dropped(monkeypatch, in_continuous_session):
    """Mixed batch: ABW pass, HDB drop."""
    captured: dict = {}
    _patch_executor(monkeypatch, captured)

    def _q_factory(symbol: str) -> dict:
        asks = {"ABW": 14.5, "HDB": 28.0}  # HDB ask too high for zone [24, 25.5]
        return {
            "quotes": [
                {
                    "symbol": symbol,
                    "boardId": "G1",
                    "offer": [{"price": asks[symbol], "quantity": 100}],
                }
            ]
        }

    client = _make_client(quote_factory=_q_factory)
    deals = [
        _make_deal("ABW", price=14100, supply_F=13800, supply_T=14100, deal_id="1"),
        _make_deal("HDB", price=25500, supply_F=24000, supply_T=25500, deal_id="2"),
    ]
    result = _submit_autofin_deals_to_dnse(
        deals,
        account_no="0003937868",
        loan_package_id=1775,
        trading_token="tok",
        client=client,
        data_dir=Path("/tmp"),
        trade_date="2026-10-08",
    )
    assert len(result["submitted"]) == 1
    assert len(result["out_of_zone"]) == 1
    assert result["out_of_zone"][0]["deal"]["symbol"] == "HDB"
    assert captured["orders"][0].symbol == "ABW"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))