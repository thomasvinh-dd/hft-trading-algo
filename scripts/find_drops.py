"""Inspect equity curve transitions to find the 113M drop."""
import json
from pathlib import Path

data = json.loads(Path("/root/hft-trading-algo/vietnam_algo/data/backtests/liquid_momentum_rotation.json").read_text())
ec = data.get("equityCurve", [])

# Find all transitions where cash drops
print("Cash transitions (delta > 1M):")
prev = None
for ep in ec:
    d = ep.get("date", "")
    cash = ep.get("cash", 0) or 0
    pos = ep.get("stockValue", 0) or 0
    if prev is not None:
        delta = cash - prev
        if abs(delta) > 1_000_000:
            print(f"  {prev_d} → {d}:  cash {prev_c:>13,.0f} → {cash:>13,.0f}  Δ={delta:>13,.0f}  pos={pos}")
    prev = cash
    prev_c = cash
    prev_d = d
