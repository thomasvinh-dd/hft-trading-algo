"""Manually trigger the EOD flow for momentum so we can see the new
apply_orders_to_state behavior in action. Equivalent to what auto_bot
would do at 15:15 ICT, minus the 09:16 submit cycle."""
import sys
from pathlib import Path

sys.path.insert(0, "/root/hft-trading-algo")

from vietnam_algo.bots.runner import _apply_dry_run_fills
from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig, generate_live_orders
from vietnam_algo.libs.notifier import eod_summary_message
from vietnam_algo.libs.portfolio import build_portfolio_snapshot

data_dir = Path("/root/hft-trading-algo/vietnam_algo/data")
engine_config = EngineConfig(
    dataDir=data_dir,
    strategy="momentum",
    updateSymbols=False,
    updatePrices=False,
)
strategy_config = StrategyConfig()

print("=== Running EOD generate_live_orders ===")
result = generate_live_orders(engine_config, strategy_config, trade_date="2026-09-09")
print(f"tradeDate: {result.get('tradeDate')}, count: {result.get('count')}")
for o in result.get("items", [])[:10]:
    print(f"  {o}")

print()
print("=== Applying to dry-run state ===")
_apply_dry_run_fills(result, engine_config, "0003937868", strategy_config)

# Build portfolio snapshot (uses dry-run state in this mode)
from vietnam_algo.bots.runner import _build_portfolio_snapshot
snap = _build_portfolio_snapshot(None, "0003937868", engine_config, live_submit=False)

print("\n=== Final portfolio ===")
msg = eod_summary_message(result, portfolio=snap)
print(msg)
