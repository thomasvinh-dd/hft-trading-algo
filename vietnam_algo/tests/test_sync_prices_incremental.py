from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.data import PriceBar, sync_prices_incremental


def _make_bar(t: str, close: float) -> PriceBar:
    return PriceBar(
        time=t, open=close, high=close, low=close, close=close, volume=1000.0,
    )


class SyncPricesIncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/incremental_test")
        # Aggressive cleanup: nuke the entire tree so a previous test
        # that created BBB.json can't pollute this one.
        import shutil
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        prices = self.tmp / "prices"
        prices.mkdir(exist_ok=True)
        # existing file: 3 bars
        existing = [
            {"time": "2026-09-01", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            {"time": "2026-09-02", "open": 100, "high": 102, "low": 100, "close": 101, "volume": 1500},
            {"time": "2026-09-03", "open": 101, "high": 103, "low": 101, "close": 102, "volume": 2000},
        ]
        (prices / "AAA.json").write_text(json.dumps({
            "source": "test", "symbol": "AAA", "count": 3, "items": existing,
        }))

    def _patch_fetch(self, bars):
        return patch(
            "vietnam_algo.data.fetch_price_bars",
            return_value=bars,
        )

    def test_appends_new_bars(self) -> None:
        new_bars = [
            _make_bar("2026-09-01", 100),
            _make_bar("2026-09-02", 101),
            _make_bar("2026-09-03", 102),
            _make_bar("2026-09-04", 103),  # new
            _make_bar("2026-09-05", 104),  # new
        ]
        with self._patch_fetch(new_bars):
            paths = sync_prices_incremental(["AAA"], self.tmp, sleep_seconds=0)
        self.assertEqual(len(paths), 1)
        merged = json.loads(paths[0].read_text())
        items = merged["items"]
        # 5 bars total (3 existing + 2 new)
        self.assertEqual(len(items), 5)
        # Sorted ascending
        self.assertEqual([r["time"] for r in items],
                         ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"])
        # Last bar's close is the new one
        self.assertEqual(items[-1]["close"], 104.0)

    def test_no_new_bars_skips_write(self) -> None:
        # All bars in the fetch are duplicates of existing
        new_bars = [
            _make_bar("2026-09-01", 100),
            _make_bar("2026-09-02", 101),
            _make_bar("2026-09-03", 102),
        ]
        with self._patch_fetch(new_bars):
            paths = sync_prices_incremental(["AAA"], self.tmp, sleep_seconds=0)
        # No new bars → no write
        self.assertEqual(paths, [])

    def test_missing_file_creates_from_scratch(self) -> None:
        # BBB has no file yet
        new_bars = [
            _make_bar("2026-09-04", 50),
            _make_bar("2026-09-05", 51),
        ]
        with self._patch_fetch(new_bars):
            paths = sync_prices_incremental(["BBB"], self.tmp, sleep_seconds=0)
        self.assertEqual(len(paths), 1)
        merged = json.loads(paths[0].read_text())
        self.assertEqual(len(merged["items"]), 2)

    def test_fetch_failure_does_not_crash(self) -> None:
        with patch("vietnam_algo.data.fetch_price_bars", side_effect=RuntimeError("network down")):
            paths = sync_prices_incremental(["AAA"], self.tmp, sleep_seconds=0)
        self.assertEqual(paths, [])
        # Existing file untouched
        merged = json.loads((self.tmp / "prices" / "AAA.json").read_text())
        self.assertEqual(len(merged["items"]), 3)

    def test_dedup_on_identical_timestamps(self) -> None:
        # Same timestamps as existing, but with UPDATED prices (last-close
        # revision from upstream). Our implementation should NOT overwrite
        # — it dedupes by 'time' and keeps the existing record.
        new_bars = [
            _make_bar("2026-09-01", 999),  # would-be overwrite
            _make_bar("2026-09-02", 999),
            _make_bar("2026-09-03", 999),
        ]
        with self._patch_fetch(new_bars):
            paths = sync_prices_incremental(["AAA"], self.tmp, sleep_seconds=0)
        self.assertEqual(paths, [])
        merged = json.loads((self.tmp / "prices" / "AAA.json").read_text())
        # First close stays at 100 (not overwritten)
        self.assertEqual(merged["items"][0]["close"], 100)

    def test_handles_empty_fetch_response(self) -> None:
        with self._patch_fetch([]):
            paths = sync_prices_incremental(["AAA"], self.tmp, sleep_seconds=0)
        # No new bars → no write
        self.assertEqual(paths, [])

    def test_sorts_merged_output(self) -> None:
        # Send out-of-order bars from the upstream
        new_bars = [
            _make_bar("2026-09-05", 105),
            _make_bar("2026-09-04", 104),
            _make_bar("2026-09-06", 106),
        ]
        with self._patch_fetch(new_bars):
            paths = sync_prices_incremental(["AAA"], self.tmp, sleep_seconds=0)
        merged = json.loads(paths[0].read_text())
        times = [r["time"] for r in merged["items"]]
        self.assertEqual(times, sorted(times))


if __name__ == "__main__":
    unittest.main()
