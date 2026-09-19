"""Combine best vol_mult with various MaxC."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import json
import math

from darvas_box_strategy.darvas import (
    StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig, run_backtest,
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


print(f'Universe: {len(tickers)} tickers')
print()
print('vol | hi | MaxC | Cool | Trades | Final NAV | Ret   | Sharpe | MaxDD')
print('-' * 80)
for vol_mult, hi_conv in [(3.0, 3.5), (3.5, 4.0)]:
    for maxc in [5, 8, 10, 15, 20, 30]:
        for cool in [5, 10, 20]:
            cfg = StrategyConfig(
                base=BaseConfig(min_days=5, max_days=20, max_range_pct=0.05),
                breakout=BreakoutConfig(
                    require_ma200=True, min_history_days=200,
                    volume_mult=vol_mult, high_conviction_mult=hi_conv,
                ),
                backtest=BacktestConfig(
                    start_nav=1e9,
                    max_concurrent_positions=maxc,
                    cooldown_bars_after_stop=cool,
                ),
            )
            r = run_backtest(cfg, tickers=tickers, prices_dir=PRICES,
                             progress=False, portfolio=True, mtm_mode='close')
            navs = [v for _, v in r.nav_curve]
            ret, mdd, sharpe, final = metrics(navs)
            print(
                f'{vol_mult:>3.1f} | {hi_conv:>3.1f} | {maxc:>4} | {cool:>4} | '
                f'{len(r.trades):>6} | {final:>14,.0f} | {ret*100:>+5.1f}% | '
                f'{sharpe:>+5.2f} | {mdd*100:>+5.1f}%'
            )
