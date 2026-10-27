"""Finer sweep around MaxC=15, Cool=10."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import json
import math

from darvas_box_strategy.darvas import (
    StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig, run_backtest,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_UNIVERSE = _PROJECT_ROOT / 'vietnam_algo' / 'data' / 'universe' / 'liquid_stocks.json'
_PRICES = _PROJECT_ROOT / 'vietnam_algo' / 'data' / 'prices'

tickers = [s['symbol'] for s in json.loads(_UNIVERSE.read_text())['items']]


def metrics(navs):
    if len(navs) < 2:
        return 0, 0, 0
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
    return ret, mdd, sharpe


print(f'Universe: {len(tickers)} tickers')
print('Fine sweep around MaxC=15, Cool=10')
print()
print('MaxC | Cool | Trades | Skipped | Final NAV | Ret   | MaxDD   | Sharpe')
print('-' * 80)

for maxc in [10, 12, 15, 20, 25, 30]:
    for cooldown in [0, 5, 10, 15, 20, 30]:
        cfg = StrategyConfig(
            base=BaseConfig(min_days=5, max_days=20, max_range_pct=0.05),
            breakout=BreakoutConfig(require_ma200=True, min_history_days=200, volume_mult=1.5),
            backtest=BacktestConfig(
                start_nav=1e9,
                max_concurrent_positions=maxc,
                cooldown_bars_after_stop=cooldown,
            ),
        )
        r = run_backtest(
            cfg, tickers=tickers, prices_dir=_PRICES,
            progress=False, portfolio=True, mtm_mode='close',
        )
        navs = [v for _, v in r.nav_curve]
        if not navs:
            continue
        ret, mdd, sharpe = metrics(navs)
        print(
            f'{maxc:>4} | {cooldown:>4} | {len(r.trades):>6} | {r.concurrent_skipped:>7} | '
            f'{navs[-1]:>14,.0f} | {ret*100:>+5.1f}% | {mdd*100:>+6.1f}% | {sharpe:>+5.2f}'
        )
