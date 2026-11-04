"""Tests for the ORPHAN DEAL notice in the EOD report (2026-11-04).

The operator confirmed the orphan deals are NOT a bot bug: the deal
WAS on autofin, but autofin's data for it was lost, so
``/signal-static-holding`` no longer lists it. The operator will
trade them MANUALLY.

So the EOD report must NAME them. Without that, a position silently
stops being managed by the bot — autofin publishes no recommendation
for it, so there is nothing for the bot to act on — and nobody
notices until the position moves against us.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# -----------------------------------------------------------------------------
# the EOD report section
# -----------------------------------------------------------------------------

def _result(orphan_deals: list[dict]) -> dict:
    return {
        "tradeDate": "2026-11-04",
        "strategy": "autofin_signal",
        "count": 0,
        "latestOrdersCsv": "/tmp/orders.csv",
        "latestOrdersJson": "/tmp/orders.json",
        "nav_source": "dnse",
        "signals": {
            "nav": 27_056_277,
            "account": "0003937868",
            "autofin_holding": 26,
            "autofin_risk": 0,
            "autofin_closing": 5,
            "fetched": 3,
            "deals_tracked": 27,
            "orphan_deals": orphan_deals,
        },
        "deals_actioned": [],
    }


def _summary(orphan_deals: list[dict]) -> str:
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    adapter = AutofinSignalAdapter()
    return adapter.format_cycle_summary(_result(orphan_deals))


def test_no_orphan_notice_when_there_are_none() -> None:
    msg = _summary([])
    assert "ORPHAN" not in msg
    assert "MANUAL ACTION REQUIRED" not in msg


def test_the_orphan_notice_names_every_orphan() -> None:
    msg = _summary([
        {
            "deal_id": "1789344000:ABB",
            "symbol": "ABB",
            "qty": 100,
            "reason": "not in /signal-static-holding",
            "recovered": True,
            "last_recomment": "🟢 Vùng mua: 12,600 - 13,000",
        },
        {
            "deal_id": "1792627200:TCB",
            "symbol": "TCB",
            "qty": 200,
            "reason": "no row in /signal-deals for this deal",
            "recovered": False,
        },
    ])

    assert "ORPHAN DEALS" in msg
    assert "MANUAL ACTION REQUIRED" in msg
    assert "Trade them manually" in msg

    # Both orphans are named, with their deal id and quantity.
    assert "ABB" in msg
    assert "deal#1789344000:ABB" in msg
    assert "qty=100" in msg
    assert "TCB" in msg
    assert "deal#1792627200:TCB" in msg
    assert "qty=200" in msg

    # The reason each one is an orphan is shown.
    assert "not in /signal-static-holding" in msg
    assert "no row in /signal-deals" in msg

    # The last autofin signal is carried for context.
    assert "Vùng mua" in msg


def test_the_orphan_count_is_shown() -> None:
    msg = _summary([
        {"deal_id": "1:AAA", "symbol": "AAA", "qty": 10,
         "reason": "not in /signal-static-holding"},
        {"deal_id": "2:BBB", "symbol": "BBB", "qty": 20,
         "reason": "not in /signal-static-holding"},
    ])
    assert "ORPHAN DEALS — MANUAL ACTION REQUIRED (2)" in msg


def test_a_long_orphan_list_is_capped_but_counted() -> None:
    orphans = [
        {"deal_id": f"{i}:SYM{i}", "symbol": f"SYM{i}", "qty": i,
         "reason": "not in /signal-static-holding"}
        for i in range(20)
    ]
    msg = _summary(orphans)
    assert "MANUAL ACTION REQUIRED (20)" in msg
    assert "+ 5 more" in msg
    # The first 15 are listed.
    assert "SYM0" in msg
    assert "SYM14" in msg
    assert "SYM15" not in msg


def test_the_notice_says_autofin_data_loss_not_a_bot_error() -> None:
    """The operator's stated cause — make sure the wording matches."""
    msg = _summary([
        {"deal_id": "1:AAA", "symbol": "AAA", "qty": 10,
         "reason": "not in /signal-static-holding"},
    ])
    assert "autofin data loss" in msg.lower()


# -----------------------------------------------------------------------------
# the adapter records the orphans
# -----------------------------------------------------------------------------

def test_the_adapter_records_orphan_deals() -> None:
    """``_augment_orphan_deals`` must populate ``_orphan_deals``."""
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    class _Client:
        def fetch_deals(self, symbol, page_size=10):
            # A row matching (_id, symbol) — the orphan IS recoverable.
            return [{
                "_id": 1789344000,
                "symbol": "ABB",
                "lastRecomment": "🟢 Vùng mua: 12,600 - 13,000",
                "hisRecomment": [],
                "numberOfProfit": 0,
            }]

    adapter = AutofinSignalAdapter()
    adapter._our_deals = {
        "1789344000:ABB": {
            "symbol": "ABB",
            "_our_qty": 100,
            "lastRecomment": "stale",
        },
    }
    # We HOLD the position — it must be reported.
    adapter._positions = {"ABB": 100}
    # The holding list does NOT contain the deal → it is an orphan.
    adapter._augment_orphan_deals(_Client(), [])

    assert len(adapter._orphan_deals) == 1
    od = adapter._orphan_deals[0]
    assert od["deal_id"] == "1789344000:ABB"
    assert od["symbol"] == "ABB"
    assert od["qty"] == 100
    assert od["recovered"] is True
    assert "Vùng mua" in od["last_recomment"]


def test_an_orphan_we_do_not_hold_is_not_listed() -> None:
    """Only the orphans we ACTUALLY HOLD are reported.

    The tracked-deal state keeps every deal we ever touched — 27 of
    them on 2026-11-04, all with ``_our_qty = 0``. Listing them all
    would bury the two that matter under 25 closed deals.
    """
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    class _Client:
        def fetch_deals(self, symbol, page_size=10):
            return [{
                "_id": 1789344000,
                "symbol": "ABB",
                "lastRecomment": "🟢 Vùng mua: 12,600 - 13,000",
                "hisRecomment": [],
                "numberOfProfit": 0,
            }]

    adapter = AutofinSignalAdapter()
    adapter._our_deals = {
        "1789344000:ABB": {"symbol": "ABB", "_our_qty": 0},
    }
    # We do NOT hold it → nothing to trade manually → not listed.
    adapter._positions = {}
    adapter._augment_orphan_deals(_Client(), [])

    assert adapter._orphan_deals == []


def test_an_unrecoverable_orphan_is_recorded_too() -> None:
    """No matching row in /signal-deals → still listed, marked unrecovered."""
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    class _Client:
        def fetch_deals(self, symbol, page_size=10):
            # A row for the same symbol but a DIFFERENT _id — must not
            # be borrowed (the 2026-10-29 VOS fix).
            return [{
                "_id": 9999999999,
                "symbol": "ABB",
                "lastRecomment": "💰 Chốt lãi lần 1 mức giá 20,000",
                "hisRecomment": [],
                "numberOfProfit": 1,
            }]

    adapter = AutofinSignalAdapter()
    adapter._our_deals = {
        "1789344000:ABB": {"symbol": "ABB", "_our_qty": 100},
    }
    adapter._positions = {"ABB": 100}
    adapter._augment_orphan_deals(_Client(), [])

    assert len(adapter._orphan_deals) == 1
    od = adapter._orphan_deals[0]
    assert od["recovered"] is False
    assert "no row in /signal-deals" in od["reason"]


def test_no_orphans_means_an_empty_list() -> None:
    """When every tracked deal IS in the holding list, nothing is listed."""
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    class _Client:
        def fetch_deals(self, symbol, page_size=10):
            raise AssertionError("must not be called when there are no orphans")

    adapter = AutofinSignalAdapter()
    adapter._our_deals = {
        "1789344000:ABB": {"symbol": "ABB", "_our_qty": 100},
    }
    adapter._positions = {"ABB": 100}
    holding = [{"_id": 1789344000, "symbol": "ABB"}]
    adapter._augment_orphan_deals(_Client(), holding)

    assert adapter._orphan_deals == []


# -----------------------------------------------------------------------------
# the decision snapshot records the orphan flag
# -----------------------------------------------------------------------------

def test_the_decision_snapshot_flags_orphans() -> None:
    """The per-deal audit view marks which deals are orphans."""
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    adapter = AutofinSignalAdapter()
    summary = adapter._build_decision_summary(
        raw_payloads={},
        holding=[{"_id": 1, "symbol": "AAA"}],
        signals=[],
        closing=[],
        risk=[],
        refresh_report={"refreshed": [], "skipped_fresh": [], "failed": []},
        all_orders=[],
        new_orders=[],
        partial_orders=[],
        close_orders=[],
        deals_actioned=[],
        our_deals={
            "1:AAA": {"symbol": "AAA", "_our_qty": 100},
            "2:BBB": {"symbol": "BBB", "_our_qty": 200},
        },
        positions={"AAA": 100, "BBB": 200},
        nav=1_000_000,
        account="0003937868",
        trade_date="2026-11-04",
        allow_new_entries=True,
        new_entry_top_n=3,
        current_prices={},
    )

    by_id = {d["deal_id"]: d for d in summary["deals"]}
    assert by_id["1:AAA"]["is_orphan"] is False
    assert by_id["2:BBB"]["is_orphan"] is True
