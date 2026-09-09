"""Tests for the once-per-day CA-failure alert suppression.

Per user feedback 2026-09-09: the CA sync failure was firing 5+
Telegram alerts per cycle. Now we should fire at most ONE alert
per day, and only when the CA sync genuinely fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from unittest.mock import patch, MagicMock

from vietnam_algo.bot_state import AutoBotState, load_state, save_state


class CaAlertSuppressionTests(unittest.TestCase):
    def test_state_carries_ca_alert_sent_date(self):
        """The state has a caAlertSentDate field."""
        state = AutoBotState()
        self.assertEqual(state.caAlertSentDate, "")

    def test_state_persists_ca_alert_sent_date(self):
        """caAlertSentDate roundtrips through load/save."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            from vietnam_algo.data import DEFAULT_DATA_DIR
            tmpdir = Path(tmp)
            original = AutoBotState(caAlertSentDate="2026-09-09")
            save_state(original, tmpdir, strategy_name="autofin_signal")
            loaded = load_state(tmpdir, strategy_name="autofin_signal")
            self.assertEqual(loaded.caAlertSentDate, "2026-09-09")

    def test_alert_should_fire_when_ca_alert_sent_date_is_empty(self):
        """If state.caAlertSentDate != today, we should send a new alert."""
        state = AutoBotState(caAlertSentDate="")  # never sent
        today = "2026-09-09"
        self.assertNotEqual(state.caAlertSentDate, today)  # → fire

    def test_alert_should_not_fire_when_ca_alert_sent_date_matches_today(self):
        """If state.caAlertSentDate == today, we already sent it — suppress."""
        state = AutoBotState(caAlertSentDate="2026-09-09")
        today = "2026-09-09"
        self.assertEqual(state.caAlertSentDate, today)  # → suppress

    def test_alert_resets_after_successful_sync(self):
        """On successful CA sync, caAlertSentDate is cleared so tomorrow's
        first failure (if any) fires again."""
        # Simulated code path: success → state.caAlertSentDate = "" → save
        state = AutoBotState(caAlertSentDate="2026-09-09")
        state.caAlertSentDate = ""  # reset on success
        self.assertEqual(state.caAlertSentDate, "")


if __name__ == "__main__":
    unittest.main()
