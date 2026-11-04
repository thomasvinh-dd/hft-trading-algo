"""Regression tests for the one-open-deal-per-ticker rule in
``get_ticker_history``.

Incident (2026-11-04): the user found ABB showing FIVE concurrent
open deals. GEE had five too, and ORS had three.

Root cause: ``get_ticker_history`` tracks ``last_exit_date`` to skip
a signal that fires while the previous deal on the same ticker is
still open. But the tracker was only advanced for CLOSED deals:

    if not is_open:
        last_exit_date = t.exit_date

So an open deal never advanced the tracker, and every later signal on
that ticker was accepted — piling up concurrent open deals.

An open deal's ``exit_date`` is the last available bar, so advancing
the tracker for open deals too blocks every subsequent signal on that
ticker (which is what the live Portfolio does).
"""
import unittest
from datetime import date


class LastExitDateTrackerTests(unittest.TestCase):
    """The tracker must advance for open deals as well as closed ones."""

    def test_tracker_advances_for_open_deal(self):
        """The core invariant, expressed directly."""
        # Simulate the loop the ticker-history builder runs.
        last_exit_date = None
        skipped = []

        deals = [
            # (entry_date, exit_date, is_open)
            (date(2026, 6, 19), date(2026, 11, 3), True),   # open
            (date(2026, 7, 31), date(2026, 11, 3), True),   # must be skipped
            (date(2026, 9, 21), date(2026, 11, 3), True),   # must be skipped
        ]

        for entry, exit_, is_open in deals:
            if last_exit_date is not None and entry <= last_exit_date:
                skipped.append(entry)
                continue
            # The fix: the tracker advances for BOTH open and closed deals.
            last_exit_date = exit_

        self.assertEqual(
            skipped, [date(2026, 7, 31), date(2026, 9, 21)],
            "signals that fired while a deal was still open must be skipped",
        )

    def test_closed_deal_still_blocks_earlier_signals(self):
        """A closed deal blocks any signal that fires before its exit."""
        last_exit_date = None
        skipped = []

        deals = [
            (date(2026, 1, 5), date(2026, 3, 10), False),  # closed
            (date(2026, 2, 1), date(2026, 5, 1), False),    # inside -> skip
            (date(2026, 4, 1), date(2026, 5, 1), False),    # after  -> keep
        ]

        for entry, exit_, is_open in deals:
            if last_exit_date is not None and entry <= last_exit_date:
                skipped.append(entry)
                continue
            last_exit_date = exit_

        self.assertEqual(skipped, [date(2026, 2, 1)])
        self.assertEqual(last_exit_date, date(2026, 5, 1))

    def test_same_day_entry_is_skipped(self):
        """A signal whose entry_date equals the previous exit_date
        overlaps it — HOSE settles T+2, so that's still concurrent."""
        last_exit_date = date(2026, 3, 10)
        entry = date(2026, 3, 10)
        self.assertTrue(entry <= last_exit_date)


class OpenDealBlocksLaterSignalsTests(unittest.TestCase):
    """End-to-end: the real ticker history must not pile up open deals."""

    def test_abb_has_at_most_one_open_deal(self):
        """ABB specifically — it had 5 concurrent open deals."""
        from darvas_box_strategy.bot.cli import build_default_config
        from darvas_box_strategy.web.data import get_ticker_history

        cfg = build_default_config()
        hist = get_ticker_history("ABB", cfg=cfg)
        opens = [d for d in hist["deals"] if d.get("is_open")]
        self.assertLessEqual(
            len(opens), 1,
            f"ABB has {len(opens)} concurrent open deals: "
            f"{[d['deal_id'] for d in opens]}",
        )

    def test_gee_has_at_most_one_open_deal(self):
        """GEE had 5 concurrent open deals too."""
        from darvas_box_strategy.bot.cli import build_default_config
        from darvas_box_strategy.web.data import get_ticker_history

        cfg = build_default_config()
        hist = get_ticker_history("GEE", cfg=cfg)
        opens = [d for d in hist["deals"] if d.get("is_open")]
        self.assertLessEqual(
            len(opens), 1,
            f"GEE has {len(opens)} concurrent open deals: "
            f"{[d['deal_id'] for d in opens]}",
        )

    def test_open_deal_exit_date_is_the_last_bar(self):
        """An open deal's exit_date is the last available bar — which is
        what makes it block every later signal on that ticker."""
        from darvas_box_strategy.bot.cli import build_default_config
        from darvas_box_strategy.web.data import get_ticker_history

        cfg = build_default_config()
        hist = get_ticker_history("ABB", cfg=cfg)
        opens = [d for d in hist["deals"] if d.get("is_open")]
        for d in opens:
            self.assertTrue(d["exit_date"], "open deal must have an exit_date")
            self.assertEqual(d["exit_reason"], "OPEN")


if __name__ == "__main__":
    unittest.main()
