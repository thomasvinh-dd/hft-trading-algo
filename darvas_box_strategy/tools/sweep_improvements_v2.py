"""Slim version - one config per improvement to test direction."""
import sys
from pathlib import Path
PROJECT_ROOT = Path("/root/hft-trading-algo")
sys.path.insert(0, str(PROJECT_ROOT))

import json
import math
import time
from datetime import date

from darvas_box_strategy.darvas import (
    StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig, TrailingConfig,
    UniverseConfig, ExitsConfig,
    run_backtest, load_price_bars,
)
from darvas_box_strategy.darvas.data_loader import derive_meta, passes_universe

PRICES_DIR = PROJECT_ROOT / "vietnam_algo" / "data" / "prices"
INDEX_PATH = PRICES_DIR / "E1VFVN30.json"
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
    ret = navs[-1] / navs[-1] / navs[0] - 1 if False else navs[-1] / navs[0] - 1
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


def build_filtered_universe():
    base_cfg = BaseConfig(min_days=5, max_days=30, max_range_pct=0.030)
    brk_cfg = BreakoutConfig(require_ma200=True, min_history_days=200)
    universe_cfg = UniverseConfig(min_avg_value_20d=1.0e9, min_price=5_000.0, max_price=200_000.0)
    temp_cfg = StrategyConfig(universe=universe_cfg, breakout=brk_cfg, base=base_cfg)
    tickers = []
    for path in sorted(PRICES_DIR.glob("*.json")):
        if path.name == "E1VFVN30.json": continue
        ticker = path.stem.upper()
        bars = load_price_bars(path)
        if len(bars) < 200: continue
        meta = derive_meta(ticker, bars)
        if passes_universe(meta, temp_cfg): tickers.append(ticker)
    return tickers


def run_period(cfg, tickers, start_date, end_date):
    filtered = []
    for t in tickers:
        bars = load_price_bars(PRICES_DIR / f"{t}.json")
        in_window = slice_bars(bars, start_date, end_date)
        if len(in_window) >= cfg.breakout.min_history_days:
            filtered.append(t)
    if not filtered: return None, None

    import darvas_box_strategy.darvas.data_loader as dl
    orig_load = dl.load_price_bars
    def patched(path):
        return slice_bars(orig_load(path), start_date, end_date)
    dl.load_price_bars = patched
    try:
        r = run_backtest(cfg, prices_dir=PRICES_DIR, tickers=filtered,
                         progress=False, portfolio=True, mtm_mode="close")
    finally:
        dl.load_price_bars = orig_load
    if not r.nav_curve: return None, None
    navs = [v for _, v in r.nav_curve]
    return metrics(navs), len(r.trades)


tickers = build_filtered_universe()
print(f"Universe: {len(tickers)} tickers", flush=True)


def make_cfg(**overrides):
    bd = dict(
        volume_mult=2.0, high_conviction_mult=3.0,
        require_ma200=True, min_history_days=200,
        require_close_above_ma=0,
        regime_index_path="", regime_ma_window=50,
    )
    btd = dict(
        start_nav=1_000_000_000.0,
        max_concurrent_positions=10,
        cooldown_bars_after_stop=5,
        max_holding_bars=0,
        cost_bps=35.0,
        size_low=0.15, size_high=0.30,
    )
    td = dict(trail_mode="pct", trail_from_peak_pct=0.07, trail_floor_lock_profit=True)
    for k, v in overrides.items():
        if k in bd: bd[k] = v
        elif k in btd: btd[k] = v
        elif k in td: td[k] = v
    return StrategyConfig(
        base=BaseConfig(min_days=5, max_days=30, max_range_pct=0.030),
        breakout=BreakoutConfig(**bd),
        exits=ExitsConfig(),
        trailing=TrailingConfig(**td),
        universe=UniverseConfig(min_avg_value_20d=1.0e9, min_price=5_000.0, max_price=200_000.0),
        backtest=BacktestConfig(**btd),
    )


def evaluate(label, **overrides):
    cfg = make_cfg(**overrides)
    t0 = time.time()
    train_m, train_n = run_period(cfg, tickers, None, TRAIN_END)
    test_m, test_n = run_period(cfg, tickers, TRAIN_END, None)
    wall = time.time() - t0
    if train_m is None or test_m is None:
        print(f"{label:<40} [skip]", flush=True)
        return None
    ret_t, mdd_t, sh_t = train_m
    ret_o, mdd_o, sh_o = test_m
    print(f"{label:<40} train={ret_t*100:+6.1f}%/{sh_t:+.2f}/-{abs(mdd_t)*100:5.1f}%/{train_n:>4}  "
          f"test={ret_o*100:+6.1f}%/{sh_o:+.2f}/-{abs(mdd_o)*100:5.1f}%/{test_n:>4}  [{wall:.0f}s]",
          flush=True)
    return {"label": label, "train": (ret_t, mdd_t, sh_t, train_n),
            "test": (ret_o, mdd_o, sh_o, test_n)}


print(flush=True)
print("=" * 130, flush=True)
print("BASELINE", flush=True)
print("=" * 130, flush=True)
evaluate("baseline")

print(flush=True)
print("#5 costs: cost_bps=70 (0.2% slip + 0.15% comm)", flush=True)
evaluate("cost_bps=70", cost_bps=70)

print(flush=True)
print("#3 time stop", flush=True)
evaluate("time_stop=20", max_holding_bars=20)

print(flush=True)
print("#2 trend confirm", flush=True)
evaluate("close>MA20", require_close_above_ma=20)

print(flush=True)
print("#1 market regime", flush=True)
evaluate("regime MA100", regime_index_path=str(INDEX_PATH), regime_ma_window=100)
evaluate("regime MA50", regime_index_path=str(INDEX_PATH), regime_ma_window=50)
evaluate("regime MA200", regime_index_path=str(INDEX_PATH), regime_ma_window=200)

print(flush=True)
print("#7 conviction sizing", flush=True)
evaluate("size 0.10/0.15 (1.5x)", size_low=0.10, size_high=0.15)
evaluate("size 0.10/0.20 (2.0x)", size_low=0.10, size_high=0.20)
evaluate("size 0.10/0.10 (1.0x)", size_low=0.10, size_high=0.10)

print(flush=True)
print("BEST COMBINATIONS", flush=True)
evaluate("regime MA100 + close>MA20", regime_index_path=str(INDEX_PATH), regime_ma_window=100, require_close_above_ma=20)
evaluate("regime MA100 + time_stop=20", regime_index_path=str(INDEX_PATH), regime_ma_window=100, max_holding_bars=20)
evaluate("regime MA100 + close>MA20 + time=20", regime_index_path=str(INDEX_PATH), regime_ma_window=100, require_close_above_ma=20, max_holding_bars=20)
evaluate("regime MA50 + close>MA20 + time=20", regime_index_path=str(INDEX_PATH), regime_ma_window=50, require_close_above_ma=20, max_holding_bars=20)
evaluate("regime MA50 + close>MA20 + time=15", regime_index_path=str(INDEX_PATH), regime_ma_window=50, require_close_above_ma=20, max_holding_bars=15)
