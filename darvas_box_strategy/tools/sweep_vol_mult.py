"""Sweep volume_mult (entry filter tightness)."""
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


print(f'Universe: {len(tickers)} tickers, MaxC=10, Cool=10')
print()
print('vol_mult | hi_conv | Trades | Skipped | Final NAV | Ret   | Sharpe | MaxDD')
print('-' * 78)
for vol_mult in [1.2, 1.5, 1.8, 2.0, 2.5, 3.0]:
    for hi_conv in [vol_mult + 0.5, vol_mult + 1.0]:
        cfg = StrategyConfig(
            base=BaseConfig(min_days=5, max_days=20, max_range_pct=0.05),
            breakout=BreakoutConfig(
                require_ma200=True, min_history_days=200,
                volume_mult=vol_mult, high_conviction_mult=hi_conv,
            ),
            backtest=BacktestConfig(
                start_nav=1e9,
                max_concurrent_positions=10,
                cooldown_bars_after_stop=10,
            ),
        )
        r = run_backtest(cfg, tickers=tickers, prices_dir=PRICES,
                         progress=False, portfolio=True, mtm_mode='close')
        navs = [v for _, v in r.nav_curve]
        ret, mdd, sharpe, final = metrics(navs)
        # Compute win rate
        wins = sum(1 for t in r.trades if t.is_winner)
        wr = wins / len(r.trades) if r.trades else 0
        print(
            f'{vol_mult:>7.2f} | {hi_conv:>6.2f} | {len(r.trades):>6} | {r.concurrent_skipped:>7} | '
            f'{final:>14,.0f} | {ret*100:>+5.1f}% | {sharpe:>+5.2f} | {mdd*100:>+5.1f}%'
        )
