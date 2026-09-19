"""Sweep max_concurrent_positions to find optimal cap."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import json
import math
from darvas_box_strategy.darvas import (
    StrategyConfig,
    BaseConfig,
    BreakoutConfig,
    BacktestConfig,
    run_backtest,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_UNIVERSE_PATH = _PROJECT_ROOT / 'vietnam_algo' / 'data' / 'universe' / 'liquid_stocks.json'
_PRICES_DIR = _PROJECT_ROOT / 'vietnam_algo' / 'data' / 'prices'

tickers = [
    s['symbol']
    for s in json.loads(_UNIVERSE_PATH.read_text())['items']
]

print(f'Universe: {len(tickers)} tickers')
print()
print('MaxConc | Trades | Final NAV | Ret   | MaxDD   | Sharpe')
print('-' * 65)
for maxc in [1, 2, 3, 5, 8, 10, 15, 20]:
    cfg = StrategyConfig(
        base=BaseConfig(min_days=5, max_days=20, max_range_pct=0.05),
        breakout=BreakoutConfig(
            require_ma200=True, min_history_days=200, volume_mult=1.5,
        ),
        backtest=BacktestConfig(
            start_nav=1e9, max_concurrent_positions=maxc,
        ),
    )
    r = run_backtest(
        cfg, tickers=tickers,
        prices_dir=_PRICES_DIR,
        progress=False, portfolio=True,
    )
    final_nav = r.nav_curve[-1][1] if r.nav_curve else cfg.backtest.start_nav
    ret = final_nav / cfg.backtest.start_nav - 1
    navs = [v for _, v in r.nav_curve]
    peak = navs[0] if navs else 1
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
    print(
        f'{maxc:>7} | {len(r.trades):>6} | {final_nav:>14,.0f} | '
        f'{ret*100:>+5.1f}% | {mdd*100:>+6.1f}% | {sharpe:>+5.2f}'
    )
