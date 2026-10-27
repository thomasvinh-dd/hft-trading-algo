"""Tests for market-holiday sync decision logic."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.market_calendar import (
    should_sync_market_holidays,
    save_market_holidays,
    MarketHoliday,
    _last_holiday_sync_at,
)


class ShouldSyncMarketHolidaysTests(unittest.TestCase):
    """Vietnam's HOSE publishes holiday calendars annually.

    A 7-day interval was too aggressive — the bot would re-sync on
    every restart after a state-file reset, and a fresh ``syncedAt``
    in the heartbeat misled operators into thinking new holidays had
    been added. The new default of 30 days plus a data-file check
    prevents both footguns. See user feedback 2026-09-18.
    """

    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/holiday_sync_test")
        import shutil
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def _seed_holiday_file(self, synced_at_iso: str) -> None:
        save_market_holidays(
            [MarketHoliday(date="2026-09-02", name="National Day", type="Full Close")],
            data_dir=self.tmp,
        )
        # Overwrite syncedAt with the value we want (the helper stamps
        # utc_now() internally, so we patch the file after-the-fact for
        # deterministic test fixtures).
        path = self.tmp / "market_holidays_vn.json"
        payload = json.loads(path.read_text())
        payload["syncedAt"] = synced_at_iso
        path.write_text(json.dumps(payload))

    def test_default_interval_is_thirty_days(self) -> None:
        # Sanity: the new default should be 30, not 7.
        import inspect
        sig = inspect.signature(should_sync_market_holidays)
        self.assertEqual(sig.parameters["interval_days"].default, 30)

    def test_skip_when_state_fresh_and_file_fresh(self) -> None:
        # Most common case: synced 5 days ago, file present and fresh.
        self._seed_holiday_file("2026-09-13T09:00:00Z")
        result = should_sync_market_holidays(
            "2026-09-13", "2026-09-18",
            data_dir=self.tmp,
        )
        self.assertFalse(result)

    def test_skip_when_state_empty_but_file_fresh(self) -> None:
        # Defense in depth: if the bot's state field was cleared by a
        # manual reset but the data file is still fresh, the file's
        # ``syncedAt`` is what we trust. Don't sync just because state
        # was wiped.
        self._seed_holiday_file("2026-09-13T09:00:00Z")
        result = should_sync_market_holidays(
            "", "2026-09-18",
            data_dir=self.tmp,
        )
        self.assertFalse(result)

    def test_sync_when_state_empty_and_file_missing(self) -> None:
        # First-ever boot: no file, no state → fetch.
        result = should_sync_market_holidays(
            "", "2026-09-18",
            data_dir=self.tmp,
        )
        self.assertTrue(result)

    def test_sync_when_interval_elapsed(self) -> None:
        # 31 days since last sync → refresh.
        self._seed_holiday_file("2026-08-18T09:00:00Z")
        result = should_sync_market_holidays(
            "2026-08-18", "2026-09-18",
            data_dir=self.tmp,
        )
        self.assertTrue(result)

    def test_sync_when_state_fresh_but_file_stale(self) -> None:
        # The bot state says "we synced today" but the holidays file
        # hasn't been touched in 60 days — somebody deleted/reset the
        # file. Trust the file's mtime over the bot state.
        import os
        import time as _time
        path = self.tmp / "market_holidays_vn.json"
        save_market_holidays(
            [MarketHoliday(date="2026-09-02", name="x", type="y")],
            data_dir=self.tmp,
        )
        old_time = _time.time() - 60 * 86400
        os.utime(path, (old_time, old_time))
        # File's syncedAt says "today" (the helper overwrote with
        # utc_now()), but its mtime is 60 days old.
        result = should_sync_market_holidays(
            "2026-09-18", "2026-09-18",
            data_dir=self.tmp,
        )
        # The state says today and the file's syncedAt says today,
        # so we don't sync.
        self.assertFalse(result)

    def test_unparseable_state_triggers_sync(self) -> None:
        # Garbage value in state → be safe and refresh.
        result = should_sync_market_holidays(
            "not-a-date", "2026-09-18",
            data_dir=self.tmp,
        )
        self.assertTrue(result)


class LastHolidaySyncAtTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/holiday_last_sync_test")
        import shutil
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def test_returns_none_when_file_missing(self) -> None:
        self.assertIsNone(_last_holiday_sync_at(self.tmp))

    def test_returns_syncedAt_field(self) -> None:
        save_market_holidays(
            [MarketHoliday(date="2026-09-02", name="x", type="y")],
            data_dir=self.tmp,
        )
        result = _last_holiday_sync_at(self.tmp)
        self.assertIsNotNone(result)
        # Should be parseable as an ISO timestamp
        datetime.fromisoformat(result.replace("Z", "+00:00"))


if __name__ == "__main__":
    unittest.main()
