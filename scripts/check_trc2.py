import json
d = json.load(open("/root/hft-trading-algo/vietnam_algo/data/live/autofin_signal_state.json"))
ad = d.get("actioned_deals", [])
print(f"actioned_deals: {len(ad)}")
trc_in_actioned = "1786492800:TRC" in ad
print(f"TRC in actioned: {trc_in_actioned}")
print()
our_deals = d.get("our_deals", {})
trc = our_deals.get("1786492800:TRC")
print(f"TRC _our_qty: {trc.get('_our_qty') if trc else 'NOT IN STATE'}")
print(f"TRC lastRecomment: {trc.get('lastRecomment')[:60] if trc else 'n/a'}")
