"""Test the EOD summary flow without running the full backtest.
We use the existing latest_orders.json to verify the new
applied/clipped/skipped annotations in the Telegram summary."""
import sys
import json
from pathlib import Path

sys.path.insert(0, "/root/hft-trading-algo")

from vietnam_algo.bots.runner import _apply_dry_run_fills, _build_portfolio_snapshot
from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig
from vietnam_algo.libs.notifier import eod_summary_message

data_dir = Path("/root/hft-trading-algo/vietnam_algo/data")
state_path = data_dir / "live" / "dry_run_state.json"

# Reset state for a clean test
state_path.unlink(missing_ok=True)

engine_config = EngineConfig(
    dataDir=data_dir, strategy="momentum", updateSymbols=False, updatePrices=False,
)
strategy_config = StrategyConfig()

# Load the existing latest_orders.json
orders_path = data_dir / "live" / "latest_orders.json"
payload = json.loads(orders_path.read_text())
print(f"loaded {len(payload.get('items', []))} orders from latest_orders.json")

# Build a result dict that mimics what generate_live_orders returns
result = {
    "latestOrdersJson": str(orders_path),
    "latestOrdersCsv": str(data_dir / "live" / "latest_orders.csv"),
    "tradeDate": payload.get("tradeDate"),
    "count": payload.get("count"),
    "strategy": "momentum",
    "metrics": {},
}

print("\n=== Applying to dry-run state ===")
_apply_dry_run_fills(result, engine_config, "0003937868", strategy_config)

print("\n=== Building portfolio snapshot ===")
snap = _build_portfolio_snapshot(None, "0003937868", engine_config, live_submit=False)
print(f"cash={snap['balance']['totalCash']:,}")
print(f"positions={len(snap['positions'])}")
for p in snap["positions"]:
    print(f"  {p['symbol']}: qty={p['quantity']} avg={p['avgPrice']:,.0f} MV={p['marketValue']:,}")

print("\n=== EOD SUMMARY (Telegram format) ===")
msg = eod_summary_message(result, portfolio=snap)
print(msg)
