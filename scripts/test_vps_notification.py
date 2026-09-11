"""Test notification polling in VPS after deploy."""
import sys, json
sys.path.insert(0, "/root/hft-trading-algo")
from pathlib import Path
from vietnam_algo.config import StrategyConfig
from vietnam_algo.daily_engine import EngineConfig
from vietnam_algo.strategy_registry import STRATEGY_REGISTRY

cfg = EngineConfig(
    dataDir=Path("/root/hft-trading-algo/vietnam_algo/data"),
    strategy="autofin_signal",
    updateSymbols=False, updatePrices=False,
)
adapter = STRATEGY_REGISTRY.get("autofin_signal")
adapter._load_state(cfg.dataDir)
print(f"Loaded: {len(adapter._our_deals)} tracked deals")
print(f"last_notification_id = {adapter._last_notification_id!r}")
print(f"notified_event_ids count: {len(adapter._notified_event_ids)}")

result = adapter.generate_orders(cfg, StrategyConfig())
print(f"orderCount = {result.get('count')}")

csv = Path("/root/hft-trading-algo/vietnam_algo/data/live/latest_orders.autofin_signal.json")
if csv.exists():
    data = json.loads(csv.read_text())
    items = data.get("items", [])
    event_orders = [
        o for o in items
        if "autofin_partial_event" in str(o.get("reason", ""))
        or "autofin_close_event" in str(o.get("reason", ""))
    ]
    print(f"Event-driven orders: {len(event_orders)}")
    for o in event_orders[:10]:
        print(f"  SELL {o.get('quantity', 0)} {o.get('symbol')} @ {o.get('price', 0):,.0f}")
        print(f"    reason: {o.get('reason', '')[:140]}")
