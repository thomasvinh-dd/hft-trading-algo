import sys, json
sys.path.insert(0, "/root/hft-trading-algo")
from pathlib import Path
from vietnam_algo.libs.backtest import close_price

data = json.loads(Path("/root/hft-trading-algo/vietnam_algo/data/prices/PVT.json").read_text())
bars_list = data["items"]


class Bar:
    def __init__(self, d):
        self.close = d["close"]
        self.open = d["open"]
        self.high = d["high"]
        self.low = d["low"]


table = {b["time"]: Bar(b) for b in bars_list}
print(f"close_price PVT 2026-09-09: {close_price('PVT', '2026-09-09', {'PVT': table})}")
print(f"All PVT dates around Sept 9:")
for b in bars_list[-5:]:
    print(f"  {b['time']} close={b['close']}")
