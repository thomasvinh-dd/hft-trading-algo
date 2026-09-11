"""Test the new apply_orders_to_state behavior in isolation — no
backtest, no price sync, just verify the apply logic on the latest
orders CSV."""
import sys
import json
from pathlib import Path

sys.path.insert(0, "/root/hft-trading-algo")

from vietnam_algo.config import StrategyConfig
from vietnam_algo.daily_engine import EngineConfig
from vietnam_algo.dry_run_state import (
    DryRunState,
    init_dry_run_state,
    save_dry_run_state,
    apply_orders_to_state,
    build_dry_run_snapshot,
)

data_dir = Path("/root/hft-trading-algo/vietnam_algo/data")
cfg = StrategyConfig()

# Keep existing state (don't reset) so we can verify the dedup behavior
state_path = data_dir / "live" / "dry_run_state.json"
state = init_dry_run_state(data_dir)
print(f"Initial: cash={state.cash:,.0f}, positions={len(state.positions)}")
for sym, p in state.positions.items():
    print(f"  {sym}: qty={p.quantity} avg={p.avg_price:,.0f}")

# Read the latest orders (today's 2 BUYs)
orders_path = data_dir / "live" / "latest_orders.json"
orders = json.loads(orders_path.read_text())["items"]
print(f"Orders to apply: {len(orders)}")
for o in orders:
    print(f"  {o['side']} {o['quantity']} {o['symbol']} @ {float(o['price']):,.0f}")

# Apply with new board-lot + fee-aware logic
fills = apply_orders_to_state(
    state,
    orders,
    board_lot=cfg.boardLotSize,
    fees_bps=cfg.feeRate * 10_000,
)

print()
print(f"=== Fills ({len(fills)}) ===")
for f in fills:
    print(f"  status={f['status']:>10}  side={f.get('side')}  {f['symbol']} qty={f.get('qty')} price={f.get('price'):,.0f}",
          end="")
    if f.get("requested_qty"):
        print(f"  (requested {f['requested_qty']})", end="")
    if f.get("reason"):
        print(f"  reason={f['reason']}", end="")
    print()

print()
print(f"=== Final state ===")
print(f"cash: {state.cash:,.0f}")
print(f"positions: {len(state.positions)}")
for sym, p in state.positions.items():
    print(f"  {sym}: qty={p.quantity} avg={p.avg_price:,.0f} cost_basis={p.cost_basis:,.0f}")

# Save state so the bot's next cycle can see it
save_dry_run_state(state, data_dir)
print(f"\nState saved to {state_path}")
