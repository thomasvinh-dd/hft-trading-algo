"""Test individual improvements on top of the OOS-validated baseline.

Baseline: vol_mult=2.0, hi_conv=3.0, MaxC=10, Cool=5, pct@0.07
Already on VPS price data + liquid_stocks_universe (72 tickers).

For each improvement, sweep the parameter and report TRAIN (2019-2022)
and TEST (2023-2026) results.
"""
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


def build_filtered_universe():
    base_cfg = BaseConfig(min_days=5, max_days=30, max_range_pct=0.030)
    brk_cfg = BreakoutConfig(require_ma200=True, min_history_days=200)
    universe_cfg = UniverseConfig(min_avg_value_20d=1.0e9, min_price=5_000.0, max_price=200_000.0)
    temp_cfg = StrategyConfig(universe=universe_cfg, breakout=brk_cfg, base=base_cfg)
    tickers = []
    for path in sorted(PRICES_DIR.glob("*.json")):
        if path.name == "E1VFVN30.json": continue  # skip the index
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
print(f"Filtered universe: {len(tickers)} tickers")


def base_kwargs(**overrides):
    """Return config kwargs with overrides applied."""
    cfg_dict = dict(
        base=BaseConfig(min_days=5, max_days=30, max_range_pct=0.030),
        breakout=BreakoutConfig(
            volume_mult=2.0, high_conviction_mult=3.0,
            require_ma200=True, min_history_days=200,
            require_close_above_ma=0,
            regime_index_path="", regime_ma_window=50,
        ),
        exits=ExitsConfig(),
        trailing=TrailingConfig(
            trail_mode="pct", trail_from_peak_pct=0.07,
            trail_floor_lock_profit=True,
        ),
        universe=UniverseConfig(min_avg_value_20d=1.0e9, min_price=5_000.0, max_price=200_000.0),
        backtest=BacktestConfig(
            start_nav=1_000_000_000.0,
            max_concurrent_positions=10,
            cooldown_bars_after_stop=5,
            max_holding_bars=0,
            cost_bps=35.0,
            size_low=0.15, size_high=0.30,
        ),
    )
    for k, v in overrides.items():
        if k == "trail_pct": cfg_dict["trailing"] = TrailingConfig(trail_mode="pct", trail_from_peak_pct=v, trail_floor_lock_profit=True)
        elif k == "cost_bps": cfg_dict["backtest"] = BacktestConfig(**{**cfg_dict["backtest"].__dict__, "cost_bps": v})
        elif k == "size_low": cfg_dict["backtest"] = BacktestConfig(**{**cfg_dict["backtest"].__dict__, "size_low": v})
        elif k == "size_high": cfg_dict["backtest"] = BacktestConfig(**{**cfg_dict["backtest"].__dict__, "size_high": v})
        elif k == "max_holding_bars": cfg_dict["backtest"] = BacktestConfig(**{**cfg_dict["backtest"].__dict__, "max_holding_bars": v})
        elif k == "require_close_above_ma": cfg_dict["breakout"] = BreakoutConfig(**{**cfg_dict["breakout"].__dict__, "require_close_above_ma": v})
        elif k == "regime_index_path": cfg_dict["breakout"] = BreakoutConfig(**{**cfg_dict["breakout"].__dict__, "regime_index_path": v})
        elif k == "regime_ma_window": cfg_dict["breakout"] = BreakoutConfig(**{**cfg_dict["breakout"].__dict__, "regime_ma_window": v})
    return cfg_dict


def make_cfg(**overrides):
    return StrategyConfig(**base_kwargs(**overrides))


def evaluate(label, **overrides):
    cfg = make_cfg(**overrides)
    t0 = time.time()
    train_m, train_n = run_period(cfg, tickers, None, TRAIN_END)
    test_m, test_n = run_period(cfg, tickers, TRAIN_END, None)
    wall = time.time() - t0
    if train_m is None or test_m is None: return None
    ret_t, mdd_t, sh_t = train_m
    ret_o, mdd_o, sh_o = test_m
    print(f"{label:<35} train={ret_t*100:+6.1f}%/{sh_t:+.2f}/-{abs(mdd_t)*100:5.1f}%/{train_n:>4}  "
          f"test={ret_o*100:+6.1f}%/{sh_o:+.2f}/-{abs(mdd_o)*100:5.1f}%/{test_n:>4}  "
          f"[{wall:.1f}s]")
    return {"label": label, "train": (ret_t, mdd_t, sh_t, train_n),
            "test": (ret_o, mdd_o, sh_o, test_n)}


print()
print("=" * 130)
print("BASELINE")
print("=" * 130)
baseline = evaluate("baseline (vol=2.0, pct@0.07)")

print()
print("=" * 130)
print("#5 REALISTIC COSTS — sweep cost_bps")
print("=" * 130)
for c in [0, 25, 35, 50, 70, 100]:
    evaluate(f"cost_bps={c}", cost_bps=c)

print()
print("=" * 130)
print("#3 TIME STOP — max_holding_bars")
print("=" * 130)
for h in [10, 15, 20, 30, 50]:
    evaluate(f"max_holding_bars={h}", max_holding_bars=h)

print()
print("=" * 130)
print("#2 TREND CONFIRM — require_close_above_ma")
print("=" * 130)
for w in [10, 20, 50]:
    evaluate(f"close > MA{w}", require_close_above_ma=w)

print()
print("=" * 130)
print("#1 MARKET REGIME — E1VFVN30 > MA50")
print("=" * 130)
evaluate("regime MA50 ON", regime_index_path=str(INDEX_PATH), regime_ma_window=50)
evaluate("regime MA100 ON", regime_index_path=str(INDEX_PATH), regime_ma_window=100)
evaluate("regime MA200 ON", regime_index_path=str(INDEX_PATH), regime_ma_window=200)

print()
print("=" * 130)
print("#7 CONVICTION SIZING — sweep size_low/size_high")
print("=" * 130)
for low, high in [(0.10, 0.15), (0.10, 0.20), (0.12, 0.18), (0.15, 0.30), (0.10, 0.30)]:
    mult = high/low if low > 0 else 0
    evaluate(f"size_low={low} size_high={high} ({mult:.1f}x)", size_low=low, size_high=high)

print()
print("=" * 130)
print("COMBINED: regime + trend + time stop + size")
print("=" * 130)
evaluate("regime MA100 + close>MA20 + time=20",
         regime_index_path=str(INDEX_PATH), regime_ma_window=100,
         require_close_above_ma=20, max_holding_bars=20)
evaluate("regime MA50 + close>MA20 + time=20",
         regime_index_path=str(INDEX_PATH), regime_ma_window=50,
         require_close_above_ma=20, max_holding_bars=20)
evaluate("regime MA100 + close>MA20 + time=15",
         regime_index_path=str(INDEX_PATH), regime_ma_window=100,
         require_close_above_ma=20, max_holding_bars=15)
