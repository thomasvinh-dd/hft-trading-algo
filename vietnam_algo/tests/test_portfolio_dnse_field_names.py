"""Tests for ``vietnam_algo.libs.portfolio.build_portfolio_snapshot``.

Pins the 2026-10-13 fix: DNSE's positions endpoint returns
``openQuantity`` (held shares) and ``accumulateQuantity`` (lifetime
buys), NOT ``quantity``. The previous parser only looked at
``quantity`` / ``qty`` / ``totalQuantity`` and silently dropped
all positions, so the strategy thought the account was flat
when it was holding 100 ABB shares.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.portfolio import build_portfolio_snapshot


def _stub_client(*, positions_payload, balances=None, accounts=None,
                 latest_quote=None) -> MagicMock:
    """Return a mock DnseClient that returns the given payloads."""
    client = MagicMock()
    client.get_accounts.return_value = accounts or {"name": "Test"}
    client.get_balances.return_value = balances or {
        "stock": {
            "totalCash": 30000000,
            "availableCash": 30000000,
            "withdrawableCash": 28000000,
        }
    }
    client.get_positions.return_value = positions_payload
    if latest_quote is None:
        latest_quote = {"quotes": [{"bid": [{"price": 17500}], "offer": [{"price": 17600}]}]}
    client.get_latest_quote.return_value = latest_quote
    return client


def test_dnse_open_quantity_parses_correctly() -> None:
    """DNSE's ``openQuantity`` field is parsed as the held position."""
    payload = {
        "positions": [
            {
                "symbol": "ABB",
                "openQuantity": 100,
                "accumulateQuantity": 100,
                "closedQuantity": 0,
                "costPrice": 17700,
            }
        ]
    }
    client = _stub_client(positions_payload=payload)
    snap = build_portfolio_snapshot(client, "0003937868")
    positions = snap["positions"]
    assert len(positions) == 1
    assert positions[0]["symbol"] == "ABB"
    assert positions[0]["quantity"] == 100


def test_dnse_accumulate_minus_closed_when_open_missing() -> None:
    """If ``openQuantity`` is missing/0, fall back to accumulate - closed."""
    payload = {
        "positions": [
            {
                "symbol": "BAF",
                "accumulateQuantity": 300,
                "closedQuantity": 100,
                "costPrice": 31050,
            }
        ]
    }
    client = _stub_client(positions_payload=payload)
    snap = build_portfolio_snapshot(client, "0003937868")
    positions = snap["positions"]
    assert len(positions) == 1
    assert positions[0]["quantity"] == 200  # 300 - 100


def test_dnse_zero_position_filtered() -> None:
    """A position with 0 shares (fully sold) is filtered out."""
    payload = {
        "positions": [
            {
                "symbol": "ABC",
                "openQuantity": 0,
                "accumulateQuantity": 100,
                "closedQuantity": 100,
            }
        ]
    }
    client = _stub_client(positions_payload=payload)
    snap = build_portfolio_snapshot(client, "0003937868")
    assert snap["positions"] == []


def test_dnse_multiple_positions_all_parsed() -> None:
    """All positions in the response are parsed, not just the first."""
    payload = {
        "positions": [
            {"symbol": "ABB", "openQuantity": 100, "accumulateQuantity": 100, "closedQuantity": 0, "costPrice": 17700},
            {"symbol": "BAF", "openQuantity": 200, "accumulateQuantity": 200, "closedQuantity": 0, "costPrice": 31050},
        ]
    }
    client = _stub_client(positions_payload=payload)
    snap = build_portfolio_snapshot(client, "0003937868")
    by_sym = {p["symbol"]: p for p in snap["positions"]}
    assert "ABB" in by_sym and "BAF" in by_sym
    assert by_sym["ABB"]["quantity"] == 100
    assert by_sym["BAF"]["quantity"] == 200


def test_dnse_quantity_alias_still_supported() -> None:
    """Other brokers / older DNSE responses using ``quantity`` still parse."""
    payload = {
        "positions": [
            {"symbol": "XYZ", "quantity": 50}
        ]
    }
    client = _stub_client(positions_payload=payload)
    snap = build_portfolio_snapshot(client, "0003937868")
    assert snap["positions"][0]["quantity"] == 50


def test_dnse_stockSymbol_alias() -> None:
    """Alternative field name ``stockSymbol`` is accepted."""
    payload = {
        "positions": [
            {"stockSymbol": "ABC", "openQuantity": 75, "accumulateQuantity": 75, "closedQuantity": 0}
        ]
    }
    client = _stub_client(positions_payload=payload)
    snap = build_portfolio_snapshot(client, "0003937868")
    assert snap["positions"][0]["symbol"] == "ABC"
    assert snap["positions"][0]["quantity"] == 75


def test_empty_positions_list() -> None:
    """Empty positions list returns empty positions (no crash)."""
    payload = {"positions": []}
    client = _stub_client(positions_payload=payload)
    snap = build_portfolio_snapshot(client, "0003937868")
    assert snap["positions"] == []
    assert snap["totals"]["positionCount"] == 0


def test_totals_reflect_actual_positions() -> None:
    """The ``positionCount`` and ``stockValue`` reflect parsed positions."""
    payload = {
        "positions": [
            {"symbol": "ABB", "openQuantity": 100, "accumulateQuantity": 100, "closedQuantity": 0, "costPrice": 17700},
        ]
    }
    client = _stub_client(positions_payload=payload)
    snap = build_portfolio_snapshot(client, "0003937868")
    assert snap["totals"]["positionCount"] == 1
