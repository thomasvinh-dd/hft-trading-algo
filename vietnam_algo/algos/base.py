"""StrategyAdapter ABC + helpers, shared by all algorithms.

This module is intentionally tiny and dependency-free so both
the registry and individual algorithm modules can import from it
without circular dependencies. Lives under ``vietnam_algo.algos``
so the public name stays ``from vietnam_algo.algos.base import
StrategyAdapter``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig


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
        *,
        skip_backtest: bool = False,
    ) -> dict[str, Any]:
        """Run the strategy and return the same payload as generate_live_orders.

        ``skip_backtest=True`` is a hint that the caller wants to
        reuse a cached backtest result if one is available (e.g. the
        09:16 submit cycle already ran today). Adapters whose
        ``generate_orders`` is a thin wrapper around
        ``generate_live_orders`` can forward the flag through.
        Adapters that run their own decision logic (e.g. autofin)
        can ignore it.
        """
        ...

    def format_cycle_summary(
        self,
        result: dict[str, Any],
        portfolio: dict[str, Any] | None = None,
    ) -> str:
        """Format a Telegram message for this strategy's EOD summary.

        ``portfolio`` is the live portfolio snapshot (cash, NAV,
        positions) the auto_bot fetched. The base implementation
        forwards it to ``eod_summary_message`` if provided;
        subclasses may override to format per-strategy context.
        """
        from vietnam_algo.libs.notifier import eod_summary_message
        return eod_summary_message(result, portfolio=portfolio)

    @property
    def backtest_csv_name(self) -> str:
        return f"{self.name}_rotation_orders.csv" if self.name else "live_orders.csv"

    @property
    def backtest_orders_filename(self) -> str:
        """Filename (relative to ``dataDir/backtests/``) of the full
        backtest orders CSV produced by the strategy's
        ``generate_orders`` path.

        The runner / dry-run replay reads this file when no live
        submit happens — e.g. the auto-trading-bot's start-of-day
        briefing replays today's filtered JSON against the same
        CSV the EOD run wrote. Default is ``f"{name}_orders.csv"``
        (matches ``libs.backtest.save_order_list`` which names the
        file after ``result.strategy``).
        """
        return f"{self.name}_orders.csv" if self.name else "backtest_orders.csv"

    @property
    def backtest_result_filename(self) -> str:
        """Filename (relative to ``dataDir/backtests/``) of the saved
        backtest result JSON.

        The dry-run apply uses the ``equityCurve[0].cash`` field of
        this file to compute the ``qty_scale`` ratio
        (``state.initial_nav / backtest_initial_cash``) so a 1B-VND
        backtest fits a 100M-VND dry-run budget. Default is
        ``f"{name}.json"`` to match ``libs.backtest.save_backtest_result``.
        """
        return f"{self.name}.json" if self.name else "backtest.json"

    @property
    def orders_csv_filename(self) -> str:
        return f"latest_orders.{self.name}.csv" if self.name else "latest_orders.csv"

    @property
    def orders_json_filename(self) -> str:
        return f"latest_orders.{self.name}.json" if self.name else "latest_orders.json"

    @property
    def state_filename(self) -> str:
        return f"auto_bot_state.{self.name}.json" if self.name else "auto_bot_state.json"
