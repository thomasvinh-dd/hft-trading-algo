from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest

from vietnam_algo.dry_run_state import (
    DEFAULT_INITIAL_NAV,
    DryRunState,
    apply_orders_to_state,
    build_dry_run_snapshot,
    init_dry_run_state,
    load_dry_run_state,
    mark_to_market,
    reset_dry_run_state,
    save_dry_run_state,
)


class DryRunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/dry_run_test")
        self.tmp.mkdir(parents=True, exist_ok=True)
        # Clean any stale state file
        state_file = self.tmp / "live" / "dry_run_state.json"
        if state_file.exists():
            state_file.unlink()

    def test_initial_state_has_default_nav(self) -> None:
        os.environ.pop("DRY_RUN_INITIAL_NAV", None)
        os.environ.pop("MOCK_NAV", None)
        state = init_dry_run_state(self.tmp)
        self.assertEqual(state.initial_nav, DEFAULT_INITIAL_NAV)
        self.assertEqual(state.cash, DEFAULT_INITIAL_NAV)
        self.assertEqual(state.positions, {})
        self.assertEqual(state.realized_pnl, 0.0)

    def test_initial_state_respects_env(self) -> None:
        os.environ["DRY_RUN_INITIAL_NAV"] = "50000000"
        try:
            state = init_dry_run_state(self.tmp)
            self.assertEqual(state.initial_nav, 50_000_000.0)
            self.assertEqual(state.cash, 50_000_000.0)
        finally:
            del os.environ["DRY_RUN_INITIAL_NAV"]

    def test_legacy_mock_nav_fallback(self) -> None:
        os.environ.pop("DRY_RUN_INITIAL_NAV", None)
        os.environ["MOCK_NAV"] = "75000000"
        try:
            state = init_dry_run_state(self.tmp)
            self.assertEqual(state.initial_nav, 75_000_000.0)
        finally:
            del os.environ["MOCK_NAV"]

    def test_init_returns_existing_state(self) -> None:
        first = init_dry_run_state(self.tmp)
        first.cash = 42_000_000
        save_dry_run_state(first, self.tmp)
        again = init_dry_run_state(self.tmp)
        # Should be the SAME state (cash preserved).
        self.assertEqual(again.cash, 42_000_000)

    def test_reset_wipes_state(self) -> None:
        first = init_dry_run_state(self.tmp)
        first.cash = 1.0
        save_dry_run_state(first, self.tmp)
        reset = reset_dry_run_state(self.tmp, initial_nav=200_000_000)
        self.assertEqual(reset.initial_nav, 200_000_000)
        self.assertEqual(reset.cash, 200_000_000)
        self.assertEqual(reset.positions, {})

    def test_buy_reduces_cash_and_opens_position(self) -> None:
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        fills = apply_orders_to_state(state, [
            {"side": "NB", "symbol": "VHM", "price": 75_000, "quantity": 100},
        ])
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["side"], "BUY")
        # 100 shares * 75k = 7.5M cash outflow
        self.assertAlmostEqual(state.cash, 92_500_000, places=0)
        self.assertIn("VHM", state.positions)
        self.assertEqual(state.positions["VHM"].quantity, 100)
        self.assertAlmostEqual(state.positions["VHM"].avg_price, 75_000, places=0)

    def test_buy_clipped_to_available_cash(self) -> None:
        state = DryRunState(initial_nav=10_000_000, cash=10_000_000)
        # Try to buy 1000 shares @ 75,000 = 75M (way more than 10M cash).
        # With board_lot=100 (Vietnam default), the buy rounds down to
        # 100 shares; with board_lot=1 it would be 133. Either way the
        # clip is enforced — and cash must not go negative.
        fills = apply_orders_to_state(
            state,
            [{"side": "NB", "symbol": "VHM", "price": 75_000, "quantity": 1000}],
            board_lot=100,
        )
        if fills:
            self.assertLessEqual(fills[0]["qty"], 100)
            self.assertGreaterEqual(state.cash, 0)

    def test_sell_records_realized_pnl(self) -> None:
        state = DryRunState(initial_nav=100_000_000, cash=50_000_000)
        # Build a position manually so we can test a SELL on it.
        from vietnam_algo.dry_run_state import DryRunPosition
        state.positions["HPG"] = DryRunPosition(
            symbol="HPG", quantity=1000, avg_price=20_000,
            last_price=22_000, cost_basis=20_000_000,
        )
        fills = apply_orders_to_state(state, [
            {"side": "NS", "symbol": "HPG", "price": 25_000, "quantity": 500},
        ])
        self.assertEqual(len(fills), 1)
        # Realized P&L = (25k - 20k) * 500 = 2.5M
        self.assertAlmostEqual(state.realized_pnl, 2_500_000, places=0)
        # Remaining 500 shares
        self.assertEqual(state.positions["HPG"].quantity, 500)
        # Cash up: 50M + 500*25k = 62.5M
        self.assertAlmostEqual(state.cash, 62_500_000, places=0)

    def test_sell_closes_position_when_quantity_zero(self) -> None:
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        apply_orders_to_state(state, [
            {"side": "NB", "symbol": "AAA", "price": 10_000, "quantity": 100},
            {"side": "NS", "symbol": "AAA", "price": 12_000, "quantity": 100},
        ])
        # Position should be removed (qty=0)
        self.assertNotIn("AAA", state.positions)
        # Realized = (12k - 10k) * 100 = 200k
        self.assertAlmostEqual(state.realized_pnl, 200_000, places=0)
        # Trade logged
        self.assertTrue(any(t.get("symbol") == "AAA" for t in state.closed_trades))

    def test_mark_to_market_updates_unrealized(self) -> None:
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        apply_orders_to_state(state, [
            {"side": "NB", "symbol": "VIC", "price": 50_000, "quantity": 200},
        ])
        mark_to_market(state, {"VIC": 60_000})
        pos = state.positions["VIC"]
        self.assertEqual(pos.last_price, 60_000)
        self.assertEqual(pos.unrealized_pnl, (60_000 - 50_000) * 200)
        self.assertEqual(state.total_unrealized_pnl, 2_000_000)

    def test_snapshot_shape_matches_live(self) -> None:
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        apply_orders_to_state(state, [
            {"side": "NB", "symbol": "VCB", "price": 80_000, "quantity": 250},
        ])
        mark_to_market(state, {"VCB": 90_000})
        snap = build_dry_run_snapshot(state, account_no="0003937868")
        # Same top-level keys as build_portfolio_snapshot
        self.assertEqual(snap["mode"], "dry_run")
        self.assertEqual(snap["account"], "0003937868")
        self.assertIn("balance", snap)
        self.assertIn("positions", snap)
        self.assertIn("totals", snap)
        # Cash after buying 250 @ 80k = 20M cost → 80M left
        self.assertEqual(snap["balance"]["totalCash"], 80_000_000)
        # Stock value = 250 * 90k = 22.5M
        self.assertEqual(snap["totals"]["stockValue"], 22_500_000)
        # NAV = 80M + 22.5M = 102.5M
        self.assertEqual(snap["totals"]["portfolioValue"], 102_500_000)
        # Unrealized = 250 * 10k = 2.5M
        self.assertEqual(snap["totals"]["totalUnrealizedPnl"], 2_500_000)
        # Initial NAV / return
        self.assertEqual(snap["totals"]["initialNav"], 100_000_000)
        # Return = (102.5M - 100M) / 100M = 2.5%
        self.assertAlmostEqual(snap["totals"]["totalReturnPct"], 0.025, places=4)

    def test_persistence_roundtrip(self) -> None:
        state = init_dry_run_state(self.tmp)
        state.cash = 77_777_777
        state.realized_pnl = 1_234_567
        save_dry_run_state(state, self.tmp)
        loaded = load_dry_run_state(self.tmp)
        assert loaded is not None
        self.assertEqual(loaded.cash, 77_777_777)
        self.assertEqual(loaded.realized_pnl, 1_234_567)

    def test_autofin_side_codes(self) -> None:
        """Autofin uses side codes NB (mua/BUY) and NS (bán/SELL)."""
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        apply_orders_to_state(state, [
            {"side": "NB", "symbol": "AAA", "price": 10_000, "quantity": 100},
            {"side": "NS", "symbol": "AAA", "price": 12_000, "quantity": 100},
        ])
        self.assertAlmostEqual(state.realized_pnl, 200_000, places=0)
        self.assertNotIn("AAA", state.positions)

    def test_skip_orders_with_zero_qty_or_price(self) -> None:
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        fills = apply_orders_to_state(state, [
            {"side": "NB", "symbol": "BAD", "price": 0, "quantity": 100},
            {"side": "NB", "symbol": "BAD", "price": 100, "quantity": 0},
            {"side": "NB", "symbol": "", "price": 100, "quantity": 100},
        ])
        self.assertEqual(fills, [])
        self.assertEqual(state.cash, 100_000_000)


if __name__ == "__main__":
    unittest.main()
