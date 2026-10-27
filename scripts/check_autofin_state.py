import json
d = json.load(open("/root/hft-trading-algo/vietnam_algo/data/live/autofin_signal_state.json"))
deals = d.get("our_deals", {})
print(f"total our_deals: {len(deals)}")
trc = [k for k in deals if "TRC" in k]
print(f"TRC-related: {trc}")
for k in trc:
    print(f"  {k}: {deals[k]}")
print()
print(f"notified_event_ids: {len(d.get('notified_event_ids', []))}")
print(f"notified_order_keys: {len(d.get('notified_order_keys', []))}")
print()
# Show all symbols
syms = sorted({deals[k].get('symbol', '') for k in deals})
print(f"tracked symbols ({len(syms)}): {syms[:30]}")
