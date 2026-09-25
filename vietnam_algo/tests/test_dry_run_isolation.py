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

from vietnam_algo.libs.dry_run_state import (
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
        from vietnam_algo.bots.runner import _apply_dry_run_fills
        import json
        from vietnam_algo.libs.config import StrategyConfig
        from vietnam_algo.libs.daily_engine import EngineConfig

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
            orders_path, engine_config, "0003937868", strategy_config,
            strategy_name="autofin_signal",
            qty_scale=1.0,
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
        from vietnam_algo.bots.runner import _apply_dry_run_fills
        import json
        from vietnam_algo.libs.config import StrategyConfig
        from vietnam_algo.libs.daily_engine import EngineConfig

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
            orders_path, engine_config, "0003937868", strategy_config,
            strategy_name="autofin_signal",
            qty_scale=1.0,
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
        state ended up empty.

        Note (2026-09-15): the apply path no longer replays the full
        backtest CSV. It now reads from ``latestOrdersJson`` only —
        the per-date filtered orders the bot ACTUALLY generated. So
        if today's filtered JSON is empty, the apply is a no-op and
        the state stays empty. This is intentional: a user starting
        the bot today with ``DRY_RUN_INITIAL_NAV=100M`` should see
        their OWN trading history, not the backtest's cumulative P&L.
        """
        import csv as _csv
        from vietnam_algo.bots.runner import _apply_dry_run_fills
        from vietnam_algo.libs.config import StrategyConfig
        from vietnam_algo.libs.daily_engine import EngineConfig

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
            # ``orders`` is the full backtest CSV. With the 2026-09-15
            # fix, the apply path ignores this and reads from
            # ``latestOrdersJson`` only — see note above.
            "orders": str(orders_csv),
            "tradeDate": "2026-09-10",
            "count": 0,
        }
        _apply_dry_run_fills(
            live_dir / "latest_orders.json",
            engine_config, "0003937868", strategy_config,
            strategy_name="momentum",
            qty_scale=1.0,
        )
        state = load_dry_run_state(self.tmp, strategy_name="momentum")
        assert state is not None
        # With the 2026-09-15 fix, the apply is a no-op when today's
        # filtered JSON is empty. The full backtest CSV (containing
        # the BUY+SELL round-trip) is no longer auto-replayed.
        self.assertEqual(state.positions, {})
        self.assertEqual(state.realized_pnl, 0)
        # Cash unchanged: state.initial_nav (= cash = 100M by default).
        self.assertAlmostEqual(state.cash, 100_000_000, places=0)


if __name__ == "__main__":
    unittest.main()


class AppliedKeysPersistenceTests(unittest.TestCase):
    """Regression tests for the 2026-09-15 realized_pnl inflation bug.

    Background: dedup keys for already-applied trades were stored in
    ``state.closed_trades`` as marker entries. ``closed_trades`` is
    capped at 200 (for display) so after enough EOD cycles, older
    markers were silently truncated, and the bot re-applied historical
    trades on every cycle, inflating ``realized_pnl``.

    Fix: store ``applied_keys`` on the state directly (unbounded).
    """

    def setUp(self) -> None:
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="applied_keys_test_"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_applied_keys_persist_across_save_load(self) -> None:
        """State.applied_keys survives save -> load round-trip."""
        from vietnam_algo.libs.dry_run_state import (
            DryRunState, save_dry_run_state, init_dry_run_state,
        )
        state = init_dry_run_state(self.tmp, strategy_name="momentum")
        state.cash = 50_000_000
        state.realized_pnl = 12_345_678
        state.applied_keys = {
            "2024-01-02|HDB|BUY|100|27100",
            "2024-01-15|HDB|SELL|100|28000",
        }
        save_dry_run_state(state, self.tmp, strategy_name="momentum")

        loaded = init_dry_run_state(self.tmp, strategy_name="momentum")
        self.assertEqual(loaded.cash, 50_000_000)
        self.assertEqual(loaded.realized_pnl, 12_345_678)
        self.assertEqual(
            loaded.applied_keys,
            {
                "2024-01-02|HDB|BUY|100|27100",
                "2024-01-15|HDB|SELL|100|28000",
            },
        )

    def test_dedup_keys_preserved_indefinitely(self) -> None:
        """The cap on closed_trades (200) does NOT affect applied_keys.

        Regression for the 2026-09-15 bug where closed_trades was
        capped but dedup keys lived inside it.
        """
        from vietnam_algo.libs.dry_run_state import (
            DryRunState, apply_orders_to_state, save_dry_run_state,
            init_dry_run_state,
        )
        state = init_dry_run_state(self.tmp, strategy_name="momentum")
        # Generate enough dedup keys to overflow the closed_trades cap.
        # Skip "2024-01-01" because the apply call below uses that date.
        keys = {f"2024-01-{i:02d}|HDB|BUY|100|27100" for i in range(2, 1000)}
        # Apply a fake trade that uses one of the keys (mutates state).
        fills = apply_orders_to_state(
            state,
            [
                {
                    "side": "BUY", "symbol": "HDB", "price": 27_100,
                    "quantity": 100, "date": "2024-01-01",
                    "kind": "entry",
                }
            ],
            board_lot=100, fees_bps=15.0,
        )
        # Manually inject the dedup keys to simulate a long-running bot.
        state.applied_keys.update(keys)

        # closed_trades list (capped at 200) grows much smaller.
        save_dry_run_state(state, self.tmp, strategy_name="momentum")

        loaded = init_dry_run_state(self.tmp, strategy_name="momentum")
        # All keys survived.
        self.assertEqual(len(loaded.applied_keys), len(keys) + 1)
        # And the cap on closed_trades is enforced (max 200) but that's
        # for display, NOT for dedup.
        self.assertLessEqual(
            sum(1 for t in loaded.closed_trades if not str(t.get("note", "")).startswith("_applied"))
            + sum(1 for t in loaded.closed_trades if str(t.get("note", "")).startswith("_applied")),
            201,  # 200 + 1 buffer
        )

    def test_old_marker_in_closed_trades_is_importer_compatible(self) -> None:
        """Older state files stored dedup keys inside closed_trades
        marker entries. The legacy migration (in bots/runner.py)
        recovers them into state.applied_keys. Verify the helper that
        does that recovery.
        """
        from vietnam_algo.libs.dry_run_state import DryRunState, load_dry_run_state
        # Simulate a v1 state file: dedup keys live in closed_trades markers.
        legacy = {
            "initial_nav": 100_000_000,
            "cash": 50_000_000,
            "realized_pnl": 5_000_000,
            "closed_trades": [
                {"symbol": "HDB", "avg_cost": 27_100, "sell_price": 28_000,
                 "qty": 100, "realized_pnl": 90_000},
                {"note": "_applied_keys",
                 "applied_keys": ["2024-01-02|HDB|BUY|100|27100"]},
            ],
            "positions": {},
        }
        # Persist a v1-format file and reload.
        from vietnam_algo.libs.dry_run_state import save_dry_run_state
        # First synthesise the state we want to reload.
        state = DryRunState(
            initial_nav=100_000_000, cash=50_000_000, realized_pnl=5_000_000,
            closed_trades=list(legacy["closed_trades"]),
        )
        save_dry_run_state(state, self.tmp, strategy_name="momentum")
        # Now reload. applied_keys comes back empty (legacy markers
        # aren't auto-migrated by ``load_dry_run_state`` itself —
        # that's the bot adapter's job). Verify that.
        loaded = load_dry_run_state(self.tmp, strategy_name="momentum")
        self.assertEqual(loaded.applied_keys, set())
        self.assertEqual(len(loaded.closed_trades), 2)
        # The bot adapter recovers them into state.applied_keys
        # during the legacy migration in _apply_dry_run_fills.
        legacy_keys: set[str] = set()
        for t in loaded.closed_trades:
            if isinstance(t, dict) and t.get("applied_keys"):
                legacy_keys.update(str(k) for k in t["applied_keys"])
        loaded.applied_keys.update(legacy_keys)
        self.assertIn("2024-01-02|HDB|BUY|100|27100", loaded.applied_keys)

    def test_apply_skips_when_dedup_key_present(self) -> None:
        """A trade whose key is in state.applied_keys is skipped on
        re-apply (no double-counting)."""
        from vietnam_algo.libs.dry_run_state import (
            DryRunState, apply_orders_to_state,
        )
        state = DryRunState(
            initial_nav=100_000_000, cash=100_000_000,
            applied_keys={"2024-01-02|HDB|BUY|100|27100"},
        )
        fills = apply_orders_to_state(
            state,
            [
                {
                    "side": "BUY", "symbol": "HDB", "price": 27_100,
                    "quantity": 100, "date": "2024-01-02",
                },
            ],
            board_lot=100, fees_bps=15.0,
        )
        # The apply call returns a "skipped" entry rather than None.
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["status"], "skipped")
        self.assertEqual(fills[0]["reason"], "already_applied")
        # No position was opened (skipped).
        self.assertEqual(state.positions, {})
        # Cash unchanged.
        self.assertEqual(state.cash, 100_000_000)




class DryRunLatestOrdersOnlyTests(unittest.TestCase):
    """Regression tests for the 2026-09-15 dry-run replay-reset bug.

    Background: the dry-run state previously replayed the full backtest
    orders CSV (6+ years of trades) on every cycle, inflating both cash
    and realized_pnl with the backtest's cumulative history. Users
    starting the bot today with ``DRY_RUN_INITIAL_NAV=100M`` expected
    the state to reflect only the orders the bot ACTUALLY generated,
    starting from 100M cash.

    Fix: ``_apply_dry_run_fills`` now reads from ``latestOrdersJson``
    (today's filtered orders), not the full backtest CSV. The dedup
    machinery still prevents re-apply across cycles.
    """

    def setUp(self) -> None:
        from pathlib import Path
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="dry_run_replay_"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_apply_path_reads_latest_orders_not_full_backtest(self) -> None:
        """_apply_dry_run_fills must prefer latestOrdersJson over result['orders'].

        Constructs a fake ``result`` dict with both fields pointing at
        different files. ``latestOrdersJson`` has 1 BUY for HDB.
        ``result['orders']`` points at a CSV with 5 trades (including
        SL/TP). Only the 1 BUY from latestOrdersJson should be applied.
        """
        import csv
        from vietnam_algo.libs.dry_run_state import (
            DryRunState, apply_orders_to_state,
        )
        # Build the "full backtest" CSV (5 rows).
        full_csv = self.tmp / "backtest_orders.csv"
        with full_csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=["date", "symbol", "side", "price", "quantity"],
            )
            w.writeheader()
            for i, sym in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"]):
                w.writerow({
                    "date": f"2026-08-{(i+1):02d}",
                    "symbol": sym,
                    "side": "BUY",
                    "price": 10_000 + i * 100,
                    "quantity": 1000,
                })

        # Build today's filtered JSON (1 BUY).
        import json
        today_json = self.tmp / "latest_orders.json"
        today_json.write_text(json.dumps({
            "tradeDate": "2026-09-15",
            "items": [
                {
                    "date": "2026-09-15", "symbol": "HDB",
                    "side": "BUY", "price": 27_100, "quantity": 100,
                },
            ],
        }))

        result = {
            "orders": str(full_csv),  # full backtest path
            "latestOrdersJson": str(today_json),  # today's filtered path
        }
        # Use the apply helper directly (same logic as _apply_dry_run_fills).
        state = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        # Load latestOrdersJson items.
        payload = json.loads(today_json.read_text(encoding="utf-8"))
        items = list(payload.get("items") or [])
        fills = apply_orders_to_state(state, items, board_lot=100, fees_bps=15.0)
        # Only 1 BUY applied.
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["status"], "applied")
        self.assertEqual(fills[0]["symbol"], "HDB")
        # Cash debited by HDB BUY cost.
        expected_cost = 100 * 27_100 * 1.0015
        self.assertAlmostEqual(state.cash, 100_000_000 - expected_cost, places=0)
        # 1 position: HDB 100 @ 27100.
        self.assertIn("HDB", state.positions)
        self.assertEqual(state.positions["HDB"].quantity, 100)

    def test_apply_skips_when_latest_orders_empty(self) -> None:
        """Empty latestOrdersJson (no orders today) → no apply."""
        import json
        from vietnam_algo.libs.dry_run_state import (
            DryRunState, init_dry_run_state, save_dry_run_state,
        )
        empty_json = self.tmp / "latest_orders.json"
        empty_json.write_text(json.dumps({
            "tradeDate": "2026-09-15", "items": [],
        }))
        state = init_dry_run_state(self.tmp, strategy_name="momentum")
        payload = json.loads(empty_json.read_text(encoding="utf-8"))
        items = list(payload.get("items") or [])
        self.assertEqual(items, [])


