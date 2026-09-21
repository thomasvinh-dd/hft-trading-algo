from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.bot_state import AutoBotState, load_state, save_state
from vietnam_algo.libs.data import get_market_data_freshness, sync_prices_incremental


def _write_price_file(
    data_dir: Path,
    symbol: str,
    bars: list[dict],
    mtime: datetime | None = None,
) -> None:
    path = data_dir / "prices" / f"{symbol}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "source": "test", "symbol": symbol, "count": len(bars), "items": bars,
    }))
    if mtime is not None:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))


class MarketDataFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/freshness_test")
        import shutil
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.tmp / "live").mkdir(exist_ok=True)
        (self.tmp / "prices").mkdir(exist_ok=True)

    def _write_sentinel(self, mtime: datetime | None = None) -> None:
        sentinel = self.tmp / "live" / "data_ready_for_eod"
        sentinel.write_text(datetime.now(tz=UTC).isoformat())
        if mtime is not None:
            os.utime(sentinel, (mtime.timestamp(), mtime.timestamp()))

    def test_fresh_when_sentinel_recent_and_bars_today(self) -> None:
        # Sentinel 1h old, sample bars all from today
        self._write_sentinel(datetime.now(tz=UTC) - timedelta(hours=1))
        today = datetime.now(tz=UTC).date().isoformat()
        for sym in ("HPG", "VHM", "FPT", "VIC", "HDB"):
            _write_price_file(self.tmp, sym, [{"time": today, "close": 100.0}])
        out = get_market_data_freshness(self.tmp)
        self.assertTrue(out["fresh"])
        self.assertEqual(out["sample_size"], 5)
        self.assertIsNotNone(out["sentinel_age_hours"])
        self.assertLess(out["sentinel_age_hours"], 25.0)
        self.assertLess(out["median_latest_bar_age_days"], 2.0)

    def test_stale_when_sentinel_old(self) -> None:
        # Sentinel 30h old
        self._write_sentinel(datetime.now(tz=UTC) - timedelta(hours=30))
        today = datetime.now(tz=UTC).date().isoformat()
        for sym in ("HPG", "VHM"):
            _write_price_file(self.tmp, sym, [{"time": today, "close": 100.0}])
        out = get_market_data_freshness(self.tmp)
        self.assertFalse(out["fresh"])
        self.assertGreater(out["sentinel_age_hours"], 25.0)

    def test_stale_when_sample_bars_old(self) -> None:
        # Sentinel fresh, but sample bars are 5 calendar days old.
        # In trading-day terms, 5 calendar days = ~3 trading days
        # (skipping the weekend), which still exceeds the 2-day
        # threshold.
        self._write_sentinel(datetime.now(tz=UTC) - timedelta(hours=1))
        old_date = (datetime.now(tz=UTC) - timedelta(days=5)).date().isoformat()
        for sym in ("HPG", "VHM", "FPT", "VIC", "HDB"):
            _write_price_file(self.tmp, sym, [{"time": old_date, "close": 100.0}])
        out = get_market_data_freshness(self.tmp)
        self.assertFalse(out["fresh"])
        self.assertGreater(out["median_latest_bar_age_days"], 2.0)
        # All tied — just verify it picks one of them
        self.assertIn(out["oldest_sample_symbol"], {"HPG", "VHM", "FPT", "VIC", "HDB"})

    def test_freshness_uses_trading_days_not_calendar_days(self) -> None:
        """User feedback 2026-09-19: "the day before is weekend
        so we can not say that the data is stale, we only compare
        with trading day not holiday."

        Vietnamese markets close on weekends and Vietnamese public
        holidays. Pre-fix the freshness check counted plain
        calendar days, so a Friday close on a Monday morning
        looked 3 days old — every Monday the operator got a false
        STALE alert. Post-fix we count trading days via
        ``trading_dates_between()`` (excludes weekends + Vietnamese
        holidays from ``load_market_holidays()``).

        This test simulates a Friday close and walks ``now``
        forward through the weekend and into the next week,
        asserting the freshness gate stays at 1 trading day until
        the next trading session opens.
        """
        from datetime import timezone, timedelta

        def _ict_to_utc(d: str, hh: int = 13, mm: int = 0) -> datetime:
            """Build a UTC datetime from an ICT date string."""
            y, m, day = (int(x) for x in d.split("-"))
            return datetime(y, m, day, hh - 7, mm, tzinfo=UTC)

        # Friday Sep 18 close — bar timestamp
        friday_bar = _ict_to_utc("2026-09-18", 14, 0)
        self._write_sentinel(friday_bar)
        for sym in ("HPG", "VHM", "FPT", "VIC", "HDB"):
            _write_price_file(self.tmp, sym, [{"time": "2026-09-18", "close": 100.0}])

        # Same Friday afternoon (1 trading day from Friday? no,
        # 0 days since today is the bar date itself).
        out = get_market_data_freshness(
            self.tmp, now=_ict_to_utc("2026-09-18", 15, 0)
        )
        self.assertEqual(out["median_latest_bar_age_days"], 0,
                         f"Friday afternoon should be 0 trading-days: {out}")

        # Saturday — still 0 trading days
        out = get_market_data_freshness(
            self.tmp, now=_ict_to_utc("2026-09-19", 12, 0)
        )
        self.assertEqual(out["median_latest_bar_age_days"], 0,
                         f"Saturday should be 0 trading-days: {out}")

        # Sunday — still 0 trading days
        out = get_market_data_freshness(
            self.tmp, now=_ict_to_utc("2026-09-20", 12, 0)
        )
        self.assertEqual(out["median_latest_bar_age_days"], 0,
                         f"Sunday should be 0 trading-days: {out}")

        # Monday morning — 1 trading day (Fri close → Mon open)
        out = get_market_data_freshness(
            self.tmp, now=_ict_to_utc("2026-09-21", 9, 0)
        )
        self.assertEqual(out["median_latest_bar_age_days"], 1,
                         f"Monday should be 1 trading-day: {out}")
        # Calendar days is 3 (Fri → Sat → Sun → Mon) — operator
        # mental model says "3 days" but the FRESH gate uses
        # trading days so we don't false-alarm on Mondays.
        self.assertGreaterEqual(out["median_latest_bar_age_calendar_days"], 3)
        self.assertLess(out["median_latest_bar_age_calendar_days"], 4)

        # Tuesday — 2 trading days (still under threshold of 2.0)
        out = get_market_data_freshness(
            self.tmp, now=_ict_to_utc("2026-09-22", 13, 0)
        )
        self.assertEqual(out["median_latest_bar_age_days"], 2)

        # Wednesday — 3 trading days → STALE (exceeds 2.0 threshold)
        out = get_market_data_freshness(
            self.tmp, now=_ict_to_utc("2026-09-23", 13, 0)
        )
        self.assertEqual(out["median_latest_bar_age_days"], 3)
        self.assertFalse(out["fresh"])

    def test_missing_sentinel_reports_none_age(self) -> None:
        # No sentinel at all
        for sym in ("HPG", "VHM"):
            today = datetime.now(tz=UTC).date().isoformat()
            _write_price_file(self.tmp, sym, [{"time": today, "close": 100.0}])
        out = get_market_data_freshness(self.tmp)
        self.assertFalse(out["sentinel_exists"])
        self.assertIsNone(out["sentinel_age_hours"])
        # Without a sentinel we can't call it fresh — there's no proof
        # that data has been synced recently.
        self.assertFalse(out["fresh"])

    def test_missing_sample_symbols_handled(self) -> None:
        # Sentinel fresh but no sample-symbol price files
        self._write_sentinel(datetime.now(tz=UTC) - timedelta(hours=1))
        out = get_market_data_freshness(self.tmp)
        self.assertEqual(out["sample_size"], 0)
        self.assertIsNone(out["median_latest_bar_age_days"])
        # Without any sample bar info we can't claim fresh.
        self.assertFalse(out["fresh"])


class AutoBotStateFreshnessFieldsTests(unittest.TestCase):
    """AutoBotState must round-trip the new freshness-alert fields."""

    def test_default_values(self) -> None:
        s = AutoBotState()
        self.assertEqual(s.dataStaleAlertSentDate, "")
        self.assertEqual(s.dataFetchErrorAlertSentDate, "")
        self.assertEqual(s.lastDataSyncErrorSummary, "")

    def test_round_trip_via_disk(self) -> None:
        tmp = Path("/tmp/opencode/bot_state_test")
        import shutil
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True, exist_ok=True)
        s = AutoBotState()
        s.dataStaleAlertSentDate = "2026-09-17"
        s.dataFetchErrorAlertSentDate = "2026-09-17"
        s.lastDataSyncErrorSummary = "3/1535 symbols failed"
        save_state(s, tmp, strategy_name="test_strategy")
        loaded = load_state(tmp, strategy_name="test_strategy")
        self.assertEqual(loaded.dataStaleAlertSentDate, "2026-09-17")
        self.assertEqual(loaded.dataFetchErrorAlertSentDate, "2026-09-17")
        self.assertEqual(loaded.lastDataSyncErrorSummary, "3/1535 symbols failed")


class SyncPricesIncrementalErrorReportingTests(unittest.TestCase):
    """sync_prices_incremental must surface per-symbol fetch failures
    in its return value so the alert path in common_sync.run() can fire."""

    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/incremental_errors_test")
        import shutil
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.tmp / "prices").mkdir(exist_ok=True)

    def test_mixed_success_and_failure(self) -> None:
        def fake_fetch(symbol: str, **kwargs):
            if symbol == "FAIL":
                raise ConnectionError("timeout")
            return []

        with patch("vietnam_algo.libs.data.fetch_price_bars", side_effect=fake_fetch):
            paths, errors = sync_prices_incremental(
                ["AAA", "FAIL", "BBB"], self.tmp, sleep_seconds=0
            )
        # AAA + BBB had no new bars → paths empty. FAIL is reported.
        self.assertEqual(paths, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], "FAIL")
        self.assertIn("timeout", errors[0][1])

    def test_all_failures_no_paths(self) -> None:
        with patch(
            "vietnam_algo.libs.data.fetch_price_bars",
            side_effect=RuntimeError("upstream down"),
        ):
            paths, errors = sync_prices_incremental(
                ["AAA", "BBB", "CCC"], self.tmp, sleep_seconds=0
            )
        self.assertEqual(paths, [])
        self.assertEqual(len(errors), 3)
        for sym, err in errors:
            self.assertIn("upstream down", err)


if __name__ == "__main__":
    unittest.main()
