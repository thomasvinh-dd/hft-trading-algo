"""Tests for the 2026-10-13 dry-run pollution fix in
``vietnam_algo.algos.autofin_signal``.

The bug: the planning step was setting
``new_our_deals[did]["_our_qty"] = o["quantity"]`` for every new
BUY order placed. This happened BEFORE the order was submitted
to DNSE. If the bot crashed between the planning step and the
next cycle's cleanup (which is the source of truth for
``_our_qty``), the state file would persist with the wrong
``_our_qty`` and the cleanup would skip the deal because
``_our_qty > 0`` looked like a real holding.

In dry-run mode the problem is even worse: the planning step
sets ``_our_qty`` but no submit ever happens, so the pollution
persists indefinitely.

Fix: the planning step no longer sets ``_our_qty``. The
``_load_state`` cleanup is the source of truth, reconciling
``_our_qty`` against the order ledger's filledQuantity on
every cycle.

The same-cycle dedup (``already_planned_symbols``) now also
includes in-flight orders from the ledger (status in
{submitting, submitted, open, partial}) so we don't re-plan a
duplicate BUY while the original is working through the
exchange.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.algos import autofin_signal as mod
from vietnam_algo.algos.autofin_signal import AutofinSignal
from vietnam_algo.libs.order_ledger import LedgerOrder, save_ledger


# -----------------------------------------------------------------------------
# _collect_in_flight_symbols helper
# -----------------------------------------------------------------------------


def _seed_ledger(tmp_path: Path, items: list[LedgerOrder]) -> None:
    save_ledger(
        {f"{i.runId}:{i.sourceOrderId}:{i.symbol}:{i.side}": i for i in items},
        tmp_path,
    )


def _make_order(symbol: str, status: str, side: str = "NB",
                quantity: int = 100, price: int = 82000) -> LedgerOrder:
    return LedgerOrder(
        runId="2026-10-13", sourceOrderId=99999, sourceDate="2026-10-13",
        symbol=symbol, side=side, quantity=quantity, price=price,
        reason=f"autofin_buy:deal#1:{symbol}:nav15pct:Khuyến nghị",
        status=status, dnseOrderId=f"dnse-{symbol}",
        createdAt="2026-10-13T09:00:00Z",
        updatedAt="2026-10-13T09:00:00Z",
    )


def test_collect_in_flight_symbols_empty_ledger(tmp_path) -> None:
    """No ledger → returns the base set unchanged."""
    assert mod._collect_in_flight_symbols({"BAF"}, tmp_path) == {"BAF"}


def test_collect_in_flight_symbols_includes_submitted(tmp_path) -> None:
    """Status='submitted' → symbol added to in-flight set."""
    _seed_ledger(tmp_path, [_make_order("BAF", "submitted")])
    assert mod._collect_in_flight_symbols(set(), tmp_path) == {"BAF"}


def test_collect_in_flight_symbols_includes_open(tmp_path) -> None:
    """Status='open' → symbol added to in-flight set."""
    _seed_ledger(tmp_path, [_make_order("BAF", "open")])
    assert mod._collect_in_flight_symbols(set(), tmp_path) == {"BAF"}


def test_collect_in_flight_symbols_includes_submitting_and_partial(tmp_path) -> None:
    """All in-flight statuses are recognized."""
    _seed_ledger(tmp_path, [
        _make_order("BAF", "submitting"),
        _make_order("ABW", "partial"),
        _make_order("TLG", "open"),
    ])
    assert mod._collect_in_flight_symbols(set(), tmp_path) == {"BAF", "ABW", "TLG"}


def test_collect_in_flight_symbols_excludes_cancelled(tmp_path) -> None:
    """Status='cancelled' is NOT in-flight (re-planable)."""
    _seed_ledger(tmp_path, [_make_order("BAF", "cancelled")])
    assert mod._collect_in_flight_symbols(set(), tmp_path) == set()


def test_collect_in_flight_symbols_excludes_rejected_and_expired(tmp_path) -> None:
    """Status='rejected' / 'expired' / 'filled' are NOT in-flight."""
    _seed_ledger(tmp_path, [
        _make_order("BAF", "rejected"),
        _make_order("ABW", "expired"),
        _make_order("TLG", "filled"),
    ])
    assert mod._collect_in_flight_symbols(set(), tmp_path) == set()


def test_collect_in_flight_symbols_preserves_base_set(tmp_path) -> None:
    """Base-set symbols are preserved (union semantics)."""
    _seed_ledger(tmp_path, [_make_order("BAF", "open")])
    result = mod._collect_in_flight_symbols({"HDB", "ABW"}, tmp_path)
    assert result == {"HDB", "ABW", "BAF"}


def test_collect_in_flight_symbols_handles_missing_ledger(tmp_path) -> None:
    """Missing ledger file → returns the base set unchanged."""
    result = mod._collect_in_flight_symbols({"BAF"}, tmp_path)
    assert result == {"BAF"}


def test_collect_in_flight_symbols_dedupes(tmp_path) -> None:
    """Multiple in-flight orders for the same symbol → only one entry."""
    _seed_ledger(tmp_path, [
        _make_order("BAF", "open"),
        LedgerOrder(
            runId="2026-10-13", sourceOrderId=88888, sourceDate="2026-10-13",
            symbol="BAF", side="NB", quantity=200, price=31000,
            reason="autofin_buy:deal#1:BAF:nav15pct:Khuyến nghị",
            status="submitted", dnseOrderId="dnse-2",
            createdAt="2026-10-13T09:00:00Z",
            updatedAt="2026-10-13T09:00:00Z",
        ),
    ])
    assert mod._collect_in_flight_symbols(set(), tmp_path) == {"BAF"}


# -----------------------------------------------------------------------------
# decide_orders no longer pre-populates _our_qty (dry-run pollution fix)
# -----------------------------------------------------------------------------


def _make_signal(symbol: str, **kwargs) -> AutofinSignal:
    raw = {
        "_id": 1786492800,
        "symbol": symbol,
        "companyName": f"{symbol} Co",
        "rateForNav": 0.15,
        "lastRecomment": "Khuyến nghị đầu tư",
        "numberOfProfit": 0,
        "supply_F": kwargs.get("supply_F", 30000),
        "supply_T": kwargs.get("supply_T", 31000),
        "tP_F": kwargs.get("tP_F", 33000),
        "tP_T": kwargs.get("tP_T", 35000),
        "sL_F": kwargs.get("sL_F", 28000),
        "sL_T": kwargs.get("sL_T", 29000),
        "statusDeal": {
            "isClosed": False, "price_Open": 0, "price_Closed": 0,
            "price_StopLoss": 28000,
        },
    }
    return AutofinSignal(
        symbol=symbol, company_name=f"{symbol} Co",
        last_recomment="Khuyến nghị đầu tư",
        supply_from=raw["supply_F"], supply_to=raw["supply_T"],
        tp_from=raw["tP_F"], tp_to=raw["tP_T"],
        sl_from=raw["sL_F"], sl_to=raw["sL_T"],
        rate_for_nav=0.15,
        is_waitting_buy=False, is_waitting_sell=False,
        market_type=1, raw=raw,
    )


def _holding(symbol: str = "BAF", rate: float = 0.15) -> list[dict]:
    return [{
        "_id": 1786492800, "symbol": symbol, "companyName": f"{symbol} Co",
        "statusDeal": {
            "isClosed": False, "price_Open": 0, "price_Closed": 0,
            "price_StopLoss": 28000,
        },
        "numberOfProfit": 0, "rateForNav": rate,
        "lastRecomment": "Khuyến nghị đầu tư",
        "supply_F": 30000, "supply_T": 31000,
        "tP_F": 33000, "tP_T": 35000,
        "sL_F": 28000, "sL_T": 29000,
    }]


def test_planning_step_does_not_set_our_qty_from_planned_order() -> None:
    """decide_orders no longer pre-populates ``_our_qty`` from the planned order.

    Reproduces the 2026-10-13 13:18 ICT bug: the planning step set
    ``_our_qty = 500`` for an ABB BUY 500, but the submit failed
    (HTTP 400). The state file persisted with ``_our_qty = 500``
    until the next cycle's cleanup fixed it. If the bot crashed
    in between, the polluted state caused a near-double-buy.
    """
    sig = _make_signal("BAF")
    holding = _holding(symbol="BAF")
    orders, our_deals, _ = mod.decide_orders(
        holding=holding, signals=[sig], our_deals={},
        our_positions={}, nav=500_000_000,
    )
    # The planning step emitted a BUY.
    assert any(o["side"] == "NB" and o["symbol"] == "BAF" for o in orders)
    # BUT it did NOT pre-populate ``_our_qty`` from the planned
    # order quantity. The cleanup at _load_state is the source of
    # truth, and will set it correctly from the ledger on the next
    # cycle.
    assert our_deals["1786492800:BAF"]["_our_qty"] == 0


def test_planning_step_respects_already_planned_symbols_with_inflight() -> None:
    """decide_orders skips symbols in the already_planned_symbols set.

    The 2026-10-13 fix added in-flight ledger orders to
    ``already_planned_symbols``. This test verifies the dedup
    is honored by decide_orders.
    """
    sig = _make_signal("BAF")
    holding = _holding(symbol="BAF")
    orders, our_deals, _ = mod.decide_orders(
        holding=holding, signals=[sig], our_deals={},
        our_positions={}, nav=500_000_000,
        already_planned_symbols={"BAF"},  # simulate in-flight
    )
    # BAF is in already_planned_symbols → no new entry.
    nb_orders = [o for o in orders if o["side"] == "NB" and o["symbol"] == "BAF"]
    assert nb_orders == []
    # Deal is still tracked in our_deals (with _our_qty=0).
    assert "1786492800:BAF" in our_deals
    assert our_deals["1786492800:BAF"]["_our_qty"] == 0
