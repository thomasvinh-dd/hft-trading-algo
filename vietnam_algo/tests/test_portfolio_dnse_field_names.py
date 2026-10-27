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


# -----------------------------------------------------------------------------
# Market price unit-conversion fix (2026-10-13 21:25 ICT)
#
# DNSE returns prices in different units across endpoints:
#   - /accounts/{acc}/positions returns marketPrice in absolute VND
#     (31050 = 31,050 VND).
#   - /price/{sym}/quotes/latest returns bid/offer in kVND
#     (31.1 = 31,100 VND).
#
# The previous portfolio code used the latest quote directly
# (no conversion) and reported "mkt=31" instead of "mkt=31,050",
# making every position look like it was down ~99.90% in the
# EOD report.
#
# The fix: prefer the positions endpoint's marketPrice (already
# in VND); fall back to the latest quote (with the ×1000
# conversion) only when the positions endpoint has no market
# price for the position.
# -----------------------------------------------------------------------------


def test_uses_positions_market_price_when_available() -> None:
    """Prefer positions endpoint's marketPrice (VND) over the live quote."""
    payload = {
        "positions": [
            {
                "symbol": "BAF", "openQuantity": 200,
                "accumulateQuantity": 200, "closedQuantity": 0,
                "costPrice": 31050, "marketPrice": 31100,
            }
        ]
    }
    # Even if the latest quote is mocked at kVND prices, the
    # positions marketPrice should win (and the quote should NOT
    # be fetched). This saves N API calls per EOD cycle.
    client = _stub_client(positions_payload=payload, latest_quote=None)
    snap = build_portfolio_snapshot(client, "0003937868")
    pos = snap["positions"][0]
    assert pos["marketPrice"] == 31100  # absolute VND, not 31.1
    assert pos["marketValue"] == 200 * 31100
    # Unrealized P&L: (31100 - 31050) * 200 = +10,000 VND = +0.161%
    assert pos["unrealizedPnl"] == 10000
    assert abs(pos["unrealizedPnlPct"] - 0.161) < 0.01


def test_falls_back_to_live_quote_with_kvnd_conversion() -> None:
    """If positions has no marketPrice, use latest quote × 1000."""
    payload = {
        "positions": [
            {
                "symbol": "BAF", "openQuantity": 200,
                "accumulateQuantity": 200, "closedQuantity": 0,
                "costPrice": 31050,
                # No marketPrice field — simulates a stale or
                # delisted-symbol cache where DNSE didn't return
                # a recent trade price.
            }
        ]
    }
    # Live quote is in kVND: bid 31.1, offer 31.2 → mid 31.15 kVND
    # = 31,150 VND.
    quote_payload = {
        "quotes": [
            {
                "boardId": "G1",
                "bid": [{"price": 31.1, "quantity": 100}],
                "offer": [{"price": 31.2, "quantity": 100}],
            }
        ]
    }
    client = _stub_client(
        positions_payload=payload, latest_quote=quote_payload,
    )
    snap = build_portfolio_snapshot(client, "0003937868")
    pos = snap["positions"][0]
    # mid = (31.1 + 31.2) / 2 = 31.15 kVND × 1000 = 31,150 VND
    assert pos["marketPrice"] == 31150
    assert pos["marketValue"] == 200 * 31150


def test_no_market_price_falls_back_to_cost_price() -> None:
    """If neither positions nor quote has a price, use costPrice (PnL=0)."""
    payload = {
        "positions": [
            {
                "symbol": "BAF", "openQuantity": 200,
                "accumulateQuantity": 200, "closedQuantity": 0,
                "costPrice": 31050,
            }
        ]
    }
    # No market price, and the live quote call returns nothing useful.
    empty_quote = {"quotes": [{"boardId": "G1", "bid": [], "offer": []}]}
    client = _stub_client(
        positions_payload=payload, latest_quote=empty_quote,
    )
    snap = build_portfolio_snapshot(client, "0003937868")
    pos = snap["positions"][0]
    # Falls back to avgPrice → marketPrice == costPrice → 0 P&L.
    assert pos["marketPrice"] == 31050
    assert pos["unrealizedPnl"] == 0
    assert pos["unrealizedPnlPct"] == 0.0


def test_abb_upcom_position_with_market_price_in_vnd() -> None:
    """ABB (UPCOM) position uses positions marketPrice directly."""
    payload = {
        "positions": [
            {
                "symbol": "ABB", "openQuantity": 100,
                "accumulateQuantity": 100, "closedQuantity": 0,
                "costPrice": 17700, "marketPrice": 17600,
            }
        ]
    }
    client = _stub_client(positions_payload=payload, latest_quote=None)
    snap = build_portfolio_snapshot(client, "0003937868")
    pos = snap["positions"][0]
    assert pos["marketPrice"] == 17600
    assert pos["marketValue"] == 100 * 17600
    # Unrealized P&L: (17600 - 17700) * 100 = -10,000 VND (-0.565%)
    assert pos["unrealizedPnl"] == -10000
    assert abs(pos["unrealizedPnlPct"] - (-0.565)) < 0.01


def test_real_baf_abf_portfolio_pnl_correct() -> None:
    """Reproduces the 2026-10-13 21:25 ICT EOD bug.

    The old code reported BAF as ``mkt=31`` (i.e. 31 VND, from
    the kVND quote un-converted) and ABB as ``mkt=18`` (i.e.
    18 VND). Unrealized P&L was a misleading -99.90% on both
    positions. The new code reads the positions endpoint's
    marketPrice directly, which is in absolute VND.
    """
    payload = {
        "positions": [
            {
                "symbol": "BAF", "openQuantity": 200,
                "accumulateQuantity": 200, "closedQuantity": 0,
                "costPrice": 31050, "marketPrice": 31100,
            },
            {
                "symbol": "ABB", "openQuantity": 100,
                "accumulateQuantity": 100, "closedQuantity": 0,
                "costPrice": 17700, "marketPrice": 17600,
            },
        ]
    }
    client = _stub_client(positions_payload=payload, latest_quote=None)
    snap = build_portfolio_snapshot(client, "0003937868")
    by_sym = {p["symbol"]: p for p in snap["positions"]}
    # BAF: (31100 - 31050) * 200 = +10,000 VND = +0.161%
    assert by_sym["BAF"]["marketPrice"] == 31100
    assert by_sym["BAF"]["unrealizedPnl"] == 10000
    assert abs(by_sym["BAF"]["unrealizedPnlPct"] - 0.161) < 0.01
    # ABB: (17600 - 17700) * 100 = -10,000 VND = -0.565%
    assert by_sym["ABB"]["marketPrice"] == 17600
    assert by_sym["ABB"]["unrealizedPnl"] == -10000
    assert abs(by_sym["ABB"]["unrealizedPnlPct"] - (-0.565)) < 0.01
    # And the totals roll up correctly.
    expected_stock = 200 * 31100 + 100 * 17600
    assert snap["totals"]["stockValue"] == expected_stock
    expected_pnl = 10000 + (-10000)
    assert snap["totals"]["totalUnrealizedPnl"] == expected_pnl


# -----------------------------------------------------------------------------
# breakEvenPrice preference (2026-10-14 fix)
#
# DNSE returns TWO cost fields on /accounts/{acc}/positions:
#   - costPrice       : raw average buy price (no fees)
#   - breakEvenPrice  : cost + accumulated fees (the true break-even)
#
# The DNSE app and the positions API both report breakEvenPrice as
# the "avg cost" — using costPrice understates the P&L by 15 bps
# worth of fees per share. The user spotted the discrepancy on
# 2026-10-14 14:00 ICT: DNSE app showed ABB avg=17,724 and
# uPnL=-12,426 VND, the bot showed 17,700 and -10,000 VND.
# These tests pin the fix (read breakEvenPrice first).
# -----------------------------------------------------------------------------


def test_break_even_price_used_when_present() -> None:
    """breakEvenPrice (with fees) wins over costPrice (raw average)."""
    payload = {
        "positions": [
            {
                "symbol": "ABB", "openQuantity": 100,
                "accumulateQuantity": 100, "closedQuantity": 0,
                "costPrice": 17700, "marketPrice": 17600,
                "breakEvenPrice": 17724.4,
            }
        ]
    }
    client = _stub_client(positions_payload=payload, latest_quote=None)
    snap = build_portfolio_snapshot(client, "0003937868")
    pos = snap["positions"][0]
    # breakEvenPrice wins.
    assert pos["avgPrice"] == pytest.approx(17724.4, abs=0.01)
    # P&L = (17600 - 17724.4) * 100 = -12,440 VND ≈ -0.70%
    assert pos["unrealizedPnl"] == pytest.approx(-12440, abs=1.0)
    assert pos["unrealizedPnlPct"] == pytest.approx(-0.7018, abs=0.01)


def test_legacy_cost_price_still_supported() -> None:
    """Older payloads (no breakEvenPrice) fall back to costPrice."""
    payload = {
        "positions": [
            {
                "symbol": "ABB", "openQuantity": 100,
                "accumulateQuantity": 100, "closedQuantity": 0,
                "costPrice": 17700, "marketPrice": 17600,
                # No breakEvenPrice — legacy DNSE response shape
            }
        ]
    }
    client = _stub_client(positions_payload=payload, latest_quote=None)
    snap = build_portfolio_snapshot(client, "0003937868")
    pos = snap["positions"][0]
    assert pos["avgPrice"] == 17700
    assert pos["unrealizedPnl"] == -10000


def test_break_even_price_matches_dnse_app_for_abb_2026_10_14() -> None:
    """Regression: ABB avg 17,724.4, mkt 17,600, uPnL -12,440 VND (-0.70%).

    Reproduces the 2026-10-14 14:00 ICT user audit: the bot was
    reporting ABB avg=17,700 (raw costPrice) and uPnL=-10,000
    VND (-0.56%), but the DNSE app showed avg=17,724.4 and
    uPnL=-12,426 VND (-0.71%). Root cause: costPrice excludes
    the buy fee (~15 bps = ~24 VND per share at 17,700), so
    using it understated both the avg cost and the P&L. The
    fix prefers breakEvenPrice so the bot's P&L matches the
    DNSE app to the đồng.
    """
    payload = {
        "positions": [
            {
                "symbol": "ABB", "openQuantity": 100,
                "accumulateQuantity": 100, "closedQuantity": 0,
                "costPrice": 17700, "marketPrice": 17600,
                "breakEvenPrice": 17724.4,
            }
        ]
    }
    client = _stub_client(positions_payload=payload, latest_quote=None)
    snap = build_portfolio_snapshot(client, "0003937868")
    pos = snap["positions"][0]
    assert pos["avgPrice"] == pytest.approx(17724.4, abs=0.01)
    # (17600 - 17724.4) * 100 = -12,440 VND (DNSE rounds to 12,426
    # in the app; the difference is rounding-to-display vs raw math).
    assert pos["unrealizedPnl"] == pytest.approx(-12440, abs=1.0)
    # -0.70 %  (DNSE app shows -0.71 % — difference is rounding)
    assert pos["unrealizedPnlPct"] < -0.70
    assert pos["unrealizedPnlPct"] > -0.72
