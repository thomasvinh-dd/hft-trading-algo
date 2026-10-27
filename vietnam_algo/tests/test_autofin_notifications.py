"""Tests for autofin notification event parsing.

Per user feedback (2026-09-09): the autofin web app uses the
``/notification/get-notify-by-topics?topics=SIGNAL`` endpoint to
populate its bell-icon notification panel. We're adding client
support for that endpoint so we can use the event stream as a
faster TP/SL detector than polling ``numberOfProfit`` deltas.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest

from vietnam_algo.libs.autofin_signals import (
    AutofinNotification,
    parse_notification,
)


def _raw_notification(**overrides):
    """Build a sample raw notification payload."""
    payload = {
        "_id": 2026090915203651800,
        "timeRequest": "2026-09-09T15:20:36.5187643+07:00",
        "tag": "SIGNAL,TRADINGTIME,TRAILINGSTOP",
        "type": "SIGNAL",
        "notifyDetail": {
            "title": "🛡️ BẢO TOÀN LỢI NHUẬN",
            "imageURL": "https://api.autofin.vn/logos/OCB.jpeg",
            "body": (
                "<p>[OCB] &#x1F6E1;&#xFE0F; Ch&#x1ED1;t l&atilde;i -8.26 % "
                "b&#x1EA3;o to&agrave;n v&#x1ED1;n t&#x1EA1;i 10,150</p>"
                "<p>N&#x1EAF;m gi&#x1EEF;: 51 ng&agrave;y</p>"
            ),
            "action": "https://api.autofin.vn/dashboard/signal?search=OCB",
        },
        "sourceService": "SignalService",
        "channel": "AUTOFIN",
        "hasRead": False,
        "hasDelete": False,
    }
    payload.update(overrides)
    return payload


class ParseNotificationTests(unittest.TestCase):
    def test_parses_basic_fields(self):
        raw = _raw_notification()
        n = parse_notification(raw)
        self.assertEqual(n.id, 2026090915203651800)
        self.assertEqual(n.time_request, "2026-09-09T15:20:36.5187643+07:00")
        self.assertEqual(n.tags, ("SIGNAL", "TRADINGTIME", "TRAILINGSTOP"))
        self.assertEqual(n.title, "🛡️ BẢO TOÀN LỢI NHUẬN")
        self.assertEqual(n.image_url, "https://api.autofin.vn/logos/OCB.jpeg")
        self.assertEqual(n.action_url, "https://api.autofin.vn/dashboard/signal?search=OCB")
        self.assertFalse(n.has_read)

    def test_strips_html_from_body(self):
        raw = _raw_notification()
        n = parse_notification(raw)
        # HTML entities decoded and tags stripped.
        self.assertIn("[OCB]", n.body)
        self.assertIn("Chốt lãi", n.body)
        self.assertIn("10,150", n.body)
        self.assertNotIn("<p>", n.body)
        self.assertNotIn("&atilde;", n.body)

    def test_tag_helpers_match_combinations(self):
        """The convenience properties map common tag combos to bool flags."""
        n_buy = parse_notification(_raw_notification(
            tag="SIGNAL,EOD,OPENDEAL,PVT",
        ))
        self.assertTrue(n_buy.is_buy_opened)
        self.assertFalse(n_buy.is_take_profit)
        self.assertFalse(n_buy.is_close_deal)

        n_tp = parse_notification(_raw_notification(
            tag="SIGNAL,TRADINGTIME,TAKEPROFIT",
        ))
        self.assertTrue(n_tp.is_take_profit)
        self.assertFalse(n_tp.is_buy_opened)

        n_close = parse_notification(_raw_notification(
            tag="SIGNAL,CLOSEDEAL,EOD",
        ))
        self.assertTrue(n_close.is_close_deal)

        n_report = parse_notification(_raw_notification(
            tag="SIGNAL,EOD,REPORT,OPENDEAL",
        ))
        self.assertTrue(n_report.is_daily_report)
        self.assertTrue(n_report.is_buy_opened)  # report + open deal

        n_stop = parse_notification(_raw_notification(
            tag="SIGNAL,TRADINGTIME,STOPORDER",
        ))
        self.assertTrue(n_stop.is_stop_order)

    def test_missing_fields_default_safely(self):
        """An incomplete raw payload shouldn't crash the parser."""
        n = parse_notification({})
        self.assertEqual(n.id, 0)
        self.assertEqual(n.time_request, "")
        self.assertEqual(n.tags, ())
        self.assertEqual(n.title, "")
        self.assertEqual(n.body, "")
        self.assertFalse(n.is_buy_opened)
        self.assertFalse(n.is_take_profit)

    def test_empty_tag_string(self):
        """Empty / missing tag should not produce a single empty-string tag."""
        n = parse_notification({"tag": ""})
        self.assertEqual(n.tags, ())
        n = parse_notification({"tag": ","})
        # Comma-only → filter empty strings.
        self.assertEqual(n.tags, ())

    def test_raw_payload_preserved(self):
        raw = _raw_notification()
        n = parse_notification(raw)
        self.assertEqual(n.raw, raw)

    def test_various_real_tags_recognized(self):
        """Match the tag distribution from the live API pull."""
        # From live pull: 13 TAKEPROFIT, 13 STOPORDER, 7 CLOSEDEAL, 10 REPORT, etc.
        cases = [
            ("SIGNAL,TRADINGTIME,TAKEPROFIT", "is_take_profit"),
            ("SIGNAL,TRADINGTIME,STOPORDER", "is_stop_order"),
            ("SIGNAL,CLOSEDEAL,EOD", "is_close_deal"),
            ("SIGNAL,EOD,REPORT,OPENDEAL", "is_daily_report"),
            ("SIGNAL,OPENDEAL,REMIND", None),  # OPENDEAL without EOD — not a new recommendation
            ("SIGNAL,EOD,OPENDEAL,REMIND", "is_buy_opened"),  # OPENDEAL + EOD → recommendation
            ("SIGNAL,TRADINGTIME,TRAILINGSTOP", None),  # not a special helper
        ]
        for tag, expected_attr in cases:
            with self.subTest(tag=tag):
                n = parse_notification(_raw_notification(tag=tag))
                if expected_attr:
                    self.assertTrue(getattr(n, expected_attr), f"expected {expected_attr} for tag={tag}")
                else:
                    # TRAILINGSTOP — not exposed as helper. Just shouldn't crash.
                    self.assertIsInstance(n.tags, tuple)


if __name__ == "__main__":
    unittest.main()
