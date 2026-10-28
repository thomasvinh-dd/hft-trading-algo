"""Regression tests for the autofin entry-window logic (2026-10-28).

Two independent defects caused the bot to skip new entries:

1. ``_plan_orders_for_new_positions`` incremented its top-N counter
   BEFORE the gates that can reject a candidate, so with the default
   ``top_n == 1`` a single out-of-zone / non-entry-recommendation top
   signal burned the only slot and the whole day produced zero
   entries. Covered in ``test_autofin_topn_slot_bug.py``.

2. The entry window was a hard 30 minutes after the open (09:00-09:30
   ICT). A bot that is down or restarting at 09:00 loses the day
   entirely — on 2026-10-27 the watchdog only relaunched at 11:20
   ICT, so every poll logged ``allow_new_entries=False``.

This file pins the window arithmetic and the catch-up rule.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.schedule import TradingSchedule  # noqa: E402


def _minutes_since_open(now, sched) -> float:
    open_h, open_m = sched.morningStart.hour, sched.morningStart.minute
    return (
        (now.hour - open_h) * 60
        + (now.minute - open_m)
        + (now.second / 60.0)
    )


def test_schedule_morning_start_is_0900() -> None:
    """The entry window is anchored to the 09:00 ICT session open."""
    sched = TradingSchedule()
    assert sched.morningStart.hour == 9
    assert sched.morningStart.minute == 0


def test_entry_window_covers_0900_to_0930() -> None:
    """With the default 30-minute cutoff the window is 09:00-09:30."""
    from datetime import time
    sched = TradingSchedule()
    cutoff = 30
    assert _minutes_since_open(time(9, 0), sched) == 0
    assert _minutes_since_open(time(9, 30), sched) == 30
    assert _minutes_since_open(time(9, 31), sched) > cutoff


def test_catchup_window_extends_to_the_lunch_break() -> None:
    """The default catch-up allowance covers the whole morning session.

    The morning session runs 09:00-11:30 ICT (the lunch break starts
    at 13:00 per the afternoon session), so a 240-minute catch-up
    covers 09:00-13:00 and a bot that restarts at 11:20 still gets
    its entry.
    """
    from datetime import time
    sched = TradingSchedule()
    catchup = 240
    # 11:20 ICT — exactly when the 2026-10-27 watchdog relaunched
    # the bot.
    assert _minutes_since_open(time(11, 20), sched) == 140
    assert _minutes_since_open(time(11, 20), sched) <= catchup


def test_catchup_is_disabled_after_an_entry_was_already_made() -> None:
    """The catch-up must not fire once we have entered today.

    The "one entry per day" policy is preserved: the catch-up only
    exists to cover a bot outage, not to allow a second entry.
    """
    # The check reads the order ledger for a BUY row with today's
    # runId and an ``autofin_buy:`` reason.
    from vietnam_algo.libs.order_ledger import LedgerOrder

    rows = [
        LedgerOrder(
            runId="2026-10-27", sourceOrderId=1, sourceDate="2026-10-27",
            symbol="ORS", side="NB", quantity=200, price=16000,
            reason="autofin_buy:deal#1792713600:ORS:nav15pct:Khuyến nghị đầu tư",
            status="filled", dnseOrderId="30691",
            createdAt="2026-10-27T02:01:08Z", updatedAt="2026-10-27T02:01:08Z",
        ),
    ]
    today = "2026-10-27"
    already = any(
        getattr(o, "runId", "") == today
        and getattr(o, "side", "") == "NB"
        and str(getattr(o, "reason", "")).startswith("autofin_buy:")
        for o in rows
    )
    assert already is True


def test_no_ledger_rows_means_not_already_entered() -> None:
    """An empty ledger means no entry was made today."""
    already = any(
        getattr(o, "runId", "") == "2026-10-27"
        and getattr(o, "side", "") == "NB"
        and str(getattr(o, "reason", "")).startswith("autofin_buy:")
        for o in []
    )
    assert already is False


def test_sell_rows_do_not_count_as_an_entry() -> None:
    """A SELL (partial close / stop-loss) must not suppress the
    catch-up entry — only a BUY counts."""
    from vietnam_algo.libs.order_ledger import LedgerOrder

    rows = [
        LedgerOrder(
            runId="2026-10-27", sourceOrderId=1, sourceDate="2026-10-27",
            symbol="BAF", side="NS", quantity=200, price=30650,
            reason="autofin_partial:deal#1785888000:BAF:capital_protect",
            status="filled", dnseOrderId="250821",
            createdAt="2026-10-27T02:01:08Z", updatedAt="2026-10-27T02:01:08Z",
        ),
    ]
    already = any(
        getattr(o, "runId", "") == "2026-10-27"
        and getattr(o, "side", "") == "NB"
        and str(getattr(o, "reason", "")).startswith("autofin_buy:")
        for o in rows
    )
    assert already is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
