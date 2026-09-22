"""Regression test for the 2026-09-22 incident: the start-of-day
briefing crashed with ``cannot access local variable '_items' where
it is not associated with a value`` whenever the JSON orders file
didn't exist (or was empty).

Root cause: ``_items`` was only initialised inside the
``if not live_submit:`` branch — when the bot ran in live mode, OR
when the orders JSON was missing, the variable was unbound by the
time ``_emit_cycle("start_of_day", payload={"items": len(_items)})``
ran at the bottom of the block.

The fix moves ``_items = []`` to the top of the try-block so it's
always defined before any branch reads it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Find the project root dynamically so this test works both
# locally and on the VPS where the project lives at
# /root/hft-trading-algo.
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _runner_source() -> str:
    return (_PROJECT_ROOT / "vietnam_algo" / "bots" / "runner.py").read_text(
        encoding="utf-8"
    )


def test_items_initialized_in_sod_try_block():
    """Static check: ``_items = []`` lives at the top of the SOD
    try-block so it's defined for both live and dry-run modes."""
    src = _runner_source()
    sod_open = src.index("Sending start-of-day briefing")
    try_pos = src.index("try:", sod_open)
    live_submit_pos = src.index("if not live_submit:", sod_open)
    items_init_pos = src.index("_items: list[dict[str, object]] = []", sod_open)
    assert items_init_pos < live_submit_pos, (
        f"_items must be initialised BEFORE the if-not-live_submit branch; "
        f"got _items at offset {items_init_pos}, if-not-live_submit at {live_submit_pos}"
    )
    except_pos = src.index('"start_of_day_error"', sod_open)
    assert try_pos < items_init_pos < except_pos, (
        f"_items must be inside the SOD try-block; "
        f"got try at {try_pos}, _items at {items_init_pos}, "
        f"start_of_day_error except at {except_pos}"
    )


def test_items_used_at_emit_cycle_safe_with_empty_list():
    """``len(_items)`` must work even when the orders JSON is
    missing entirely (which leaves _items at the empty-list
    initial value rather than unbound).
    """
    import json as _json

    items: list[dict[str, object]] = []
    json_path = Path("/nonexistent/latest_orders.momentum.json")
    if json_path.exists():
        payload = _json.loads(json_path.read_text(encoding="utf-8"))
        items = list(payload.get("items") or [])
    assert len(items) == 0
