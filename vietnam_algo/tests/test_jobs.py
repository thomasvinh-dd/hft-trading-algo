"""Tests for the standalone jobs in ``vietnam_algo.jobs``.

Covers:
  - jobs.get_otp: SHARED_TOKEN_PATH, already_refreshed_today idempotency
  - jobs.common_sync: already_done_today idempotency, sentinel stamping
  - token_store.load_shared_trading_token
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.jobs import common_sync, get_otp
from vietnam_algo.libs.dnse.auth import SHARED_TOKEN_PATH, load_shared_trading_token


class _TmpDataDir:
    """Yield a fresh data dir for each test so we don't pollute the
    real ``vietnam_algo/data`` tree (which is committed)."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vietnam_algo_jobs_"))
        (self.tmp / "live").mkdir(parents=True, exist_ok=True)
        return self.tmp

    def __exit__(self, *exc):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class GetOtpSharedStoreTests(unittest.TestCase):
    def test_already_refreshed_today_when_match(self) -> None:
        with _TmpDataDir() as tmp:
            today = datetime.now(tz=timezone.utc).date().isoformat()
            (tmp / "live" / "dnse_trading_token.json").write_text(
                json.dumps({"token": "abc", "date": today, "account_no": "x"})
            )
            with unittest.mock.patch.object(get_otp, "SHARED_TOKEN_PATH", tmp / "live" / "dnse_trading_token.json"):
                self.assertTrue(get_otp.already_refreshed_today())

    def test_already_refreshed_today_no_file(self) -> None:
        with _TmpDataDir() as tmp:
            with unittest.mock.patch.object(get_otp, "SHARED_TOKEN_PATH", tmp / "live" / "dnse_trading_token.json"):
                self.assertFalse(get_otp.already_refreshed_today())

    def test_shared_token_path_default(self) -> None:
        self.assertEqual(SHARED_TOKEN_PATH.name, "dnse_trading_token.json")
        self.assertEqual(SHARED_TOKEN_PATH.parent.name, "live")


class CommonSyncStateTests(unittest.TestCase):
    def test_already_done_today_when_match(self) -> None:
        with _TmpDataDir() as tmp:
            today = "2026-09-12"
            (tmp / "live" / "common_sync_state.json").write_text(
                json.dumps({"date": today, "ran_at": "2026-09-12T01:30:00Z"})
            )
            with unittest.mock.patch.object(common_sync, "TRADE_STATE", tmp / "live" / "common_sync_state.json"):
                self.assertTrue(common_sync.already_done_today(today))

    def test_already_done_today_mismatch(self) -> None:
        with _TmpDataDir() as tmp:
            (tmp / "live" / "common_sync_state.json").write_text(
                json.dumps({"date": "2026-09-11"})
            )
            with unittest.mock.patch.object(common_sync, "TRADE_STATE", tmp / "live" / "common_sync_state.json"):
                self.assertFalse(common_sync.already_done_today("2026-09-12"))


class CommonSyncAlertingTests(unittest.TestCase):
    """common_sync must alert on (a) >5% price fetch errors and
    (b) full job failure. Below-threshold errors stay silent."""

    def _setup_data_dir(self, tmp: Path) -> None:
        (tmp / "prices").mkdir(parents=True, exist_ok=True)
        for sym in ("AAA", "BBB", "CCC"):
            (tmp / "prices" / f"{sym}.json").write_text(
                json.dumps({"source": "test", "symbol": sym, "count": 0, "items": []})
            )

    def test_main_alerts_on_full_failure(self) -> None:
        with _TmpDataDir() as tmp:
            self._setup_data_dir(tmp)
            with unittest.mock.patch.object(common_sync, "DATA_DIR", tmp), \
                 unittest.mock.patch.object(common_sync, "TRADE_STATE", tmp / "live" / "common_sync_state.json"), \
                 unittest.mock.patch.object(common_sync, "SENTINEL_PATH", tmp / "live" / "data_ready_for_eod"), \
                 unittest.mock.patch.object(common_sync, "run", side_effect=RuntimeError("boom")), \
                 unittest.mock.patch("vietnam_algo.jobs.common_sync.notify") as notify_mock:
                rc = common_sync.main([])
            self.assertEqual(rc, 1)
            self.assertTrue(notify_mock.called)
            sent_text = notify_mock.call_args[0][0]
            self.assertIn("common_sync FAILED", sent_text)
            self.assertIn("boom", sent_text)

    def _run_with_mocks(self, tmp: Path, symbols: list[str], errors: list[tuple[str, str]]) -> unittest.mock.Mock:
        self._setup_data_dir(tmp)
        fake_helpers = unittest.mock.MagicMock(run_ca_incremental=lambda **kw: [])
        with unittest.mock.patch.object(common_sync, "DATA_DIR", tmp), \
             unittest.mock.patch.object(common_sync, "TRADE_STATE", tmp / "live" / "common_sync_state.json"), \
             unittest.mock.patch.object(common_sync, "SENTINEL_PATH", tmp / "live" / "data_ready_for_eod"), \
             unittest.mock.patch.object(common_sync, "sync_symbols"), \
             unittest.mock.patch.object(common_sync, "load_saved_symbols", return_value=symbols), \
             unittest.mock.patch.object(common_sync, "is_trading_day", return_value=True), \
             unittest.mock.patch.object(common_sync, "sync_prices_incremental", return_value=([], errors)), \
             unittest.mock.patch("vietnam_algo.jobs.common_sync.notify") as notify_mock, \
             unittest.mock.patch.dict(sys.modules, {"vietnam_algo.jobs._helpers": fake_helpers}):
            common_sync.run()
        return notify_mock

    def test_price_fetch_errors_above_5pct_alert(self) -> None:
        with _TmpDataDir() as tmp:
            symbols = [f"SYM{i:03d}" for i in range(200)]
            errors = [(s, "ConnectionError: timeout") for s in symbols[:11]]
            notify_mock = self._run_with_mocks(tmp, symbols, errors)
        self.assertTrue(notify_mock.called)
        sent_text = notify_mock.call_args[0][0]
        self.assertIn("market-data price fetch errors", sent_text)
        self.assertIn("11/200", sent_text)

    def test_price_fetch_errors_below_5pct_no_alert(self) -> None:
        with _TmpDataDir() as tmp:
            symbols = [f"SYM{i:03d}" for i in range(200)]
            errors = [(s, "ConnectionError: timeout") for s in symbols[:5]]  # 2.5%
            notify_mock = self._run_with_mocks(tmp, symbols, errors)
        self.assertFalse(notify_mock.called)


class CommonSyncSentinelStampTests(unittest.TestCase):
    """Sentinel stamp must be gated on the success rate of the
    DNSE-accepted universe (total symbols minus the denylist).

    Background (2026-09-23): the previous design stamped the sentinel
    unconditionally at the end of the price sync — but the price sync
    could finish with 0 successful fetches and still stamp the
    sentinel, fooling downstream bots into thinking data was fresh.
    Fix: only stamp when success rate (excluding denylisted symbols)
    is >= the threshold (default 50%).
    """

    def _run_sentinel_test(
        self,
        tmp: Path,
        symbols: list[str],
        errors: list[tuple[str, str]],
        denylist: set[str] | None = None,
    ) -> tuple[bool, unittest.mock.Mock]:
        (tmp / "prices").mkdir(parents=True, exist_ok=True)
        for sym in symbols[:5]:
            (tmp / "prices" / f"{sym}.json").write_text(
                json.dumps({"source": "test", "symbol": sym, "count": 0, "items": []})
            )
        if denylist:
            from vietnam_algo.libs.data import save_invalid_symbol_denylist
            save_invalid_symbol_denylist(tmp, set(denylist))
        fake_helpers = unittest.mock.MagicMock(run_ca_incremental=lambda **kw: [])
        notify_mock = unittest.mock.MagicMock()
        with unittest.mock.patch.object(common_sync, "DATA_DIR", tmp), \
             unittest.mock.patch.object(common_sync, "TRADE_STATE", tmp / "live" / "common_sync_state.json"), \
             unittest.mock.patch.object(common_sync, "SENTINEL_PATH", tmp / "live" / "data_ready_for_eod"), \
             unittest.mock.patch.object(common_sync, "sync_symbols"), \
             unittest.mock.patch.object(common_sync, "load_saved_symbols", return_value=symbols), \
             unittest.mock.patch.object(common_sync, "is_trading_day", return_value=True), \
             unittest.mock.patch.object(common_sync, "sync_prices_incremental", return_value=([], errors)), \
             unittest.mock.patch("vietnam_algo.jobs.common_sync.notify", notify_mock), \
             unittest.mock.patch.dict(sys.modules, {"vietnam_algo.jobs._helpers": fake_helpers}):
            common_sync.run()
        stamped = (tmp / "live" / "data_ready_for_eod").exists()
        return stamped, notify_mock

    def test_high_success_rate_stamps_sentinel(self) -> None:
        with _TmpDataDir() as tmp:
            symbols = [f"SYM{i:03d}" for i in range(100)]
            errors = [(s, "ConnectionError: timeout") for s in symbols[:10]]  # 90% success
            stamped, notify_mock = self._run_sentinel_test(tmp, symbols, errors)
        self.assertTrue(stamped, "sentinel should be stamped at 90% success rate")
        # Success path → no "sentinel NOT stamped" alert
        warn_texts = [c.args[0] for c in notify_mock.call_args_list
                      if c.args and "sentinel NOT stamped" in c.args[0]]
        self.assertEqual(warn_texts, [])

    def test_low_success_rate_skips_sentinel(self) -> None:
        with _TmpDataDir() as tmp:
            symbols = [f"SYM{i:03d}" for i in range(100)]
            errors = [(s, "ConnectionError: timeout") for s in symbols[:80]]  # 20% success
            stamped, notify_mock = self._run_sentinel_test(tmp, symbols, errors)
        self.assertFalse(stamped, "sentinel must NOT be stamped below 50% success")
        # Operator gets one warning per run
        warn_texts = [c.args[0] for c in notify_mock.call_args_list
                      if c.args and "sentinel NOT stamped" in c.args[0]]
        self.assertEqual(len(warn_texts), 1)

    def test_denylisted_symbols_excluded_from_denominator(self) -> None:
        """If 80 of 100 symbols are on the denylist, we only sync 20;
        if 5 of those 20 fail (75% success on the DNSE-accepted
        universe), the sentinel MUST be stamped — even though 85/100
        of the raw symbols 'failed'."""
        with _TmpDataDir() as tmp:
            symbols = [f"SYM{i:03d}" for i in range(100)]
            # Pretend 80 symbols are denylisted (DNSE never carries them)
            denylist = {f"SYM{i:03d}" for i in range(80)}
            # Of the remaining 20, 5 fail with a real (non-400) error
            errors = [(f"SYM{i:03d}", "ConnectionError: timeout") for i in range(85, 90)]
            stamped, _ = self._run_sentinel_test(tmp, symbols, errors, denylist=denylist)
        self.assertTrue(
            stamped,
            "sentinel should be stamped when 75% of the DNSE-accepted "
            "universe succeeds, even though 85% of the raw universe "
            "appears to have failed",
        )

    def test_denylisted_symbols_with_400s_dont_count_as_failures(self) -> None:
        """400 'invalid symbol' errors are a denylist concern, not a
        sync failure. The success-rate denominator excludes them via
        the denylist filter; the numerator counts everything else as
        success."""
        with _TmpDataDir() as tmp:
            symbols = [f"SYM{i:03d}" for i in range(100)]
            # 50 symbols return 400 'invalid symbol' — these should be
            # added to the denylist and NOT count against success rate.
            invalid_err = (
                'RuntimeError: DNSE GET /price/ohlc -> HTTP 400 '
                '{"status":400,"code":"BAD_REQUEST","message":"invalid symbol"}'
            )
            errors = [(f"SYM{i:03d}", invalid_err) for i in range(50)]
            stamped, _ = self._run_sentinel_test(tmp, symbols, errors)
        self.assertTrue(
            stamped,
            "all 400 invalid symbol errors should be treated as "
            "denylist additions, leaving 100% effective success rate",
        )

    def test_existing_threshold_env_override(self) -> None:
        """``COMMON_SYNC_SENTINEL_MIN_SUCCESS_PCT`` overrides the default 50%."""
        with _TmpDataDir() as tmp:
            symbols = [f"SYM{i:03d}" for i in range(100)]
            errors = [(s, "ConnectionError: timeout") for s in symbols[:60]]  # 40% success
            # Default threshold: 50% → would NOT stamp
            stamped_default, _ = self._run_sentinel_test(tmp, symbols, errors)
            self.assertFalse(stamped_default)
            # Loosened threshold: 25% → WOULD stamp
            with unittest.mock.patch.dict(os.environ,
                                          {"COMMON_SYNC_SENTINEL_MIN_SUCCESS_PCT": "25.0"}):
                stamped_loose, _ = self._run_sentinel_test(tmp, symbols, errors)
            self.assertTrue(stamped_loose)


class LoadSharedTokenTests(unittest.TestCase):
    def test_load_returns_none_when_file_missing(self) -> None:
        with _TmpDataDir() as tmp:
            self.assertIsNone(load_shared_trading_token(tmp))

    def test_load_returns_payload_when_valid(self) -> None:
        with _TmpDataDir() as tmp:
            (tmp / "live" / "dnse_trading_token.json").write_text(
                json.dumps({"token": "t", "date": "2026-09-12", "account_no": "x"})
            )
            self.assertEqual(load_shared_trading_token(tmp)["token"], "t")

    def test_load_returns_none_when_malformed(self) -> None:
        with _TmpDataDir() as tmp:
            (tmp / "live" / "dnse_trading_token.json").write_text("not json")
            self.assertIsNone(load_shared_trading_token(tmp))


if __name__ == "__main__":
    unittest.main()


class TokenFreshnessEdgeCaseTests(unittest.TestCase):
    """is_token_fresh must accept UUID-style DNSE tokens that lack
    a parseable JWT ``exp`` claim — see the 2026-08 token rotation.

    The shared-file helper ``load_shared_trading_token`` always pairs
    the token with an ``issued_at`` ISO 8601 string; we honour that
    as the freshness anchor.
    """

    def test_uuid_token_with_recent_issued_at_is_fresh(self) -> None:
        from datetime import datetime, timedelta, timezone
        from vietnam_algo.libs.dnse.auth import is_token_fresh
        issued = (datetime.now(tz=timezone.utc) - timedelta(minutes=5)).isoformat()
        self.assertTrue(
            is_token_fresh("20375f6c-5007-46b1-8b2a-07361590822e", issued_at=issued)
        )

    def test_uuid_token_with_old_issued_at_is_stale(self) -> None:
        from datetime import datetime, timedelta, timezone
        from vietnam_algo.libs.dnse.auth import is_token_fresh
        issued = (datetime.now(tz=timezone.utc) - timedelta(hours=24)).isoformat()
        self.assertFalse(
            is_token_fresh("20375f6c-5007-46b1-8b2a-07361590822e", issued_at=issued)
        )

    def test_uuid_token_without_issued_at_is_stale(self) -> None:
        from vietnam_algo.libs.dnse.auth import is_token_fresh
        self.assertFalse(is_token_fresh("20375f6c-5007-46b1-8b2a-07361590822e"))

    def test_dry_run_placeholder_token_is_never_fresh(self) -> None:
        from vietnam_algo.libs.dnse.auth import is_token_fresh
        self.assertFalse(is_token_fresh("dry-run-token"))
