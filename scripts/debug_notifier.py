"""Debug the notifier lookup."""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/root/hft-trading-algo")
from vietnam_algo.notifier import eod_summary_message
from vietnam_algo.auto_bot import _apply_dry_run_fills, _build_portfolio_snapshot
from vietnam_algo.config import StrategyConfig
from vietnam_algo.daily_engine import EngineConfig

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
_apply_dry_run_fills(result, engine_config, "0003937868", strategy_config)
snap = _build_portfolio_snapshot(None, "0003937868", engine_config, live_submit=False)

raw = json.loads(state_path.read_text())
last_marker = None
for t in raw.get("closed_trades", []):
    if isinstance(t, dict) and t.get("note", "").startswith("_applied:"):
        last_marker = t
print("=== last marker ===")
print(json.dumps(last_marker, indent=2))

print()
print("=== lookup keys ===")
for sk in last_marker.get("skipped") or []:
    k = (sk.get("symbol"), sk.get("side"), sk.get("price"), sk.get("requested_qty") or sk.get("qty"))
    print(f"  skipped key: {k}")
for cl in last_marker.get("clipped") or []:
    k = (cl.get("symbol"), cl.get("side"), cl.get("price"), cl.get("requested_qty") or cl.get("qty"))
    print(f"  clipped key: {k}")

print()
print("=== order keys ===")
for o in payload["items"]:
    print(f"  {(o['symbol'], o['side'], float(o['price']), float(o['quantity']))}")

print()
print("=== final EOD SUMMARY ===")
msg = eod_summary_message(result, portfolio=snap)
print(msg)
