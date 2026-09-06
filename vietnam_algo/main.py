from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
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
from .corporate_actions import fetch_vietstock_events, save_corporate_actions, sync_corporate_actions_incremental
from .daily_engine import EngineConfig, run_daily_once, trading_token_from_email
from .data import DEFAULT_DATA_DIR, DEFAULT_PRICE_LIMIT, load_saved_symbols, sync_prices, sync_symbols
from .data_investing import (
    DEFAULT_INVESTING_PRICES_DIR,
    fetch_investing_history,
    fetch_investing_symbols,
    load_investing_id_map,
    save_investing_id_map,
    sync_investing_prices,
)
from .data_investing import (
    load_investing_prices as load_investing_prices_func,
)
from .dnse_client import (
    DEFAULT_DNSE_BASE_URL,
    DEFAULT_DNSE_VERSION,
    DnseClient,
    sync_dnse_instruments,
    sync_dnse_prices,
)
from .market_calendar import live_trading_dates, sync_market_holidays
from .notifier import TelegramConfig, notify
from .risk_controls import live_risk_limits_from_env, validate_live_orders
from .schedule import TradingSchedule
from .trading_engine import (
    build_corporate_action_exit_orders,
    apply_sellable_quantity_cap,
    execute_dnse_orders,
    execute_dnse_orders_with_monitoring,
    extract_sellable_positions,
    prepare_dnse_orders_with_risk_report,
    price_orders_from_orderbook,
    selected_order_date,
)
from .daily_engine import execute_orders_with_ledger
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

    events_parser = subparsers.add_parser("sync-corporate-actions", help="fetch Vietstock events into corporate_action.json")
    events_parser.add_argument("--from-date", required=True, help="start date YYYY-MM-DD")
    events_parser.add_argument("--to-date", required=True, help="end date YYYY-MM-DD")
    events_parser.add_argument("--vietstock-token", default=os.environ.get("VIETSTOCK_REQUEST_TOKEN", ""))
    events_parser.add_argument("--vietstock-cookie", default=os.environ.get("VIETSTOCK_COOKIE", ""))
    events_parser.add_argument("--page", type=int, default=1)
    events_parser.add_argument("--page-size", type=int, default=100)
    events_parser.add_argument("--symbol", default="", help="optional single stock symbol")

    events_incremental_parser = subparsers.add_parser("sync-corporate-actions-incremental", help="fetch new Vietstock events and merge into corporate_action.json")
    events_incremental_parser.add_argument("--to-date", default=None, help="end date YYYY-MM-DD; defaults to today")
    events_incremental_parser.add_argument("--vietstock-token", default=os.environ.get("VIETSTOCK_REQUEST_TOKEN", ""))
    events_incremental_parser.add_argument("--vietstock-cookie", default=os.environ.get("VIETSTOCK_COOKIE", ""))
    events_incremental_parser.add_argument("--page-size", type=int, default=100)

    holiday_parser = subparsers.add_parser("sync-market-holidays", help="fetch Vietnam market holidays from market-holiday.com")

    investing_symbols_parser = subparsers.add_parser(
        "investing-fetch-symbols",
        help="scrape vn.investing.com /equities/vietnam and persist a (ticker, pair_id) map",
    )
    investing_symbols_parser.add_argument(
        "--url",
        default="https://vn.investing.com/equities/vietnam",
        help="overrides the equities list URL (rarely needed)",
    )

    investing_history_parser = subparsers.add_parser(
        "investing-sync-history",
        help="fetch Investing.com OHLCV history and cache to data/prices_investing/",
    )
    investing_history_parser.add_argument(
        "--id-map",
        default=None,
        help="path to investing_id_map.json; defaults to data/investing_id_map.json",
    )
    investing_history_parser.add_argument(
        "--from-date",
        required=True,
        help="start date YYYY-MM-DD",
    )
    investing_history_parser.add_argument(
        "--to-date",
        required=True,
        help="end date YYYY-MM-DD",
    )
    investing_history_parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="optional subset of tickers; defaults to every ticker in the id map",
    )
    investing_history_parser.add_argument(
        "--time-frame",
        choices=["Daily", "Weekly", "Monthly"],
        default="Daily",
    )
    investing_history_parser.add_argument(
        "--chunk-days",
        type=int,
        default=365,
        help="days per request chunk (default 365; Investing.com caps responses)",
    )
    investing_history_parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=1.0,
        help="pause between per-ticker requests to be polite",
    )
    investing_history_parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="refetch cached tickers",
    )

    investing_smoke_parser = subparsers.add_parser(
        "investing-smoke",
        help="pull a 1-month window of daily bars for one pair_id and print them",
    )
    investing_smoke_parser.add_argument("--pair-id", type=int, default=41913)
    investing_smoke_parser.add_argument("--from-date", default="2026-07-23")
    investing_smoke_parser.add_argument("--to-date", default="2026-08-23")

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

    autofin_bt = subparsers.add_parser(
        "backtest-autofin-signal",
        help="backtest autofin.vn signal mirroring using /closing-signals",
    )
    autofin_bt.add_argument(
        "--initial-nav",
        type=float,
        default=1_000_000_000,
        help="starting NAV in VND (default 1B)",
    )
    autofin_bt.add_argument(
        "--fee-per-share",
        type=float,
        default=0.0,
        help="flat fee per share, applied on BUY+SELL (default 0)",
    )
    autofin_bt.add_argument(
        "--slippage-pct",
        type=float,
        default=0.0,
        help="round-trip slippage as fraction (e.g. 0.001 = 0.1%%)",
    )
    autofin_bt.add_argument(
        "--max-pages",
        type=int,
        default=20,
        help="pagination cap on /closing-signals (default 20 → 1000 deals)",
    )
    autofin_bt.add_argument(
        "--min-profit-steps",
        type=int,
        default=0,
        help="skip deals whose numberOfProfit is below this (e.g. 2 = only "
             "take deals that triggered ≥2 partial TPs)",
    )
    autofin_bt.add_argument(
        "--stop-loss-pct",
        type=float,
        default=0.0,
        help="simulate a stop-loss at this fraction below entry "
             "(e.g. 0.10 = 10%%). When the final close would have hit "
             "the SL, model the exit at the SL level.",
    )
    autofin_bt.add_argument(
        "--out-dir",
        type=Path,
        default=Path("vietnam_algo/data/backtests"),
        help="where to write backtest JSON+CSV (default data/backtests/)",
    )
    autofin_bt.add_argument(
        "--notify",
        action="store_true",
        help="send the summary to Telegram",
    )

    dnse_accounts = subparsers.add_parser("dnse-accounts", help="list DNSE trading accounts")
    add_dnse_auth_arguments(dnse_accounts)

    dnse_instruments_parser = subparsers.add_parser(
        "dnse-sync-instruments",
        help="fetch the DNSE instrument master list to data/instruments_dnse.json",
    )
    add_dnse_auth_arguments(dnse_instruments_parser)

    dnse_prices_parser = subparsers.add_parser(
        "dnse-sync-prices",
        help="fetch DNSE OHLC daily bars and cache to data/prices_dnse/",
    )
    add_dnse_auth_arguments(dnse_prices_parser)
    dnse_prices_parser.add_argument("--from-date", default="2015-01-01", help="start date YYYY-MM-DD (DNSE supports deep history)")
    dnse_prices_parser.add_argument("--to-date", default=None, help="end date YYYY-MM-DD; defaults to today")
    dnse_prices_parser.add_argument("--symbols", nargs="+", default=None, help="optional subset; defaults to symbols.json")
    dnse_prices_parser.add_argument("--max-symbols", type=int, default=0, help="limit number of symbols; 0 = all")
    dnse_prices_parser.add_argument("--sleep-seconds", type=float, default=0.5)
    dnse_prices_parser.add_argument("--no-skip-existing", action="store_true", help="refetch cached symbols")

    dnse_latest_parser = subparsers.add_parser(
        "dnse-latest-quotes",
        help="print latest quotes for one symbol (smoke test)",
    )
    add_dnse_auth_arguments(dnse_latest_parser)
    dnse_latest_parser.add_argument("--symbol", required=True)

    dnse_quote_bbo = subparsers.add_parser(
        "dnse-quote",
        help="print the BBO + up to 3-level depth for a symbol",
    )
    add_dnse_auth_arguments(dnse_quote_bbo)
    dnse_quote_bbo.add_argument("--symbol", required=True)
    dnse_quote_bbo.add_argument("--no-trading-token", action="store_true", help="skip the trading-token header (use only for deeply read-only quotes)")

    dnse_trades = subparsers.add_parser(
        "dnse-trades",
        help="print the most recent trade prints for a symbol",
    )
    add_dnse_auth_arguments(dnse_trades)
    dnse_trades.add_argument("--symbol", required=True)
    dnse_trades.add_argument("--limit", type=int, default=20)

    dnse_positions = subparsers.add_parser("dnse-positions", help="list DNSE stock positions")
    add_dnse_auth_arguments(dnse_positions)
    dnse_positions.add_argument("--account-no", required=True)

    dnse_ppse = subparsers.add_parser("dnse-ppse", help="check DNSE buying/selling power for a symbol")
    add_dnse_auth_arguments(dnse_ppse)
    dnse_ppse.add_argument("--account-no", required=True)
    dnse_ppse.add_argument("--symbol", required=True)
    dnse_ppse.add_argument("--price", type=float, required=True)
    dnse_ppse.add_argument("--loan-package-id", type=int, required=True)

    dnse_loan_packages = subparsers.add_parser(
        "dnse-loan-packages",
        help="list DNSE margin loan packages available for the account",
    )
    add_dnse_auth_arguments(dnse_loan_packages)
    dnse_loan_packages.add_argument("--account-no", required=True)
    dnse_loan_packages.add_argument("--market-type", choices=["STOCK", "DERIVATIVE"], default="STOCK")
    dnse_loan_packages.add_argument("--symbol", default=None, help="if set, list packages for this ticker only")

    dnse_otp = subparsers.add_parser("dnse-send-email-otp", help="request DNSE email OTP")
    add_dnse_auth_arguments(dnse_otp)

    dnse_token = subparsers.add_parser("dnse-trading-token", help="exchange OTP for DNSE trading token")
    add_dnse_auth_arguments(dnse_token)
    dnse_token.add_argument("--otp-type", choices=["email_otp", "smart_otp"], default="smart_otp")
    dnse_token.add_argument("--passcode", required=True)

    dnse_trade_secret = subparsers.add_parser(
        "dnse-trade-secret",
        help="end-to-end: send email OTP, poll IMAP, exchange, persist trading token",
    )
    add_dnse_auth_arguments(dnse_trade_secret)
    dnse_trade_secret.add_argument(
        "--gmail-address",
        default=os.environ.get("GMAIL_ADDRESS", ""),
        help="Gmail account receiving DNSE OTPs (default: env GMAIL_ADDRESS)",
    )
    dnse_trade_secret.add_argument(
        "--gmail-app-password",
        default=os.environ.get("GMAIL_APP_PASSWORD", ""),
        help="16-char Gmail App Password (default: env GMAIL_APP_PASSWORD)",
    )
    dnse_trade_secret.add_argument(
        "--imaphost",
        default=os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com"),
    )
    dnse_trade_secret.add_argument(
        "--imappassword",
        default=os.environ.get("GMAIL_MAILBOX", "INBOX"),
        help="mailbox name (default: env GMAIL_MAILBOX)",
    )
    dnse_trade_secret.add_argument(
        "--otp-sender",
        default=os.environ.get("DNSE_OTP_SENDER", ""),
    )
    dnse_trade_secret.add_argument(
        "--otp-subject",
        default=os.environ.get("DNSE_OTP_SUBJECT", ""),
    )
    dnse_trade_secret.add_argument(
        "--otp-regex",
        default=os.environ.get("DNSE_OTP_REGEX", r"\b(\d{6})\b"),
    )
    dnse_trade_secret.add_argument(
        "--wait-seconds",
        type=int,
        default=int(os.environ.get("DNSE_OTP_WAIT_SECONDS", "20")),
    )
    dnse_trade_secret.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("DNSE_OTP_POLL_SECONDS", "3")),
    )
    dnse_trade_secret.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.environ.get("DNSE_OTP_TIMEOUT_SECONDS", "60")),
    )
    dnse_trade_secret.add_argument(
        "--token-output",
        type=Path,
        default=DEFAULT_DATA_DIR / "live" / "dnse_trading_token.json",
    )

    telegram = subparsers.add_parser("telegram-test", help="send a test Telegram notification")
    telegram.add_argument("--message", default="Vietnam Algo Telegram notification test")
    telegram.add_argument("--bot-token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    telegram.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID", ""))

    preview = subparsers.add_parser("dnse-preview-orders", help="preview DNSE orders converted from algo CSV")
    add_order_engine_arguments(preview)

    submit = subparsers.add_parser("dnse-submit-orders", help="submit DNSE orders converted from algo CSV; requires --yes")
    add_dnse_auth_arguments(submit)
    add_order_engine_arguments(submit)
    submit.add_argument("--account-no", required=True)
    submit.add_argument("--trading-token", default=os.environ.get("DNSE_TRADING_TOKEN", ""))
    submit.add_argument("--yes", action="store_true", help="actually submit orders to DNSE")
    submit.add_argument("--monitor", action="store_true", help="monitor submitted orders and replace price toward current order book")
    submit.add_argument("--initial-orderbook-pricing", action="store_true", help="price initial orders from best ask/bid before submit")
    submit.add_argument("--max-price-drift-percent", type=float, default=2.0)
    submit.add_argument("--cancel-if-unfilled", action="store_true")
    submit.add_argument("--skip-live-risk-check", action="store_true")
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
    auto_bot.add_argument(
        "--heartbeat-time",
        default=None,
        help="override heartbeat time HH:MM (default 10:30 ICT)",
    )
    # --strategy is already added by add_daily_engine_arguments; we accept the
    # registered-strategy names too. Don't redeclare it here.

    # New: list available strategies
    subparsers.add_parser(
        "list-strategies",
        help="list all registered trading strategies",
    )

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
    parser.add_argument("--corporate-actions-file", type=Path, default=None)
    parser.add_argument("--corporate-action-window-days", type=int, default=3)
    parser.add_argument("--corporate-action-buy-window-days", type=int, default=2)
    parser.add_argument("--corporate-action-sell-window-days", type=int, default=3)
    parser.add_argument("--no-corporate-action-guard", action="store_true")


def add_daily_engine_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--strategy", default="momentum",
                        help="strategy name (see `list-strategies`; default: momentum)")
    parser.add_argument("--trade-date", default=None, help="optional order execution date to export/submit")
    parser.add_argument("--update-symbols", action="store_true")
    parser.add_argument("--update-prices", action="store_true")
    parser.add_argument("--price-limit", type=int, default=DEFAULT_PRICE_LIMIT)
    parser.add_argument("--price-sleep-seconds", type=float, default=1.0)
    parser.add_argument("--max-symbols", type=int, default=0)


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def corporate_actions_file_from_args(args: argparse.Namespace) -> Path | None:
    if getattr(args, "no_corporate_action_guard", False):
        return None
    path = getattr(args, "corporate_actions_file", None) or args.data_dir / "corporate_action.json"
    return path if path.exists() else None


def dnse_client_from_args(args: argparse.Namespace) -> DnseClient:
    if not args.api_key or not args.api_secret:
        raise RuntimeError("DNSE credentials are required. Set DNSE_API_KEY/DNSE_API_SECRET or pass --api-key/--api-secret.")
    return DnseClient(args.api_key, args.api_secret, base_url=args.base_url)


def strategy_config_from_args(args: argparse.Namespace) -> StrategyConfig:
    from .config import UniverseConfig
    universe = UniverseConfig(
        lookbackDays=args.universe_lookback_days,
        minAvgValue=args.universe_min_avg_value,
        minAvgVolume=args.universe_min_avg_volume,
        minPrice=args.universe_min_price,
        minHistoryDays=args.universe_min_history_days,
        maxSymbols=args.universe_max_symbols,
        maxStaleDays=args.universe_max_stale_days,
    )
    return StrategyConfig(
        initialCash=args.initial_cash,
        maxPositions=args.max_positions,
        bearMaxPositions=args.bear_max_positions,
        boardLotSize=args.board_lot_size,
        sellSettlementDays=args.sell_settlement_days,
        rebalanceDays=args.rebalance_days,
        return1mDays=args.return_1m_days,
        return12mDays=args.return_12m_days,
        skipRecentDays=args.skip_recent_days,
        momentum1mWeight=args.momentum_1m_weight,
        momentum3mWeight=args.momentum_3m_weight,
        momentum6mWeight=args.momentum_6m_weight,
        momentum12mWeight=args.momentum_12m_weight,
        useDownsideVolatility=args.use_downside_volatility,
        volatilityPenaltyWeight=args.volatility_penalty_weight,
        trendUpWeight=args.trend_up_weight,
        trendDrawdownPenaltyWeight=args.trend_drawdown_penalty_weight,
        smaSlopeWeight=args.sma_slope_weight,
        volumeTrendWeight=args.volume_trend_weight,
        relativeStrength3mWeight=args.relative_strength_3m_weight,
        relativeStrength6mWeight=args.relative_strength_6m_weight,
        replaceRawMomentumWithRelative=args.replace_raw_momentum_with_relative,
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
        useCorporateActionGuard=args.use_corporate_action_guard,
        corporateActionWindowDays=args.corporate_action_window_days,
        corporateActionBuyWindowDays=args.corporate_action_buy_window_days,
        corporateActionSellWindowDays=args.corporate_action_sell_window_days,
        corporateActionRebuyAfterExDate=args.corporate_action_rebuy_after_exdate,
        corporateActionPreExitDays=args.corporate_action_pre_exit_days,
        corporateActionPreExitSignalMode=args.corporate_action_pre_exit_signal_mode,
        corporateActionPreExitSignalDropPct=args.corporate_action_pre_exit_signal_drop_pct,
        corporateActionPreExitSignalMaPeriod=args.corporate_action_pre_exit_signal_ma_period,
        corporateActionPreExitSignalLookback=args.corporate_action_pre_exit_signal_lookback,
        corporateActionPreExitSignalShortPeriod=args.corporate_action_pre_exit_signal_short_period,
        corporateActionPreExitSignalLongPeriod=args.corporate_action_pre_exit_signal_long_period,
        corporateActionHighRiskBuyWindowDays=args.corporate_action_high_risk_buy_window_days,
        corporateActionHighRiskSellWindowDays=args.corporate_action_high_risk_sell_window_days,
        corporateActionRebuyBullMaxRank=args.corporate_action_rebuy_bull_max_rank,
        corporateActionRebuyNeutralMaxRank=args.corporate_action_rebuy_neutral_max_rank,
        corporateActionRebuyMinAvgVolume=args.corporate_action_rebuy_min_avg_volume,
        corporateActionReserveCashForRebuy=args.corporate_action_reserve_cash_for_rebuy,
        corporateActionReserveExpiryDays=args.corporate_action_reserve_expiry_days,
        corporateActionForceSellStandardEvents=not args.no_corporate_action_force_sell_standard_events,
        corporateActionRebuyAsAdjustment=args.corporate_action_rebuy_as_adjustment,
        corporateActionRebuyDelayDays=args.corporate_action_rebuy_delay_days,
        corporateActionRebuyDelayHighRiskOnly=args.corporate_action_rebuy_delay_high_risk_only,
        corporateActionRebuySignalMode=args.corporate_action_rebuy_signal_mode,
        corporateActionRebuySignalRecoveryPct=args.corporate_action_rebuy_signal_recovery_pct,
        corporateActionRebuySignalRangePct=args.corporate_action_rebuy_signal_range_pct,
        corporateActionRebuySignalMaPeriod=args.corporate_action_rebuy_signal_ma_period,
        corporateActionRebuySignalLookback=args.corporate_action_rebuy_signal_lookback,
        corporateActionRebuySignalMaxDays=args.corporate_action_rebuy_signal_max_days,
        corporateActionRebuySignalConfirmDays=args.corporate_action_rebuy_signal_confirm_days,
        corporateActionRebuySignalMinDays=args.corporate_action_rebuy_signal_min_days,
        universe=universe,
        usePointInTimeUniverse=args.point_in_time_universe,
        useVolTargeting=args.use_vol_targeting,
        volTargetAnnualized=args.vol_target_annualized,
        volWindowDays=args.vol_window_days,
        volMaxLeverage=args.vol_max_leverage,
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


def _with_strategy(args: argparse.Namespace, strategy_name: str) -> EngineConfig:
    """Build EngineConfig with the CLI --strategy override applied."""
    from dataclasses import replace
    cfg = engine_config_from_args(args)
    return replace(cfg, strategy=strategy_name)


def add_backtest_arguments(backtest_parser: argparse.ArgumentParser) -> None:
    backtest_parser.add_argument("--universe-name", default="liquid_stocks")
    backtest_parser.add_argument("--initial-cash", type=float, default=1_000_000_000)
    backtest_parser.add_argument("--max-positions", type=int, default=5)
    backtest_parser.add_argument("--bear-max-positions", type=int, default=2)
    backtest_parser.add_argument("--board-lot-size", type=int, default=100)
    backtest_parser.add_argument("--sell-settlement-days", type=int, default=3)
    backtest_parser.add_argument("--rebalance-days", type=int, default=5)
    backtest_parser.add_argument("--return-1m-days", type=int, default=21)
    backtest_parser.add_argument("--return-12m-days", type=int, default=252)
    backtest_parser.add_argument("--skip-recent-days", type=int, default=0)
    backtest_parser.add_argument("--momentum-1m-weight", type=float, default=0.0)
    backtest_parser.add_argument("--momentum-3m-weight", type=float, default=0.6)
    backtest_parser.add_argument("--momentum-6m-weight", type=float, default=0.4)
    backtest_parser.add_argument("--momentum-12m-weight", type=float, default=0.0)
    backtest_parser.add_argument("--use-downside-volatility", action="store_true")
    backtest_parser.add_argument("--volatility-penalty-weight", type=float, default=0.3)
    backtest_parser.add_argument("--trend-up-weight", type=float, default=0.0)
    backtest_parser.add_argument("--trend-drawdown-penalty-weight", type=float, default=0.0)
    backtest_parser.add_argument("--sma-slope-weight", type=float, default=0.0)
    backtest_parser.add_argument("--volume-trend-weight", type=float, default=0.0)
    backtest_parser.add_argument("--relative-strength-3m-weight", type=float, default=0.0)
    backtest_parser.add_argument("--relative-strength-6m-weight", type=float, default=0.0)
    backtest_parser.add_argument("--replace-raw-momentum-with-relative", action="store_true")
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
    backtest_parser.add_argument("--use-corporate-action-guard", action="store_true")
    backtest_parser.add_argument("--corporate-action-window-days", type=int, default=3)
    backtest_parser.add_argument("--corporate-action-buy-window-days", type=int, default=2)
    backtest_parser.add_argument("--corporate-action-sell-window-days", type=int, default=3)
    backtest_parser.add_argument(
        "--corporate-action-rebuy-after-exdate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="after a force-sell for a non-cash CA, queue a post-ex rebuy (default on; turn off with --no-corporate-action-rebuy-after-exdate)",
    )
    backtest_parser.add_argument("--corporate-action-high-risk-buy-window-days", type=int, default=5)
    backtest_parser.add_argument("--corporate-action-high-risk-sell-window-days", type=int, default=3)
    backtest_parser.add_argument("--corporate-action-rebuy-bull-max-rank", type=int, default=10)
    backtest_parser.add_argument("--corporate-action-rebuy-neutral-max-rank", type=int, default=5)
    backtest_parser.add_argument("--corporate-action-rebuy-min-avg-volume", type=float, default=300_000)
    backtest_parser.add_argument(
        "--corporate-action-reserve-cash-for-rebuy",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="reserve freed-up cash against the post-CA rebuy (default on; turn off with --no-corporate-action-reserve-cash-for-rebuy)",
    )
    backtest_parser.add_argument("--corporate-action-reserve-expiry-days", type=int, default=25)
    backtest_parser.add_argument("--no-corporate-action-force-sell-standard-events", action="store_true")
    backtest_parser.add_argument(
        "--corporate-action-rebuy-as-adjustment",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="bypass rank/SMA/volume filters for the post-CA rebuy (default on; turn off with --no-corporate-action-rebuy-as-adjustment)",
    )
    backtest_parser.add_argument(
        "--corporate-action-rebuy-delay-days",
        type=int,
        default=0,
        help="number of trading days to wait after the ex-date before the post-CA rebuy is allowed (default 0 = rebuy ASAP)",
    )
    backtest_parser.add_argument(
        "--corporate-action-rebuy-delay-high-risk-only",
        action="store_true",
        help="apply the rebuy delay only to high-risk CA events (rights offerings, share swaps, additional issuances)",
    )
    backtest_parser.add_argument(
        "--corporate-action-rebuy-signal-mode",
        choices=["fixed_delay", "recovery", "stabilization", "new_high", "ma_cross", "no_new_low", "combined", "stabilization_or_no_new_low"],
        default="fixed_delay",
        help="post-CA rebuy signal mode: fixed_delay (legacy), recovery (bounce from post-ex low), stabilization (range tightened), new_high (close above all closes since ex), ma_cross (close crossed above N-day MA), no_new_low (no break of recent low), combined (recovery + stabilization), stabilization_or_no_new_low (either stabilization or no new low)",
    )
    backtest_parser.add_argument(
        "--corporate-action-rebuy-signal-ma-period",
        type=int,
        default=5,
        help="MA period for the ma_cross signal (default 5)",
    )
    backtest_parser.add_argument(
        "--corporate-action-rebuy-signal-recovery-pct",
        type=float,
        default=0.005,
        help="recovery threshold as a fraction of post-ex low (default 0.005 = 0.5%%)",
    )
    backtest_parser.add_argument(
        "--corporate-action-rebuy-signal-range-pct",
        type=float,
        default=0.025,
        help="max acceptable range over the lookback window (default 0.025 = 2.5%%)",
    )
    backtest_parser.add_argument(
        "--corporate-action-pre-exit-days",
        type=int,
        default=5,
        help="number of trading days before ex-date to force-sell a position with an upcoming non-cash CA (default 5)",
    )
    backtest_parser.add_argument(
        "--corporate-action-pre-exit-signal-mode",
        choices=["fixed_window", "drop_from_high", "below_ma", "momentum_reversal", "combined"],
        default="fixed_window",
        help="pre-exit trigger mode: fixed_window (legacy), drop_from_high (close has dropped X%% from window peak), below_ma (close < N-day MA), momentum_reversal (short MA crossed below long MA), combined (drop + below_ma)",
    )
    backtest_parser.add_argument(
        "--corporate-action-pre-exit-signal-drop-pct",
        type=float,
        default=0.02,
        help="drop threshold for drop_from_high signal as a fraction (default 0.02 = 2%%)",
    )
    backtest_parser.add_argument(
        "--corporate-action-pre-exit-signal-ma-period",
        type=int,
        default=5,
        help="MA period for the below_ma signal (default 5)",
    )
    backtest_parser.add_argument(
        "--corporate-action-pre-exit-signal-lookback",
        type=int,
        default=14,
        help="number of recent closes used to compute the pre-exit signal (default 14)",
    )
    backtest_parser.add_argument(
        "--corporate-action-pre-exit-signal-short-period",
        type=int,
        default=3,
        help="short-MA period for the momentum_reversal signal (default 3)",
    )
    backtest_parser.add_argument(
        "--corporate-action-pre-exit-signal-long-period",
        type=int,
        default=8,
        help="long-MA period for the momentum_reversal signal (default 8)",
    )
    backtest_parser.add_argument(
        "--corporate-action-rebuy-signal-lookback",
        type=int,
        default=3,
        help="number of recent closes for the stabilization and no_new_low checks (default 3)",
    )
    backtest_parser.add_argument(
        "--corporate-action-rebuy-signal-max-days",
        type=int,
        default=10,
        help="fallback: force the rebuy if the signal has not triggered within this many post-ex trading days (default 10)",
    )
    backtest_parser.add_argument(
        "--corporate-action-rebuy-signal-confirm-days",
        type=int,
        default=1,
        help="require the signal to be true for N consecutive days before firing (default 1 = single-day)",
    )
    backtest_parser.add_argument(
        "--corporate-action-rebuy-signal-min-days",
        type=int,
        default=0,
        help="earliest post-ex day to consider the signal (default 0 = signal can fire immediately)",
    )
    backtest_parser.add_argument(
        "--point-in-time-universe",
        dest="point_in_time_universe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="rebuild the liquid universe as of each rebalance date from data available at that time (default on; turn off with --no-point-in-time-universe for legacy biased results)",
    )
    backtest_parser.add_argument("--universe-lookback-days", type=int, default=20)
    backtest_parser.add_argument("--universe-min-avg-value", type=float, default=20_000_000_000)
    backtest_parser.add_argument("--universe-min-avg-volume", type=float, default=300_000)
    backtest_parser.add_argument("--universe-min-price", type=float, default=10_000)
    backtest_parser.add_argument("--universe-min-history-days", type=int, default=252)
    backtest_parser.add_argument("--universe-max-symbols", type=int, default=100)
    backtest_parser.add_argument(
        "--universe-max-stale-days",
        type=int,
        default=30,
        help="exclude symbols whose most recent bar is older than this many days (effectively delisted/suspended)",
    )
    backtest_parser.add_argument(
        "--use-vol-targeting",
        action="store_true",
        help="scale position sizes by realized portfolio vol to target an annualized vol",
    )
    backtest_parser.add_argument(
        "--vol-target-annualized",
        type=float,
        default=0.25,
        help="target annualized portfolio vol when --use-vol-targeting is on (e.g. 0.25 = 25%%)",
    )
    backtest_parser.add_argument(
        "--vol-window-days",
        type=int,
        default=120,
        help="rolling window in trading days for realized vol estimation (longer = smoother)",
    )
    backtest_parser.add_argument(
        "--vol-max-leverage",
        type=float,
        default=1.5,
        help="cap on the vol-targeting scalar (e.g. 1.5 means exposure can be up to 1.5x the regime base)",
    )


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

    if args.command == "sync-corporate-actions":
        if not args.vietstock_token or not args.vietstock_cookie:
            raise RuntimeError(
                "Vietstock token and cookie are required. Pass --vietstock-token/--vietstock-cookie "
                "or set VIETSTOCK_REQUEST_TOKEN/VIETSTOCK_COOKIE."
            )
        actions = fetch_vietstock_events(
            args.from_date,
            args.to_date,
            args.vietstock_token,
            args.vietstock_cookie,
            page=args.page,
            page_size=args.page_size,
            timeout=args.timeout,
            code=args.symbol,
        )
        path = save_corporate_actions(actions, args.data_dir)
        print(f"Saved {len(actions)} corporate actions to {path}")
        return 0

    if args.command == "sync-corporate-actions-incremental":
        print_json(
            sync_corporate_actions_incremental(
                args.data_dir,
                request_verification_token=args.vietstock_token,
                cookie=args.vietstock_cookie,
                to_date=args.to_date,
                page_size=args.page_size,
                timeout=args.timeout,
            )
        )
        return 0

    if args.command == "sync-market-holidays":
        print_json(sync_market_holidays(args.data_dir, timeout=args.timeout))
        return 0

    if args.command == "investing-fetch-symbols":
        try:
            symbols = fetch_investing_symbols(timeout=args.timeout)
        except RuntimeError as exc:
            raise RuntimeError(
                "Failed to fetch Investing.com equities list. "
                "Run from a host with vn.investing.com reachable, "
                "or pass --url to a manually extracted list."
            ) from exc
        if not symbols:
            raise RuntimeError(
                "No Investing.com symbols parsed. Page layout may have changed; "
                "share the HTML snippet and we will update the parser."
            )
        path = save_investing_id_map(symbols, args.data_dir)
        print(f"Saved {len(symbols)} Investing.com (ticker, pair_id) pairs to {path}")
        return 0

    if args.command == "investing-sync-history":
        id_map_path = Path(args.id_map) if args.id_map else (args.data_dir / "investing_id_map.json")
        if not id_map_path.exists():
            raise RuntimeError(
                f"Investing.com id map not found at {id_map_path}. "
                "Run `investing-fetch-symbols` first or pass --id-map."
            )
        id_map = load_investing_id_map(id_map_path)
        if args.symbols:
            requested = {s.strip().upper() for s in args.symbols}
            id_map = {k: v for k, v in id_map.items() if k in requested}
            if not id_map:
                raise RuntimeError(
                    "None of the requested tickers were in the id map."
                )
        if not id_map:
            raise RuntimeError("id map is empty")
        paths = sync_investing_prices(
            id_map=id_map,
            start_date=args.from_date,
            end_date=args.to_date,
            data_dir=args.data_dir,
            chunk_days=args.chunk_days,
            sleep_seconds=args.sleep_seconds,
            skip_existing=not args.no_skip_existing,
            timeout=args.timeout,
        )
        print(
            f"Saved {len(paths)} Investing.com price files to "
            f"{args.data_dir / DEFAULT_INVESTING_PRICES_DIR}"
        )
        return 0

    if args.command == "investing-smoke":
        bars = fetch_investing_history(
            pair_id=args.pair_id,
            start_date=args.from_date,
            end_date=args.to_date,
        )
        print_json({
            "pair_id": args.pair_id,
            "from_date": args.from_date,
            "to_date": args.to_date,
            "count": len(bars),
            "first": asdict(bars[0]) if bars else None,
            "last": asdict(bars[-1]) if bars else None,
        })
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

    if args.command == "backtest-autofin-signal":
        from .backtest_autofin import (
            format_backtest_report,
            run_autofin_backtest,
            write_backtest_outputs,
        )
        from .notifier import notify
        result = run_autofin_backtest(
            initial_nav=args.initial_nav,
            fee_per_share=args.fee_per_share,
            slippage_pct=args.slippage_pct,
            max_pages=args.max_pages,
            min_profit_steps=args.min_profit_steps,
            stop_loss_pct=args.stop_loss_pct,
        )
        json_path, csv_path = write_backtest_outputs(result, args.out_dir)
        report = format_backtest_report(result)
        print(report)
        print()
        print(f"Saved backtest JSON to {json_path}")
        print(f"Saved per-trade CSV to {csv_path}")
        if args.notify:
            notify(report)
        return 0

    if args.command == "dnse-accounts":
        print_json(dnse_client_from_args(args).get_accounts())
        return 0

    if args.command == "dnse-positions":
        print_json(dnse_client_from_args(args).get_positions(args.account_no))
        return 0

    if args.command == "dnse-ppse":
        print_json(dnse_client_from_args(args).get_ppse(args.account_no, "STOCK", args.symbol, args.price, args.loan_package_id))
        return 0

    if args.command == "dnse-loan-packages":
        from .dnse_client import load_trading_token
        token = None
        saved_token_path = args.data_dir / "live" / "dnse_trading_token.json"
        if saved_token_path.exists():
            try:
                token = load_trading_token(saved_token_path).get("tradingToken")
            except Exception:
                token = None
        client = dnse_client_from_args(args)
        rows = client.list_loan_packages(args.account_no, args.market_type, args.symbol)
        print_json({
            "accountNo": args.account_no,
            "marketType": args.market_type,
            "symbol": args.symbol,
            "count": len(rows),
            "items": rows,
        })
        return 0

    if args.command == "dnse-sync-instruments":
        client = dnse_client_from_args(args)
        path = sync_dnse_instruments(client, args.data_dir)
        print(f"Saved DNSE instruments to {path}")
        return 0

    if args.command == "dnse-sync-prices":
        client = dnse_client_from_args(args)
        if args.symbols:
            symbols = [s.upper() for s in args.symbols]
        else:
            symbols_path = args.data_dir / "symbols.json"
            if not symbols_path.exists():
                raise RuntimeError(f"symbols.json not found at {symbols_path}; pass --symbols explicitly")
            symbols_payload = json.loads(symbols_path.read_text(encoding="utf-8"))
            symbols = sorted({
                str(it["ticker"]).upper()
                for it in symbols_payload.get("items", [])
                if it.get("ticker")
            })
        if args.max_symbols > 0:
            symbols = symbols[: args.max_symbols]
        to_date = args.to_date or datetime.now(UTC).date().isoformat()
        paths = sync_dnse_prices(
            client=client,
            symbols=symbols,
            from_date=args.from_date,
            to_date=to_date,
            data_dir=args.data_dir,
            sleep_seconds=args.sleep_seconds,
            skip_existing=not args.no_skip_existing,
        )
        total_bars = sum(
            int(json.loads(p.read_text(encoding="utf-8")).get("count", 0))
            for p in paths.values()
        )
        print(f"Saved {len(paths)} DNSE price files ({total_bars} bars total) under {args.data_dir / 'prices_dnse'}")
        return 0

    if args.command == "dnse-latest-quotes":
        print_json(dnse_client_from_args(args).get_latest_quote(args.symbol))
        return 0

    if args.command == "dnse-quote":
        from .order_monitor import quote_match_price
        from .dnse_client import load_trading_token
        client = dnse_client_from_args(args)
        extra = None
        if not args.no_trading_token:
            saved_token = args.data_dir / "live" / "dnse_trading_token.json"
            if saved_token.exists():
                try:
                    tok = load_trading_token(saved_token).get("tradingToken")
                    if tok:
                        extra = {"trading-token": tok}
                except Exception:
                    extra = None
        quote = client.get_latest_quote(args.symbol)
        # DNSE returns: {"quotes": [ {bid, offer, boardId, ...}, ... ]}
        # Pick the first (or boardId=G1 / HoSE main) board
        boards = quote.get("quotes", []) if isinstance(quote, dict) else []
        if not boards and isinstance(quote, list):
            boards = quote
        primary = boards[0] if boards else quote
        bid_levels = primary.get("bid") or [] if isinstance(primary, dict) else []
        offer_levels = primary.get("offer") or [] if isinstance(primary, dict) else []
        bbo = {
            "boardId": primary.get("boardId") if isinstance(primary, dict) else None,
            "bid": bid_levels[0] if bid_levels else None,
            "offer": offer_levels[0] if offer_levels else None,
            "spread": None,
            "depth": {
                "bid_levels": len(bid_levels),
                "offer_levels": len(offer_levels),
                "total_bid_qty": sum(l["quantity"] for l in bid_levels),
                "total_offer_qty": sum(l["quantity"] for l in offer_levels),
            },
        }
        if bbo["bid"] and bbo["offer"]:
            bbo["spread"] = round(bbo["offer"]["price"] - bbo["bid"]["price"], 4)
        # DNSE side: 'NS' = sell, 'NB' = buy
        print_json({
            "symbol": args.symbol,
            "boards": [
                {
                    "boardId": q.get("boardId"),
                    "bid": q.get("bid", [])[:1] + [{**l, "depth": i + 1} for i, l in enumerate(q.get("bid", [])[1:])],
                    "offer": q.get("offer", []),
                }
                for q in boards
            ] if boards else None,
            "primary_bbo": bbo,
            "matchPriceForBuy": quote_match_price(primary, "NB"),
            "matchPriceForSell": quote_match_price(primary, "NS"),
            "ts": primary.get("time") if isinstance(primary, dict) else None,
        })
        return 0

    if args.command == "dnse-trades":
        client = dnse_client_from_args(args)
        to_date = (datetime.now(timezone.utc)).isoformat()
        from_date = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        # DNSE trades endpoint expects unix seconds via "from"/"to" query params
        from_ts = int((datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp())
        to_ts = int(datetime.now(timezone.utc).timestamp())
        response = client.request(
            "GET",
            f"/price/{args.symbol.upper()}/trades",
            {"from": from_ts, "to": to_ts, "limit": args.limit, "order": "DESC"},
        )
        print_json(response)
        return 0

    if args.command == "dnse-send-email-otp":
        print_json(dnse_client_from_args(args).send_email_otp())
        return 0

    if args.command == "dnse-trading-token":
        print_json({"tradingToken": dnse_client_from_args(args).trading_token(args.otp_type, args.passcode)})
        return 0

    if args.command == "dnse-trade-secret":
        from .otp_reader import latest_email_otp
        if not args.gmail_address or not args.gmail_app_password:
            raise RuntimeError(
                "dnse-trade-secret requires --gmail-address and --gmail-app-password "
                "(or env GMAIL_ADDRESS and GMAIL_APP_PASSWORD)."
            )
        client = dnse_client_from_args(args)
        print(f"[1/4] Sending email OTP via {client.base_url} ...")
        send_result = client.send_email_otp()
        print(f"  -> {send_result}")
        wait_seconds = max(0, args.wait_seconds - 2)
        if wait_seconds:
            print(f"  sleeping {wait_seconds:.0f}s before first IMAP poll ...")
            time.sleep(wait_seconds)
        deadline = time.time() + args.timeout_seconds
        otp: str | None = None
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                otp = latest_email_otp(
                    username=args.gmail_address,
                    app_password=args.gmail_app_password,
                    mailbox=args.imappassword,
                    sender_filter=args.otp_sender,
                    subject_filter=args.otp_subject,
                    otp_regex=args.otp_regex,
                    host=args.imaphost,
                    limit=10,
                )
                if otp:
                    break
            except Exception as exc:
                print(f"  poll {attempt}: {type(exc).__name__}: {exc}")
            time.sleep(args.poll_seconds)
        if not otp:
            raise RuntimeError(
                f"dnse-trade-secret: no OTP email found within {args.timeout_seconds:.0f}s. "
                f"Check GMAIL_APP_PASSWORD, DNSE_OTP_SENDER, DNSE_OTP_REGEX."
            )
        print(f"[2/4] OTP from email: {otp}")
        print(f"[3/4] Exchanging for trading token (email_otp) ...")
        token = client.create_trading_token("email_otp", otp)
        print(f"  token len: {len(token)}")
        print(f"[4/4] Persisting to {args.token_output} ...")
        accounts = client.request("GET", "/accounts", extra_headers={"trading-token": token})
        account_no = None
        if isinstance(accounts, dict):
            account_no = (accounts.get("accounts") or [{}])[0].get("id")
        token_payload = {
            "tradingToken": token,
            "otpType": "email_otp",
            "issuedAt": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "expiresAt": (datetime.now(UTC) + timedelta(hours=8)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "investorId": accounts.get("investorId") if isinstance(accounts, dict) else None,
            "accountNo": account_no,
            "source": client.base_url + "/registration/trading-token",
        }
        args.token_output.parent.mkdir(parents=True, exist_ok=True)
        args.token_output.write_text(json.dumps(token_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(token_payload, indent=2))
        return 0

    if args.command == "telegram-test":
        if not args.bot_token or not args.chat_id:
            raise RuntimeError("Telegram bot token and chat ID are required. Set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID or pass --bot-token/--chat-id.")
        print_json(notify(args.message, TelegramConfig(args.bot_token, args.chat_id)) or {"ok": False, "skipped": True})
        return 0

    if args.command == "dnse-preview-orders":
        selected_date = selected_order_date(args.orders_file, args.trade_date) or args.trade_date
        trading_dates = live_trading_dates(args.data_dir, selected_date, "2030-12-31") if selected_date else None
        orders, decisions = prepare_dnse_orders_with_risk_report(
            args.orders_file,
            args.loan_package_id,
            trade_date=args.trade_date,
            limit=args.limit,
            order_type=args.order_type,
            board_lot_size=args.board_lot_size,
            corporate_actions_path=corporate_actions_file_from_args(args),
            corporate_action_window_days=args.corporate_action_window_days,
            corporate_action_buy_window_days=args.corporate_action_buy_window_days,
            corporate_action_sell_window_days=args.corporate_action_sell_window_days,
            trading_dates=trading_dates,
        )
        print_json({"orders": [order.__dict__ for order in orders], "corporateActionDecisions": [decision.__dict__ for decision in decisions]})
        return 0

    if args.command == "dnse-submit-orders":
        if not args.trading_token:
            raise RuntimeError("DNSE trading token is required. Use --trading-token or set DNSE_TRADING_TOKEN.")
        selected_date = selected_order_date(args.orders_file, args.trade_date) or args.trade_date
        trading_dates = live_trading_dates(args.data_dir, selected_date, "2030-12-31") if selected_date else None
        orders, decisions = prepare_dnse_orders_with_risk_report(
            args.orders_file,
            args.loan_package_id,
            trade_date=args.trade_date,
            limit=args.limit,
            order_type=args.order_type,
            board_lot_size=args.board_lot_size,
            corporate_actions_path=corporate_actions_file_from_args(args),
            corporate_action_window_days=args.corporate_action_window_days,
            corporate_action_buy_window_days=args.corporate_action_buy_window_days,
            corporate_action_sell_window_days=args.corporate_action_sell_window_days,
            trading_dates=trading_dates,
        )
        actions_path = corporate_actions_file_from_args(args)
        if actions_path:
            from .corporate_actions import load_corporate_actions

            exit_orders, exit_decisions = build_corporate_action_exit_orders(
                dnse_client_from_args(args),
                args.account_no,
                args.loan_package_id,
                selected_date,
                load_corporate_actions(actions_path),
                orders,
                order_type=args.order_type,
                board_lot_size=args.board_lot_size,
                sell_window_days=args.corporate_action_sell_window_days,
                trading_dates=trading_dates,
            )
            orders.extend(exit_orders)
            decisions.extend(exit_decisions)
        client = dnse_client_from_args(args)
        sellable_decisions: list[dict[str, object]] = []
        if args.yes:
            sellable = extract_sellable_positions(client.positions(args.account_no), args.board_lot_size)
            orders, sellable_decisions = apply_sellable_quantity_cap(orders, sellable, args.board_lot_size)
        pricing_decisions: list[dict[str, object]] = []
        signal_prices = {order.sourceOrderId: order.price for order in orders}
        if args.initial_orderbook_pricing:
            orders, pricing_decisions = price_orders_from_orderbook(client, orders, max_drift_percent=args.max_price_drift_percent)
        risk_violations: list[dict[str, object]] = []
        if args.yes and not args.skip_live_risk_check:
            risk_violations = validate_live_orders(orders, live_risk_limits_from_env())
            if risk_violations:
                print_json({"status": "blocked_by_live_risk_limits", "riskViolations": risk_violations})
                return 1
        if args.yes and not args.monitor:
            results, reconciliation, submit_complete = execute_orders_with_ledger(
                client,
                args.account_no,
                args.trading_token,
                orders,
                args.data_dir,
                selected_date or "unknown",
            )
        elif args.monitor:
            results = execute_dnse_orders_with_monitoring(
                client,
                args.account_no,
                args.trading_token,
                orders,
                dry_run=not args.yes,
                max_checks=args.max_checks,
                check_interval_seconds=args.check_interval_seconds,
                signal_prices=signal_prices,
                max_drift_percent=args.max_price_drift_percent,
                cancel_if_unfilled=args.cancel_if_unfilled,
            )
            reconciliation = []
            submit_complete = False
        else:
            results = execute_dnse_orders(
                client,
                args.account_no,
                args.trading_token,
                orders,
                dry_run=not args.yes,
            )
            reconciliation = []
            submit_complete = not args.yes
        print_json(
            {
                "submitResults": results,
                "corporateActionDecisions": [decision.__dict__ for decision in decisions],
                "sellableDecisions": sellable_decisions,
                "pricingDecisions": pricing_decisions,
                "riskViolations": risk_violations,
                "reconciliation": reconciliation,
                "submitComplete": submit_complete,
            }
        )
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
        schedule = TradingSchedule()
        if getattr(args, "heartbeat_time", None):
            from datetime import datetime as _dt
            try:
                parsed = _dt.strptime(args.heartbeat_time, "%H:%M").time()
                schedule = TradingSchedule(heartbeatTime=parsed)
            except ValueError:
                print(f"--heartbeat-time must be HH:MM, got {args.heartbeat_time!r}; using default")
        # Resolve strategy: CLI > existing config.strategy
        strategy_name = getattr(args, "strategy", None) or args.strategy
        # If user explicitly overrode, persist into the config
        if getattr(args, "strategy", None):
            cfg = engine_config_from_args(args)
            object.__setattr__(cfg, "strategy", strategy_name)
        run_auto_bot(
            engine_config_from_args(args) if not getattr(args, "strategy", None) else _with_strategy(args, strategy_name),
            strategy_config_from_args(args),
            dnse_client_from_args(args),
            args.account_no,
            args.loan_package_id,
            live_submit=args.live_submit,
            once=args.once,
            sleep_seconds=args.sleep_seconds,
            schedule=schedule,
            strategy_name=strategy_name,
        )
        return 0

    if args.command == "list-strategies":
        from .strategy_registry import list_strategies
        for entry in list_strategies():
            print(f"  {entry['name']:<25}  {entry['description']}")
            if entry["env"]:
                print(f"    env: vietnam_algo/env/{entry['env']}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
