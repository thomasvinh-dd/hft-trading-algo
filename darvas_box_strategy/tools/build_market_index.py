"""Build the equal-weight blue-chip market index used by the market
filters.

The index is the mean close of the 70 HOSE blue chips the strategy
trades, on every date where at least 30 of them have a bar. It is
written in the same JSON shape as the other price files so the backtest
can load it with `load_price_bars`.

The index has to be REBUILT regularly: the volatility filter reads the
market's recent volatility, so a stale index means the filter is
judging today's market on last quarter's data. The `refresh-cache`
cron job calls this.

Usage:
    python3 -m darvas_box_strategy.tools.build_market_index \
        [--out darvas_box_strategy/data/market_index.json]
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from darvas_box_strategy.bot.cli import load_blue_chips
from darvas_box_strategy.darvas.db_prices import load_bars

MIN_BREADTH = 30  # require at least this many tickers on a date


def build_index(tickers=None):
    """Return the index bars as a list of dicts."""
    tickers = tickers or load_blue_chips()
    closes = defaultdict(dict)  # date -> ticker -> close
    for t in tickers:
        try:
            bars = load_bars(t, None)
        except Exception as exc:  # a missing ticker must not kill the index
            print(f"  WARN: {t}: {exc}", file=sys.stderr)
            continue
        for b in bars:
            closes[b.date][t] = b.close

    dates = sorted(closes)
    if not dates:
        raise SystemExit("no price data — cannot build the index")

    items = []
    for d in dates:
        vals = list(closes[d].values())
        if len(vals) < MIN_BREADTH:
            continue
        idx = sum(vals) / len(vals)
        items.append({
            "time": d.isoformat(),
            "open": idx, "high": idx, "low": idx, "close": idx, "volume": 0,
        })
    return items


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out",
        default=str(ROOT / "darvas_box_strategy" / "data" / "market_index.json"),
        help="output path for the index JSON",
    )
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    tickers = load_blue_chips()
    items = build_index(tickers)
    if not items:
        raise SystemExit("index is empty — refusing to write it")

    out = {
        "symbol": "VN-SYNTHETIC-BLUE-70",
        "source": "live-bluechip-equal-weight-70",
        "fetchedAt": items[-1]["time"],
        "count": len(items),
        "items": items,
    }
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out))

    if not args.quiet:
        print(f"index bars: {len(items)}")
        print(f"  {items[0]['time']} {items[0]['close']:,.2f}")
        print(f"  {items[-1]['time']} {items[-1]['close']:,.2f}")
        print(f"WROTE {path}")


if __name__ == "__main__":
    main()
