import json
d = json.load(open("/root/hft-trading-algo/vietnam_algo/data/backtests/liquid_momentum_rotation.json"))
ec = d["equityCurve"]
print(f"equity curve points: {len(ec)}")
print(f"first date: {ec[0].get('date')} cash={ec[0].get('cash'):,.0f}")
print(f"last date: {ec[-1].get('date')} cash={ec[-1].get('cash'):,.0f}")
print()
print("Last 15 equity points:")
for ep in ec[-15:]:
    d = ep.get("date", "")
    cash = ep.get("cash", 0) or 0
    eq = ep.get("equity", 0) or 0
    print(f"  {d}: cash={cash:,.0f} equity={eq:,.0f}")
