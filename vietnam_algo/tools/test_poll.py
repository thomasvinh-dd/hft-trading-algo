"""Run the new notification poll directly to verify it works on VPS."""
import sys, json
sys.path.insert(0, '/root/hft-trading-algo')
from pathlib import Path
from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig
from vietnam_algo.algos.registry import STRATEGY_REGISTRY

cfg = EngineConfig(
    dataDir=Path('/root/hft-trading-algo/vietnam_algo/data'),
    strategy='autofin_signal',
    updateSymbols=False, updatePrices=False,
)
adapter = STRATEGY_REGISTRY.get('autofin_signal')
adapter._load_state(cfg.dataDir)

print(f'Loaded: {len(adapter._our_deals)} tracked deals')
print(f'Pre-poll: last_notification_id = {adapter._last_notification_id!r}')
print(f'Pre-poll: notified_event_ids = {len(adapter._notified_event_ids)}')

print('\n=== Calling _poll_notifications_for_events() ===')
orders = adapter._poll_notifications_for_events()
print(f'emitted {len(orders)} SELL orders from notifications')
for o in orders:
    sym = o.get('symbol', '?')
    qty = o.get('quantity', 0)
    price = o.get('price', 0)
    reason = o.get('reason', '')[:90]
    print(f'  SELL {qty} {sym} @ {price:,.0f}  ({reason})')

adapter._save_state(cfg.dataDir)
print(f'\nPost-poll: last_notification_id = {adapter._last_notification_id!r}')
print(f'Post-poll: notified_event_ids = {len(adapter._notified_event_ids)}')
