"""Tests for per-strategy dry-run state isolation.

Two bots (autofin_signal + momentum) on the same VPS must use
separate dry_run_state.<strategy>.json files. Otherwise each bot's
``_apply_dry_run_fills`` call clobbers the other bot's simulated cash
and positions — see the 2026-09-10 incident where autofin's EOD
applied 8 orders on top of momentum's state, producing a Telegram
message that showed momentum's positions in the autofin portfolio.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.dry_run_state import (
    DryRunState,
    _state_path,
    apply_orders_to_state,
    init_dry_run_state,
    load_dry_run_state,
    save_dry_run_state,
)


class PerStrategyStateIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/dry_run_isolation_test")
        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.tmp / "live").mkdir(parents=True, exist_ok=True)
        for suf in ("", ".autofin_signal", ".momentum"):
            p = self.tmp / "live" / f"dry_run_state{suf}.json"
            if p.exists():
                p.unlink()

    def test_state_path_per_strategy(self) -> None:
        """Each strategy gets its own state file."""
        p_autofin = _state_path(self.tmp, "autofin_signal")
        p_momentum = _state_path(self.tmp, "momentum")
        p_legacy = _state_path(self.tmp, "")
        self.assertNotEqual(p_autofin, p_momentum)
        self.assertEqual(p_autofin.name, "dry_run_state.autofin_signal.json")
        self.assertEqual(p_momentum.name, "dry_run_state.momentum.json")
        self.assertEqual(p_legacy.name, "dry_run_state.json")

    def test_init_creates_separate_files(self) -> None:
        """init_dry_run_state with different strategies creates
        separate files that don't interfere."""
        a = init_dry_run_state(self.tmp, strategy_name="autofin_signal")
        m = init_dry_run_state(self.tmp, strategy_name="momentum")
        # Both files exist, both default to 100M cash.
        self.assertTrue((self.tmp / "live" / "dry_run_state.autofin_signal.json").exists())
        self.assertTrue((self.tmp / "live" / "dry_run_state.momentum.json").exists())
        self.assertEqual(a.cash, 100_000_000)
        self.assertEqual(m.cash, 100_000_000)

    def test_apply_to_one_strategy_does_not_affect_other(self) -> None:
        """Apply momentum's orders; autofin's state should be untouched."""
        momentum = init_dry_run_state(self.tmp, strategy_name="momentum")
        autofin = init_dry_run_state(self.tmp, strategy_name="autofin_signal")
        apply_orders_to_state(
            momentum,
            [{"side": "BUY", "symbol": "PVT", "price": 21722, "quantity": 2900}],
            board_lot=100,
        )
        save_dry_run_state(momentum, self.tmp, strategy_name="momentum")
        # Reload autofin — should still be untouched.
        autofin2 = load_dry_run_state(self.tmp, strategy_name="autofin_signal")
        assert autofin2 is not None
        self.assertEqual(autofin2.cash, 100_000_000)
        self.assertEqual(autofin2.positions, {})

    def test_apply_skips_bracket_kind(self) -> None:
        """``_apply_dry_run_fills`` filters out SL/TP bracket orders
        so they don't fill at plan time and instantly close entries.

        Reproduces the 2026-09-10 incident where autofin emits a
        BUY + SL SELL pair; the SELL (kind='sl') was being applied
        as an immediate fill, locking in losses on every new entry.
        """
        from vietnam_algo.auto_bot import _apply_dry_run_fills
        import json
        from vietnam_algo.config import StrategyConfig
        from vietnam_algo.daily_engine import EngineConfig

        engine_config = EngineConfig(
            dataDir=self.tmp, strategy="autofin_signal",
            updateSymbols=False, updatePrices=False,
        )
        strategy_config = StrategyConfig()

        # Write a fake latest_orders.autofin_signal.json with a BUY
        # entry + SL bracket SELL.
        live_dir = self.tmp / "live"
        orders_path = live_dir / "latest_orders.autofin_signal.json"
        orders_path.write_text(json.dumps({
            "tradeDate": "2026-09-10",
            "items": [
                {"side": "NB", "symbol": "KLB", "price": 11950, "quantity": 1200,
                 "kind": "entry",
                 "reason": "autofin_buy:deal#1787270400:KLB:nav15pct"},
                {"side": "NS", "symbol": "KLB", "price": 11100, "quantity": 1200,
                 "kind": "sl",
                 "reason": "autofin_sl:deal#1787270400:KLB:11100"},
            ],
        }))
        result = {
            "latestOrdersJson": str(orders_path),
            "latestOrdersCsv": str(live_dir / "latest_orders.autofin_signal.csv"),
            "tradeDate": "2026-09-10",
        }
        _apply_dry_run_fills(
            result, engine_config, "0003937868", strategy_config,
            strategy_name="autofin_signal",
        )
        # Now read the autofin state — KLB should be a HOLDING
        # (entry applied, SL bracket NOT applied as immediate fill).
        state = load_dry_run_state(self.tmp, strategy_name="autofin_signal")
        assert state is not None
        self.assertIn("KLB", state.positions)
        self.assertEqual(state.positions["KLB"].quantity, 1200)
        # No realized loss from the SL bracket.
        self.assertEqual(state.realized_pnl, 0.0)

    def test_apply_skips_bracket_by_reason_prefix(self) -> None:
        """The autofin adapter doesn't always populate ``kind`` on the
        items it writes to ``latest_orders.autofin_signal.json`` —
        the SL/TP bracket arms are detectable via the ``reason``
        prefix. Without the secondary filter the apply would treat
        them as immediate fills."""
        from vietnam_algo.auto_bot import _apply_dry_run_fills
        import json
        from vietnam_algo.config import StrategyConfig
        from vietnam_algo.daily_engine import EngineConfig

        engine_config = EngineConfig(
            dataDir=self.tmp, strategy="autofin_signal",
            updateSymbols=False, updatePrices=False,
        )
        strategy_config = StrategyConfig()
        live_dir = self.tmp / "live"
        orders_path = live_dir / "latest_orders.autofin_signal.json"
        # Mimic the actual autofin latest_orders.json shape — no
        # ``kind`` field on either the entry or the SL arm.
        orders_path.write_text(json.dumps({
            "tradeDate": "2026-09-10",
            "items": [
                {"side": "NB", "symbol": "BAF", "price": 32050, "quantity": 900,
                 "reason": "autofin_buy:deal#1788393600:BAF:nav30pct"},
                {"side": "NS", "symbol": "BAF", "price": 29100, "quantity": 900,
                 "reason": "autofin_sl:deal#1788393600:BAF:29100"},
                {"side": "NB", "symbol": "ABW", "price": 14000, "quantity": 1000,
                 "reason": "autofin_buy:deal#1785456000:ABW:nav15pct"},
                {"side": "NS", "symbol": "ABW", "price": 12600, "quantity": 1000,
                 "reason": "autofin_user_sl:deal#1785456000:ABW:10%:12600"},
                {"side": "NB", "symbol": "PET", "price": 38000, "quantity": 300,
                 "reason": "autofin_buy:deal#1785456000:PET:nav15pct"},
                {"side": "NS", "symbol": "PET", "price": 34200, "quantity": 300,
                 "reason": "autofin_user_sl:deal#1785456000:PET:10%:34200"},
            ],
        }))
        result = {
            "latestOrdersJson": str(orders_path),
            "latestOrdersCsv": str(live_dir / "latest_orders.autofin_signal.csv"),
            "tradeDate": "2026-09-10",
        }
        _apply_dry_run_fills(
            result, engine_config, "0003937868", strategy_config,
            strategy_name="autofin_signal",
        )
        state = load_dry_run_state(self.tmp, strategy_name="autofin_signal")
        assert state is not None
        # All three entries applied, no SL bracket fills → no realized losses.
        self.assertEqual(set(state.positions.keys()), {"BAF", "ABW", "PET"})
        self.assertEqual(state.realized_pnl, 0.0)

    def test_momentum_apply_walks_full_backtest_csv(self) -> None:
        """Reproduces the 2026-09-10 incident where the bot wiped its
        state file (per-strategy isolation fix), then on Sept 10 EOD
        produced 0 filtered orders (backtest ends Sept 9) and the
        state ended up empty. The fix: the apply walks the FULL
        backtest orders CSV (1442 trades) and dedup skips already-
        applied ones, so the state self-heals from any wipe/restart.
        """
        import csv as _csv
        import io
        from vietnam_algo.auto_bot import _apply_dry_run_fills
        from vietnam_algo.config import StrategyConfig
        from vietnam_algo.daily_engine import EngineConfig

        engine_config = EngineConfig(
            dataDir=self.tmp, strategy="momentum",
            updateSymbols=False, updatePrices=False,
        )
        strategy_config = StrategyConfig()
        live_dir = self.tmp / "live"
        # Write a small but realistic orders csv with one entry + exit.
        orders_csv = self.tmp / "backtests" / "liquid_momentum_rotation_orders.csv"
        orders_csv.parent.mkdir(parents=True, exist_ok=True)
        with orders_csv.open("w", encoding="utf-8", newline="") as f:
            writer = _csv.DictWriter(
                f,
                fieldnames=["orderId", "date", "symbol", "side", "price",
                            "quantity", "value", "fee", "reason"],
            )
            writer.writeheader()
            writer.writerow({
                "orderId": "1", "date": "2026-09-08", "symbol": "PVT",
                "side": "BUY", "price": "21722", "quantity": "2900",
                "value": "62992930", "fee": "94489",
                "reason": "top_rank",
            })
            writer.writerow({
                "orderId": "2", "date": "2026-09-09", "symbol": "PVT",
                "side": "SELL", "price": "21850", "quantity": "2900",
                "value": "63365000", "fee": "95047",
                "reason": "rank_exit",
            })

        # Empty latest_orders.json (mimics a Sept 10 cycle where the
        # per-date filter returns 0 rows).
        (live_dir / "latest_orders.json").write_text(json.dumps({
            "tradeDate": "2026-09-10", "items": []
        }))
        result = {
            "latestOrdersJson": str(live_dir / "latest_orders.json"),
            "latestOrdersCsv": str(orders_csv),
            # In production this is the FULL backtest orders csv
            # exposed by ``generate_live_orders`` under the "orders"
            # key. The dry-run apply walks it instead of the
            # per-date filtered JSON so the state self-heals from
            # a wiped state file or a backtest that ends "yesterday".
            "orders": str(orders_csv),
            "tradeDate": "2026-09-10",
            "count": 0,
        }
        _apply_dry_run_fills(
            result, engine_config, "0003937868", strategy_config,
            strategy_name="momentum",
        )
        state = load_dry_run_state(self.tmp, strategy_name="momentum")
        assert state is not None
        # Both backtest trades applied: BUY then SELL of the same 2900
        # shares. PVT position is now closed; realized P&L from the
        # round-trip is (21850 - 21722) * 2900 = 371,200.
        self.assertEqual(state.positions, {})
        self.assertAlmostEqual(state.realized_pnl, 371_200, places=-1)
        # Cash: started at 100M, BUY cost ≈ 63M, SELL proceeds ≈ 63.36M.
        # Plus 15bps fees on each leg, so cash ≈ 100M - 94489 + ...
        self.assertGreater(state.cash, 99_000_000)


if __name__ == "__main__":
    unittest.main()
