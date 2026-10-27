"""Telegram bot for the Darvas Box + AutoFin-tiered-exits strategy."""
import sys
from pathlib import Path

# When running as a script (`python -m bot.cli`), the parent package
# `darvas_box_strategy` may not be on sys.path. Add it explicitly.
_PKG_PARENT = Path(__file__).resolve().parents[2]
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))
if str(_PKG_PARENT.parent) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT.parent))

from darvas_box_strategy.bot.telegram import load_telegram_config, send_telegram
from darvas_box_strategy.bot.report import (
    format_backtest_report,
    format_recent_trades,
    format_ticker_breakdown,
    chunk_message,
)
from darvas_box_strategy.bot.cli import (
    build_default_config,
    load_blue_chips,
    filter_universe,
    main,
)

__all__ = [
    "load_telegram_config",
    "send_telegram",
    "format_backtest_report",
    "format_recent_trades",
    "format_ticker_breakdown",
    "chunk_message",
    "build_default_config",
    "load_blue_chips",
    "filter_universe",
    "main",
]
