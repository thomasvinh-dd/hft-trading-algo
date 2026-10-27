"""CLI entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from darvas_box_strategy.darvas import (
    StrategyConfig,
    recent_trades,
    run_backtest,
    summary_report,
    ticker_breakdown,
    trade_detail,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the Darvas Box + AutoFin-tiered-exits backtest."
    )
    parser.add_argument(
        "--ticker", action="append", default=None,
        help="Ticker to scan (repeatable).",
    )
    parser.add_argument(
        "--prices-dir", type=Path, default=None,
        help="Override the default prices directory.",
    )
    parser.add_argument(
        "--detail", action="store_true",
        help="Print fill-by-fill detail for every closed trade.",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="How many recent trades to show.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-ticker progress output.",
    )
    parser.add_argument(
        "--max-range-pct", type=float, default=None,
        help="Override BaseConfig.max_range_pct (default 0.030).",
    )
    parser.add_argument(
        "--volume-mult", type=float, default=None,
        help="Override BreakoutConfig.volume_mult (default 1.5).",
    )
    parser.add_argument(
        "--start-nav", type=float, default=None,
        help="Override BacktestConfig.start_nav (default 1e9 VND).",
    )
    parser.add_argument(
        "--max-concurrent-positions", type=int, default=None,
        help="Override BacktestConfig.max_concurrent_positions (default 8).",
    )
    args = parser.parse_args(argv)

    cfg = StrategyConfig.default()
    if args.max_range_pct is not None:
        cfg = _replace(cfg, "base", _replace(cfg.base, "max_range_pct", args.max_range_pct))
    if args.volume_mult is not None:
        cfg = _replace(cfg, "breakout", _replace(cfg.breakout, "volume_mult", args.volume_mult))
    if args.start_nav is not None:
        cfg = _replace(cfg, "backtest", _replace(cfg.backtest, "start_nav", args.start_nav))
    if args.max_concurrent_positions is not None:
        cfg = _replace(cfg, "backtest", _replace(
            cfg.backtest, "max_concurrent_positions", args.max_concurrent_positions,
        ))

    prices_dir = args.prices_dir or (ROOT.parent / "vietnam_algo" / "data" / "prices")
    prices_dir = prices_dir.resolve()
    print(f"Prices directory: {prices_dir}")
    print(f"Portfolio mode: ON (max_concurrent={cfg.backtest.max_concurrent_positions}, "
          f"cooldown={cfg.backtest.cooldown_bars_after_stop})")

    result = run_backtest(
        cfg,
        prices_dir=prices_dir,
        tickers=args.ticker,
        progress=not args.quiet,
        portfolio=True,
        mtm_mode="close",
    )
    print(f"Backtest done. trades={len(result.trades)}, nav_pts={len(result.nav_curve)}", flush=True)
    print()
    print(summary_report(result))
    print()
    print("--- Per-ticker breakdown ---")
    print(ticker_breakdown(result))
    print()
    print(f"--- Recent trades (last {args.limit}) ---")
    print(recent_trades(result, n=args.limit))

    if args.detail and result.trades:
        print()
        print("=" * 72)
        print("FILL-LEVEL DETAIL")
        print("=" * 72)
        for t in sorted(result.trades, key=lambda x: (x.signal.entry_date, x.signal.ticker)):
            print(trade_detail(t))
            print()

    return 0


def _replace(obj, field_name, value):
    import dataclasses
    return dataclasses.replace(obj, **{field_name: value})


if __name__ == "__main__":
    raise SystemExit(main())
