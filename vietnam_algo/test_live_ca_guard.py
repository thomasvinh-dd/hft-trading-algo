"""Tests for the upgraded live-path corporate-action guard.

The live engine uses ``apply_corporate_action_guard`` and
``build_corporate_action_exit_orders`` to:
  - block BUYs within the CA window,
  - force SELLs before ex-dates,
  - handle standard vs high-risk events separately.

These tests pin down the window-union behaviour and the classifier-integrated
decision log so the live path matches the backtest.
"""

from __future__ import annotations

import unittest

from vietnam_algo.libs.corporate_actions import CorporateAction
from vietnam_algo.libs.trading_engine import apply_corporate_action_guard, CorporateActionDecision


def _corp(eventId: int, symbol: str, exDate: str, *, name: str = "") -> CorporateAction:
    return CorporateAction(
        eventId=eventId, eventTypeId=1, channelId=0, catId=0,
        symbol=symbol, companyName="Co", exchange="HOSE",
        exDate=exDate, recordDate=None, paymentDate=None,
        name=name, note="", title="", fileUrl="",
        isCashOnly=False, isNonCash=True,
    )


class _Order:
    """Minimal AlgoOrder-like object; apply_corporate_action_guard only reads
    .side, .date, .orderId, .symbol."""

    def __init__(self, side: str, symbol: str, date: str, orderId: int = 1) -> None:
        self.side = side
        self.symbol = symbol
        self.date = date
        self.orderId = orderId


class ApplyGuardTests(unittest.TestCase):
    def test_no_events_keeps_all_orders(self) -> None:
        orders = [_Order("BUY", "VNM", "2026-09-21", orderId=1)]
        kept, decisions = apply_corporate_action_guard(orders, [])
        self.assertEqual(kept, orders)
        self.assertEqual(decisions, [])

    def test_buy_blocked_in_standard_window(self) -> None:
        orders = [_Order("BUY", "VNM", "2026-09-21", orderId=1)]
        actions = [_corp(1, "VNM", "2026-09-22")]
        trading_dates = ["2026-09-21", "2026-09-22"]
        kept, decisions = apply_corporate_action_guard(
            orders, actions,
            buy_window_days=2, sell_window_days=3,
            trading_dates=trading_dates,
        )
        self.assertEqual(kept, [])
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "blocked")

    def test_high_risk_only_ca_extends_block(self) -> None:
        """A high-risk CA event with sessions_until > standard_window but within
        high_risk_window must still block the BUY. This is the parity fix."""
        orders = [_Order("BUY", "SHS", "2026-09-20", orderId=1)]
        # SHS has a rights offering (high-risk) 3 trading days later
        actions = [_corp(1, "SHS", "2026-09-25", name="Phát hành thêm cổ phiếu")]
        trading_dates = [f"2026-09-{d:02d}" for d in range(18, 30)]
        kept, decisions = apply_corporate_action_guard(
            orders, actions,
            buy_window_days=2,
            high_risk_buy_window_days=5,
            trading_dates=trading_dates,
        )
        self.assertEqual(kept, [], "BUY should be blocked by high-risk CA at sessions_until=4")
        self.assertEqual(decisions[0].action, "blocked")
        # Without the high_risk parameter, the BUY would have been allowed.
        kept_no_hr, _ = apply_corporate_action_guard(
            orders, actions,
            buy_window_days=2,
            high_risk_buy_window_days=None,
            trading_dates=trading_dates,
        )
        self.assertEqual(kept_no_hr, orders, "Without high_risk window, BUY passes through")

    def test_sell_allowed_in_standard_window(self) -> None:
        orders = [_Order("SELL", "VNM", "2026-09-21", orderId=1)]
        actions = [_corp(1, "VNM", "2026-09-22")]
        trading_dates = ["2026-09-21", "2026-09-22"]
        kept, decisions = apply_corporate_action_guard(
            orders, actions,
            buy_window_days=2, sell_window_days=3,
            trading_dates=trading_dates,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(decisions[0].action, "allowed_exit")

    def test_sell_allowed_in_high_risk_window_only(self) -> None:
        """Parity: a high-risk CA in the wider high-risk window should allow
        the SELL even when it's outside the standard sell window."""
        orders = [_Order("SELL", "SHS", "2026-09-20", orderId=1)]
        actions = [_corp(1, "SHS", "2026-09-25", name="Phát hành thêm cổ phiếu")]
        trading_dates = [f"2026-09-{d:02d}" for d in range(18, 30)]
        kept, decisions = apply_corporate_action_guard(
            orders, actions,
            sell_window_days=2,
            high_risk_sell_window_days=5,
            trading_dates=trading_dates,
        )
        self.assertEqual(len(kept), 1, "SELL should be allowed by high-risk window")
        self.assertEqual(decisions[0].action, "allowed_exit")

    def test_no_window_params_keeps_all_orders(self) -> None:
        """Backward compat: passing 0 windows means no guarding at all."""
        orders = [_Order("BUY", "VNM", "2026-09-21", orderId=1)]
        actions = [_corp(1, "VNM", "2026-09-22")]
        kept, decisions = apply_corporate_action_guard(
            orders, actions,
            buy_window_days=0, sell_window_days=0,
            high_risk_buy_window_days=None, high_risk_sell_window_days=None,
            trading_dates=["2026-09-21", "2026-09-22"],
        )
        self.assertEqual(kept, orders)
        self.assertEqual(decisions, [])

    def test_decision_includes_event_payload(self) -> None:
        orders = [_Order("BUY", "VNM", "2026-09-21", orderId=42)]
        actions = [_corp(1, "VNM", "2026-09-22", name="Trả cổ tức bằng cổ phiếu")]
        trading_dates = ["2026-09-21", "2026-09-22"]
        _, decisions = apply_corporate_action_guard(
            orders, actions,
            buy_window_days=2, sell_window_days=3,
            trading_dates=trading_dates,
        )
        self.assertEqual(len(decisions), 1)
        d = decisions[0]
        self.assertEqual(d.symbol, "VNM")
        self.assertEqual(d.side, "BUY")
        self.assertEqual(len(d.events), 1)
        self.assertEqual(d.events[0]["symbol"], "VNM")


if __name__ == "__main__":
    unittest.main()
