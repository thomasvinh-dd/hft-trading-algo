"""Verify the autofin adapter no longer emits a stale VNM close order
when the dry-run state has no VNM position.

Reproduces the 2026-09-11 bug where the bot emitted
"NS 400 VNM @ 0 (autofin_closed:deal#1786924800:VNM)" even though the
dry-run state had zero VNM shares — the autofin tracking state had
_our_qty=400 from a previous cycle whose BUY was never actually
applied to the dry-run state.
"""
import sys
import json
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.dry_run_state import (
    DryRunPosition,
    DryRunState,
    save_dry_run_state,
)
from vietnam_algo.strategies_impl.autofin_signal import decide_orders


class AutofinReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/autofin_reconcile_test")
        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.tmp / "live").mkdir(parents=True, exist_ok=True)

    def test_close_skipped_when_dry_run_state_has_zero_qty(self) -> None:
        """The autofin tracking state has VNM with _our_qty=400 (stale)
        but the dry-run state has no VNM. decide_orders() should NOT
        emit a close order for VNM because we don't actually hold it.
        """
        # Dry-run state: 0 positions.
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        state_path = self.tmp / "live" / "dry_run_state.autofin_signal.json"
        save_dry_run_state(state, self.tmp, strategy_name="autofin_signal")

        # Autofin tracking state: VNM with _our_qty=400 (stale).
        vnm_deal = {
            "_id": 1786924800,
            "symbol": "VNM",
            "lastRecomment": "Khuyến nghị đầu tư",
            "numberOfProfit": 0,
            "rateForNav": 0.30,
            "statusDeal": {"isClosed": False},
            "_our_qty": 400,
        }
        our_deals = {"1786924800:VNM": vnm_deal}

        # Empty signals, empty holding (autofin has exited VNM).
        signals: list = []
        holding: list = []

        orders, new_our_deals, deals_actioned = decide_orders(
            holding=holding,
            signals=signals,
            our_deals=our_deals,
            our_positions={},
            nav=100_000_000,
            data_dir=self.tmp,
        )
        # No close order for VNM should be emitted because the dry-run
        # state has 0 VNM shares — reconcile() resets _our_qty to 0
        # before _plan_close_orders runs.
        vnm_closes = [
            o for o in orders
            if o.get("symbol") == "VNM" and o.get("side") == "NS"
        ]
        self.assertEqual(
            vnm_closes, [],
            f"close orders for VNM should be skipped but got: {vnm_closes}",
        )

    def test_close_emitted_when_dry_run_state_holds_position(self) -> None:
        """If the dry-run state DOES hold the position (real case),
        the close order should still be emitted."""
        state = DryRunState(initial_nav=100_000_000, cash=70_000_000)
        state.positions["VNM"] = DryRunPosition(
            symbol="VNM", quantity=400, avg_price=60000,
            last_price=60000, cost_basis=24000000,
        )
        state_path = self.tmp / "live" / "dry_run_state.autofin_signal.json"
        save_dry_run_state(state, self.tmp, strategy_name="autofin_signal")

        vnm_deal = {
            "_id": 1786924800,
            "symbol": "VNM",
            "lastRecomment": "Khuyến nghị đầu tư",
            "numberOfProfit": 0,
            "rateForNav": 0.30,
            "statusDeal": {"isClosed": False},
            "_our_qty": 400,
        }
        our_deals = {"1786924800:VNM": vnm_deal}
        signals: list = []
        holding: list = []  # autofin exited

        orders, _, _ = decide_orders(
            holding=holding,
            signals=signals,
            our_deals=our_deals,
            our_positions={"VNM": 400},
            nav=100_000_000,
            data_dir=self.tmp,
        )
        # Close order SHOULD be emitted because dry-run confirms we hold VNM.
        vnm_closes = [
            o for o in orders
            if o.get("symbol") == "VNM" and o.get("side") == "NS"
        ]
        self.assertEqual(len(vnm_closes), 1)
        self.assertEqual(vnm_closes[0]["quantity"], 400)


if __name__ == "__main__":
    unittest.main()
