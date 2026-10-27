"""Final combined sweep with all improvements."""
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


print(f'Universe: {len(tickers)} tickers')
print()
print('All combined sweep')
print('vol | hi  | MaxC | Cool | Ret      | Sharpe | MaxDD')
print('-' * 70)

configs = [
    # (vol, hi, maxc, cool, trail_mode, trail_mult, label)
    (1.5, 2.5, 10, 10, 'pct', 0.10, 'BASELINE (vol=1.5, pct trail)'),
    (3.0, 3.5, 10, 10, 'pct', 0.10, 'vol=3.0 only'),
    (3.0, 3.5, 10, 10, 'atr', 1.5, 'vol=3.0 + atr@1.5  ← CURRENT DEFAULT'),
    (3.0, 3.5, 10, 10, 'atr', 2.0, 'vol=3.0 + atr@2.0'),
    (3.0, 3.5, 10, 10, 'atr', 4.0, 'vol=3.0 + atr@4.0'),
    (3.0, 3.5, 15, 10, 'atr', 1.5, 'vol=3.0 + atr@1.5 + MaxC=15'),
    (3.0, 3.5, 8, 10, 'atr', 1.5, 'vol=3.0 + atr@1.5 + MaxC=8'),
    (3.0, 3.5, 10, 5, 'atr', 1.5, 'vol=3.0 + atr@1.5 + Cool=5'),
    (3.0, 3.5, 10, 20, 'atr', 1.5, 'vol=3.0 + atr@1.5 + Cool=20'),
    (3.5, 4.0, 10, 10, 'atr', 1.5, 'vol=3.5 + atr@1.5'),
    (3.0, 3.5, 10, 10, 'atr', 1.0, 'vol=3.0 + atr@1.0 (tightest)'),
    (3.0, 3.5, 10, 10, 'atr', 3.0, 'vol=3.0 + atr@3.0'),
]

for vol, hi, maxc, cool, tmode, tmult, label in configs:
    trail = TrailingConfig(
        trail_mode=tmode,
        trail_from_peak_pct=tmult if tmode == 'pct' else 0.10,
        trail_atr_mult=tmult if tmode == 'atr' else 2.0,
    )
    cfg = StrategyConfig(
        base=BaseConfig(min_days=5, max_days=20, max_range_pct=0.05),
        breakout=BreakoutConfig(
            require_ma200=True, min_history_days=200,
            volume_mult=vol, high_conviction_mult=hi,
        ),
        backtest=BacktestConfig(
            start_nav=1e9,
            max_concurrent_positions=maxc,
            cooldown_bars_after_stop=cool,
        ),
        trailing=trail,
    )
    r = run_backtest(cfg, tickers=tickers, prices_dir=PRICES,
                     progress=False, portfolio=True, mtm_mode='close')
    navs = [v for _, v in r.nav_curve]
    ret, mdd, sharpe, final = metrics(navs)
    print(f'{vol:>3.1f} | {hi:>3.1f} | {maxc:>4} | {cool:>4} | {ret*100:>+7.1f}% | {sharpe:>+5.2f} | {mdd*100:>+5.1f}%  | {label}')
