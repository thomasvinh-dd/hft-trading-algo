"""Tests for the ORPHAN DEALS notice in the EOD report.

2026-11-04: the operator asked for a notice listing the orphan deals
so he could trade them manually.

2026-11-05 (corrected): the operator clarified what an orphan IS::

    "the orphan deal mean we can not get detail with that deal or
     ticker anymore, not they are not in static holding list, pls
     check"

So the test is NOT "is the symbol in /signal-static-holding". It is:
"can autofin still give us DETAIL for the deal we entered?" — judged
by the tracked deal-id, which is the authoritative link to the
autofin deal.

Two flavours, needing different handling:

* DEAL LOST — the tracked deal-id is gone from /signal-deals.
  autofin has no recommendation for the position. The bot CANNOT act
  on it; the operator must trade it manually. (ABB, TCB)

* LIST LOST — the tracked deal-id still exists and is still OPEN, but
  only the static holding list dropped it. The augmentation recovers
  the recomment so the bot CAN still act, but autofin's own dashboard
  does not show the position. (AAS)
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
        "tradeDate": "2026-11-05",
        "strategy": "autofin_signal",
        "count": 0,
        "latestOrdersCsv": "/tmp/orders.csv",
        "latestOrdersJson": "/tmp/orders.json",
        "nav_source": "dnse",
        "signals": {
            "nav": 28_240_263,
            "account": "0003937868",
            "autofin_holding": 29,
            "autofin_risk": 13,
            "autofin_closing": 50,
            "fetched": 6,
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


def test_deal_lost_positions_are_listed_for_manual_trading() -> None:
    """The ABB / TCB case — autofin has no deal for us any more."""
    msg = _summary([
        {
            "deal_id": "1789344000:ABB",
            "symbol": "ABB",
            "qty": 100,
            "reason": "autofin no longer has this deal — no recommendation available",
            "flavour": "detail_lost",
            "recovered": False,
        },
        {
            "deal_id": "1792627200:TCB",
            "symbol": "TCB",
            "qty": 200,
            "reason": "autofin no longer has this deal — no recommendation available",
            "flavour": "detail_lost",
            "recovered": False,
        },
    ])

    assert "ORPHAN DEALS" in msg
    assert "2 position(s) have NO autofin recommendation" in msg
    assert "TRADE MANUALLY" in msg

    assert "ABB" in msg
    assert "deal#1789344000:ABB" in msg
    assert "qty=100" in msg
    assert "TCB" in msg
    assert "deal#1792627200:TCB" in msg
    assert "qty=200" in msg


def test_list_lost_positions_are_separated_from_deal_lost() -> None:
    """The AAS case — the deal is intact, only the holding list lost it."""
    msg = _summary([
        {
            "deal_id": "1792713600:AAS",
            "symbol": "AAS",
            "qty": 300,
            "reason": "deal still on autofin, but missing from the holding list",
            "flavour": "list_lost",
            "recovered": True,
            "last_recomment": "Khuyến nghị đầu tư",
        },
    ])

    assert "ORPHAN DEALS" in msg
    # It is NOT in the "trade manually" bucket.
    assert "have NO autofin recommendation" not in msg
    assert "still tradeable by the bot" in msg
    assert "AAS" in msg
    assert "qty=300" in msg
    assert "Khuyến nghị" in msg


def test_both_flavours_are_shown_in_one_report() -> None:
    """The real 2026-11-05 report: AAS (list lost) + ABB/TCB (deal lost)."""
    msg = _summary([
        {
            "deal_id": "1792713600:AAS", "symbol": "AAS", "qty": 300,
            "reason": "deal still on autofin, but missing from the holding list",
            "flavour": "list_lost", "recovered": True,
            "last_recomment": "Khuyến nghị đầu tư",
        },
        {
            "deal_id": "1789344000:ABB", "symbol": "ABB", "qty": 100,
            "reason": "autofin no longer has this deal — no recommendation available",
            "flavour": "detail_lost", "recovered": False,
        },
        {
            "deal_id": "1792627200:TCB", "symbol": "TCB", "qty": 200,
            "reason": "autofin no longer has this deal — no recommendation available",
            "flavour": "detail_lost", "recovered": False,
        },
    ])

    assert "ORPHAN DEALS (3)" in msg
    assert "2 position(s) have NO autofin recommendation" in msg
    assert "1 position(s) still tradeable by the bot" in msg
    for sym in ("AAS", "ABB", "TCB"):
        assert sym in msg


def test_the_wording_says_detail_is_lost() -> None:
    """The operator's definition must be reflected in the wording."""
    msg = _summary([
        {
            "deal_id": "1:AAA", "symbol": "AAA", "qty": 10,
            "reason": "autofin no longer has this deal — no recommendation available",
            "flavour": "detail_lost", "recovered": False,
        },
    ])
    assert "can no longer give us detail" in msg


def test_a_long_list_is_capped_but_counted() -> None:
    orphans = [
        {"deal_id": f"{i}:SYM{i}", "symbol": f"SYM{i}", "qty": i,
         "reason": "autofin no longer has this deal — no recommendation available",
         "flavour": "detail_lost", "recovered": False}
        for i in range(20)
    ]
    msg = _summary(orphans)
    assert "ORPHAN DEALS (20)" in msg
    assert "+ 5 more" in msg
    assert "SYM0" in msg
    assert "SYM14" in msg
    assert "SYM15" not in msg


# -----------------------------------------------------------------------------
# the adapter's detection
# -----------------------------------------------------------------------------

class _Client:
    """A fake autofin client.

    ``deals`` maps a symbol to the list of rows /signal-deals returns
    for it. Only a row whose ``_id`` matches is a match.
    """

    def __init__(self, deals: dict[str, list[dict]]) -> None:
        self.deals = deals
        self.calls: list[str] = []

    def fetch_deals(self, symbol, page_size=10):
        self.calls.append(symbol)
        return list(self.deals.get(symbol, []))


def _row(_id: int, symbol: str, recomment: str = "Khuyến nghị đầu tư") -> dict:
    return {
        "_id": _id,
        "symbol": symbol,
        "lastRecomment": recomment,
        "hisRecomment": [],
        "numberOfProfit": 0,
    }


def test_a_deal_that_autofin_no_longer_has_is_detail_lost() -> None:
    """ABB: our tracked _id is not among autofin's ABB deals."""
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    client = _Client({
        "ABB": [_row(1776643200, "ABB", "Đóng DEAL chạm điểm chặn lãi (Lãi: 30.04 %)")],
    })
    adapter = AutofinSignalAdapter()
    adapter._our_deals = {"1789344000:ABB": {"symbol": "ABB", "_our_qty": 0}}
    adapter._positions = {"ABB": 100}
    adapter._augment_orphan_deals(client, [])

    assert len(adapter._orphan_deals) == 1
    od = adapter._orphan_deals[0]
    assert od["symbol"] == "ABB"
    assert od["qty"] == 100
    assert od["flavour"] == "detail_lost"
    assert od["recovered"] is False


def test_a_deal_that_still_exists_is_list_lost() -> None:
    """AAS: our tracked _id IS on autofin, but not in the holding list."""
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    client = _Client({"AAS": [_row(1792713600, "AAS")]})
    adapter = AutofinSignalAdapter()
    adapter._our_deals = {"1792713600:AAS": {"symbol": "AAS", "_our_qty": 0}}
    adapter._positions = {"AAS": 300}
    adapter._augment_orphan_deals(client, [])

    assert len(adapter._orphan_deals) == 1
    od = adapter._orphan_deals[0]
    assert od["symbol"] == "AAS"
    assert od["qty"] == 300
    assert od["flavour"] == "list_lost"
    assert od["recovered"] is True
    assert "Khuyến nghị" in od["last_recomment"]


def test_a_position_autofin_holds_normally_is_not_an_orphan() -> None:
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    client = _Client({"AAA": [_row(100, "AAA")]})
    adapter = AutofinSignalAdapter()
    adapter._our_deals = {"100:AAA": {"symbol": "AAA", "_our_qty": 100}}
    adapter._positions = {"AAA": 100}
    holding = [{"_id": 100, "symbol": "AAA"}]
    adapter._augment_orphan_deals(client, holding)

    assert adapter._orphan_deals == []


def test_a_position_with_no_tracked_deal_is_detail_lost() -> None:
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    client = _Client({})
    adapter = AutofinSignalAdapter()
    adapter._our_deals = {}
    adapter._positions = {"XYZ": 100}
    adapter._augment_orphan_deals(client, [])

    assert len(adapter._orphan_deals) == 1
    od = adapter._orphan_deals[0]
    assert od["symbol"] == "XYZ"
    assert od["flavour"] == "detail_lost"
    assert od["deal_id"] == "(untracked):XYZ"


def test_a_position_is_reported_only_once() -> None:
    """One line per position — the position check and the deal walk
    must not both report the same symbol."""
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    client = _Client({"AAS": [_row(1792713600, "AAS")]})
    adapter = AutofinSignalAdapter()
    adapter._our_deals = {"1792713600:AAS": {"symbol": "AAS", "_our_qty": 300}}
    adapter._positions = {"AAS": 300}
    adapter._augment_orphan_deals(client, [])

    assert len(adapter._orphan_deals) == 1
    od = adapter._orphan_deals[0]
    assert od["symbol"] == "AAS"
    assert od["qty"] == 300
    assert od["flavour"] == "list_lost"
    assert od["recovered"] is True


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
        trade_date="2026-11-05",
        allow_new_entries=True,
        new_entry_top_n=3,
        current_prices={},
        orphan_deals=[
            {"deal_id": "2:BBB", "symbol": "BBB", "qty": 200,
             "flavour": "detail_lost", "recovered": False},
        ],
    )

    by_id = {d["deal_id"]: d for d in summary["deals"]}
    assert by_id["1:AAA"]["is_orphan"] is False
    assert by_id["2:BBB"]["is_orphan"] is True
    assert by_id["2:BBB"]["orphan_flavour"] == "detail_lost"


def test_the_snapshot_orphan_flag_does_not_depend_on_our_qty() -> None:
    """2026-11-05: the old definition required ``_our_qty > 0``.

    The tracked state has ``_our_qty = 0`` for EVERY deal we still
    hold (it is not updated on a live fill), so the flag never fired.
    """
    from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter

    adapter = AutofinSignalAdapter()
    summary = adapter._build_decision_summary(
        raw_payloads={},
        holding=[],
        signals=[],
        closing=[],
        risk=[],
        refresh_report={"refreshed": [], "skipped_fresh": [], "failed": []},
        all_orders=[],
        new_orders=[],
        partial_orders=[],
        close_orders=[],
        deals_actioned=[],
        our_deals={"2:BBB": {"symbol": "BBB", "_our_qty": 0}},
        positions={"BBB": 200},
        nav=1_000_000,
        account="0003937868",
        trade_date="2026-11-05",
        allow_new_entries=True,
        new_entry_top_n=3,
        current_prices={},
        orphan_deals=[
            {"deal_id": "2:BBB", "symbol": "BBB", "qty": 200,
             "flavour": "detail_lost", "recovered": False},
        ],
    )

    by_id = {d["deal_id"]: d for d in summary["deals"]}
    assert by_id["2:BBB"]["is_orphan"] is True
