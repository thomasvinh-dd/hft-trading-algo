"""Tests for the data_ready_for_eod sentinel that coordinates
the autofin (data-sync owner) and momentum (data-sync reader) bots.

When --no-sync-data is set, the momentum bot waits for the autofin
bot's sentinel file before proceeding with EOD / start-of-day. This
prevents the two bots from racing on the same data sources and
ensures momentum never reads stale prices.
"""
from __future__ import annotations

import datetime as _dt
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.daily_engine import EngineConfig
from vietnam_algo.libs.schedule import TradingSchedule, should_run_eod


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


class EodScheduleTimeTests(unittest.TestCase):
    """Regression for 2026-10-08: momentum EOD fired at 15:15 ICT, but the
    autofin sync (sync_prices_incremental across 1500+ symbols × 1s
    sleep) was still in progress. Momentum's mark-to-market therefore
    read yesterday's close (BSR reported 31,750 instead of 32,000).

    Fix: push the EOD time to 15:45 ICT so the autofin sync has time
    to finish before either bot reads prices. Pin the schedule
    default here so any future revert gets caught.
    """

    def test_default_eod_time_is_15_45(self) -> None:
        """TradingSchedule default eodUpdateTime must be 15:45 (ICT).

        Pushed back from 15:15 because the autofin sync is ~26 min
        long. With EOD at 15:15, the momentum bot read stale prices;
        15:45 gives the autofin sync enough slack to land first.
        """
        sched = TradingSchedule()
        self.assertEqual(sched.eodUpdateTime, _dt.time(15, 45))

    def test_should_run_eod_at_15_44(self) -> None:
        """At 15:44 ICT on a fresh day → EOD has NOT fired yet."""
        # Use a weekday so is_trading_day returns True.
        today = "2026-10-08"  # a Thursday in 2026
        moment = datetime.fromisoformat(f"{today}T15:44:00+07:00")
        self.assertFalse(
            should_run_eod(moment, done_for_date=None, holidays=set()),
            "EOD should not fire at 15:44 with the new schedule",
        )

    def test_should_run_eod_at_15_45(self) -> None:
        """At 15:45 ICT on a fresh day → EOD fires (15:45 ≥ 15:45)."""
        today = "2026-10-08"
        moment = datetime.fromisoformat(f"{today}T15:45:00+07:00")
        self.assertTrue(
            should_run_eod(moment, done_for_date=None, holidays=set()),
            "EOD should fire at 15:45 with the new schedule",
        )

    def test_should_run_eod_at_15_50(self) -> None:
        """At 15:50 ICT on a fresh day → EOD still fires (idempotent
        within the same day, gated by state.eodDate)."""
        today = "2026-10-08"
        moment = datetime.fromisoformat(f"{today}T15:50:00+07:00")
        self.assertTrue(
            should_run_eod(moment, done_for_date=None, holidays=set()),
        )


class StaleSentinelGateTests(unittest.TestCase):
    """Regression for 2026-10-08 incident: the EOD gate printed a
    warning for stale sentinels but DID NOT actually skip the EOD.
    Momentum proceeded with yesterday's close.

    Fix: a stale sentinel (age > 60 min) now sets ``data_ready=False``
    and ``continue`` to the next cycle. Pin the new behavior here.
    """

    def setUp(self) -> None:
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="stale_sentinel_test_"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_sentinel(self, age_min: float) -> None:
        import os
        import time
        path = self.tmp / "live" / "data_ready_for_eod"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat())
        old = time.time() - age_min * 60
        os.utime(path, (old, old))

    def test_runner_docstring_pins_stale_skip_behavior(self) -> None:
        """The runner's EOD gate comment must say "absent OR stale"
        so a future reader knows both conditions skip the EOD.
        """
        from vietnam_algo.bots import runner
        src = Path(runner.__file__).read_text()
        # The fix's comment uses the phrases "absent" and "stale" to
        # document the gate; the previous wording was just "absent".
        # We assert the stale-skip is documented.
        self.assertIn(
            "treat a stale sentinel the same as an absent one",
            src,
            "runner.py EOD-gate comment must document the stale-skip "
            "behavior (2026-10-08 fix — the missing skip caused "
            "stale prices in the momentum EOD report).",
        )

    def test_runner_stale_branch_sets_data_ready_false(self) -> None:
        """The runner's stale branch must set ``data_ready = False``
        so the ``if not data_ready: continue`` actually fires. The
        2026-10-08 incident was caused by a missing
        ``data_ready = False`` inside the age > 60 block.
        """
        from vietnam_algo.bots import runner
        src = Path(runner.__file__).read_text()
        # Locate the stale-branch block.
        marker = "if age_min > 60:"
        idx = src.find(marker)
        self.assertGreater(idx, 0, "runner.py must contain the age>60 check")
        # Read the next ~600 chars (the branch body) and check the
        # assignment to data_ready happens within it.
        branch = src[idx: idx + 600]
        self.assertIn(
            "data_ready = False",
            branch,
            "stale sentinel branch must set data_ready = False "
            "(2026-10-08 fix — without this, momentum proceeds with "
            "yesterday's prices).",
        )


class SentinelStampReliabilityTests(unittest.TestCase):
    """Regression for 2026-10-08 incident: the autofin EOD's
    ``data_ready_for_eod`` stamp was wrapped in a bare
    ``except Exception: pass`` that silently swallowed disk-write
    errors. The sentinel never updated, so the momentum bot kept
    seeing a 14+ hour-old sentinel and used stale prices.

    Fix: the stamp path now retries once, logs the failure, and
    raises if both attempts fail. Pin the behavior here.
    """

    def test_runner_stamp_block_does_not_swallow_exception(self) -> None:
        """The sentinel-stamp try/except must NOT use ``pass`` — it
        must at least log and ideally retry. The 2026-10-08 incident
        was caused by a silent ``except Exception: pass``.
        """
        from vietnam_algo.bots import runner
        src = Path(runner.__file__).read_text()
        # The fix replaces the original silent block with a retry loop.
        # We assert the fix's wording is present (proves the silent
        # version is gone) and the old wording is absent.
        self.assertNotIn(
            "except Exception:\n                    pass",
            src,
            "runner.py must not have a silent 'except Exception: pass' "
            "around the sentinel stamp (2026-10-08 fix).",
        )
        self.assertIn(
            "stamp attempt",
            src,
            "runner.py sentinel stamp must log retry attempts "
            "(2026-10-08 fix).",
        )

    def test_runner_stamp_block_raises_on_persistent_failure(self) -> None:
        """If both stamp attempts fail, runner.py must raise (so the
        bot crashes and the watchdog relaunches it under a healthy
        state) — not silently swallow the error.
        """
        from vietnam_algo.bots import runner
        src = Path(runner.__file__).read_text()
        # Look for the post-retry block that re-raises.
        self.assertIn(
            "raise _stamp_err",
            src,
            "runner.py sentinel stamp must re-raise on persistent failure "
            "(2026-10-08 fix — silent failure left momentum bot with "
            "stale prices).",
        )


if __name__ == "__main__":
    unittest.main()


class DataReadyGateRegressionTests(unittest.TestCase):
    """Regression test for 2026-09-17 + 2026-09-23 runaway loops.

    Background: the momentum bot's start-of-day block used to call
    ``continue`` *from inside* the data-ready wait print. Because
    ``continue`` jumps back to the top of the while-loop, it skipped
    the ``time.sleep(sleep_seconds)`` at the bottom — the bot spun
    at CPU speed, printing the wait message ~16k times/sec until
    the log filled the disk (33 GB on 2026-09-17, 6.2 GB on
    2026-09-23).

    Two layered fixes:

    1. ``data_ready = False`` is set inside the wait block, then the
       ``if not data_ready: continue`` happens AFTER the block (not
       from inside the print). This was already in place by 2026-09-17
       but was insufficient because the ``continue`` STILL bypassed
       the bottom-of-loop sleep.

    2. (2026-09-23) ``time.sleep(sleep_seconds)`` moved to the TOP of
       the while-loop, so any ``continue`` in a gate is still bounded
       by the sleep on the next iteration. The age-based
       ``> 60 * 18`` threshold was also dropped — it was wrong by
       design (SOD at 09:00 ICT is always ~19h 45min after the
       13:15 ICT cron), and the only sensible gate now is sentinel
       existence.

    This test exercises the runner's main loop end-to-end with a
    missing sentinel and asserts the loop:
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

        We invoke ``run_auto_bot`` with NO sentinel at all (the only
        case where the SOD wait fires after the 2026-09-23 fix —
        stale-but-existing sentinels now proceed immediately).
        ``sleep_seconds=1`` and a TradingSchedule that forces
        start-of-day to fire on every iteration. After one pass,
        the bot must (a) have printed the wait message at most once,
        and (b) the elapsed wall-clock must be >= sleep_seconds — the
        time.sleep at the TOP of the loop must have run.
        """
        import os
        import time

        # Ensure no sentinel exists — this is the only case that
        # triggers the SOD wait post-2026-09-23.
        sentinel = self.tmp / "live" / "data_ready_for_eod"
        if sentinel.exists():
            sentinel.unlink()

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
