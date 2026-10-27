import os
import sys

# Set PYTHONPATH explicitly
os.environ["PYTHONPATH"] = "/root/hft-trading-algo"
sys.path.insert(0, "/root/hft-trading-algo")

# Load telegram + dnse env
for envfile in ["/root/hft-trading-algo/vietnam_algo/env/.env.dnse-secrets",
                "/root/hft-trading-algo/vietnam_algo/env/.env.telegram"]:
    if os.path.exists(envfile):
        for line in open(envfile):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                os.environ[k.strip()] = v

# Reset state for momentum
import json
from pathlib import Path
momentum_state = Path("/root/hft-trading-algo/vietnam_algo/data/live/auto_bot_state.momentum.json")
d = json.loads(momentum_state.read_text())
d["eodDate"] = "2026-09-07"
momentum_state.write_text(json.dumps(d, indent=2))

dry_state = Path("/root/hft-trading-algo/vietnam_algo/data/live/dry_run_state.json")
if dry_state.exists():
    dry_state.unlink()

from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig, generate_live_orders
from vietnam_algo.bots.runner import (
    _build_portfolio_snapshot,
    _apply_dry_run_fills,
)
from vietnam_algo.libs.notifier import notify, eod_summary_message

cfg = EngineConfig(
    dataDir=Path("/root/hft-trading-algo/vietnam_algo/data"),
    strategy="momentum",
    updateSymbols=False,
    updatePrices=False,
    maxSymbols=0,
)
strategy_config = StrategyConfig()
result = generate_live_orders(
    cfg, strategy_config,
    strategy_name="momentum",
    notify_telegram=False,
)
print(f"momentum orderCount: {result.get('count')}", flush=True)
_apply_dry_run_fills(result, cfg, "0003937868")
portfolio = _build_portfolio_snapshot(None, "0003937868", cfg, live_submit=False)
print(f"NAV: {portfolio.get('totals', {}).get('portfolioValue'):,.0f}", flush=True)
msg = eod_summary_message(result, portfolio=portfolio)
print("=== Momentum MSG ===", flush=True)
print(msg, flush=True)
print("=== Sending ===", flush=True)
res = notify(msg)
print(f"OK={res.get('ok') if isinstance(res, dict) else res}", flush=True)
