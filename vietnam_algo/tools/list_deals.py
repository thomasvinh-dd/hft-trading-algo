import json
d = json.load(open("/root/hft-trading-algo/vietnam_algo/data/live/autofin_signal_state.json"))
deals = d.get("our_deals", {})
print("=== All tracked deals with _our_qty > 0 ===")
for k, v in deals.items():
    if v.get("_our_qty", 0) > 0:
        print(f"  {k}: _our_qty={v.get('_our_qty')}  lastRecomment={v.get('lastRecomment', '')[:60]}")
print()
print("=== Deals with _our_qty == 0 or missing ===")
for k, v in deals.items():
    if v.get("_our_qty", 0) <= 0:
        print(f"  {k}: _our_qty={v.get('_our_qty')}  lastRecomment={v.get('lastRecomment', '')[:60]}")
