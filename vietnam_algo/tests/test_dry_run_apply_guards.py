"""Tests for the dry-run order-application guards added after the
2026-09-09 incident where cash-constrained BUYs were producing fractional
positions (e.g. 146-share VIC) and re-applying the same orders across
EOD cycles was double-buying existing positions.

Covers:
  * board lot rounding on cash-constrained buys
  * per-order dedup across multiple apply calls
  * status field (applied / clipped / skipped) on each fill
  * skip reasons (insufficient_cash, below_board_lot, no_position, already_applied)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.dry_run_state import (
    DryRunState,
    apply_orders_to_state,
)


class DryRunApplyGuardsTests(unittest.TestCase):
    def test_buy_clipped_to_board_lot(self) -> None:
        """Cash-constrained BUY should round DOWN to nearest board lot.

        With 10M cash and price=75,000:
          max_qty = 10M / 75k = 133 (no fees)
          board lot = 100  →  lot_qty = 100
        So we should buy exactly 100 shares, not 133 (which is not a
        valid Vietnamese board lot).
        """
        state = DryRunState(initial_nav=10_000_000, cash=10_000_000)
        fills = apply_orders_to_state(
            state,
            [{"side": "BUY", "symbol": "VHM", "price": 75_000, "quantity": 1000}],
            board_lot=100,
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["status"], "clipped")
        self.assertEqual(fills[0]["qty"], 100)
        self.assertEqual(fills[0]["requested_qty"], 1000)
        self.assertEqual(state.positions["VHM"].quantity, 100)

    def test_buy_skipped_below_board_lot(self) -> None:
        """If cash can only afford <1 board lot, skip the order."""
        state = DryRunState(initial_nav=200_000, cash=200_000)
        # 200k / 75k = 2.66 raw shares (no fees); with board_lot=100
        # that's not enough for even 1 lot → below_board_lot.
        fills = apply_orders_to_state(
            state,
            [{"side": "BUY", "symbol": "VHM", "price": 75_000, "quantity": 100}],
            board_lot=100,
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["status"], "skipped")
        self.assertEqual(fills[0]["reason"], "below_board_lot")
        self.assertEqual(state.positions, {})

    def test_buy_skipped_no_cash_at_all(self) -> None:
        """If cash=0, skip with insufficient_cash (before lot check)."""
        state = DryRunState(initial_nav=10_000_000, cash=0)
        fills = apply_orders_to_state(
            state,
            [{"side": "BUY", "symbol": "VHM", "price": 75_000, "quantity": 100}],
            board_lot=100,
        )
        self.assertEqual(fills[0]["status"], "skipped")
        self.assertEqual(fills[0]["reason"], "insufficient_cash")

    def test_buy_skipped_no_cash(self) -> None:
        state = DryRunState(initial_nav=10_000_000, cash=0)
        fills = apply_orders_to_state(
            state,
            [{"side": "BUY", "symbol": "VHM", "price": 75_000, "quantity": 100}],
            board_lot=100,
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["status"], "skipped")
        self.assertEqual(fills[0]["reason"], "insufficient_cash")
        self.assertEqual(state.positions, {})

    def test_full_buy_marked_applied(self) -> None:
        """A BUY that fits cash-wise should be status='applied'."""
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        fills = apply_orders_to_state(
            state,
            [{"side": "BUY", "symbol": "VHM", "price": 75_000, "quantity": 100}],
            board_lot=100,
        )
        self.assertEqual(fills[0]["status"], "applied")
        self.assertEqual(fills[0]["qty"], 100)

    def test_sell_skipped_when_no_position(self) -> None:
        """SELL on a symbol we don't own should be skipped, not negative-shares."""
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        fills = apply_orders_to_state(
            state,
            [{"side": "SELL", "symbol": "HPG", "price": 25_000, "quantity": 100}],
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["status"], "skipped")
        self.assertEqual(fills[0]["reason"], "no_position")

    def test_buy_dedup_across_apply_calls(self) -> None:
        """Same exact order passed twice should only apply once."""
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        order = {
            "side": "BUY", "symbol": "PVT",
            "price": 21_721.7, "quantity": 2900,
            "date": "2026-09-09",
        }
        # First call: applies.
        first = apply_orders_to_state(state, [order], board_lot=100)
        self.assertEqual(first[0]["status"], "applied")
        self.assertEqual(state.positions["PVT"].quantity, 2900)
        cash_after_first = state.cash
        # Second call with the same order: dedup kicks in, skips.
        second = apply_orders_to_state(state, [order], board_lot=100)
        self.assertEqual(second[0]["status"], "skipped")
        self.assertEqual(second[0]["reason"], "already_applied")
        self.assertEqual(state.positions["PVT"].quantity, 2900)
        self.assertEqual(state.cash, cash_after_first)

    def test_dedup_persists_across_state_reload(self) -> None:
        """Dedup keys must survive a state save+load round-trip so a bot
        restart doesn't re-apply already-applied orders."""
        import json
        from vietnam_algo.libs.dry_run_state import save_dry_run_state, load_dry_run_state

        tmp = Path("/tmp/opencode/dry_run_dedup_test")
        tmp.mkdir(parents=True, exist_ok=True)
        state_path = tmp / "live"
        state_path.mkdir(parents=True, exist_ok=True)

        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        order = {
            "side": "BUY", "symbol": "VIC",
            "price": 252_252, "quantity": 200,
            "date": "2026-09-09",
        }
        apply_orders_to_state(state, [order], board_lot=100)
        # Wipe cash / positions to simulate a fresh process having loaded
        # the state mid-cycle: we should still NOT re-apply the order.
        save_dry_run_state(state, tmp)
        reloaded = load_dry_run_state(tmp)
        assert reloaded is not None
        # Zero out everything except closed_trades — simulating a state
        # where someone manually reset cash/positions but kept history.
        reloaded.cash = 37_000_000
        reloaded.positions = {}
        second = apply_orders_to_state(reloaded, [order], board_lot=100)
        # Order is in dedup history → skipped, not re-applied.
        self.assertEqual(second[0]["status"], "skipped")
        self.assertEqual(second[0]["reason"], "already_applied")
        self.assertEqual(reloaded.positions, {})

    def test_board_lot_off_disables_rounding(self) -> None:
        """board_lot=1 disables lot rounding (legacy behavior)."""
        state = DryRunState(initial_nav=10_000_000, cash=10_000_000)
        fills = apply_orders_to_state(
            state,
            [{"side": "BUY", "symbol": "VHM", "price": 75_000, "quantity": 1000}],
            board_lot=1,
        )
        # 10M / 75k = 133 shares (no rounding)
        self.assertEqual(fills[0]["status"], "clipped")
        self.assertEqual(fills[0]["qty"], 133)

    def test_fees_bps_debits_cash(self) -> None:
        """fees_bps should reduce the cash balance by the round-trip fee."""
        state = DryRunState(initial_nav=10_000_000, cash=10_000_000)
        fills = apply_orders_to_state(
            state,
            [{"side": "BUY", "symbol": "VHM", "price": 100_000, "quantity": 100}],
            board_lot=100,
            fees_bps=15.0,
        )
        # 100 shares * 100k = 10M + 15bps fee = 10,015,000 — but we have
        # only 10M. So the cash-constraint clip kicks in: max_qty =
        # 10M / 100k / 1.0015 = 99.85 → 99 shares (lot 100 below).
        # Better to test with extra cash headroom:
        state2 = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        fills2 = apply_orders_to_state(
            state2,
            [{"side": "BUY", "symbol": "VHM", "price": 100_000, "quantity": 100}],
            board_lot=100,
            fees_bps=15.0,
        )
        # 100 * 100k * 1.0015 = 10,015,000 cash debit.
        self.assertEqual(fills2[0]["status"], "applied")
        self.assertAlmostEqual(state2.cash, 89_985_000, places=0)

    def test_real_world_pvt_vic_clip(self) -> None:
        """Reproduce the 2026-09-09 scenario: 100M cash, BUY 2900 PVT then
        BUY 200 VIC. The 2nd order should be clipped because cash is
        insufficient. With board lot 100, it should buy exactly 100 VIC.
        """
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        fills = apply_orders_to_state(
            state,
            [
                {"side": "BUY", "symbol": "PVT", "price": 21_721.7, "quantity": 2900},
                {"side": "BUY", "symbol": "VIC", "price": 252_252, "quantity": 200},
            ],
            board_lot=100,
            fees_bps=15.0,
        )
        self.assertEqual(fills[0]["status"], "applied")
        self.assertEqual(fills[1]["status"], "clipped")
        # 100M - 2900 * 21721.7 * 1.0015 = 100M - 63,082,000 ≈ 36,918,000
        # VIC max = 36,918,000 / 252,252 = 146 (no lot). With lot 100 → 100.
        self.assertEqual(fills[1]["qty"], 100)
        self.assertEqual(fills[1]["requested_qty"], 200)
        self.assertEqual(state.positions["PVT"].quantity, 2900)
        self.assertEqual(state.positions["VIC"].quantity, 100)

    def test_zero_or_negative_qty_skipped(self) -> None:
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        fills = apply_orders_to_state(
            state,
            [
                {"side": "BUY", "symbol": "X", "price": 1000, "quantity": 0},
                {"side": "BUY", "symbol": "X", "price": 1000, "quantity": -10},
                {"side": "BUY", "symbol": "X", "price": 0, "quantity": 100},
            ],
        )
        self.assertEqual(fills, [])


if __name__ == "__main__":
    unittest.main()
