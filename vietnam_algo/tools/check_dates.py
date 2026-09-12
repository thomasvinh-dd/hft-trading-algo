"""Check what dates are available."""
import sys, json
from pathlib import Path

sys.path.insert(0, "/root/hft-trading-algo")
from vietnam_algo.libs.backtest import sorted_trading_dates, PriceBar

data = json.loads(Path("/root/hft-trading-algo/vietnam_algo/data/prices/PVT.json").read_text())
items = data["items"]


class Bar:
    def __init__(self, d):
        self.close = d["close"]
        self.open = d["open"]
        self.high = d["high"]
        self.low = d["low"]
        self.volume = d.get("volume", 0)


# Build the price_table the backtest uses.
table = {"PVT": {b["time"]: Bar(b) for b in items}}
dates = sorted_trading_dates(table)
print(f"Total dates: {len(dates)}")
print(f"First 3: {dates[:3]}")
print(f"Last 5: {dates[-5:]}")
