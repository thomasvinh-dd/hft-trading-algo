import json
import sys
from pathlib import Path

sys.path.insert(0, "/root/hft-trading-algo")
from vietnam_algo.auto_bot import _apply_dry_run_fills, _build_portfolio_snapshot
from vietnam_algo.config import StrategyConfig
from vietnam_algo.daily_engine import EngineConfig
from vietnam_algo.notifier import eod_summary_message

data_dir = Path("/root/hft-trading-algo/vietnam_algo/data")
state_path = data_dir / "live" / "dry_run_state.json"
state_path.unlink(missing_ok=True)

engine_config = EngineConfig(dataDir=data_dir, strategy="momentum", updateSymbols=False, updatePrices=False)
strategy_config = StrategyConfig()

orders_path = data_dir / "live" / "latest_orders.json"
payload = json.loads(orders_path.read_text())
result = {
    "latestOrdersJson": str(orders_path),
    "latestOrdersCsv": str(data_dir / "live" / "latest_orders.csv"),
    "tradeDate": payload.get("tradeDate"),
    "count": payload.get("count"),
    "strategy": "momentum",
    "metrics": {},
}

p = Path(result["latestOrdersCsv"])
print(f"latestOrdersCsv parent: {p.parent}")
print(f"expected state path: {p.parent / 'dry_run_state.json'}")
_apply_dry_run_fills(result, engine_config, "0003937868", strategy_config)

sp = p.parent / "dry_run_state.json"
print(f"state exists? {sp.exists()}")
if sp.exists():
    raw = json.loads(sp.read_text())
    for t in raw.get("closed_trades", []):
        if isinstance(t, dict) and t.get("note", "").startswith("_applied:"):
            print(f"clipped: {t.get('clipped')}")

snap = _build_portfolio_snapshot(None, "0003937868", engine_config, live_submit=False)
print(f"snap.mode: {snap.get('mode')}")
msg = eod_summary_message(result, portfolio=snap)
print(msg)
