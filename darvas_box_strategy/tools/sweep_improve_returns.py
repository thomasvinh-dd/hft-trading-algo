"""Improve returns by letting winners run and increasing sizes.

Tests:
1. Wider trail (0.10, 0.12, 0.15) — let winners compound
2. Bigger sizes (0.20/0.40, 0.25/0.50) — more capital per winner
3. Tighter stops on losers (cut earlier, faster capital recycle)
4. Combinations
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

from darvas_box_strategy.darvas import (
    StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig, TrailingConfig,
    UniverseConfig, ExitsConfig,
    run_backtest, load_price_bars,
)
from darvas_box_strategy.darvas.data_loader import derive_meta, passes_universe

PRICES_DIR = PROJECT_ROOT / "vietnam_algo" / "data" / "prices"
TRAIN_END = date(2022, 12, 31)


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


def build_universe():
    tickers = json.loads(
        (PROJECT_ROOT / "darvas_box_strategy" / "data" / "hose_blue_chips.json").read_text()
    )["tickers"]
    cfg = StrategyConfig()
    out = []
    for t in tickers:
        bars = load_price_bars(PRICES_DIR / f"{t}.json")
        if len(bars) < 200: continue
        meta = derive_meta(t, bars)
        if passes_universe(meta, cfg): out.append(t)
    return out


def run_period(cfg, tickers, start_date, end_date):
    filtered = []
    for t in tickers:
        bars = load_price_bars(PRICES_DIR / f"{t}.json")
        in_window = [b for b in bars
                     if (start_date is None or b.date >= start_date)
                     and (end_date is None or b.date <= end_date)]
        if len(in_window) >= cfg.breakout.min_history_days:
            filtered.append(t)
    if not filtered: return None, None

    import darvas_box_strategy.darvas.data_loader as dl
    orig = dl.load_price_bars
    tickers_set = set(filtered)

    def patched(path):
        ticker = path.stem.upper()
        if ticker not in tickers_set:
            return orig(path)
        bars = orig(path)
        return [b for b in bars
                if (start_date is None or b.date >= start_date)
                and (end_date is None or b.date <= end_date)]
    dl.load_price_bars = patched
    try:
        r = run_backtest(cfg, prices_dir=PRICES_DIR, tickers=filtered,
                         progress=False, portfolio=True, mtm_mode="close")
    finally:
        dl.load_price_bars = orig
    if not r.nav_curve: return None, None
    navs = [v for _, v in r.nav_curve]
    return metrics(navs), len(r.trades)


def evaluate_one(args):
    label, cfg_overrides, tickers = args
    cfg = make_cfg(**cfg_overrides)
    train = run_period(cfg, tickers, None, TRAIN_END)
    test = run_period(cfg, tickers, TRAIN_END, None)
    full = run_period(cfg, tickers, None, None)
    return label, train, test, full


def make_cfg(**overrides):
    bd = dict(
        volume_mult=2.0, high_conviction_mult=3.0,
        require_ma200=True, min_history_days=200,
        require_close_above_ma=20,
    )
    btd = dict(
        start_nav=1_000_000_000.0,
        max_concurrent_positions=10,
        cooldown_bars_after_stop=5,
        cost_bps=25.0,
        size_low=0.15, size_high=0.30,
        vol_target_pct=0.02,
        vol_target_floor=0.05, vol_target_ceiling=0.30,
    )
    td = dict(trail_mode="pct", trail_from_peak_pct=0.07, trail_floor_lock_profit=True)
    ex = dict(
        scale_out_pct_at_sl_T=0.5,
        scale_out_pct_at_midpoint=0.5,
        sl_buffer_pct=0.01,
    )
    for k, v in overrides.items():
        if k in bd: bd[k] = v
        elif k in btd: btd[k] = v
        elif k in td: td[k] = v
        elif k in ex: ex[k] = v
    return StrategyConfig(
        base=BaseConfig(min_days=5, max_days=30, max_range_pct=0.030),
        breakout=BreakoutConfig(**bd),
        exits=ExitsConfig(**ex),
        trailing=TrailingConfig(**td),
        universe=UniverseConfig(min_avg_value_20d=1.0e9, min_price=5_000.0, max_price=200_000.0),
        backtest=BacktestConfig(**btd),
    )


tickers = build_universe()
print(f"Universe: {len(tickers)} HOSE blue chips", flush=True)

# The key experiment: vary trail from tight to very wide
configs = [
    # 1. Trail width sweep
    ("baseline trail=0.07", {}),
    ("trail=0.10", {"trail_from_peak_pct": 0.10}),
    ("trail=0.12", {"trail_from_peak_pct": 0.12}),
    ("trail=0.15", {"trail_from_peak_pct": 0.15}),
    ("trail=0.20", {"trail_from_peak_pct": 0.20}),

    # 2. Position size sweep
    ("size 0.20/0.40", {"size_low": 0.20, "size_high": 0.40}),
    ("size 0.25/0.50", {"size_low": 0.25, "size_high": 0.50}),
    ("size 0.15/0.50", {"size_low": 0.15, "size_high": 0.50}),

    # 3. Vol-targeting intensity sweep
    ("vol=4.0% big", {"vol_target_pct": 0.04}),
    ("no vol-target (rely on 2x sizing)", {"vol_target_pct": 0.0}),

    # 4. Stop-loss tightening — cut losers earlier
    ("sl_buffer=0.005 (tighter)", {"sl_buffer_pct": 0.005}),

    # 5. Combinations of best
    ("trail=0.12 + size 0.20/0.40",
     {"trail_from_peak_pct": 0.12, "size_low": 0.20, "size_high": 0.40}),
    ("trail=0.15 + size 0.20/0.40",
     {"trail_from_peak_pct": 0.15, "size_low": 0.20, "size_high": 0.40}),
    ("trail=0.10 + size 0.25/0.50",
     {"trail_from_peak_pct": 0.10, "size_low": 0.25, "size_high": 0.50}),
]


def main():
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(evaluate_one, (l, o, tickers)): l for l, o in configs}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                l, train, test, full = fut.result()
                if train is None or test is None or full is None:
                    print(f"{l}: skip", flush=True)
                    continue
                ret_t, mdd_t, sh_t = train[0]
                tn = train[1]
                ret_o, mdd_o, sh_o = test[0]
                on = test[1]
                ret_f, mdd_f, sh_f = full[0]
                score = sh_o - 0.3 * abs(mdd_o)
                print(f"{l:<48}  TRAIN={ret_t*100:+6.1f}%/{sh_t:+.2f}/{tn:>4}  "
                      f"TEST={ret_o*100:+6.1f}%/{sh_o:+.2f}/{on:>4}  "
                      f"FULL={ret_f*100:+6.1f}%/{sh_f:+.2f}  "
                      f"score={score:+.2f}",
                      flush=True)
            except Exception as e:
                import traceback
                print(f"{label}: ERROR {e}", flush=True)
                traceback.print_exc()
    print(f"\nWall: {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
