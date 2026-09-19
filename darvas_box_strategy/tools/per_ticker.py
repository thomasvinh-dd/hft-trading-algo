"""Per-ticker breakdown with portfolio manager."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import json
from collections import defaultdict
from darvas_box_strategy.darvas import (
    StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig, run_backtest,
)

UNIVERSE = Path('/home/vst/Documents/Ai-trade/hft-trading-algo/vietnam_algo/data/universe/liquid_stocks.json')
PRICES = Path('/home/vst/Documents/Ai-trade/hft-trading-algo/vietnam_algo/data/prices')

tickers = [s['symbol'] for s in json.loads(UNIVERSE.read_text())['items']]
cfg = StrategyConfig(
    base=BaseConfig(min_days=5, max_days=20, max_range_pct=0.05),
    breakout=BreakoutConfig(require_ma200=True, min_history_days=200, volume_mult=1.5),
    backtest=BacktestConfig(start_nav=1e9, max_concurrent_positions=10, cooldown_bars_after_stop=10),
)
r = run_backtest(cfg, tickers=tickers, prices_dir=PRICES, progress=False,
                 portfolio=True, mtm_mode='close')

# Per-ticker P&L
by_ticker = defaultdict(lambda: {'n': 0, 'wins': 0, 'pnl': 0.0, 'trades': []})
for t in r.trades:
    agg = by_ticker[t.signal.ticker]
    agg['n'] += 1
    if t.is_winner:
        agg['wins'] += 1
    agg['pnl'] += t.realized_pnl
    agg['trades'].append(t)

# Sort by P&L descending
rows = [(t, a['n'], a['wins'], a['pnl'], a['pnl']/a['n']) for t, a in by_ticker.items()]
rows.sort(key=lambda r: -r[3])

print(f"{'Ticker':<6} {'N':>4} {'Wins':>4} {'PnL (M)':>12} {'Avg/trade':>12}")
print('-' * 42)
total = 0
for t, n, wins, pnl, avg in rows:
    print(f'{t:<6} {n:>4} {wins:>4} {pnl/1e6:>12,.1f} {avg/1e3:>10,.0f}k')
    total += pnl
print('-' * 42)
print(f"{'TOTAL':<6} {'':>4} {'':>4} {total/1e6:>12,.1f}")

# Win rate by ticker
print('\n--- Win rate distribution ---')
win_rates = [wins/n for _, n, wins, _, _ in rows if n > 0]
win_rates.sort()
print(f'Median win rate: {win_rates[len(win_rates)//2]*100:.1f}%')
print(f'Min win rate: {min(win_rates)*100:.1f}% (worst ticker)')
print(f'Max win rate: {max(win_rates)*100:.1f}% (best ticker)')
print(f'Tickers with WR < 45%: {sum(1 for w in win_rates if w < 0.45)}/{len(win_rates)}')
print(f'Tickers with WR > 60%: {sum(1 for w in win_rates if w > 0.60)}/{len(win_rates)}')
