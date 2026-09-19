"""Test that ``_apply_dry_run_fills`` rounds scaled quantities
down to the board lot so the resulting position has a valid
Vietnamese lot size.

Pre-2026-09-19 the runner scaled by ``qty_scale = dry_nav /
backtest_initial_cash`` but did NOT round to the lot boundary.
A 1B-VND backtest emits BUY 6200 BSR; scaling by 0.1 = 620
(6.2 lots — invalid). The bot would report qty=620 in the
portfolio while the order file still showed 6200, confusing
the operator.

The fix: round scaled qty down to the nearest board lot.
"""
import json
from pathlib import Path

from vietnam_algo.bots.runner import _apply_dry_run_fills
from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig
from vietnam_algo.libs.dry_run_state import (
    init_dry_run_state,
    load_dry_run_state,
)


def _write_latest_orders(tmp: Path, items: list[dict]) -> Path:
    """Write a latest_orders.<strategy>.json file the way the
    backtest / EOD pipeline would."""
    live_dir = tmp / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    path = live_dir / "latest_orders.test.json"
    path.write_text(json.dumps({
        "tradeDate": "2026-09-19",
        "items": items,
    }))
    return path


def test_apply_dry_run_fills_rounds_scaled_qty_to_board_lot(tmp_path: Path) -> None:
    """6200 BSR × 0.1 scale = 620 (invalid). Should round down to 600."""
    engine_config = EngineConfig(
        dataDir=tmp_path, strategy="test",
        updateSymbols=False, updatePrices=False,
    )
    strategy_config = StrategyConfig(boardLotSize=100)
    # Use a pre-existing backtest result so the runner computes
    # qty_scale from its initialCash. We patch the saved result.
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    (backtests_dir / "test_backtest.json").write_text(json.dumps({
        "equityCurve": [{"date": "2026-01-01", "cash": 1_000_000_000}],
        "trades": [],
    }))
    # Dry-run state initial_nav = 100M → scale = 0.1
    init_dry_run_state(tmp_path, strategy_name="test",
                       initial_nav=100_000_000)
    orders_path = _write_latest_orders(tmp_path, [
        {"date": "2026-09-19", "symbol": "BSR", "side": "BUY",
         "quantity": 6200, "price": 30980,
         "kind": "entry", "reason": "top_rank", "value": 192076000},
        {"date": "2026-09-19", "symbol": "BVH", "side": "BUY",
         "quantity": 2700, "price": 69969,
         "kind": "entry", "reason": "top_rank", "value": 188916300},
    ])
    result_payload = {
        "latestOrdersJson": str(orders_path),
        "result": str(backtests_dir / "test_backtest.json"),
        "tradeDate": "2026-09-19",
        "items": [],  # not used; loader reads from latestOrdersJson
    }
    _apply_dry_run_fills(
        result_payload,
        engine_config,
        account_no="0003937868",
        strategy_config=strategy_config,
        strategy_name="test",
    )
    # The in-memory scaled quantities get applied to the dry-run
    # state. The on-disk latest_orders.json keeps the raw
    # 6200/2700 (backtest output) — that's what triggered the
    # user-visible mismatch on 2026-09-19. The fix is twofold:
    #   1. Round scaled qty DOWN to the lot boundary (this test)
    #   2. Override initialCash so the backtest emits native lot
    #      sizes (covered by the EOD path test in test_dry_run_isolation)
    state = load_dry_run_state(tmp_path, strategy_name="test")
    pos_qtys = {sym: p.quantity for sym, p in state.positions.items()}
    # 6200 × 0.1 = 620 → floor to 600. 2700 × 0.1 = 270 → floor to 200.
    assert pos_qtys == {"BVH": 200, "BSR": 600}, (
        f"Scaled qtys should round DOWN to board lots, got {pos_qtys}"
    )


def test_apply_marker_dedup_reapplies_when_orders_change(tmp_path: Path) -> None:
    """When the backtest is rerun with different parameters for the
    same trade_date, the per-order dedup keys differ — the new
    orders should still apply, not be blocked by the trade_date
    marker.

    Pre-2026-09-19 the marker dedup was coarse and skipped ALL
    orders for a given trade_date once any had been applied. The
    momentum EOD at 100M NAV printed BUY 600 BSR (new orders)
    but the dry-run state stayed at qty=620 from the previous
    1B-NAV backtest. The fix: when the marker is present, fall
    through to per-order dedup so new orders apply.
    """
    from vietnam_algo.bots.runner import _apply_dry_run_fills
    from vietnam_algo.libs.config import StrategyConfig
    from vietnam_algo.libs.daily_engine import EngineConfig
    from vietnam_algo.libs.dry_run_state import (
        init_dry_run_state,
        load_dry_run_state,
    )

    engine_config = EngineConfig(
        dataDir=tmp_path, strategy="test",
        updateSymbols=False, updatePrices=False,
    )
    strategy_config = StrategyConfig(boardLotSize=100)
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    # Backtest initialCash = dry-run initial_nav so scaling is 1.0
    # and the order quantities pass through unchanged. This keeps
    # the test focused on the marker dedup behavior, not the
    # scaling math.
    (backtests_dir / "test_backtest.json").write_text(json.dumps({
        "equityCurve": [{"date": "2026-01-01", "cash": 100_000_000}],
        "trades": [],
    }))
    init_dry_run_state(tmp_path, strategy_name="test",
                       initial_nav=100_000_000)

    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    orders_path = live_dir / "latest_orders.test.json"
    # First apply: BUY 600 BSR @ 28679 (matches dry-run NAV).
    orders_path.write_text(json.dumps({
        "tradeDate": "2026-09-15",
        "items": [
            {"date": "2026-09-15", "symbol": "BSR", "side": "BUY",
             "quantity": 600, "price": 28679, "kind": "entry"},
        ],
    }))
    _apply_dry_run_fills(
        {"latestOrdersJson": str(orders_path), "tradeDate": "2026-09-15"},
        engine_config, account_no="0000000001",
        strategy_config=strategy_config, strategy_name="test",
    )

    # Sanity check: first apply worked.
    state = load_dry_run_state(tmp_path, strategy_name="test")
    assert "BSR" in state.positions
    assert state.positions["BSR"].quantity == 600
    first_key = next(iter(state.applied_keys))
    assert "600" in first_key and "28679" in first_key

    # Second apply: SAME trade_date, but the backtest now emits a
    # DIFFERENT order (different qty + a second position). The
    # marker _applied:2026-09-15 is already in closed_trades.
    # Pre-fix: dedup would skip everything. Post-fix: dedup falls
    # through to per-order keys, sees new keys, applies them.
    orders_path.write_text(json.dumps({
        "tradeDate": "2026-09-15",
        "items": [
            {"date": "2026-09-15", "symbol": "BSR", "side": "SELL",
             "quantity": 600, "price": 31000, "kind": "exit"},
            {"date": "2026-09-15", "symbol": "ABB", "side": "BUY",
             "quantity": 1100, "price": 17317, "kind": "entry"},
        ],
    }))
    _apply_dry_run_fills(
        {"latestOrdersJson": str(orders_path), "tradeDate": "2026-09-15"},
        engine_config, account_no="0000000001",
        strategy_config=strategy_config, strategy_name="test",
    )

    state = load_dry_run_state(tmp_path, strategy_name="test")
    # The new BUY 1100 ABB should have applied.
    assert any(
        "ABB" in k and "1100" in k
        for k in state.applied_keys
    ), f"New ABB order not applied. applied_keys={list(state.applied_keys)}"
