import json
from pathlib import Path

data = json.loads(Path("/root/hft-trading-algo/vietnam_algo/data/backtests/liquid_momentum_rotation.json").read_text())
ec = data.get("equityCurve", [])
print("Equity around Sept 2-9:")
for ep in ec:
    d = ep.get("date", "")
    if d >= "2026-09-02" and d <= "2026-09-09":
        cash = ep.get("cash", 0) or 0
        pos = ep.get("stockValue", 0) or 0
        nav = ep.get("equity", 0) or 0
        print(f"  {d}  cash={cash:>13,.0f}  pos={pos:>13,.0f}  NAV={nav:>13,.0f}")
