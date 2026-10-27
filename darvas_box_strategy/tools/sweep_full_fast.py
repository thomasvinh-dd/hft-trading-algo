"""Test on different universe sizes - just full period to compare.

No slicing needed - just measure full-period return for different universes.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path("/root/hft-trading-algo")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "darvas_box_strategy"))

import json
import math
import time
from datetime import date
from concurrent.futures import ProcessPoolExecutor, as_completed

from darvas import (
    StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig, TrailingConfig,
    UniverseConfig, ExitsConfig,
    run_backtest, load_price_bars,
)
from darvas.data_loader import derive_meta, passes_universe

PRICES_DIR = PROJECT_ROOT / "vietnam_algo" / "data" / "prices"


def metrics(navs):
    if len(navs) < 2: return 0, 0, 0
    ret = navs[-1] / navs[0] - 1
    peak = navs[0]; mdd = 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0: mdd = min(mdd, v/peak - 1)
    daily = []
    for i in range(1, len(navs)):
        if navs[i-1] > 0: daily.append(navs[i]/navs[i-1]-1)
    if daily and len(daily) > 1:
        mu = sum(daily)/len(daily)
        var = sum((x-mu)**2 for x in daily)/len(daily)
        sd = math.sqrt(var)
        return ret, mdd, mu/sd*math.sqrt(252) if sd else 0
    return ret, mdd, 0


def build_universe(min_value_20d=None, min_bars=200):
    universe_cfg = UniverseConfig(
        min_avg_value_20d=min_value_20d if min_value_20d else 1.0,
        min_price=5_000.0, max_price=200_000.0,
    )
    base_cfg = BaseConfig(min_days=5, max_days=30, max_range_pct=0.030)
    brk_cfg = BreakoutConfig(require_ma200=True, min_history_days=200)
    temp_cfg = StrategyConfig(universe=universe_cfg, breakout=brk_cfg, base=base_cfg)
    tickers = []
    for path in sorted(PRICES_DIR.glob("*.json")):
        if path.name == "E1VFVN30.json": continue
        ticker = path.stem.upper()
        bars = load_price_bars(path)
        if len(bars) < min_bars: continue
        meta = derive_meta(ticker, bars)
        if passes_universe(meta, temp_cfg): tickers.append(ticker)
    return tickers


def run_full(cfg, tickers):
    """Run on full period (no slicing)."""
    filtered = []
    for t in tickers:
        bars = load_price_bars(PRICES_DIR / f"{t}.json")
        if len(bars) >= cfg.breakout.min_history_days:
            filtered.append(t)
    if not filtered: return None

    r = run_backtest(cfg, prices_dir=PRICES_DIR, tickers=filtered,
                     progress=False, portfolio=True, mtm_mode="close")
    if not r.nav_curve: return None
    navs = [v for _, v in r.nav_curve]
    return metrics(navs), len(r.trades), filtered


def evaluate_one(args):
    label, cfg, tickers = args
    result = run_full(cfg, tickers)
    if result is None:
        return label, None
    m, n, filt = result
    return label, (m, n, len(filt))


print("Building universes...", flush=True)
universe_full = build_universe(min_value_20d=None)
universe_liquid = build_universe(min_value_20d=1.0e9)
universe_liquid_loose = build_universe(min_value_20d=2.0e8)
print(f"Full (no liquidity): {len(universe_full)} tickers", flush=True)
print(f"Liquid (1B VND): {len(universe_liquid)} tickers", flush=True)
print(f"Loose liquid (200M VND): {len(universe_liquid_loose)} tickers", flush=True)


cfg = StrategyConfig(
    base=BaseConfig(min_days=5, max_days=30, max_range_pct=0.030),
    breakout=BreakoutConfig(
        volume_mult=2.0, high_conviction_mult=3.0,
        require_ma200=True, min_history_days=200,
        require_close_above_ma=20,
    ),
    exits=ExitsConfig(),
    trailing=TrailingConfig(trail_mode="pct", trail_from_peak_pct=0.07),
    universe=UniverseConfig(),
    backtest=BacktestConfig(
        start_nav=1_000_000_000.0,
        max_concurrent_positions=10,
        cooldown_bars_after_stop=5,
        cost_bps=25.0,
        size_low=0.15, size_high=0.30,
        vol_target_pct=0.02,
    ),
)

# Pre-filter to min_history_days
def pre_filter(tickers, cfg):
    out = []
    for t in tickers:
        bars = load_price_bars(PRICES_DIR / f"{t}.json")
        if len(bars) >= cfg.breakout.min_history_days:
            out.append(t)
    return out


configs = [
    ("FULL (no liquidity)", pre_filter(universe_full, cfg)),
    ("Loose liquid (200M VND)", pre_filter(universe_liquid_loose, cfg)),
    ("LIQUID (1B VND)", pre_filter(universe_liquid, cfg)),
]

t0 = time.time()
with ProcessPoolExecutor(max_workers=3) as ex:
    futures = {ex.submit(evaluate_one, (l, cfg, t)): l for l, t in configs}
    for fut in as_completed(futures):
        label = futures[fut]
        try:
            l, res = fut.result()
            if res is None:
                print(f"{l}: skip", flush=True)
                continue
            (ret, mdd, sh), n, n_filtered = res
            print(f"\n=== {l} (n={n_filtered}, trades={n}) ===", flush=True)
            print(f"  Full: ret={ret*100:+.1f}%, sharpe={sh:+.2f}, DD={mdd*100:+.1f}%", flush=True)
        except Exception as e:
            import traceback
            print(f"{label}: ERROR {e}", flush=True)
            traceback.print_exc()

print(f"\nWall: {time.time()-t0:.0f}s", flush=True)
