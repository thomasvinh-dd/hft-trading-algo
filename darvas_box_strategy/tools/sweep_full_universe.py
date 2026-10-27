"""Test on FULL universe (386 tickers with 200+ bars) vs the 71 we used.

Uses the new defaults: vol_target=2%, MA20 filter, cost=25bps, pct@0.07 trail.
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
TRAIN_END = date(2022, 12, 31)


def slice_bars(bars, start_date=None, end_date=None):
    out = []
    for b in bars:
        if start_date and b.date < start_date: continue
        if end_date and b.date > end_date: continue
        out.append(b)
    return out


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
    """Build universe with optional liquidity filter."""
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


def run_period(cfg, tickers, start_date, end_date, label=""):
    filtered_data = []
    for t in tickers:
        bars = load_price_bars(PRICES_DIR / f"{t}.json")
        in_window = slice_bars(bars, start_date, end_date)
        if len(in_window) >= cfg.breakout.min_history_days:
            filtered_data.append((t, in_window))
    if not filtered_data: return None, None

    import tempfile, json as J
    tmpdir = Path(tempfile.mkdtemp(prefix="hft_"))
    for t, bars in filtered_data:
        items = [{"close": b.close, "high": b.high, "low": b.low,
                  "open": b.open, "time": b.time.isoformat() if hasattr(b.time, 'isoformat') else str(b.time),
                  "volume": b.volume} for b in bars]
        data = {"count": len(items), "items": items, "symbol": t, "source": "sliced"}
        (tmpdir / f"{t}.json").write_text(J.dumps(data))

    try:
        r = run_backtest(cfg, prices_dir=tmpdir,
                         tickers=[t for t, _ in filtered_data],
                         progress=False, portfolio=True, mtm_mode="close")
    finally:
        import shutil
        shutil.rmtree(tmpdir)
    if not r.nav_curve: return None, None
    navs = [v for _, v in r.nav_curve]
    return metrics(navs), len(r.trades)


def evaluate_one(args):
    label, cfg, tickers = args
    train = run_period(cfg, tickers, None, TRAIN_END, "train")
    test = run_period(cfg, tickers, TRAIN_END, None, "test")
    full = run_period(cfg, tickers, None, None, "full")
    return label, train, test, full


# Build universes
print("Building universes...", flush=True)
universe_full = build_universe(min_value_20d=None)  # no liquidity filter
universe_liquid = build_universe(min_value_20d=1.0e9)  # with liquidity filter
print(f"Full universe (no liquidity filter, 200+ bars): {len(universe_full)} tickers", flush=True)
print(f"Liquid universe (1B VND value, 200+ bars): {len(universe_liquid)} tickers", flush=True)


# Use new defaults
cfg = StrategyConfig(
    base=BaseConfig(min_days=5, max_days=30, max_range_pct=0.030),
    breakout=BreakoutConfig(
        volume_mult=2.0, high_conviction_mult=3.0,
        require_ma200=True, min_history_days=200,
        require_close_above_ma=20,
        require_close_above_weekly_ma=0,
        regime_index_path="",
        sector_data_path="",
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
        vol_target_floor=0.05, vol_target_ceiling=0.30,
    ),
)

# Run with both universes in parallel
configs = [
    ("FULL universe (no liquidity)", cfg, universe_full),
    ("LIQUID universe (1B VND)", cfg, universe_liquid),
]

t0 = time.time()
with ProcessPoolExecutor(max_workers=2) as ex:
    futures = {ex.submit(evaluate_one, (l, c, t)): l for l, c, t in configs}
    for fut in as_completed(futures):
        label = futures[fut]
        try:
            l, train, test, full = fut.result()
            if train is None or test is None:
                print(f"{l}: skip", flush=True)
                continue
            ret_t, mdd_t, sh_t, train_n = train[0][0], train[0][1], train[0][2], train[1]
            ret_o, mdd_o, sh_o, test_n = test[0][0], test[0][1], test[0][2], test[1]
            ret_f, mdd_f, sh_f = full[0]
            print(f"\n=== {l} ===", flush=True)
            print(f"  TRAIN (2019-2022): ret={ret_t*100:+.1f}%, sharpe={sh_t:+.2f}, DD={mdd_t*100:+.1f}%, trades={train_n}", flush=True)
            print(f"  TEST  (2023-2026): ret={ret_o*100:+.1f}%, sharpe={sh_o:+.2f}, DD={mdd_o*100:+.1f}%, trades={test_n}", flush=True)
            print(f"  FULL  (2019-2026): ret={ret_f*100:+.1f}%, sharpe={sh_f:+.2f}", flush=True)
        except Exception as e:
            import traceback
            print(f"{label}: ERROR {e}", flush=True)
            traceback.print_exc()

print(f"\nWall: {time.time()-t0:.0f}s", flush=True)
