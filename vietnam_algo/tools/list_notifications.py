import json
d = json.load(open("/root/hft-trading-algo/vietnam_algo/data/live/autofin_signal_state.json"))
print("notified_order_keys (first 20):")
for k in list(d.get("notified_order_keys", []))[:20]:
    print(f"  {k}")
print(f"total notified_order_keys: {len(d.get('notified_order_keys', []))}")
print()
print("notified_event_ids:")
for k in list(d.get("notified_event_ids", []))[:10]:
    print(f"  {k}")
print(f"total notified_event_ids: {len(d.get('notified_event_ids', []))}")
