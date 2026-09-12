import json
d = json.load(open("/root/hft-trading-algo/vietnam_algo/data/live/dry_run_state.momentum.json"))
print(f"cash: {d['cash']:,.0f}")
print(f"initial_nav: {d['initial_nav']:,.0f}")
print(f"positions:")
for sym, p in d["positions"].items():
    print(f"  {sym}: qty={p['quantity']} avg={p['avg_price']:.0f} cost_basis={p['cost_basis']:.0f}")
print(f"realized_pnl: {d['realized_pnl']:,.0f}")
print(f"total P&L: {d['realized_pnl'] + sum(p['last_price']*p['quantity'] - p['avg_price']*p['quantity'] for p in d['positions'].values()):,.0f}")

# Compare to backtest last point
print("\n=== Backtest last equity point ===")
b = json.load(open("/root/hft-trading-algo/vietnam_algo/data/backtests/liquid_momentum_rotation.json"))
last = b["equityCurve"][-1]
print(f"  date: {last['date']}")
print(f"  cash: {last['cash']:,.0f}")
print(f"  equity: {last['equity']:,.0f}")
