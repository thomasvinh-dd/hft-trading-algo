from __future__ import annotations

import argparse
from pathlib import Path

from .backtest import run_momentum_rotation, run_volume_breakout, save_backtest_result, save_order_list
from .config import StrategyConfig
from .data import DEFAULT_DATA_DIR, DEFAULT_PRICE_LIMIT, load_saved_symbols, sync_prices, sync_symbols
from .universe import build_liquid_universe, save_liquid_universe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vietnam-algo")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--timeout", type=float, default=30.0)
    subparsers = parser.add_subparsers(dest="command", required=True)

    symbols_parser = subparsers.add_parser("sync-symbols", help="fetch and store all Vietnam stock symbols")
    symbols_parser.add_argument("--page-limit", type=int, default=50)

    prices_parser = subparsers.add_parser("sync-prices", help="fetch and store historical prices")
    prices_parser.add_argument("--symbols", nargs="+", default=None, help="symbols to fetch; defaults to saved symbols.json")
    prices_parser.add_argument("--price-limit", type=int, default=DEFAULT_PRICE_LIMIT)
    prices_parser.add_argument("--sleep-seconds", type=float, default=0.2)
    prices_parser.add_argument("--no-skip-existing", action="store_true", help="refetch price files that already exist")

    all_parser = subparsers.add_parser("sync-all", help="fetch symbols then historical prices")
    all_parser.add_argument("--page-limit", type=int, default=50)
    all_parser.add_argument("--price-limit", type=int, default=DEFAULT_PRICE_LIMIT)
    all_parser.add_argument("--sleep-seconds", type=float, default=0.2)
    all_parser.add_argument("--max-symbols", type=int, default=0, help="limit symbols for testing; 0 means all")
    all_parser.add_argument("--no-skip-existing", action="store_true", help="refetch price files that already exist")

    universe_parser = subparsers.add_parser("build-universe", help="build a high-liquidity stock universe")
    universe_parser.add_argument("--lookback-days", type=int, default=20)
    universe_parser.add_argument("--min-avg-value", type=float, default=20_000_000_000)
    universe_parser.add_argument("--min-avg-volume", type=float, default=300_000)
    universe_parser.add_argument("--min-price", type=float, default=10_000)
    universe_parser.add_argument("--min-history-days", type=int, default=252)
    universe_parser.add_argument("--max-symbols", type=int, default=100)
    universe_parser.add_argument("--name", default="liquid_stocks")

    backtest_parser = subparsers.add_parser("backtest-momentum", help="backtest liquid momentum rotation")
    add_backtest_arguments(backtest_parser)

    breakout_parser = subparsers.add_parser("backtest-breakout", help="backtest standalone volume breakout")
    add_backtest_arguments(breakout_parser)
    return parser


def add_backtest_arguments(backtest_parser: argparse.ArgumentParser) -> None:
    backtest_parser.add_argument("--universe-name", default="liquid_stocks")
    backtest_parser.add_argument("--initial-cash", type=float, default=1_000_000_000)
    backtest_parser.add_argument("--max-positions", type=int, default=5)
    backtest_parser.add_argument("--bear-max-positions", type=int, default=2)
    backtest_parser.add_argument("--board-lot-size", type=int, default=100)
    backtest_parser.add_argument("--sell-settlement-days", type=int, default=3)
    backtest_parser.add_argument("--rebalance-days", type=int, default=5)
    backtest_parser.add_argument("--breakout-lookback-days", type=int, default=55)
    backtest_parser.add_argument("--breakout-volume-days", type=int, default=20)
    backtest_parser.add_argument("--breakout-volume-multiplier", type=float, default=1.5)
    backtest_parser.add_argument("--breakout-max-rank", type=int, default=20)
    backtest_parser.add_argument("--breakout-score-bonus", type=float, default=1.0)
    backtest_parser.add_argument("--max-breakout-entries-per-week", type=int, default=2)
    backtest_parser.add_argument("--max-sector-positions", type=int, default=2)
    backtest_parser.add_argument("--max-sma50-extension-percent", type=float, default=25.0)
    backtest_parser.add_argument("--market-sma-days", type=int, default=100)
    backtest_parser.add_argument("--market-bear-sma-days", type=int, default=200)
    backtest_parser.add_argument(
        "--market-exit-regime",
        type=int,
        default=1,
        help="0 disables regime exits, 1 exits in bear, 2 exits in neutral or bear",
    )
    backtest_parser.add_argument("--neutral-trailing-stop-percent", type=float, default=7.0)
    backtest_parser.add_argument("--bear-trailing-stop-percent", type=float, default=5.0)
    backtest_parser.add_argument("--bull-exposure-percent", type=float, default=100.0)
    backtest_parser.add_argument("--neutral-exposure-percent", type=float, default=50.0)
    backtest_parser.add_argument("--bear-exposure-percent", type=float, default=20.0)
    backtest_parser.add_argument("--exit-rank", type=int, default=20)
    backtest_parser.add_argument("--stop-loss-percent", type=float, default=8.0)
    backtest_parser.add_argument("--trailing-stop-percent", type=float, default=10.0)
    backtest_parser.add_argument("--fee-rate", type=float, default=0.0015)
    backtest_parser.add_argument("--slippage-rate", type=float, default=0.001)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sync-symbols":
        path = sync_symbols(args.data_dir, limit=args.page_limit, timeout=args.timeout)
        print(f"Saved Vietnam symbols to {path}")
        return 0

    if args.command == "sync-prices":
        symbols = [symbol.upper() for symbol in args.symbols] if args.symbols else load_saved_symbols(args.data_dir)
        paths = sync_prices(
            symbols,
            args.data_dir,
            args.price_limit,
            args.timeout,
            args.sleep_seconds,
            skip_existing=not args.no_skip_existing,
        )
        print(f"Saved {len(paths)} Vietnam price files under {args.data_dir / 'prices'}")
        return 0

    if args.command == "sync-all":
        symbols_path = sync_symbols(args.data_dir, limit=args.page_limit, timeout=args.timeout)
        symbols = load_saved_symbols(args.data_dir)
        if args.max_symbols > 0:
            symbols = symbols[: args.max_symbols]
        paths = sync_prices(
            symbols,
            args.data_dir,
            args.price_limit,
            args.timeout,
            args.sleep_seconds,
            skip_existing=not args.no_skip_existing,
        )
        print(f"Saved Vietnam symbols to {symbols_path}")
        print(f"Saved {len(paths)} Vietnam price files under {args.data_dir / 'prices'}")
        return 0

    if args.command == "build-universe":
        universe = build_liquid_universe(
            args.data_dir,
            lookback_days=args.lookback_days,
            min_avg_value=args.min_avg_value,
            min_avg_volume=args.min_avg_volume,
            min_price=args.min_price,
            min_history_days=args.min_history_days,
            max_symbols=args.max_symbols,
        )
        path = save_liquid_universe(universe, args.data_dir, args.name)
        print(f"Saved {len(universe)} liquid stocks to {path}")
        for stock in universe[:20]:
            print(f"{stock.symbol}: avgValue20={stock.avgValue20:,.0f} avgVolume20={stock.avgVolume20:,.0f}")
        return 0

    if args.command in {"backtest-momentum", "backtest-breakout"}:
        config = StrategyConfig(
            initialCash=args.initial_cash,
            maxPositions=args.max_positions,
            bearMaxPositions=args.bear_max_positions,
            boardLotSize=args.board_lot_size,
            sellSettlementDays=args.sell_settlement_days,
            rebalanceDays=args.rebalance_days,
            breakoutLookbackDays=args.breakout_lookback_days,
            breakoutVolumeDays=args.breakout_volume_days,
            breakoutVolumeMultiplier=args.breakout_volume_multiplier,
            breakoutMaxRank=args.breakout_max_rank,
            breakoutScoreBonus=args.breakout_score_bonus,
            maxBreakoutEntriesPerWeek=args.max_breakout_entries_per_week,
            maxSectorPositions=args.max_sector_positions,
            maxSma50ExtensionPercent=args.max_sma50_extension_percent,
            marketSmaDays=args.market_sma_days,
            marketBearSmaDays=args.market_bear_sma_days,
            marketExitRegime=args.market_exit_regime,
            neutralTrailingStopPercent=args.neutral_trailing_stop_percent,
            bearTrailingStopPercent=args.bear_trailing_stop_percent,
            bullExposurePercent=args.bull_exposure_percent,
            neutralExposurePercent=args.neutral_exposure_percent,
            bearExposurePercent=args.bear_exposure_percent,
            exitRank=args.exit_rank,
            stopLossPercent=args.stop_loss_percent,
            trailingStopPercent=args.trailing_stop_percent,
            feeRate=args.fee_rate,
            slippageRate=args.slippage_rate,
        )
        if args.command == "backtest-breakout":
            result = run_volume_breakout(args.data_dir, args.universe_name, config)
        else:
            result = run_momentum_rotation(args.data_dir, args.universe_name, config)
        path = save_backtest_result(result, args.data_dir)
        orders_path = save_order_list(result, args.data_dir)
        print(f"Saved backtest result to {path}")
        print(f"Saved order list to {orders_path}")
        for key, value in result.metrics.items():
            print(f"{key}: {value}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
