"""Tests for the standalone jobs in ``vietnam_algo.jobs``.

Covers:
  - jobs.get_otp: SHARED_TOKEN_PATH, already_refreshed_today idempotency
  - jobs.common_sync: already_done_today idempotency, sentinel stamping
  - token_store.load_shared_trading_token
"""
from __future__ import annotations

import json
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
