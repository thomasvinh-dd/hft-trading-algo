from __future__ import annotations

import json
import os
import sys
import tempfile
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
        # Sentinel 30h old. Since the 2026-10-25 fix the gate counts
        # TRADING days, so 30 wall-clock hours may still be only 1
        # trading day (weekend) → fresh. To exercise the stale path we
        # need a sentinel that is 3+ TRADING days old, which is what
        # "the sync cron hasn't run for 3 sessions" looks like.
        # 5 calendar days back spans 3 trading days (weekend
        # excluded) — comfortably over the 2-day threshold.
        self._write_sentinel(datetime.now(tz=UTC) - timedelta(days=5))
        today = datetime.now(tz=UTC).date().isoformat()
        for sym in ("HPG", "VHM"):
            _write_price_file(self.tmp, sym, [{"time": today, "close": 100.0}])
        out = get_market_data_freshness(self.tmp)
        self.assertFalse(out["fresh"])
        self.assertGreater(out["sentinel_age_hours"], 25.0)
        self.assertGreater(out["sentinel_age_trading_days"], 2.0)

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
        # All symbols have the SAME age → no meaningful "oldest"
        # (2026-10-25 fix). An arbitrary tie-break winner would read
        # as "this ticker is broken" when nothing is wrong.
        self.assertIsNone(out["oldest_sample_symbol"])

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


class SentinelTradingDayAgeTests(unittest.TestCase):
    """Regression test for the 2026-10-25 false-STALE alert.

    Incident: on Sunday 2026-10-25 (a weekend) the momentum bot
    sent a "market data STALE" Telegram alert reading:

        data_ready_for_eod age: 43.0h (threshold 25h)
        sample latest-bar median age: 0 trading-days (2.15 calendar-days)
        oldest sample: HPG

    Two separate bugs:

    1. The sentinel gate compared RAW WALL-CLOCK HOURS against a 25h
       threshold. The sentinel was stamped Friday 2026-10-23 15:30
       ICT; by Sunday 22:45 ICT it was 43h old — but ZERO trading
       days had elapsed (Sat/Sun are non-trading). Commit c6d09ac had
       already converted the BAR-age gate to trading days but left the
       sentinel gate on raw hours, so every weekend produced a false
       alert.

    2. "oldest sample: HPG" was an arbitrary tie-break winner. All
       five sample symbols had identical age (0 trading-days), so
       ``max()`` returned whichever was first in the tuple — reading
       as "HPG is broken" when nothing was wrong. HPG's last bar was
       2026-10-23, the latest trading close, same as every other
       symbol.

    The fix: gate the sentinel on TRADING days (weekends + Vietnamese
    holidays excluded), and only report ``oldest_sample_symbol`` when
    there's an actual age spread.
    """

    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/sentinel_td_test")
        import shutil
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.tmp / "live").mkdir(exist_ok=True)
        (self.tmp / "prices").mkdir(exist_ok=True)

    def _ict_to_utc(self, d: str, hh: int = 13, mm: int = 0) -> datetime:
        """Build a UTC datetime from an ICT date string."""
        y, m, day = (int(x) for x in d.split("-"))
        return datetime(y, m, day, hh - 7, mm, tzinfo=UTC)

    def _write_sentinel_at(self, when_utc: datetime) -> None:
        sentinel = self.tmp / "live" / "data_ready_for_eod"
        sentinel.write_text(when_utc.isoformat())
        os.utime(sentinel, (when_utc.timestamp(), when_utc.timestamp()))

    def _write_friday_bars(self) -> None:
        """All sample symbols carry Friday 2026-10-23's close."""
        for sym in ("HPG", "VHM", "FPT", "VIC", "HDB"):
            _write_price_file(self.tmp, sym, [
                {"time": "2026-10-22", "close": 20900.0},
                {"time": "2026-10-23", "close": 20950.0},
            ])

    def test_weekend_sentinel_is_fresh(self) -> None:
        """Friday-stamped sentinel on Sunday evening = FRESH.

        This is the exact 2026-10-25 incident: sentinel stamped
        Friday 2026-10-23 15:30 ICT (08:30 UTC), probe at Sunday
        2026-10-25 22:45 ICT (15:45 UTC) → 43h wall-clock but ZERO
        trading days elapsed.
        """
        self._write_sentinel_at(self._ict_to_utc("2026-10-23", 15, 30))
        self._write_friday_bars()
        out = get_market_data_freshness(
            self.tmp, now=self._ict_to_utc("2026-10-25", 22, 45)
        )
        # Raw wall-clock age is > 25h — that's what tripped the alert.
        self.assertGreater(out["sentinel_age_hours"], 25.0)
        # But no trading day elapsed since the Friday stamp.
        self.assertEqual(out["sentinel_age_trading_days"], 0.0)
        # Bars are Friday's close → 0 trading-days old.
        self.assertEqual(out["median_latest_bar_age_days"], 0.0)
        # ...so the whole thing is FRESH. No false alert.
        self.assertTrue(
            out["fresh"],
            f"Weekend must not trip the stale gate: {out}",
        )

    def test_monday_morning_sentinel_is_fresh(self) -> None:
        """Friday stamp → Monday 09:00 ICT = 1 trading day = fresh.

        Monday 09:00 ICT is BEFORE the 13:15 ICT common_sync cron
        fires, so the sentinel is still Friday's stamp. Pre-fix this
        was 65h old → false STALE alert every Monday morning.
        """
        self._write_sentinel_at(self._ict_to_utc("2026-10-23", 15, 30))
        self._write_friday_bars()
        out = get_market_data_freshness(
            self.tmp, now=self._ict_to_utc("2026-10-26", 9, 0)
        )
        self.assertGreater(out["sentinel_age_hours"], 25.0)
        self.assertEqual(out["sentinel_age_trading_days"], 1.0)
        self.assertTrue(out["fresh"], f"Monday morning must be fresh: {out}")

    def test_tuesday_sentinel_trips_after_missed_session(self) -> None:
        """Friday stamp → Tuesday = 2 trading days → still fresh (<= 2).

        Wednesday = 3 trading days → STALE. The gate has to actually
        catch a missed trading session, not just weekends.
        """
        self._write_sentinel_at(self._ict_to_utc("2026-10-23", 15, 30))
        self._write_friday_bars()
        # Tuesday 2026-10-27 → 2 trading days (Mon 26th, Tue 27th)
        out = get_market_data_freshness(
            self.tmp, now=self._ict_to_utc("2026-10-27", 10, 0)
        )
        self.assertEqual(out["sentinel_age_trading_days"], 2.0)
        # Bars are still Friday's → 2 trading-days old → fresh.
        self.assertTrue(out["fresh"], f"Tuesday should still be fresh: {out}")
        # Wednesday 2026-10-28 → 3 trading days for the sentinel AND
        # for the bars → STALE.
        out = get_market_data_freshness(
            self.tmp, now=self._ict_to_utc("2026-10-28", 10, 0)
        )
        self.assertEqual(out["sentinel_age_trading_days"], 3.0)
        self.assertEqual(out["median_latest_bar_age_days"], 3.0)
        self.assertFalse(
            out["fresh"],
            f"Wednesday must trip the stale gate: {out}",
        )

    def test_oldest_sample_symbol_omitted_when_all_tied(self) -> None:
        """When every sample symbol has the same age, report NO
        'oldest sample' — an arbitrary tie-break winner reads as
        'this ticker is broken' (2026-10-25: "oldest sample: HPG"
        when HPG was perfectly fresh)."""
        self._write_sentinel_at(self._ict_to_utc("2026-10-23", 15, 30))
        self._write_friday_bars()
        out = get_market_data_freshness(
            self.tmp, now=self._ict_to_utc("2026-10-25", 22, 45)
        )
        self.assertEqual(out["sample_size"], 5)
        self.assertIsNone(
            out["oldest_sample_symbol"],
            f"All-tied sample must not name an 'oldest' symbol: {out}",
        )

    def test_oldest_sample_symbol_reported_when_real_spread(self) -> None:
        """When one symbol IS genuinely older, it must be named."""
        self._write_sentinel_at(self._ict_to_utc("2026-10-23", 15, 30))
        for sym in ("HPG", "VHM", "FPT", "VIC", "HDB"):
            _write_price_file(self.tmp, sym, [
                {"time": "2026-10-23", "close": 20950.0},
            ])
        # HPG lags by one trading day.
        _write_price_file(self.tmp, "HPG", [
            {"time": "2026-10-22", "close": 20900.0},
        ])
        out = get_market_data_freshness(
            self.tmp, now=self._ict_to_utc("2026-10-26", 10, 0)
        )
        self.assertEqual(
            out["oldest_sample_symbol"], "HPG",
            f"A genuine age spread must still name the symbol: {out}",
        )

    def test_missing_sentinel_has_no_trading_day_age(self) -> None:
        """No sentinel → no trading-day age (and still not fresh)."""
        out = get_market_data_freshness(self.tmp)
        self.assertFalse(out["sentinel_exists"])
        self.assertIsNone(out["sentinel_age_hours"])
        self.assertIsNone(out["sentinel_age_trading_days"])
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


class HeldPositionFreshnessTest(unittest.TestCase):
    """2026-10-21 incident (MST): the dry-run state held 9,500 MST
    shares but the fixed 5-symbol freshness sample never included
    MST, so ``fresh: True`` was reported even though MST's price
    file was 67+ calendar days stale. The fix unions the sample
    with held positions from the dry-run state and surfaces
    ``stale_held_positions`` separately so the operator gets a
    targeted Telegram alert.

    These tests exercise the ``include_held_positions=True``
    default path through ``get_market_data_freshness``.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="freshness-held-"))
        from vietnam_algo.libs import dry_run_state as drs
        self._state_path_module = drs
        self._tmp_live = self.tmp / "live"
        self._tmp_live.mkdir(parents=True, exist_ok=True)
        # /prices directory for sample price files
        (self.tmp / "prices").mkdir(exist_ok=True)
        # sentinel file (used for sentinel_age_hours)
        sentinel = self.tmp / "live" / "data_ready_for_eod"
        sentinel.write_text("ok", encoding="utf-8")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_dry_run_state(self, strategy: str, positions: dict) -> None:
        """Write a per-strategy dry-run state file with the given
        ``positions`` dict, mimicking what `_apply_dry_run_fills`
        would persist after a successful BUY."""
        sp = self._state_path_module._state_path(self.tmp, strategy)
        sp.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "initial_nav": 100_000_000.0,
            "cash": 50_000_000.0,
            "realized_pnl": 0.0,
            "closed_trades": [],
            "applied_keys": [],
            "positions": positions,
            "created_at": "2026-08-01T00:00:00+00:00",
            "updated_at": "2026-08-01T00:00:00+00:00",
        }
        sp.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _price_file_with_last_date(self, symbol: str, last_date_iso: str) -> None:
        """Write a synthetic price file with a single bar at
        ``last_date_iso`` so the freshness check can compute ages."""
        from datetime import date as _date
        path = self.tmp / "prices" / f"{symbol}.json"
        bar_date = _date.fromisoformat(last_date_iso)
        path.write_text(
            json.dumps(
                {
                    "source": "test",
                    "fetchedAt": "2026-08-01T00:00:00Z",
                    "count": 1,
                    "symbol": symbol,
                    "items": [
                        {
                            "time": last_date_iso,
                            "open": 100.0,
                            "high": 100.0,
                            "low": 100.0,
                            "close": 100.0,
                            "volume": 0.0,
                        }
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_held_stale_position_flags_stale_held(self) -> None:
        """2026-10-21 MST scenario: MST is held but its price file
        is 30 trading days stale. The fixed 5-symbol sample is
        fresh. Pre-fix: ``fresh: True`` was returned and the
        staleness was invisible. Post-fix: ``stale_held_positions``
        lists MST and ``fresh: False`` is returned (median is bumped
        by the stale held position)."""
        from vietnam_algo.libs.schedule import vietnam_now
        # Dry-run state holds 9,500 MST (the actual incident numbers).
        self._write_dry_run_state(
            "momentum",
            {"MST": {"symbol": "MST", "quantity": 9500, "avg_price": 10410.4}},
        )
        # MST's price file is 30 calendar days old (last bar mid-Sep).
        old = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
        self._price_file_with_last_date("MST", old)
        # Fixed sample is fully fresh (today).
        today = datetime.now(UTC).date().isoformat()
        for s in ("HPG", "VHM", "FPT", "VIC", "HDB"):
            self._price_file_with_last_date(s, today)
        # Now run the freshness check.
        r = get_market_data_freshness(
            self.tmp, now=vietnam_now(),
            held_position_strategies=("momentum",),
        )
        # Held positions are surfaced.
        self.assertEqual(r["held_position_count"], 1)
        self.assertEqual(r["held_positions"], ["MST"])
        # MST is in stale_held_positions (no_price_file=None, but
        # here we have a 30-day-old file → flagged as stale).
        self.assertEqual(len(r["stale_held_positions"]), 1)
        stale = r["stale_held_positions"][0]
        self.assertEqual(stale["symbol"], "MST")
        self.assertGreater(stale["age_trading_days"], 2.0)
        # The overall fresh flag must be False now (stale held should
        # trip the median > 2.0 check, AND we explicitly flip it).
        self.assertFalse(r["fresh"])
        # Oldest sample symbol should be MST, not HPG.
        self.assertEqual(r["oldest_sample_symbol"], "MST")

    def test_held_fresh_position_does_not_flag(self) -> None:
        """If a held position's price file is fresh (today), the
        check returns ``stale_held_positions=[]`` and ``fresh: True``
        — the inclusion of held positions must not introduce false
        positives."""
        from vietnam_algo.libs.schedule import vietnam_now
        self._write_dry_run_state(
            "momentum",
            {"HPG": {"symbol": "HPG", "quantity": 1000, "avg_price": 25000}},
        )
        # HPG's price file is fresh today.
        today = datetime.now(UTC).date().isoformat()
        self._price_file_with_last_date("HPG", today)
        for s in ("VHM", "FPT", "VIC", "HDB"):
            self._price_file_with_last_date(s, today)
        r = get_market_data_freshness(
            self.tmp, now=vietnam_now(),
            held_position_strategies=("momentum",),
        )
        # HPG is in held_positions but its bar is fresh today.
        self.assertEqual(r["held_position_count"], 1)
        self.assertEqual(r["held_positions"], ["HPG"])
        self.assertEqual(r["stale_held_positions"], [])
        self.assertTrue(r["fresh"])

    def test_held_position_no_price_file(self) -> None:
        """When a held symbol has NO price file on disk at all, it
        is reported as stale with ``reason='no_price_file'`` so the
        operator knows to add the symbol to the price-sync universe.
        """
        from vietnam_algo.libs.schedule import vietnam_now
        # Held MST but no data/prices/MST.json exists.
        self._write_dry_run_state(
            "autofin_signal",
            {"MST": {"symbol": "MST", "quantity": 100, "avg_price": 9300}},
        )
        today_iso = datetime.now(UTC).date().isoformat()
        for s in ("HPG", "VHM", "FPT", "VIC", "HDB"):
            self._price_file_with_last_date(s, today_iso)
        r = get_market_data_freshness(
            self.tmp, now=vietnam_now(),
            held_position_strategies=("autofin_signal",),
        )
        self.assertEqual(r["held_positions"], ["MST"])
        # No price file → flagged as stale with reason='no_price_file'.
        self.assertEqual(len(r["stale_held_positions"]), 1)
        self.assertEqual(r["stale_held_positions"][0]["reason"], "no_price_file")

    def test_include_held_positions_false(self) -> None:
        """Operators can opt out of the held-position inclusion via
        ``include_held_positions=False`` (used by some unit tests
        that don't want to depend on dry-run state files)."""
        from vietnam_algo.libs.schedule import vietnam_now
        # Held MST (would normally be flagged).
        self._write_dry_run_state(
            "momentum",
            {"MST": {"symbol": "MST", "quantity": 9500, "avg_price": 10410.4}},
        )
        old = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
        self._price_file_with_last_date("MST", old)
        today = datetime.now(UTC).date().isoformat()
        for s in ("HPG", "VHM", "FPT", "VIC", "HDB"):
            self._price_file_with_last_date(s, today)
        r = get_market_data_freshness(
            self.tmp, now=vietnam_now(),
            include_held_positions=False,
        )
        # Held positions are ignored.
        self.assertEqual(r["held_position_count"], 0)
        self.assertEqual(r["held_positions"], [])
        self.assertEqual(r["stale_held_positions"], [])
        self.assertEqual(r["sample_total"], 5)
        # MST is NOT in the sample because we opted out.


if __name__ == "__main__":
    unittest.main()
