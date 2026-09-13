"""Strategy registry — pluggable trading algorithms sharing DNSE infrastructure.

Adding a new algorithm
----------------------

Drop a Python module under ``vietnam_algo/algos/`` that subclasses
``StrategyAdapter`` from ``.base``, then append it to
``STRATEGY_REGISTRY`` below. The auto-trading-bot accepts the new
``--strategy <name>`` flag automatically.

Each algorithm module is expected to provide:
  - name             (str) : short identifier used in CLI/state/env files
  - description      (str) : human-readable label
  - generate_orders  : run the strategy's full decision pipeline and
                       return a result payload (same shape as the legacy
                       ``generate_live_orders`` output).
  - format_cycle_summary (optional) : per-algo Telegram formatter; the
                       default uses ``eod_summary_message``.

Per-strategy state files (auto-namespaced by ``strategy_name``):
  - env:    vietnam_algo/env/.env.{name}
  - state:  data/live/auto_bot_state.{name}.json
  - orders: data/live/latest_orders.{name}.csv + .json

Multiple bots can run concurrently on the same VPS, each with its own
state, env, and Telegram notifications.
"""
from __future__ import annotations

from .autofin_signal import AutofinSignalAdapter
from .base import StrategyAdapter
from .momentum import MomentumRotationAdapter

# Registry — add new algorithms here. The order here is also the
# ordering in ``list_strategies()`` output.
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
