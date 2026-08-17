from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .backtest import (
    run_hybrid_momentum_breakout_sector,
    run_market_regime_benchmark,
    run_momentum_rotation,
    run_pullback_mean_reversion,
    run_sector_momentum,
    run_volume_breakout,
    save_backtest_result,
    save_order_list,
)
from .auto_bot import run_auto_bot
from .config import StrategyConfig
from .daily_engine import EngineConfig, run_daily_once, trading_token_from_email
from .data import DEFAULT_DATA_DIR, DEFAULT_PRICE_LIMIT, load_saved_symbols, sync_prices, sync_symbols
from .dnse_client import DEFAULT_DNSE_BASE_URL, DnseClient
from .trading_engine import execute_dnse_orders, execute_dnse_orders_with_monitoring, prepare_dnse_orders
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

    pullback_parser = subparsers.add_parser("backtest-pullback", help="backtest pullback mean reversion")
    add_backtest_arguments(pullback_parser)

    sector_parser = subparsers.add_parser("backtest-sector", help="backtest sector momentum rotation")
    add_backtest_arguments(sector_parser)

    benchmark_parser = subparsers.add_parser("backtest-market", help="backtest market-regime benchmark")
    add_backtest_arguments(benchmark_parser)

    hybrid_parser = subparsers.add_parser("backtest-hybrid", help="backtest hybrid momentum, breakout, and sector strategy")
    add_backtest_arguments(hybrid_parser)

    dnse_accounts = subparsers.add_parser("dnse-accounts", help="list DNSE trading accounts")
    add_dnse_auth_arguments(dnse_accounts)

    dnse_positions = subparsers.add_parser("dnse-positions", help="list DNSE stock positions")
    add_dnse_auth_arguments(dnse_positions)
    dnse_positions.add_argument("--account-no", required=True)

    dnse_ppse = subparsers.add_parser("dnse-ppse", help="check DNSE buying/selling power for a symbol")
    add_dnse_auth_arguments(dnse_ppse)
    dnse_ppse.add_argument("--account-no", required=True)
    dnse_ppse.add_argument("--symbol", required=True)
    dnse_ppse.add_argument("--price", type=float, required=True)
    dnse_ppse.add_argument("--loan-package-id", type=int, required=True)

    dnse_otp = subparsers.add_parser("dnse-send-email-otp", help="request DNSE email OTP")
    add_dnse_auth_arguments(dnse_otp)

    dnse_token = subparsers.add_parser("dnse-trading-token", help="exchange OTP for DNSE trading token")
    add_dnse_auth_arguments(dnse_token)
    dnse_token.add_argument("--otp-type", choices=["email_otp", "smart_otp"], default="smart_otp")
    dnse_token.add_argument("--passcode", required=True)

    preview = subparsers.add_parser("dnse-preview-orders", help="preview DNSE orders converted from algo CSV")
    add_order_engine_arguments(preview)

    submit = subparsers.add_parser("dnse-submit-orders", help="submit DNSE orders converted from algo CSV; requires --yes")
    add_dnse_auth_arguments(submit)
    add_order_engine_arguments(submit)
    submit.add_argument("--account-no", required=True)
    submit.add_argument("--trading-token", default=os.environ.get("DNSE_TRADING_TOKEN", ""))
    submit.add_argument("--yes", action="store_true", help="actually submit orders to DNSE")
    submit.add_argument("--monitor", action="store_true", help="monitor submitted orders and replace price toward current order book")
    submit.add_argument("--max-checks", type=int, default=3)
    submit.add_argument("--check-interval-seconds", type=float, default=5.0)

    daily_generate = subparsers.add_parser("daily-generate-orders", help="update local data, run strategy, and export latest live orders")
    add_daily_engine_arguments(daily_generate)
    add_backtest_arguments(daily_generate)

    daily_submit = subparsers.add_parser("daily-submit-orders", help="generate latest live orders and submit to DNSE; requires --yes")
    add_dnse_auth_arguments(daily_submit)
    add_daily_engine_arguments(daily_submit)
    add_backtest_arguments(daily_submit)
    daily_submit.add_argument("--account-no", required=True)
    daily_submit.add_argument("--loan-package-id", type=int, required=True)
    daily_submit.add_argument("--trading-token", default=os.environ.get("DNSE_TRADING_TOKEN", ""))
    daily_submit.add_argument("--auto-email-otp", action="store_true", help="request DNSE email OTP, read Gmail, and create trading token")
    daily_submit.add_argument("--yes", action="store_true", help="actually submit generated orders to DNSE")

    auto_bot = subparsers.add_parser("auto-trading-bot", help="run all-day Vietnam auto trading bot")
    add_dnse_auth_arguments(auto_bot)
    add_daily_engine_arguments(auto_bot)
    add_backtest_arguments(auto_bot)
    auto_bot.add_argument("--account-no", required=True)
    auto_bot.add_argument("--loan-package-id", type=int, required=True)
    auto_bot.add_argument("--sleep-seconds", type=int, default=60)
    auto_bot.add_argument("--once", action="store_true", help="evaluate schedule once and exit")
    auto_bot.add_argument("--live-submit", action="store_true", help="enable real DNSE submissions and real email OTP")
    return parser


def add_dnse_auth_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-key", default=os.environ.get("DNSE_API_KEY", ""))
    parser.add_argument("--api-secret", default=os.environ.get("DNSE_API_SECRET", ""))
    parser.add_argument("--base-url", default=os.environ.get("DNSE_BASE_URL", DEFAULT_DNSE_BASE_URL))


def add_order_engine_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--orders-file",
        type=Path,
        default=DEFAULT_DATA_DIR / "backtests" / "liquid_momentum_rotation_orders.csv",
    )
    parser.add_argument("--loan-package-id", type=int, required=True)
    parser.add_argument("--trade-date", default=None, help="only process orders with this execution date YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--order-type", default="LO")
    parser.add_argument("--board-lot-size", type=int, default=100)


def add_daily_engine_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--strategy", choices=["momentum", "hybrid"], default="momentum")
    parser.add_argument("--trade-date", default=None, help="optional order execution date to export/submit")
    parser.add_argument("--update-symbols", action="store_true")
    parser.add_argument("--update-prices", action="store_true")
    parser.add_argument("--price-limit", type=int, default=DEFAULT_PRICE_LIMIT)
    parser.add_argument("--price-sleep-seconds", type=float, default=1.0)
    parser.add_argument("--max-symbols", type=int, default=0)


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def dnse_client_from_args(args: argparse.Namespace) -> DnseClient:
    if not args.api_key or not args.api_secret:
        raise RuntimeError("DNSE credentials are required. Set DNSE_API_KEY/DNSE_API_SECRET or pass --api-key/--api-secret.")
    return DnseClient(args.api_key, args.api_secret, base_url=args.base_url)


def strategy_config_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
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
        rsiWindow=args.rsi_window,
        pullbackRsiEntry=args.pullback_rsi_entry,
        pullbackRsiExit=args.pullback_rsi_exit,
        pullbackLookbackDays=args.pullback_lookback_days,
        pullbackMinPercent=args.pullback_min_percent,
        pullbackMaxPercent=args.pullback_max_percent,
        pullbackVolumeDays=args.pullback_volume_days,
        pullbackPanicVolumeMultiplier=args.pullback_panic_volume_multiplier,
        pullbackExitSmaDays=args.pullback_exit_sma_days,
        pullbackMaxHoldingDays=args.pullback_max_holding_days,
        maxPullbackEntriesPerWeek=args.max_pullback_entries_per_week,
        sectorReturn1mDays=args.sector_return_1m_days,
        sectorReturn3mDays=args.sector_return_3m_days,
        topSectors=args.top_sectors,
        topStocksPerSector=args.top_stocks_per_sector,
        sectorExitRank=args.sector_exit_rank,
        benchmarkTopSymbols=args.benchmark_top_symbols,
        hybridTopSectors=args.hybrid_top_sectors,
        hybridBreakoutRequired=args.hybrid_breakout_required,
    )


def engine_config_from_args(args: argparse.Namespace) -> EngineConfig:
    return EngineConfig(
        dataDir=args.data_dir,
        strategy=args.strategy,
        updateSymbols=args.update_symbols,
        updatePrices=args.update_prices,
        priceLimit=args.price_limit,
        priceSleepSeconds=args.price_sleep_seconds,
        maxSymbols=args.max_symbols,
    )


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
    backtest_parser.add_argument("--rsi-window", type=int, default=5)
    backtest_parser.add_argument("--pullback-rsi-entry", type=float, default=30.0)
    backtest_parser.add_argument("--pullback-rsi-exit", type=float, default=55.0)
    backtest_parser.add_argument("--pullback-lookback-days", type=int, default=10)
    backtest_parser.add_argument("--pullback-min-percent", type=float, default=3.0)
    backtest_parser.add_argument("--pullback-max-percent", type=float, default=12.0)
    backtest_parser.add_argument("--pullback-volume-days", type=int, default=20)
    backtest_parser.add_argument("--pullback-panic-volume-multiplier", type=float, default=2.5)
    backtest_parser.add_argument("--pullback-exit-sma-days", type=int, default=20)
    backtest_parser.add_argument("--pullback-max-holding-days", type=int, default=10)
    backtest_parser.add_argument("--max-pullback-entries-per-week", type=int, default=3)
    backtest_parser.add_argument("--sector-return-1m-days", type=int, default=21)
    backtest_parser.add_argument("--sector-return-3m-days", type=int, default=63)
    backtest_parser.add_argument("--top-sectors", type=int, default=3)
    backtest_parser.add_argument("--top-stocks-per-sector", type=int, default=3)
    backtest_parser.add_argument("--sector-exit-rank", type=int, default=5)
    backtest_parser.add_argument("--benchmark-top-symbols", type=int, default=20)
    backtest_parser.add_argument("--hybrid-top-sectors", type=int, default=3)
    backtest_parser.add_argument("--hybrid-breakout-required", type=int, default=0)


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

    if args.command in {
        "backtest-momentum",
        "backtest-breakout",
        "backtest-pullback",
        "backtest-sector",
        "backtest-market",
        "backtest-hybrid",
    }:
        config = strategy_config_from_args(args)
        if args.command == "backtest-hybrid":
            result = run_hybrid_momentum_breakout_sector(args.data_dir, args.universe_name, config)
        elif args.command == "backtest-market":
            result = run_market_regime_benchmark(args.data_dir, args.universe_name, config)
        elif args.command == "backtest-sector":
            result = run_sector_momentum(args.data_dir, args.universe_name, config)
        elif args.command == "backtest-pullback":
            result = run_pullback_mean_reversion(args.data_dir, args.universe_name, config)
        elif args.command == "backtest-breakout":
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

    if args.command == "dnse-accounts":
        print_json(dnse_client_from_args(args).accounts())
        return 0

    if args.command == "dnse-positions":
        print_json(dnse_client_from_args(args).positions(args.account_no))
        return 0

    if args.command == "dnse-ppse":
        print_json(dnse_client_from_args(args).ppse(args.account_no, args.symbol, args.loan_package_id, args.price))
        return 0

    if args.command == "dnse-send-email-otp":
        print_json(dnse_client_from_args(args).send_email_otp())
        return 0

    if args.command == "dnse-trading-token":
        print_json({"tradingToken": dnse_client_from_args(args).trading_token(args.otp_type, args.passcode)})
        return 0

    if args.command == "dnse-preview-orders":
        orders = prepare_dnse_orders(
            args.orders_file,
            args.loan_package_id,
            trade_date=args.trade_date,
            limit=args.limit,
            order_type=args.order_type,
            board_lot_size=args.board_lot_size,
        )
        print_json([order.__dict__ for order in orders])
        return 0

    if args.command == "dnse-submit-orders":
        if not args.trading_token:
            raise RuntimeError("DNSE trading token is required. Use --trading-token or set DNSE_TRADING_TOKEN.")
        orders = prepare_dnse_orders(
            args.orders_file,
            args.loan_package_id,
            trade_date=args.trade_date,
            limit=args.limit,
            order_type=args.order_type,
            board_lot_size=args.board_lot_size,
        )
        if args.monitor:
            results = execute_dnse_orders_with_monitoring(
                dnse_client_from_args(args),
                args.account_no,
                args.trading_token,
                orders,
                dry_run=not args.yes,
                max_checks=args.max_checks,
                check_interval_seconds=args.check_interval_seconds,
            )
        else:
            results = execute_dnse_orders(
                dnse_client_from_args(args),
                args.account_no,
                args.trading_token,
                orders,
                dry_run=not args.yes,
            )
        print_json(results)
        return 0

    if args.command == "daily-generate-orders":
        print_json(run_daily_once(engine_config_from_args(args), strategy_config_from_args(args), trade_date=args.trade_date))
        return 0

    if args.command == "daily-submit-orders":
        if not args.yes:
            raise RuntimeError("Refusing real submit without --yes. Use daily-generate-orders first to inspect latest_orders.csv.")
        client = dnse_client_from_args(args)
        trading_token = args.trading_token
        if args.auto_email_otp:
            trading_token = trading_token_from_email(client)
        if not trading_token:
            raise RuntimeError("DNSE trading token is required. Use --trading-token, DNSE_TRADING_TOKEN, or --auto-email-otp.")
        print_json(
            run_daily_once(
                engine_config_from_args(args),
                strategy_config_from_args(args),
                trade_date=args.trade_date,
                submit=True,
                account_no=args.account_no,
                loan_package_id=args.loan_package_id,
                trading_token=trading_token,
                client=client,
            )
        )
        return 0

    if args.command == "auto-trading-bot":
        if not args.live_submit:
            print("Running in dry-run mode. Add --live-submit only when ready for real DNSE orders.")
        run_auto_bot(
            engine_config_from_args(args),
            strategy_config_from_args(args),
            dnse_client_from_args(args),
            args.account_no,
            args.loan_package_id,
            live_submit=args.live_submit,
            once=args.once,
            sleep_seconds=args.sleep_seconds,
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
