import json
d = json.load(open("/root/hft-trading-algo/vietnam_algo/data/live/autofin_signal_state.json"))
print(f"last_poll_at: {d.get('last_poll_at')}")
print(f"last_notification_poll_at: {d.get('last_notification_poll_at')}")
print(f"last_notification_id: {d.get('last_notification_id')}")
print()
for k, v in d.get("our_deals", {}).items():
    if v.get("symbol") == "TRC":
        print(f"deal {k}:")
        print(f"  _our_qty: {v.get('_our_qty')}")
        print(f"  lastRecomment: {v.get('lastRecomment')}")
        print(f"  numberOfProfit: {v.get('numberOfProfit')}")
        print(f"  isClosed: {v.get('statusDeal', {}).get('isClosed')}")
