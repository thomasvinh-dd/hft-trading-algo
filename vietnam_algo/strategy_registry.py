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

from pathlib import Path
from typing import Any

from .config import StrategyConfig
from .data import DEFAULT_DATA_DIR
from .daily_engine import EngineConfig, generate_live_orders
from .notifier import eod_summary_message
from .strategy_base import StrategyAdapter
from .strategies_impl.autofin_signal import AutofinSignalAdapter


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
    "autofin_signal": AutofinSignalAdapter(),
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
