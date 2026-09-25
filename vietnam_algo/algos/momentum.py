"""The legacy liquid-momentum rotation algorithm.

Strategy
--------

Replays a pre-computed backtest CSV (data/backtests/liquid_momentum_rotation_orders.csv)
once per EOD cycle, surfacing today's planned orders via the same
``generate_live_orders()`` pipeline the rest of the legacy ``vietnam-algo``
commands use. The strategy itself owns no decision logic — the backtest
runner picks which trades fall on the latest CSV date and feeds them to
``generate_live_orders`` for persistence + dry-run replay.

Why a dedicated file (separate from AutofinSignalAdapter)
---------------------------------------------------------

Each algorithm under ``vietnam_algo.algos/`` gets its own file so:

  - the registry stays a one-screen lookup table;
  - per-algo state, env, and orders naming is colocated;
  - adding a new algo is a 2-file change (drop a file here, append to
    ``algos/registry.py``).

See ``algos/autofin_signal.py`` for the contrast — that adapter carries
its own signal-decision logic (deal tracking, SL/TP brackets, actioned
state, etc.), whereas this one is a thin wrapper around the legacy
backtest pipeline.

Per-strategy state files
------------------------

  - env:    vietnam_algo/env/.env.vietnam-algo-best-ca-fixed-delay6
  - state:  data/live/auto_bot_state.momentum.json
  - orders: data/live/latest_orders.momentum.{csv,json}
"""
from __future__ import annotations

from typing import Any

from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig, generate_live_orders

from .base import StrategyAdapter


class MomentumRotationAdapter(StrategyAdapter):
    """The legacy liquid-momentum rotation (DRYRUN default).

    Delegates entirely to ``generate_live_orders()`` which runs the
    full backtest via ``strategy_runner('momentum')`` and writes
    per-strategy order files.
    """

    name = "momentum"
    description = "Liquid momentum rotation with corporate-action awareness"
    env_filename = ".env.vietnam-algo-best-ca-fixed-delay6"

    # The legacy ``run_momentum_rotation`` backtest sets
    # ``BacktestResult(strategy="liquid_momentum_rotation", ...)``, so
    # ``save_backtest_result`` and ``save_order_list`` write to files
    # prefixed with ``liquid_momentum_rotation_`` rather than the
    # ``momentum_`` default the base adapter would produce. Override
    # the two properties so callers can ask the adapter "where do you
    # write your backtest artefacts?" without hardcoding the prefix.
    @property
    def backtest_orders_filename(self) -> str:
        return "liquid_momentum_rotation_orders.csv"

    @property
    def backtest_result_filename(self) -> str:
        return "liquid_momentum_rotation.json"

    def generate_orders(
        self,
        config: EngineConfig,
        strategy_config: StrategyConfig,
        trade_date: str | None = None,
        *,
        skip_backtest: bool = False,
    ) -> dict[str, Any]:
        # Use the legacy generate_live_orders path which writes to backtests/.
        # Pass ``strategy_name=self.name`` so the per-strategy latest_orders
        # files (data/live/latest_orders.momentum.csv/.json) get the same
        # contents the start-of-day briefing reads. Without this arg,
        # ``generate_live_orders`` falls back to the legacy
        # ``latest_orders.json`` filename, leaving the strategy-namespaced
        # file stale and the briefing showing yesterday's plan.
        #
        # ``skip_backtest=True`` reuses the orders CSV written by an
        # earlier ``generate_live_orders`` call today (typically the
        # 09:16 submit cycle) and only re-runs the cheap
        # ``export_orders_for_date`` step. This saves the ~3 min the
        # full ~6-year momentum backtest takes when the auto-trading-
        # bot's EOD cycle runs at 15:15 ICT. See 2026-09-25 review
        # issue #1 — the same backtest used to run twice per day.
        result = generate_live_orders(
            config, strategy_config, trade_date=trade_date,
            strategy_name=self.name,
            skip_backtest=skip_backtest,
        )
        return result
