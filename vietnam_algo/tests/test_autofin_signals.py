"""Tests for autofin_signals parsing + signal-to-order logic."""
from __future__ import annotations

import json
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.autofin_signals import (
    AutofinSignal,
    AutofinClient,
    _jwt_exp_seconds,
    _token_still_valid,
    parse_signal,
)


class JwtExpiryTests(unittest.TestCase):
    """Verify proactive refresh detection.

    The bot hit a silent stale-token state on 2026-09-18 because
    ``_token_still_valid`` only checked file mtime, not the JWT
    ``exp`` claim embedded in the cached payload. A token saved
    on day 0 with ``exp`` at day +10 passed the mtime check
    (under 6 days) but the JWT was already past its declared
    expiry, leading to 401s from the autofin API. These tests
    pin the fix: exp-claim checking must short-circuit the mtime
    fallback.
    """

    def test_exp_in_past_marks_token_invalid(self) -> None:
        # exp was 1 hour ago
        payload = {
            "accessToken": "x",
            "exp": int(time.time()) - 3600,
            "_savedAt": time.time(),  # fresh mtime
        }
        self.assertFalse(_token_still_valid(payload))

    def test_exp_within_leeway_marks_token_invalid(self) -> None:
        # exp is 30 minutes from now — within the 1-hour default leeway
        payload = {
            "accessToken": "x",
            "exp": int(time.time()) + 1800,
            "_savedAt": time.time(),
        }
        self.assertFalse(_token_still_valid(payload))

    def test_exp_well_in_future_marks_token_valid(self) -> None:
        # exp is 7 days from now
        payload = {
            "accessToken": "x",
            "exp": int(time.time()) + 7 * 86400,
            "_savedAt": time.time() - 86400,  # 1 day old
        }
        self.assertTrue(_token_still_valid(payload))

    def test_no_exp_falls_back_to_mtime(self) -> None:
        # No exp claim — old mtime check still applies.
        fresh_payload = {"accessToken": "x", "_savedAt": time.time()}
        stale_payload = {
            "accessToken": "x",
            "_savedAt": time.time() - 7 * 86400,  # 7 days old
        }
        self.assertTrue(_token_still_valid(fresh_payload))
        self.assertFalse(_token_still_valid(stale_payload, max_age_seconds=6 * 86400))

    def test_missing_access_token_always_invalid(self) -> None:
        # No token → can't use it regardless of exp.
        self.assertFalse(_token_still_valid({"exp": int(time.time()) + 86400}))

    def test_exp_nested_under_data(self) -> None:
        # Some autofin payloads nest exp under a "data" key.
        exp = int(time.time()) + 86400
        payload = {
            "accessToken": "x",
            "data": {"exp": exp},
            "_savedAt": time.time(),
        }
        self.assertTrue(_token_still_valid(payload))
        self.assertEqual(_jwt_exp_seconds(payload), float(exp))


class AutofinTokenRefreshTests(unittest.TestCase):
    """End-to-end token refresh flow.

    Covers both proactive refresh (exp claim / mtime) and reactive
    401 retry. The latter is the safety net for tokens that the
    server revokes out-of-band.
    """

    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/autofin_token_test")
        import shutil
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.tmp / "token.json"

    def _login_payload(self, exp_offset_seconds: int = 86400 * 7) -> dict:
        return {
            "accessToken": "fresh-token-XYZ",
            "refreshToken": "refresh-XYZ",
            "userId": 581,
            "exp": int(time.time()) + exp_offset_seconds,
        }

    def test_get_token_uses_cached_token_when_valid(self) -> None:
        """First call after a fresh login reads from disk and returns
        the cached token without re-logging in."""
        payload = self._login_payload()
        with patch.object(AutofinClient, "_post_login") as login:
            client = AutofinClient(
                email="u@example.com",
                password="p",
                token_cache_path=self.cache_path,
            )
            client._save_tokens(payload)
            # Reset in-memory state so we exercise the cache path.
            client._token = None
            client._token_exp = None
            tok = client.get_token()
        self.assertEqual(tok, "fresh-token-XYZ")
        self.assertEqual(client._token_exp, payload["exp"])
        login.assert_not_called()

    def test_get_token_proactively_refreshes_when_expired(self) -> None:
        """If the cached token's JWT exp has passed, get_token() must
        login fresh instead of using the dead token."""
        stale_payload = self._login_payload(exp_offset_seconds=-3600)  # 1h ago
        with patch.object(AutofinClient, "_post_login") as login:
            client = AutofinClient(
                email="u@example.com",
                password="p",
                token_cache_path=self.cache_path,
            )
            client._save_tokens(stale_payload)
            client._token = None
            client._token_exp = None
            login.return_value = self._login_payload(exp_offset_seconds=86400)
            tok = client.get_token()
        login.assert_called_once()
        self.assertEqual(tok, "fresh-token-XYZ")

    def test_request_helper_retries_once_on_401(self) -> None:
        """The 401 retry pattern: server rejects stale token, helper
        refreshes and retries; second 401 propagates."""
        good_payload = self._login_payload()
        with patch.object(AutofinClient, "_post_login") as login:
            client = AutofinClient(
                email="u@example.com",
                password="p",
                token_cache_path=self.cache_path,
            )
            client._save_tokens(good_payload)
            login.return_value = good_payload

            # First call returns 401, second succeeds.
            responses = [
                urllib.error.HTTPError(
                    "http://x", 401, "Unauthorized", {}, None,
                ),
                {"data": [{"_id": 1, "symbol": "HPG"}]},
            ]

            def fake_urlopen(req, timeout=None):
                resp = responses.pop(0)
                if isinstance(resp, Exception):
                    raise resp
                # Build a context-manager-like mock response.
                m = MagicMock()
                m.read.return_value = json.dumps(resp).encode()
                m.__enter__ = lambda s: m
                m.__exit__ = lambda s, *a: None
                return m

            with patch(
                "vietnam_algo.libs.autofin_signals.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                out = client._request_with_token_refresh(
                    "http://x/api",
                    headers={"accept": "*/*"},
                )
        self.assertEqual(out, {"data": [{"_id": 1, "symbol": "HPG"}]})

    def test_request_helper_propagates_after_second_401(self) -> None:
        """Two consecutive 401s surface the underlying error."""
        with patch.object(AutofinClient, "_post_login") as login:
            client = AutofinClient(
                email="u@example.com",
                password="p",
                token_cache_path=self.cache_path,
            )
            client._save_tokens(self._login_payload())
            login.return_value = self._login_payload()

            def fake_urlopen(req, timeout=None):
                raise urllib.error.HTTPError(
                    "http://x", 401, "Unauthorized", {}, None,
                )

            with patch(
                "vietnam_algo.libs.autofin_signals.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                with self.assertRaises(urllib.error.HTTPError):
                    client._request_with_token_refresh(
                        "http://x/api",
                        headers={"accept": "*/*"},
                    )
        # The retry triggered exactly one refresh (the second 401
        # is not eligible for a third attempt).
        self.assertEqual(login.call_count, 1)



def test_parse_signal_basic():
    raw = {
        "_id": 123,
        "symbol": "APH",
        "companyName": "An Phat Holdings",
        "statusDeal": {"market_TypePrice": 1},
        "supply_F": 5910,
        "supply_T": 6100,
        "tP_F": 6500,
        "tP_T": 7000,
        "sL_F": 5320,
        "sL_T": 5610,
        "rateForNav": 0.15,
        "lastRecomment": "Khuyến nghị đầu tư",
        "isWaittingBuy": True,
        "isWaittingSell": False,
        "numberOfProfit": 0,
        "rateIncome": 0,
        "rateIncome_TypePrice": 3,
    }
    s = parse_signal(raw)
    assert s.symbol == "APH"
    assert s.company_name == "An Phat Holdings"
    # Autofin API now returns RAW VND (e.g. supply_F=5910 means 5,910 VND).
    # We do not multiply by 1000 — the price is ready to send to DNSE.
    assert s.supply_from == 5910
    assert s.supply_to == 6100
    assert s.tp_from == 6500
    assert s.tp_to == 7000
    assert s.sl_from == 5320
    assert s.sl_to == 5610
    assert s.rate_for_nav == 0.15
    assert s.is_waitting_buy is True
    assert s.is_waitting_sell is False
    assert s.market_type == 1
    assert s.last_recomment == "Khuyến nghị đầu tư"


def test_parse_signal_handles_missing_fields():
    raw = {"symbol": "VCB", "statusDeal": {}}
    s = parse_signal(raw)
    assert s.symbol == "VCB"
    assert s.supply_from == 0.0
    assert s.is_waitting_buy is False


def test_parse_signal_handles_alternate_keys():
    raw = {
        "symbol": "HPG",
        "statusDeal": {"market_TypePrice": 2},
    }
    s = parse_signal(raw)
    assert s.market_type == 2
