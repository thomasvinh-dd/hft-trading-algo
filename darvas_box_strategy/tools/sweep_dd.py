"""Sweep position-sizing and max-concurrent to find best return/DD ratio.

Use this when the drawdown feels too high. The historical winner
(Oct 2026) was reducing size from 20/40 to 15/30 — see README.

Usage: PYTHONPATH=/path/to/hft-trading-algo python3 sweep_dd.py
"""
import json
import time
from dataclasses import replace
from pathlib import Path

from darvas_box_strategy.darvas.backtest import run_backtest_with_portfolio
from darvas_box_strategy.bot.cli import build_default_config
from darvas_box_strategy.bot.cli import load_blue_chips

prices_dir = Path("/home/vst/Documents/Ai-trade/hft-trading-algo/vietnam_algo/data/prices")
tickers = load_blue_chips()
baseline = build_default_config()
market_index_path = str(Path("/home/vst/Documents/Ai-trade/hft-trading-algo/darvas_box_strategy/data/market_index.json").absolute())


def run(label, **kwargs):
    cfg = baseline
    new_back = replace(
        cfg.backtest,
        max_concurrent_positions=kwargs.get("max_concurrent", cfg.backtest.max_concurrent_positions),
        size_low=kwargs.get("size_low", cfg.backtest.size_low),
        size_high=kwargs.get("size_high", cfg.backtest.size_high),
    )
    cfg = replace(cfg, backtest=new_back)
    if kwargs.get("regime_window"):
        new_breakout = replace(
            cfg.breakout,
            regime_index_path=market_index_path,
            regime_ma_window=kwargs["regime_window"],
            regime_require_above_ma=True,
        )
        cfg = replace(cfg, breakout=new_breakout)

    t0 = time.time()
    portfolio = run_backtest_with_portfolio(
        cfg, prices_dir=prices_dir, tickers=tickers, progress=False,
    )
    elapsed = time.time() - t0

    start_nav = cfg.backtest.start_nav
    final_nav = portfolio._daily_nav[-1][1] if portfolio._daily_nav else start_nav
    total_return = (final_nav - start_nav) / start_nav * 100
    navs = [n for _, n in portfolio._daily_nav]
    peak = navs[0]
    max_dd = 0
    for n in navs:
        if n > peak:
            peak = n
        dd = (n - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
    n_trades = len(portfolio.history)
    closed = [t for t in portfolio.history if t.exit_reason != "EXHAUST"]
    winners = [t for t in closed if t.realized_pnl > 0]
    losers = [t for t in closed if t.realized_pnl <= 0]
    win_rate = len(winners) / len(closed) * 100 if closed else 0
    total_gain = sum(t.realized_pnl for t in winners)
    total_loss = abs(sum(t.realized_pnl for t in losers))
    pf = total_gain / total_loss if total_loss > 0 else float('inf')
    ret_dd = total_return / abs(max_dd) if max_dd < 0 else float('inf')

    print(f"  {label:<40s} ret={total_return:+6.1f}% DD={max_dd:6.1f}% R/DD={ret_dd:5.2f} trades={n_trades:4d} WR={win_rate:.1f}% PF={pf:.2f} ({elapsed:.0f}s)")
    return {"label": label, "return": total_return, "max_dd": max_dd, "ret_dd": ret_dd}


def main():
    print(f"Sweeping {len(tickers)} blue chips, "
          f"{len(baseline.breakout.min_history_days) and 'configurable'} config...")
    print()
    print("Single-factor sweeps:")
    results = []
    results.append(run("baseline (max=10, size=20/40)"))
    results.append(run("reduce_max=5", max_concurrent=5))
    results.append(run("reduce_max=7", max_concurrent=7))
    results.append(run("reduce_size=15/30", size_low=0.15, size_high=0.30))
    results.append(run("reduce_size=12/25", size_low=0.12, size_high=0.25))
    results.append(run("regime_50", regime_window=50))
    results.append(run("regime_200", regime_window=200))
    results.append(run("regime_100", regime_window=100))

    print()
    print("Combinations:")
    results.append(run("size 15/30 + max=7", size_low=0.15, size_high=0.30, max_concurrent=7))
    results.append(run("size 15/30 + max=8", size_low=0.15, size_high=0.30, max_concurrent=8))
    results.append(run("size 15/30 + regime_200", size_low=0.15, size_high=0.30, regime_window=200))

    print()
    print("Best by Return/DD ratio:")
    results.sort(key=lambda r: r["ret_dd"], reverse=True)
    for r in results[:5]:
        print(f"  {r['label']:<40s} R/DD={r['ret_dd']:5.2f}  ret={r['return']:+6.1f}%  DD={r['max_dd']:6.1f}%")


if __name__ == "__main__":
    main()
