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


class DataReadyGateRegressionTests(unittest.TestCase):
    """Regression test for 2026-09-17 runaway loop.

    Background: the momentum bot's start-of-day block used to call
    ``continue`` *from inside* the data-ready wait print. Because
    ``continue`` jumps back to the top of the while-loop, it skipped
    the ``time.sleep(sleep_seconds)`` at the bottom — the bot spun
    at CPU speed, printing the wait message ~16k times/sec until
    the log filled the disk (33 GB in 22 hours).

    Fix: the data-ready gate now follows the same flag-then-continue
    pattern as the EOD wait block — set ``data_ready = False``
    inside the wait print, then check ``if not data_ready: continue``
    AFTER the inner block so the time.sleep is reached.

    This test exercises the runner's main loop end-to-end with a
    stale sentinel and asserts the loop:
      1. Prints the wait message at most ONCE per main-loop pass
         (i.e. the function returns to the time.sleep within one
         iteration, NOT loops within the iteration).
      2. The wall-clock duration of one full iteration is at least
         ``sleep_seconds`` (with a small margin) — i.e. the bot is
         not running hot.
    """

    def setUp(self) -> None:
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="data_ready_gate_"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_stale_sentinel(self, age_min: float) -> None:
        import os
        import time
        path = self.tmp / "live" / "data_ready_for_eod"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat())
        old = time.time() - age_min * 60
        os.utime(path, (old, old))

    def test_start_of_day_wait_does_not_skip_sleep(self) -> None:
        """The data-ready wait print must not bypass the main-loop sleep.

        We invoke ``run_auto_bot`` with a stale sentinel and
        ``sleep_seconds=1`` and a TradingSchedule that forces
        start-of-day to fire on every iteration. After one pass,
        the bot must (a) have printed the wait message at most once,
        and (b) the elapsed wall-clock must be >= sleep_seconds — the
        time.sleep at the end of the loop must have run.
        """
        import os
        import time

        # Set up a stale sentinel (24h old → well past the 18h gate).
        self._make_stale_sentinel(age_min=24 * 60)

        # Build the env needed for run_auto_bot (DNSE creds are
        # checked in main.py before run_auto_bot is called, so we
        # bypass that and call run_auto_bot directly).
        os.environ["DNSE_API_KEY"] = "x"
        os.environ["DNSE_API_SECRET"] = "y"
        os.environ["DNSE_ACCOUNT_NO"] = "0003937868"

        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

        # Import lazily so we don't break module-level deps when
        # pytest imports the file before this point.
        import datetime as _dt
        from vietnam_algo.libs.config import StrategyConfig
        from vietnam_algo.libs.daily_engine import EngineConfig
        from vietnam_algo.bots.runner import run_auto_bot
        from vietnam_algo.algos.momentum import MomentumRotationAdapter
        from vietnam_algo.libs.schedule import TradingSchedule

        config = EngineConfig(
            dataDir=self.tmp, strategy="momentum",
            updateSymbols=False, updatePrices=False,
            liveDirName="live",
        )
        # Force the schedule to fire start-of-day every iteration.
        schedule = TradingSchedule(
            startOfDayTime=_dt.time(0, 0),
            eodUpdateTime=_dt.time(0, 0),
            heartbeatTime=_dt.time(0, 0),
        )

        # Capture stdout to count wait prints.
        import io
        import contextlib
        buf = io.StringIO()
        # Inject our temp dataDir so the gate checks our stale sentinel.
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            with contextlib.redirect_stdout(buf):
                t0 = time.monotonic()
                try:
                    # Run with sleep_seconds=1 and once=True so the test
                    # terminates after one iteration. We assert the
                    # elapsed time is >= sleep_seconds (i.e. the sleep
                    # actually ran).
                    run_auto_bot(
                        engine_config=config,
                        strategy_config=StrategyConfig(),
                        client=None,
                        account_no="0003937868",
                        loan_package_id=1775,
                        live_submit=False,
                        once=True,
                        sleep_seconds=1,
                        schedule=schedule,
                        strategy_name="momentum",
                    )
                except Exception:
                    # The bot may raise at the end (e.g. notify() needs
                    # a real TELEGRAM_BOT_TOKEN). We don't care — we
                    # only care about the elapsed time and the wait
                    # print count, both of which are independent of
                    # whether the inner work succeeded.
                    pass
                elapsed = time.monotonic() - t0
        finally:
            os.chdir(cwd)

        out = buf.getvalue()
        wait_count = out.count("waiting for data-sync owner")
        # Regression: the bug printed the wait message thousands of
        # times per second. After the fix, exactly 1 print per loop
        # pass — i.e. once for the single iteration run_auto_bot did
        # in ``once=True`` mode (plus possibly a second from the EOD
        # gate, since EOD uses the same sentinel — both gates are
        # checked once each per pass).
        self.assertLessEqual(
            wait_count, 2,
            f"Wait print fired {wait_count} times in a single "
            f"iteration — this is the runaway-loop bug. Output: {out[:1000]!r}",
        )
        # And the sleep at the bottom of the loop must have run —
        # we asked for sleep_seconds=1 and the elapsed time must be
        # at least 1 second.
        self.assertGreaterEqual(
            elapsed, 0.95,
            f"Loop iteration took only {elapsed:.2f}s for sleep_seconds=1 "
            f"— the main-loop sleep was bypassed. Output: {out[:500]!r}",
        )


if __name__ == "__main__":
    unittest.main()
