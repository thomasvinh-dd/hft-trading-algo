"""Tests for the live post-CA rebuy build functions.

These exercise ``build_post_ca_rebuy_orders``, ``mark_pending_rebuys_filled``,
and ``enqueue_pending_rebuys_from_force_exits`` so the live engine has the
same guarantees as the backtest runner for the post-CA recovery path.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vietnam_algo.libs.ca_rebuy_state import CaRebuyState, PendingRebuy, ReservedCash
from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.trading_engine import (
    DnseOrder,
    build_post_ca_rebuy_orders,
    enqueue_pending_rebuys_from_force_exits,
    mark_pending_rebuys_filled,
)


def _write_price_file(data_dir: Path, symbol: str, bars: list[tuple[str, float]]) -> None:
    """Helper: write a fake ``data/prices/SYM.json`` for tests."""
    prices_dir = data_dir / "prices"
    prices_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": symbol,
        "count": len(bars),
        "items": [
            {"time": d, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000.0}
            for d, c in bars
        ],
    }
    (prices_dir / f"{symbol}.json").write_text(json.dumps(payload))


def _exit_order(symbol: str, price: float, quantity: int, source_date: str) -> DnseOrder:
    return DnseOrder(
        symbol=symbol,
        side="NS",
        quantity=quantity,
        price=price,
        loanPackageId=1,
        orderType="LO",
        sourceOrderId=-12345,
        sourceDate=source_date,
        reason="corporate_action_forced_exit",
    )


class BuildPostCaRebuyOrdersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        # Five trading dates, day 2 is the ex-date.
        self.trading_dates = [
            "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
        ]
        self.ex_date = "2026-09-23"
        self.current_date = "2026-09-25"  # days_since_ex = 2
        self.current_index = self.trading_dates.index(self.current_date)
        self.config = StrategyConfig(
            corporateActionRebuyAfterExDate=True,
            corporateActionRebuySignalMode="fixed_delay",
            corporateActionRebuyDelayDays=2,
            corporateActionRebuyDelayHighRiskOnly=False,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_drops_expired_first(self) -> None:
        state = CaRebuyState()
        state.enqueue(
            symbol="VNM", exit_date="2026-09-21", exit_price=100.0,
            target_value=1_000_000.0, ex_date="2026-09-22",
            has_high_risk=False, expiry_index=1,  # already expired at index 4
        )
        orders, decisions = build_post_ca_rebuy_orders(
            state, self.tmp_dir, self.current_date, self.trading_dates, self.current_index, self.config,
            loan_package_id=1,
        )
        self.assertEqual(orders, [])
        self.assertEqual(state.pendingRebuys, [])
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "rebuy_dropped")

    def test_skips_until_min_days_met(self) -> None:
        # ex_date = 2026-09-23, current_date = 2026-09-25 → days_since_ex=2
        # delay=2 → should fire.
        state = CaRebuyState()
        state.enqueue(
            symbol="VNM", exit_date="2026-09-22", exit_price=80000.0,
            target_value=10_000_000.0, ex_date="2026-09-23",
            has_high_risk=False, expiry_index=99,
        )
        _write_price_file(self.tmp_dir, "VNM", [
            ("2026-09-21", 80000.0),
            ("2026-09-22", 80000.0),
            ("2026-09-23", 76000.0),
            ("2026-09-24", 76000.0),
            ("2026-09-25", 76000.0),
        ])
        orders, decisions = build_post_ca_rebuy_orders(
            state, self.tmp_dir, self.current_date, self.trading_dates, self.current_index, self.config,
            loan_package_id=1,
        )
        self.assertEqual(len(orders), 1)
        self.assertTrue(orders[0].reason.startswith("corporate_action_rebuy_"))
        # Decision log records the fire reason.
        self.assertEqual(decisions[0].action, "rebuy_emitted")

    def test_skips_before_min_days(self) -> None:
        # Set current_date to day after ex-date.
        state = CaRebuyState()
        state.enqueue(
            symbol="VNM", exit_date="2026-09-22", exit_price=80000.0,
            target_value=10_000_000.0, ex_date="2026-09-23",
            has_high_risk=False, expiry_index=99,
        )
        orders, decisions = build_post_ca_rebuy_orders(
            state, self.tmp_dir,
            "2026-09-24",  # days_since_ex = 1
            self.trading_dates,
            self.trading_dates.index("2026-09-24"),
            self.config,
            loan_package_id=1,
        )
        self.assertEqual(orders, [])
        self.assertEqual(decisions, [])

    def test_signal_mode_waits_for_recovery(self) -> None:
        # With signal=recovery r=0.005, need 0.5% bounce from local low.
        state = CaRebuyState()
        state.enqueue(
            symbol="VNM", exit_date="2026-09-22", exit_price=80000.0,
            target_value=10_000_000.0, ex_date="2026-09-23",
            has_high_risk=False, expiry_index=99,
        )
        _write_price_file(self.tmp_dir, "VNM", [
            ("2026-09-23", 80000.0),
            ("2026-09-24", 76000.0),  # 5% drop
            ("2026-09-25", 76050.0),  # 0.07% bounce - too small
        ])
        signal_cfg = StrategyConfig(
            corporateActionRebuyAfterExDate=True,
            corporateActionRebuySignalMode="recovery",
            corporateActionRebuySignalRecoveryPct=0.005,  # 0.5%
            corporateActionRebuySignalMaxDays=10,
        )
        orders, _ = build_post_ca_rebuy_orders(
            state, self.tmp_dir, self.current_date, self.trading_dates, self.current_index, signal_cfg,
            loan_package_id=1,
        )
        self.assertEqual(orders, [], "0.07% bounce should not trigger 0.5% recovery signal")

        # Now have a real bounce: +1.2% from low
        _write_price_file(self.tmp_dir, "VNM", [
            ("2026-09-23", 80000.0),
            ("2026-09-24", 76000.0),
            ("2026-09-25", 76900.0),  # +1.18% from low
        ])
        orders, _ = build_post_ca_rebuy_orders(
            state, self.tmp_dir, self.current_date, self.trading_dates, self.current_index, signal_cfg,
            loan_package_id=1,
        )
        self.assertEqual(len(orders), 1)

    def test_skips_when_no_price_available(self) -> None:
        state = CaRebuyState()
        state.enqueue(
            symbol="NTP", exit_date="2026-09-22", exit_price=52125.0,
            target_value=5_000_000.0, ex_date="2026-09-23",
            has_high_risk=False, expiry_index=99,
        )
        # No price file written for NTP.
        orders, decisions = build_post_ca_rebuy_orders(
            state, self.tmp_dir, self.current_date, self.trading_dates, self.current_index, self.config,
            loan_package_id=1,
        )
        self.assertEqual(orders, [])
        self.assertEqual(decisions[0].action, "rebuy_skipped")


class EnqueueFromForceExitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = CaRebuyState()

    def test_creates_pending_for_each_exit(self) -> None:
        orders = [
            _exit_order("VNM", price=80277, quantity=4000, source_date="2020-09-29"),
            _exit_order("SHS", price=16622, quantity=2000, source_date="2021-08-19"),
        ]
        trading_dates = ["2020-09-29", "2020-09-30"]
        cfg = StrategyConfig(corporateActionReserveExpiryDays=10)
        queued = enqueue_pending_rebuys_from_force_exits(
            self.state,
            orders,
            trading_dates,
            current_trading_index=0,
            config=cfg,
            symbol_ex_dates={"VNM": "2020-09-29", "SHS": "2021-08-20"},
            symbol_high_risk={"VNM": False, "SHS": True},
        )
        self.assertEqual(queued, 2)
        self.assertEqual(len(self.state.pendingRebuys), 2)
        self.assertEqual(len(self.state.reservedCash), 2)
        vnm = self.state.pending_for("VNM")
        self.assertIsNotNone(vnm)
        self.assertEqual(vnm.exitPrice, 80277.0)
        self.assertEqual(vnm.targetValue, 80277 * 4000)
        self.assertFalse(vnm.hasHighRisk)
        shs = self.state.pending_for("SHS")
        self.assertTrue(shs.hasHighRisk)


class MarkFilledTests(unittest.TestCase):
    def test_drops_pending_on_fill(self) -> None:
        state = CaRebuyState()
        state.enqueue(
            symbol="VNM", exit_date="2020-09-29", exit_price=80277.0,
            target_value=10_000_000.0, ex_date="2020-09-30", has_high_risk=False,
            expiry_index=99,
        )
        dropped = mark_pending_rebuys_filled(state, {"VNM": 1})
        self.assertEqual(dropped, 1)
        self.assertIsNone(state.pending_for("VNM"))
        self.assertEqual(state.reservedCash, [])

    def test_keeps_unfilled(self) -> None:
        state = CaRebuyState()
        state.enqueue(
            symbol="VNM", exit_date="2020-09-29", exit_price=80277.0,
            target_value=10_000_000.0, ex_date="2020-09-30", has_high_risk=False,
            expiry_index=99,
        )
        dropped = mark_pending_rebuys_filled(state, {})  # no fills
        self.assertEqual(dropped, 0)
        self.assertIsNotNone(state.pending_for("VNM"))


if __name__ == "__main__":
    unittest.main()
