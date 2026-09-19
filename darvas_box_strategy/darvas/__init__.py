"""Darvas Box strategy package."""
from .base_detector import Base, BaseScanner, detect_base
from .ca_handler import (
    CAEvent,
    DEFAULT_CA_PATH,
    adjust_bars,
    build_event_index,
    load_events,
    parse_event,
)
from .config import (
    BacktestConfig,
    BaseConfig,
    BreakoutConfig,
    ExitsConfig,
    StrategyConfig,
    TrailingConfig,
    UniverseConfig,
)
from .data_loader import (
    PriceBar,
    TickerMeta,
    iter_universe,
    list_universe_tickers,
    load_price_bars,
)
from .executor import FillEvent, TradeResult, simulate_trade
from .backtest import BacktestResult, run_backtest
from .report import recent_trades, summary_report, ticker_breakdown, trade_detail
from .signal_generator import Signal, generate_signal

__all__ = [
    "StrategyConfig",
    "BaseConfig",
    "BreakoutConfig",
    "ExitsConfig",
    "TrailingConfig",
    "UniverseConfig",
    "BacktestConfig",
    "PriceBar",
    "TickerMeta",
    "Base",
    "BaseScanner",
    "Signal",
    "FillEvent",
    "TradeResult",
    "BacktestResult",
    "CAEvent",
    "load_events",
    "parse_event",
    "build_event_index",
    "adjust_bars",
    "DEFAULT_CA_PATH",
    "run_backtest",
    "load_price_bars",
    "iter_universe",
    "list_universe_tickers",
    "detect_base",
    "generate_signal",
    "simulate_trade",
    "summary_report",
    "ticker_breakdown",
    "recent_trades",
    "trade_detail",
]
