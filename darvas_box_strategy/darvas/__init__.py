"""Darvas Box strategy package.

Architecture:
  - Signal layer (darvas.signals): pure signals, no portfolio concerns
  - Strategy logic (darvas.signal_generator, darvas.base_detector): detect
    opportunities and emit Signals
  - Portfolio layer (darvas.portfolio): take Signals, manage positions,
    decide sizing, track history
  - Backtest (darvas.backtest): orchestrate the signal + portfolio layers
    over historical data
"""
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
    TrailingConfig,
    UniverseConfig,
    StrategyConfig,
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
from .signal_generator import Signal, SignalHistory, generate_signal
from .signals import Signal as PureSignal, SignalHistory as PureSignalHistory
from .portfolio import (
    Order,
    Position,
    Trade,
    Portfolio,
    size_position,
)

__all__ = [
    # Config
    "StrategyConfig",
    "BaseConfig",
    "BreakoutConfig",
    "ExitsConfig",
    "TrailingConfig",
    "UniverseConfig",
    "BacktestConfig",
    # Data
    "PriceBar",
    "TickerMeta",
    "CAEvent",
    "load_events",
    "parse_event",
    "build_event_index",
    "adjust_bars",
    "DEFAULT_CA_PATH",
    "load_price_bars",
    "iter_universe",
    "list_universe_tickers",
    # Detection
    "Base",
    "BaseScanner",
    "detect_base",
    # Signal layer
    "Signal",
    "SignalHistory",
    "PureSignal",
    "PureSignalHistory",
    "generate_signal",
    "simulate_trade",
    # Portfolio layer
    "Order",
    "Position",
    "Trade",
    "Portfolio",
    "size_position",
    # Backtest
    "BacktestResult",
    "run_backtest",
    "FillEvent",
    "TradeResult",
    "summary_report",
    "ticker_breakdown",
    "recent_trades",
    "trade_detail",
]
