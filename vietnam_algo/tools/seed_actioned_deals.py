"""Seed the actioned_deals set in autofin_signal_state.json from the
already-notified order keys. This protects against re-entry of any
deal we've ever actioned (entry / partial / full close), even on a
restart where the explicit actioned_deals set is empty."""
import json
import re
import sys
from pathlib import Path

path = Path("/root/hft-trading-algo/vietnam_algo/data/live/autofin_signal_state.json")
d = json.loads(path.read_text())
existing_actioned = set(d.get("actioned_deals") or [])
print(f"actioned_deals already in state: {len(existing_actioned)}")

# Parse notified_order_keys for deal_ids: format "{deal_id}|{side}|..."
pattern = re.compile(r"^(\d+:[A-Z]+)\|")
deal_ids: set[str] = set()
for key in d.get("notified_order_keys") or []:
    m = pattern.match(key)
    if m:
        deal_ids.add(m.group(1))

print(f"deal_ids from notified_order_keys: {len(deal_ids)}")
print(f"  e.g. {sorted(deal_ids)[:5]}")

# Merge
merged = existing_actioned | deal_ids
print(f"merged total: {len(merged)}")
print(f"  newly added: {len(merged - existing_actioned)}")

# Save
d["actioned_deals"] = sorted(merged)
path.write_text(json.dumps(d, indent=2))
print(f"saved to {path}")
