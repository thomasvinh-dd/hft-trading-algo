import json
from pathlib import Path
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/root/hft-trading-algo/vietnam_algo/data/live/latest_orders.json"
d = json.loads(Path(path).read_text())
print(f"tradeDate: {d.get('tradeDate')}, count: {d.get('count')}")
for i in d.get("items", []):
    print(f"  {i.get('side')} {i.get('quantity')} {i.get('symbol')} @ {i.get('price')}")
