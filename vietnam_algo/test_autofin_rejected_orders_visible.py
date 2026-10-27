"""Regression tests for issue A — DNSE rejections must be VISIBLE.

Live-confirmed 2026-10-26: a filled BUY whose paired stop-loss was
rejected by DNSE was reported as "1 submitted" with NO mention of the
rejection. The submit path computed a ``rejected`` counter, but no
caller ever read it, so a refused order produced zero Telegram output
and the position was left silently unprotected.

These tests pin the contract:

  1. ``_submit_autofin_deals_to_dnse`` returns the ``rejected`` count
     AND a ``rejected_details`` list naming symbol / leg / error.
  2. ``token_rejected`` (the non-terminal status used for
     ``INVALID_TRADING_TOKEN``) is counted too — it previously fell
     through every counter and was completely invisible.
  3. ``rejected_orders_message`` renders a non-empty message for a
     declined order and returns None when nothing was declined.
  4. The force-run summary shows the rejected count, so the operator
     is never told "submitted" while an order was actually refused.
  5. A rejected stop-loss leg is called out as leaving the position
     UNPROTECTED.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest

from vietnam_algo.libs.notifier import (
    force_run_summary_message,
    rejected_orders_message,
)


def _submit_result(**overrides):
    """Build a submit_result dict shaped like the runner's return value."""
    base = {
        "submitted": ["9001"],
        "skipped_existing": 0,
        "unknown": 0,
        "rejected": 0,
        "token_rejected": 0,
        "rejected_details": [],
        "total": 1,
    }
    base.update(overrides)
    return base


class TestRejectedOrdersMessage(unittest.TestCase):
    def test_returns_none_when_nothing_declined(self):
        self.assertIsNone(rejected_orders_message(_submit_result()))
        self.assertIsNone(rejected_orders_message(None))
        self.assertIsNone(rejected_orders_message({}))

    def test_renders_symbol_leg_and_error(self):
        msg = rejected_orders_message(
            _submit_result(
                submitted=["9001"],
                rejected=1,
                total=2,
                rejected_details=[
                    {
                        "symbol": "ORS",
                        "side": "NS",
                        "kind": "sl",
                        "quantity": 200,
                        "price": 14400,
                        "error": "HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
                        "status": "rejected",
                    }
                ],
            ),
            "2026-10-27",
            "autofin mid-day",
        )
        self.assertIsNotNone(msg)
        self.assertIn("ORS", msg)
        self.assertIn("sl", msg)
        self.assertIn("TRADE_QUANTITY_NOT_ENOUGH", msg)
        self.assertIn("2026-10-27", msg)
        self.assertIn("rejected: 1", msg)

    def test_rejected_stop_loss_warns_position_unprotected(self):
        msg = rejected_orders_message(
            _submit_result(
                rejected=1,
                total=2,
                rejected_details=[
                    {
                        "symbol": "ORS",
                        "side": "NS",
                        "kind": "sl",
                        "quantity": 200,
                        "price": 14400,
                        "error": "HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
                    }
                ],
            ),
            "2026-10-27",
        )
        self.assertIn("UNPROTECTED", msg)

    def test_token_rejected_is_counted(self):
        msg = rejected_orders_message(
            _submit_result(
                submitted=[],
                token_rejected=1,
                total=1,
                rejected_details=[
                    {
                        "symbol": "KDC",
                        "side": "NB",
                        "kind": "",
                        "quantity": 100,
                        "price": 25000,
                        "error": "HTTP 400 INVALID_TRADING_TOKEN",
                        "status": "token_rejected",
                    }
                ],
            ),
            "2026-10-27",
        )
        self.assertIsNotNone(msg)
        self.assertIn("tokenRejected: 1", msg)
        self.assertIn("INVALID_TRADING_TOKEN", msg)


class TestForceRunSummaryShowsRejections(unittest.TestCase):
    def _summary(self, submit_result):
        return force_run_summary_message(
            strategy_name="autofin_signal",
            trade_date="2026-10-27",
            poll_result={"count": 2, "newOrders": 2},
            submit_result=submit_result,
            live_submit=True,
        )

    def test_rejected_count_appears_in_summary(self):
        out = self._summary(
            _submit_result(
                submitted=["9001"],
                rejected=1,
                total=2,
                rejected_details=[
                    {
                        "symbol": "ORS",
                        "side": "NS",
                        "kind": "sl",
                        "quantity": 200,
                        "price": 14400,
                        "error": "HTTP 400 TRADE_QUANTITY_NOT_ENOUGH",
                    }
                ],
            )
        )
        self.assertIn("1 rejected", out)
        self.assertIn("REJECTED BY DNSE", out)
        self.assertIn("ORS", out)
        self.assertIn("TRADE_QUANTITY_NOT_ENOUGH", out)
        # A refused order must not render as a clean success.
        self.assertNotIn("✅", out)

    def test_token_rejected_count_appears_in_summary(self):
        out = self._summary(
            _submit_result(submitted=[], token_rejected=1, total=1)
        )
        self.assertIn("1 token-rejected", out)

    def test_clean_submit_still_shows_success_icon(self):
        out = self._summary(_submit_result(submitted=["9001"], total=1))
        self.assertIn("✅", out)
        self.assertIn("0 rejected", out)


class TestRunnerSubmitResultShape(unittest.TestCase):
    """The submit helper must expose the rejection counters."""

    def test_return_dict_carries_rejection_fields(self):
        import inspect
        from vietnam_algo.bots import runner

        src = inspect.getsource(runner._submit_autofin_deals_to_dnse)
        for key in ("rejected", "token_rejected", "rejected_details"):
            self.assertIn(f'"{key}"', src, f"submit result missing {key}")


if __name__ == "__main__":
    unittest.main()
