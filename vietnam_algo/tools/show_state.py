import json
import os

path = "/root/hft-trading-algo/vietnam_algo/data/live/dry_run_state.json"
if os.path.exists(path):
    d = json.load(open(path))
    print(f"cash: {d['cash']}")
    print(f"positions: {len(d.get('positions', {}))}")
    for sym, p in d.get("positions", {}).items():
        print(f"  {sym}: qty={p['quantity']} avg={p['avg_price']:.0f}")
    print(f"closed_trades: {len(d['closed_trades'])}")
    for t in d["closed_trades"][-10:]:
        print(f"    {t}")
else:
    print("not yet")
