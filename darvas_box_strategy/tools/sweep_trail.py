"""Sweep trailing-stop modes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import json
import math

from darvas_box_strategy.darvas import (
    StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig, TrailingConfig,
    run_backtest,
)

UNIVERSE = Path('/home/vst/Documents/Ai-trade/hft-trading-algo/vietnam_algo/data/universe/liquid_stocks.json')
PRICES = Path('/home/vst/Documents/Ai-trade/hft-trading-algo/vietnam_algo/data/prices')

tickers = [s['symbol'] for s in json.loads(UNIVERSE.read_text())['items']]


def metrics(navs):
    if len(navs) < 2:
        return 0, 0, 0, 0
    final = navs[-1]
    ret = final / navs[0] - 1
    peak = navs[0]
    mdd = 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0:
            dd = v / peak - 1
            if dd < mdd:
                mdd = dd
    daily = []
    for i in range(1, len(navs)):
        if navs[i - 1] > 0:
            daily.append(navs[i] / navs[i - 1] - 1)
    if daily and len(daily) > 1:
        mu = sum(daily) / len(daily)
        var = sum((x - mu) ** 2 for x in daily) / len(daily)
        sd = math.sqrt(var) if var > 0 else 0
        sharpe = (mu / sd * math.sqrt(252)) if sd > 0 else 0
    else:
        sharpe = 0
    return ret, mdd, sharpe, final


print(f'Universe: {len(tickers)} tickers, vol=3.0, hi=3.5, MaxC=10, Cool=10')
print()
print('Trail | Mode | Mult | Trades | Final NAV | Ret   | Sharpe | MaxDD')
print('-' * 80)


def run(mode, mult):
    trail = TrailingConfig(trail_mode=mode,
                           trail_from_peak_pct=mult if mode == "pct" else 0.10,
                           trail_atr_mult=mult if mode == "atr" else 2.0)
    cfg = StrategyConfig(
        base=BaseConfig(min_days=5, max_days=20, max_range_pct=0.05),
        breakout=BreakoutConfig(
            require_ma200=True, min_history_days=200,
            volume_mult=3.0, high_conviction_mult=3.5,
        ),
        backtest=BacktestConfig(
            start_nav=1e9,
            max_concurrent_positions=10,
            cooldown_bars_after_stop=10,
        ),
        trailing=trail,
    )
    return run_backtest(cfg, tickers=tickers, prices_dir=PRICES,
                        progress=False, portfolio=True, mtm_mode='close')


# Percentage mode
for pct in [0.05, 0.07, 0.10, 0.13, 0.15, 0.20]:
    r = run('pct', pct)
    navs = [v for _, v in r.nav_curve]
    ret, mdd, sharpe, final = metrics(navs)
    print(f'{"":>5} | pct  | {pct:>5.2f} | {len(r.trades):>6} | {final:>14,.0f} | {ret*100:>+5.1f}% | {sharpe:>+5.2f} | {mdd*100:>+5.1f}%')

# ATR mode
for atr_mult in [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]:
    r = run('atr', atr_mult)
    navs = [v for _, v in r.nav_curve]
    ret, mdd, sharpe, final = metrics(navs)
    print(f'{"":>5} | atr  | {atr_mult:>5.2f} | {len(r.trades):>6} | {final:>14,.0f} | {ret*100:>+5.1f}% | {sharpe:>+5.2f} | {mdd*100:>+5.1f}%')
