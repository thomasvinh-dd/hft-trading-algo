"""Verify the dry-run apply works on VPS with the actual backtest CSV."""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/root/hft-trading-algo")

from vietnam_algo.auto_bot import _apply_dry_run_fills
from vietnam_algo.config import StrategyConfig
from vietnam_algo.daily_engine import EngineConfig
from vietnam_algo.dry_run_state import load_dry_run_state

data_dir = Path("/root/hft-trading-algo/vietnam_algo/data")
ec = EngineConfig(dataDir=data_dir, strategy="momentum", updateSymbols=False, updatePrices=False)
sc = StrategyConfig()

result = {
    "result": str(data_dir / "backtests" / "liquid_momentum_rotation.json"),
    "latestOrdersJson": str(data_dir / "live" / "latest_orders.json"),
    "latestOrdersCsv": str(data_dir / "backtests" / "liquid_momentum_rotation_orders.csv"),
    "orders": str(data_dir / "backtests" / "liquid_momentum_rotation_orders.csv"),
    "tradeDate": "2026-09-10",
    "count": 0,
}
print(f"orders csv exists: {Path(result['latestOrdersCsv']).exists()}")
print(f"latest orders json: {Path(result['latestOrdersJson']).exists()}")

# Wipe state for clean test
state_path = data_dir / "live" / "dry_run_state.momentum.json"
if state_path.exists():
    state_path.unlink()
    print("wiped momentum state")

_apply_dry_run_fills(result, ec, "0003937868", sc, strategy_name="momentum")

state = load_dry_run_state(data_dir, strategy_name="momentum")
print(f"\n=== After apply ===")
print(f"cash: {state.cash}")
print(f"positions: {list(state.positions.keys())}")
print(f"realized_pnl: {state.realized_pnl}")
print(f"closed_trades count: {len(state.closed_trades)}")
print(f"applied marker: {[t for t in state.closed_trades if isinstance(t, dict) and t.get('note', '').startswith('_applied:')]}")
