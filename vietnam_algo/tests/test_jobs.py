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
from vietnam_algo.token_store import SHARED_TOKEN_PATH, load_shared_trading_token


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
        from vietnam_algo.token_store import is_token_fresh
        issued = (datetime.now(tz=timezone.utc) - timedelta(minutes=5)).isoformat()
        self.assertTrue(
            is_token_fresh("20375f6c-5007-46b1-8b2a-07361590822e", issued_at=issued)
        )

    def test_uuid_token_with_old_issued_at_is_stale(self) -> None:
        from datetime import datetime, timedelta, timezone
        from vietnam_algo.token_store import is_token_fresh
        issued = (datetime.now(tz=timezone.utc) - timedelta(hours=24)).isoformat()
        self.assertFalse(
            is_token_fresh("20375f6c-5007-46b1-8b2a-07361590822e", issued_at=issued)
        )

    def test_uuid_token_without_issued_at_is_stale(self) -> None:
        from vietnam_algo.token_store import is_token_fresh
        self.assertFalse(is_token_fresh("20375f6c-5007-46b1-8b2a-07361590822e"))

    def test_dry_run_placeholder_token_is_never_fresh(self) -> None:
        from vietnam_algo.token_store import is_token_fresh
        self.assertFalse(is_token_fresh("dry-run-token"))
