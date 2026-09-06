"""Strategy registry — pluggable trading strategies sharing DNSE infrastructure.

Each strategy is a Python class registered in STRATEGY_REGISTRY. It exposes:
  - name: short identifier used in CLI/state/env files
  - description: human-readable label
  - generate_orders(...) -> dict: run the strategy's full backtest and
    return the result payload (same shape as the legacy
    generate_live_orders)
  - format_cycle_summary(result) -> str: optional Telegram-format override
    (default uses eod_summary_message)
  - backtest_csv_name: filename for the orders CSV (default "<name>_orders.csv")

Per-strategy state files:
  - env:  vietnam_algo/env/.env.{name}  (overrides for strategy params)
  - state: data/live/auto_bot_state.{name}.json
  - orders: data/live/latest_orders.{name}.csv + .json

Multiple bots can run concurrently on the same VPS, each with its own
state, env, and Telegram notifications.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .config import StrategyConfig
from .data import DEFAULT_DATA_DIR
from .daily_engine import EngineConfig, generate_live_orders
from .notifier import eod_summary_message


class StrategyAdapter(ABC):
    """Base class for a registered trading strategy."""

    name: str = ""
    description: str = ""
    env_filename: str = ""  # e.g. ".env.vietnam-algo-best-ca-fixed-delay6"; "" → use default

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
        return eod_summary_message(result)

    @property
    def backtest_csv_name(self) -> str:
        return f"{self.name}_rotation_orders.csv" if self.name else "live_orders.csv"

    @property
    def orders_csv_filename(self) -> str:
        """Filename (basename, no dir) for the latest-orders output."""
        return f"latest_orders.{self.name}.csv" if self.name else "latest_orders.csv"

    @property
    def orders_json_filename(self) -> str:
        return f"latest_orders.{self.name}.json" if self.name else "latest_orders.json"

    @property
    def state_filename(self) -> str:
        return f"auto_bot_state.{self.name}.json" if self.name else "auto_bot_state.json"


class MomentumRotationAdapter(StrategyAdapter):
    """The legacy liquid-momentum rotation (DRYRUN default)."""

    name = "momentum"
    description = "Liquid momentum rotation with corporate-action awareness"
    env_filename = ".env.vietnam-algo-best-ca-fixed-delay6"

    def generate_orders(
        self,
        config: EngineConfig,
        strategy_config: StrategyConfig,
        trade_date: str | None = None,
    ) -> dict[str, Any]:
        # Use the legacy generate_live_orders path which writes to backtests/.
        # We also need to copy the CSV into our strategy-named latest_orders
        # location so different strategies don't clobber each other.
        result = generate_live_orders(config, strategy_config, trade_date=trade_date)
        return result


# Registry — add new strategies here
STRATEGY_REGISTRY: dict[str, StrategyAdapter] = {
    "momentum": MomentumRotationAdapter(),
}


class _EmptyStrategy(StrategyAdapter):
    """Minimal concrete subclass used internally for tests; not in the registry."""

    def generate_orders(self, config, strategy_config, trade_date=None):  # pragma: no cover
        return {}


def get_strategy(name: str) -> StrategyAdapter:
    """Look up a strategy by name. Raises ValueError if not found."""
    adapter = STRATEGY_REGISTRY.get(name)
    if adapter is None:
        names = sorted(STRATEGY_REGISTRY.keys())
        raise ValueError(f"Unknown strategy: {name!r}. Available: {names}")
    return adapter


def list_strategies() -> list[dict[str, str]]:
    return [
        {"name": a.name, "description": a.description, "env": a.env_filename}
        for a in STRATEGY_REGISTRY.values()
    ]
