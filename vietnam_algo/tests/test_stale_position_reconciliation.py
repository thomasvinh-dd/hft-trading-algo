"""Reproduce the 2026-11-05 stale-position bug locally.

The ledger says we SOLD the ORS and BAF positions, but the tracked
state still claims ``_our_qty > 0``. That puts those symbols into
``already_planned_symbols`` — the "we already own this, don't enter
it again" set — which silently blocks a legitimate new entry.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
from vietnam_algo.libs.order_ledger import LedgerOrder, save_ledger


def _seed_ledger(data_dir: Path) -> None:
    """The ORS + BAF history: bought, then fully sold."""
    live = data_dir / "live"
    live.mkdir(parents=True, exist_ok=True)
    rows = {
        # ORS: bought 200, then the protective SL filled for 200 -> flat.
        "2026-10-26:1:ORS:NB": LedgerOrder(
            runId="2026-10-26", sourceOrderId=1, sourceDate="2026-10-26",
            symbol="ORS", side="NB", quantity=200, price=14800,
            reason="autofin_buy:deal#1792713600:ORS:nav15pct:Khuyến nghị đầu tư",
            status="filled", dnseOrderId="b1",
            createdAt="2026-10-26T02:00:00Z", updatedAt="2026-10-26T02:30:00Z",
            lastDetail={"orderStatus": "Filled", "fillQuantity": 200},
        ),
        "2026-10-26:2:ORS:NS": LedgerOrder(
            runId="2026-10-26", sourceOrderId=2, sourceDate="2026-10-26",
            symbol="ORS", side="NS", quantity=200, price=14400,
            reason="autofin_user_sl:deal#1792713600:ORS:10%:14400",
            status="filled", dnseOrderId="b2",
            createdAt="2026-10-26T03:00:00Z", updatedAt="2026-10-26T03:30:00Z",
            lastDetail={"orderStatus": "Filled", "fillQuantity": 200},
        ),
        # BAF: bought 200, then a capital_protect partial closed 200 -> flat.
        "2026-10-13:3:BAF:NB": LedgerOrder(
            runId="2026-10-13", sourceOrderId=3, sourceDate="2026-10-13",
            symbol="BAF", side="NB", quantity=200, price=31200,
            reason="autofin_buy:deal#1785888000:BAF:nav30pct:Khuyến nghị đầu tư",
            status="filled", dnseOrderId="b3",
            createdAt="2026-10-13T06:00:00Z", updatedAt="2026-10-13T06:30:00Z",
            lastDetail={"orderStatus": "Filled", "fillQuantity": 200},
        ),
        "2026-10-21:4:BAF:NS": LedgerOrder(
            runId="2026-10-21", sourceOrderId=4, sourceDate="2026-10-21",
            symbol="BAF", side="NS", quantity=200, price=30650,
            reason="autofin_partial:deal#1785888000:BAF:capital_protect:lap0:pct50:@30650",
            status="filled", dnseOrderId="b4",
            createdAt="2026-10-21T03:00:00Z", updatedAt="2026-10-21T03:30:00Z",
            lastDetail={"orderStatus": "Filled", "fillQuantity": 200},
        ),
        # VOS: bought 300, trimmed 200 -> still hold 100.
        "2026-10-29:5:VOS:NB": LedgerOrder(
            runId="2026-10-29", sourceOrderId=5, sourceDate="2026-10-29",
            symbol="VOS", side="NB", quantity=300, price=11800,
            reason="autofin_buy:deal#1792627200:VOS:nav15pct:Khuyến nghị đầu tư",
            status="filled", dnseOrderId="b5",
            createdAt="2026-10-29T02:00:00Z", updatedAt="2026-10-29T02:30:00Z",
            lastDetail={"orderStatus": "Filled", "fillQuantity": 300},
        ),
        "2026-11-02:6:VOS:NS": LedgerOrder(
            runId="2026-11-02", sourceOrderId=6, sourceDate="2026-11-02",
            symbol="VOS", side="NS", quantity=200, price=10200,
            reason="autofin_partial:deal#1792627200:VOS:reduce_lap:lap2:pct50:@11100",
            status="filled", dnseOrderId="b6",
            createdAt="2026-11-02T06:00:00Z", updatedAt="2026-11-02T06:30:00Z",
            lastDetail={"orderStatus": "Filled", "fillQuantity": 200},
        ),
    }
    save_ledger(rows, data_dir)


def _state(data_dir: Path) -> None:
    """The STALE state file: it still claims we hold ORS and BAF."""
    live = data_dir / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "autofin_signal_state.json").write_text(json.dumps({
        "our_deals": {
            "1792713600:ORS": {
                "symbol": "ORS", "_our_qty": 200,
                "lastRecomment": "⚠️ Hạ tỷ trọng lần 3",
            },
            "1785888000:BAF": {
                "symbol": "BAF", "_our_qty": 200,
                "lastRecomment": "⚠️ Hạ tỷ trọng lần 1",
            },
            "1792627200:VOS": {
                "symbol": "VOS", "_our_qty": 300,
                "lastRecomment": "⚠️ Hạ tỷ trọng lần 2",
            },
        },
        "actioned_deals": [
            "1792713600:ORS", "1785888000:BAF", "1792627200:VOS",
        ],
    }))


def test_a_fully_sold_position_is_not_treated_as_held(tmp_path) -> None:
    """ORS + BAF were sold — the state file is stale.

    Before the fix they stayed in ``already_planned_symbols`` and
    silently blocked a legitimate new entry (the 2026-11-05 case:
    3 in-band candidates, ZERO orders).
    """
    _seed_ledger(tmp_path)
    _state(tmp_path)

    adapter = AutofinSignalAdapter()
    adapter._load_state(tmp_path)

    # ORS: bought 200, sold 200 -> flat.
    assert adapter._our_deals["1792713600:ORS"]["_our_qty"] == 0, (
        "ORS was fully sold — it must not be treated as held"
    )
    # BAF: bought 200, sold 200 -> flat.
    assert adapter._our_deals["1785888000:BAF"]["_our_qty"] == 0, (
        "BAF was fully sold — it must not be treated as held"
    )
    # VOS: bought 300, sold 200 -> still hold 100.
    assert adapter._our_deals["1792627200:VOS"]["_our_qty"] == 100, (
        "VOS was only trimmed — 100 shares are still held"
    )


def test_a_sold_position_is_not_in_already_planned_symbols(tmp_path) -> None:
    """The whole point: ORS + BAF must be free to enter again."""
    _seed_ledger(tmp_path)
    _state(tmp_path)

    adapter = AutofinSignalAdapter()
    adapter._load_state(tmp_path)

    already_planned = {
        str(sig.get("symbol") or "").strip().upper()
        for sig in adapter._our_deals.values()
        if int(sig.get("_our_qty", 0) or 0) > 0
    }
    assert "ORS" not in already_planned, (
        "ORS was sold — it must not block a new entry"
    )
    assert "BAF" not in already_planned, (
        "BAF was sold — it must not block a new entry"
    )
    assert "VOS" in already_planned, "VOS is still held"


def test_a_partially_sold_position_is_reconciled_down(tmp_path) -> None:
    """VOS: bought 300, trimmed 200 -> the state must say 100."""
    _seed_ledger(tmp_path)
    _state(tmp_path)

    adapter = AutofinSignalAdapter()
    adapter._load_state(tmp_path)

    assert adapter._our_deals["1792627200:VOS"]["_our_qty"] == 100


def test_a_position_with_no_ledger_history_keeps_its_state_qty(tmp_path) -> None:
    """No ledger evidence -> trust the state file (do not zero it)."""
    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "autofin_signal_state.json").write_text(json.dumps({
        "our_deals": {
            "999:XYZ": {"symbol": "XYZ", "_our_qty": 100},
        },
        "actioned_deals": ["999:XYZ"],
    }))
    save_ledger({}, tmp_path)

    adapter = AutofinSignalAdapter()
    adapter._load_state(tmp_path)

    assert adapter._our_deals["999:XYZ"]["_our_qty"] == 100
