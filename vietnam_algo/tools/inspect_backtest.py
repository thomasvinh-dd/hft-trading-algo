"""Inspect last trades from a saved backtest result."""
import json
from pathlib import Path

# Look at the saved backtest result to find the last trades.
p = Path("/root/hft-trading-algo/vietnam_algo/data/backtests/liquid_momentum_rotation.json")
data = json.loads(p.read_text())

print("Top-level keys:", list(data.keys())[:10])
trades = data.get("trades", [])
print(f"Total trades: {len(trades)}")
print()
print("Last 10 trades:")
for t in trades[-10:]:
    print(f"  {t.get('date')} {t.get('side'):4} {t.get('quantity'):>5} {t.get('symbol'):<6} @ {t.get('price'):>10,.0f}  value={t.get('value', 0):>13,.0f}")

# Also check equity curve
ec = data.get("equityCurve") or data.get("equity_curve") or []
print()
print(f"Equity curve entries: {len(ec)}")
if ec:
    print("Last 5 equity points:")
    for ep in ec[-5:]:
        print(f"  {ep.get('date')}  cash={ep.get('cash', 0):>13,.0f}  pos_value={ep.get('stockValue') or ep.get('stock_value') or ep.get('positionsValue') or 0:>13,.0f}  NAV={ep.get('equity') or ep.get('nav') or 0:>13,.0f}")
