"""strategy_base.py — StrategyAdapter ABC + helpers, shared by all strategies.

This module is intentionally tiny and dependency-free so both
strategy_registry and individual strategy modules can import from it
without circular dependencies.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .config import StrategyConfig
from .daily_engine import EngineConfig


class StrategyAdapter(ABC):
    """Base class for a registered trading strategy."""

    name: str = ""
    description: str = ""
    env_filename: str = ""  # e.g. ".env.vietnam-algo-best-ca-fixed-delay6"; "" → default

    @abstractmethod
    def generate_orders(
        self,
        config: EngineConfig,
        strategy_config: StrategyConfig,
        trade_date: str | None = None,
    ) -> dict[str, Any]:
        """Run the strategy and return the same payload as generate_live_orders."""
        ...

    def format_cycle_summary(self, result: dict[str, Any]) -> str:
        """Format a Telegram message for this strategy's EOD summary."""
        from .notifier import eod_summary_message
        return eod_summary_message(result)

    @property
    def backtest_csv_name(self) -> str:
        return f"{self.name}_rotation_orders.csv" if self.name else "live_orders.csv"

    @property
    def orders_csv_filename(self) -> str:
        return f"latest_orders.{self.name}.csv" if self.name else "latest_orders.csv"

    @property
    def orders_json_filename(self) -> str:
        return f"latest_orders.{self.name}.json" if self.name else "latest_orders.json"

    @property
    def state_filename(self) -> str:
        return f"auto_bot_state.{self.name}.json" if self.name else "auto_bot_state.json"
