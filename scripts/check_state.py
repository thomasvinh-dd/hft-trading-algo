import json
from pathlib import Path
state_path = Path("/root/hft-trading-algo/vietnam_algo/data/live/dry_run_state.json")
print(f"state_path exists: {state_path.exists()}")
raw = json.loads(state_path.read_text())
print(f"closed_trades: {len(raw.get('closed_trades', []))}")
for t in raw.get('closed_trades', [])[-1:]:
    print(f"  note: {t.get('note')}")
    print(f"  clipped: {t.get('clipped')}")
