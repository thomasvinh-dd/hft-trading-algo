import json
import sys

for path in [
    "/root/hft-trading-algo/vietnam_algo/data/live/dry_run_state.autofin_signal.json",
    "/root/hft-trading-algo/vietnam_algo/data/live/dry_run_state.momentum.json",
]:
    print(f"\n=== {path} ===")
    d = json.load(open(path))
    print(f"  cash: {d.get('cash'):,.0f}")
    positions = d.get('positions', {})
    print(f"  positions: {len(positions)}")
    for sym, p in positions.items():
        print(f"    {sym}: qty={p['quantity']} avg={p['avg_price']:,.0f}")
    closed = d.get('closed_trades', [])
    print(f"  closed_trades: {len(closed)}")
    for t in closed:
        if isinstance(t, dict) and 'note' in t and t['note'].startswith('_applied:'):
            print(f"    {t['note']}: applied={t.get('applied_count')} clipped={t.get('clipped_count')} skipped={t.get('skipped_count')}")
