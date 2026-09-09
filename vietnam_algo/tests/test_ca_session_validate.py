"""Tests for the Vietstock session validation probe.

Per user feedback (2026-09-09): when the bot ran retry loops on a
dead Vietstock session, the upstream returned empty/HTML on every
attempt, so the retries never recovered. We added a small probe
(``validate_vietstock_session``) that issues a tiny POST and checks
the response is JSON before trusting the session for the real fetch.
"""
from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest

from vietnam_algo.corporate_actions import (
    _looks_like_json_response,
    validate_vietstock_session,
    fetch_vietstock_session,
)


class ValidateSessionTests(unittest.TestCase):
    # --- _looks_like_json_response ------------------------------------

    def test_looks_like_json_accepts_json_content_type(self):
        self.assertTrue(_looks_like_json_response("[1,2,3]", "application/json"))

    def test_looks_like_json_accepts_object_body_with_json_ct(self):
        self.assertTrue(_looks_like_json_response('{"a":1}', "application/json;charset=utf-8"))

    def test_looks_like_json_accepts_text_plain_with_array_start(self):
        self.assertTrue(_looks_like_json_response("[1,2]", "text/plain"))

    def test_looks_like_json_accepts_text_plain_with_object_start(self):
        self.assertTrue(_looks_like_json_response('{"k":"v"}', "text/plain"))

    def test_looks_like_json_rejects_empty_body(self):
        self.assertFalse(_looks_like_json_response("", "application/json"))
        self.assertFalse(_looks_like_json_response("   ", "application/json"))

    def test_looks_like_json_rejects_html_body(self):
        """The actual failure mode: Vietstock returns full HTML page when
        session is invalid."""
        body = "<!DOCTYPE html><html><head></head><body>...</body></html>"
        self.assertFalse(_looks_like_json_response(body, "text/html"))
        self.assertFalse(_looks_like_json_response(body, "text/html;charset=utf-8"))

    def test_looks_like_json_rejects_html_even_with_benign_first_char(self):
        body = '<!DOCTYPE html><html>'  # starts with '<' not '[' or '{'
        self.assertFalse(_looks_like_json_response(body, "text/html"))

    # --- validate_vietstock_session ------------------------------------

    def _fake_session(self, post_return_value):
        """Build a minimal stand-in for VietstockSession.

        ``post_return_value`` is the value that ``opener.open(...)``
        should return — typically a context manager whose ``.read()`` and
        ``.headers.get(...)`` give us the body and content-type.
        """
        session = MagicMock()
        session.token = "TEST_TOKEN_AAAA"
        # opener.open returns this context manager
        session.opener.open.return_value.__enter__.return_value = post_return_value
        session.opener.open.return_value.__exit__.return_value = False
        return session

    def test_validate_session_returns_true_for_valid_json(self):
        """Valid session returns JSON with at least one event row."""
        # Vietstock wraps rows in [[row, ...], total]. Valid probe
        # needs at least one row in the inner list.
        body = (
            '[[{"EventID":1,"Code":"AAA","EventTypeID":1},'
            '{"EventID":2,"Code":"BBB","EventTypeID":1}]]'
        )
        fake_response = MagicMock()
        fake_response.read.return_value = body.encode()
        fake_response.headers.get.return_value = "application/json"
        session = self._fake_session(fake_response)

        with patch("vietnam_algo.corporate_actions.fetch_vietstock_session", return_value=session):
            ok = validate_vietstock_session(session, timeout=5.0)
        self.assertTrue(ok)

    def test_validate_session_rejects_empty_json_array(self):
        """Invalid session often returns []. The probe must REJECT this.

        The whole point of the strengthened probe: an empty list is
        indistinguishable from \"valid session, no events today\" — but
        a 7-day window should always have events on a trading day, so
        an empty result means the session is fake.
        """
        fake_response = MagicMock()
        fake_response.read.return_value = b"[]"
        fake_response.headers.get.return_value = "application/json"
        session = self._fake_session(fake_response)

        with patch("vietnam_algo.corporate_actions.fetch_vietstock_session", return_value=session):
            ok = validate_vietstock_session(session, timeout=5.0)
        self.assertFalse(ok, "empty []] is invalid for a 7-day window")

    def test_validate_session_rejects_double_bracket_with_empty_inner(self):
        """Vietstock returns [[]] when no events — also invalid for 7-day."""
        fake_response = MagicMock()
        fake_response.read.return_value = b"[[]]"
        fake_response.headers.get.return_value = "application/json"
        session = self._fake_session(fake_response)

        with patch("vietnam_algo.corporate_actions.fetch_vietstock_session", return_value=session):
            ok = validate_vietstock_session(session, timeout=5.0)
        self.assertFalse(ok)

    def test_validate_session_rejects_non_list_json(self):
        """Vietstock sometimes returns an unexpected shape — reject."""
        fake_response = MagicMock()
        fake_response.read.return_value = b'{"unexpected": "shape"}'
        fake_response.headers.get.return_value = "application/json"
        session = self._fake_session(fake_response)

        with patch("vietnam_algo.corporate_actions.fetch_vietstock_session", return_value=session):
            ok = validate_vietstock_session(session, timeout=5.0)
        self.assertFalse(ok)

    def test_validate_session_returns_false_for_html(self):
        """Vietstock returns HTML when session is invalid → probe rejects."""
        fake_response = MagicMock()
        fake_response.read.return_value = b"<!DOCTYPE html><html>...</html>"
        fake_response.headers.get.return_value = "text/html; charset=utf-8"
        session = self._fake_session(fake_response)

        with patch("vietnam_algo.corporate_actions.fetch_vietstock_session", return_value=session):
            ok = validate_vietstock_session(session, timeout=5.0)
        self.assertFalse(ok)

    def test_validate_session_returns_false_for_empty_body(self):
        """Empty body → invalid."""
        fake_response = MagicMock()
        fake_response.read.return_value = b""
        fake_response.headers.get.return_value = ""
        session = self._fake_session(fake_response)

        with patch("vietnam_algo.corporate_actions.fetch_vietstock_session", return_value=session):
            ok = validate_vietstock_session(session, timeout=5.0)
        self.assertFalse(ok)

    def test_validate_session_returns_false_on_network_error(self):
        """Network errors → invalid (returned as False, not raised).

        The caller decides whether to retry or surface the error.
        """
        import urllib.error
        session = MagicMock()
        session.token = "TEST"
        session.opener.open.side_effect = urllib.error.URLError("connection refused")

        with patch("vietnam_algo.corporate_actions.fetch_vietstock_session", return_value=session):
            ok = validate_vietstock_session(session, timeout=5.0)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
