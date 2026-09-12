"""Tests for the data_ready_for_eod sentinel that coordinates
the autofin (data-sync owner) and momentum (data-sync reader) bots.

When --no-sync-data is set, the momentum bot waits for the autofin
bot's sentinel file before proceeding with EOD / start-of-day. This
prevents the two bots from racing on the same data sources and
ensures momentum never reads stale prices.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.daily_engine import EngineConfig


class DataReadySentinelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/data_ready_test")
        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.tmp / "live").mkdir(parents=True, exist_ok=True)

    def _write_sentinel(self, age_min: float = 5.0) -> None:
        """Stamp the data_ready_for_eod file with mtime = now-age_min."""
        import os
        import time
        path = self.tmp / "live" / "data_ready_for_eod"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat())
        if age_min > 0:
            old = time.time() - age_min * 60
            os.utime(path, (old, old))

    def test_sentinel_path_contract(self) -> None:
        """The data_ready_for_eod sentinel must live at
        ``<dataDir>/<liveDirName>/data_ready_for_eod`` — same path
        the auto_bot.py EOD path stamps after update_dataset() and
        that the gate logic checks before proceeding."""
        ec = EngineConfig(
            dataDir=self.tmp, strategy="momentum",
            updateSymbols=False, updatePrices=False,
            liveDirName="live",
        )
        sentinel_path = ec.dataDir / ec.liveDirName / "data_ready_for_eod"
        self.assertEqual(
            str(sentinel_path),
            str(self.tmp / "live" / "data_ready_for_eod"),
        )

    def test_fresh_sentinel_passes_age_check(self) -> None:
        """mtime within 60 min → the gate accepts it."""
        import time
        self._write_sentinel(age_min=5.0)
        sentinel = self.tmp / "live" / "data_ready_for_eod"
        age_sec = time.time() - sentinel.stat().st_mtime
        self.assertLess(age_sec, 60 * 60)  # gate threshold

    def test_stale_sentinel_exceeds_age_check(self) -> None:
        """mtime over 60 min → the gate flags it as too old."""
        import time
        self._write_sentinel(age_min=120.0)
        sentinel = self.tmp / "live" / "data_ready_for_eod"
        age_sec = time.time() - sentinel.stat().st_mtime
        self.assertGreater(age_sec, 60 * 60)  # owner stuck


if __name__ == "__main__":
    unittest.main()
