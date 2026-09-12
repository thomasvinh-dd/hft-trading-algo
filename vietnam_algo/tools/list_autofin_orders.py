import json
d = json.load(open("/root/hft-trading-algo/vietnam_algo/data/live/latest_orders.autofin_signal.json"))
items = d.get("items", [])
print(f"items count: {len(items)}")
for i in items:
    print(f"  {i.get('side')} {i.get('quantity')} {i.get('symbol')} @ {i.get('price'):,.0f}  kind={i.get('kind')}")
