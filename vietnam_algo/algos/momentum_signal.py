"""Signal-driven momentum rotation.

Why this module exists
----------------------

The original momentum strategy was **replay-driven**: every EOD cycle it
re-ran a ~6-year backtest and then picked the CSV rows whose ``date``
happened to equal today::

    selected = [row for row in rows if row.get("date") == selected_date]

That only works if the backtest happens to have produced a trade on
exactly today. In practice it almost never does — the backtest's last
signal was 2026-11-18 and the strategy generated ``count: 0`` for every
session from 2026-11-19 onward. The bot was alive and "healthy" while
never once submitting an order.

This module computes today's signals **live** from the price data,
using exactly the same decision math the backtest uses, so the bot
trades whenever the strategy actually has an opinion.

The decision math is REUSED, not re-implemented
-----------------------------------------------

Every rule below comes from the backtest engine, so the live decisions
cannot drift from the backtested behaviour:

  * ``rank_symbols``          — the momentum score / candidate ranking
  * ``market_regime``         — bull / neutral / bear classification
  * ``allowed_positions_for_regime`` — position cap per regime
  * ``breakout_symbols``      — volume-breakout candidates
  * ``MomentumWithBreakoutStrategy.select_entries`` — the entry decision
  * ``regime_exit_reason`` / ``trailing_stop_percent_for_regime`` — exits
  * ``risk_controls``         — vol targeting and the exposure caps

What is different from the replay
---------------------------------

The replay could only ever *enter* on a date the backtest had already
entered. This module computes both sides of the book live:

  * entries — from the ranking, the regime cap and the breakout set;
  * exits  — from the LIVE DNSE portfolio: regime exits, stop-loss,
    trailing stop, and rank exit.

Position state that the backtest kept in memory (entry price, peak
price, entry date) is reconstructed from the live portfolio plus a
small per-strategy state file, because the trailing stop needs to
remember the high-water mark.

See ``libs/momentum_signal.py`` for the decision engine and this
module for the adapter that wires it into the bot.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig

from .base import StrategyAdapter


def _live_client():
    """Build a DNSE client from the environment.

    Returns ``(client, account_no)``. The client is ``None`` when the
    credentials are missing — the engine then falls back to the
    tracked state and the backtest's (fictional) cash balance, and the
    cycle summary says so.
    """
    import os

    api_key = os.environ.get("DNSE_API_KEY", "").strip()
    api_secret = os.environ.get("DNSE_API_SECRET", "").strip()
    account_no = os.environ.get("DNSE_ACCOUNT_NO", "").strip()
    if not api_key or not api_secret or not account_no:
        return None, account_no
    try:
        from vietnam_algo.libs.dnse.client import DnseClient

        return DnseClient(api_key, api_secret), account_no
    except Exception:
        return None, account_no


class MomentumSignalAdapter(StrategyAdapter):
    """Signal-driven liquid momentum rotation.

    Computes today's entries and exits from the live price data plus
    the live DNSE portfolio, instead of replaying a historical backtest.
    """

    name = "momentum_signal"
    description = "Live signal-driven liquid momentum rotation"
    env_filename = ".env.vietnam-algo-best-ca-fixed-delay6"

    @property
    def backtest_orders_filename(self) -> str:
        """The signal path writes its own per-day order file.

        It is named after this adapter so it can never be confused
        with the replay-driven ``liquid_momentum_rotation_orders.csv``
        that the backtest writes — the two have different semantics
        (one is "the plan for today", the other is "every trade the
        backtest ever made").
        """
        return "momentum_signal_orders.csv"

    @property
    def backtest_result_filename(self) -> str:
        return "momentum_signal.json"

    def generate_orders(
        self,
        config: EngineConfig,
        strategy_config: StrategyConfig,
        trade_date: str | None = None,
        *,
        skip_backtest: bool = False,
        live_submit: bool = False,
    ) -> dict[str, Any]:
        """Compute today's orders from live data.

        ``skip_backtest`` is a no-op — there is no backtest to skip.
        ``live_submit`` is forwarded to the decision engine so it can
        avoid emitting side-effecting orders (the same guard the
        autofin adapter uses).
        """
        del skip_backtest

        from vietnam_algo.libs.momentum_signal import (
            compute_signal_orders,
            write_signal_outputs,
        )

        today = trade_date or datetime.now(timezone.utc).date().isoformat()

        # The live DNSE client, so the engine can read the real
        # portfolio and the real NAV. Sizing against the backtest's
        # fictional cash balance would order many times the money that
        # is actually in the account.
        client = None
        account_no = ""
        try:
            client, account_no = _live_client()
        except Exception:
            client = None

        result = compute_signal_orders(
            data_dir=config.dataDir,
            strategy_config=strategy_config,
            trade_date=today,
            live_submit=live_submit,
            client=client,
            account_no=account_no,
        )

        # 1. write the per-strategy order files the dry-run replay and
        #    the start-of-day briefing read.
        out_dir = config.dataDir / config.liveDirName
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / self.orders_csv_filename
        json_path = out_dir / self.orders_json_filename
        write_signal_outputs(
            csv_path=csv_path,
            json_path=json_path,
            orders=result.get("orders") or [],
            trade_date=today,
        )

        # 2. the legacy backtest artefacts are still read by the
        #    dry-run replay to derive the qty scale. Keep them
        #    present and in sync so the replay does not break.
        backtests_dir = config.dataDir / "backtests"
        backtests_dir.mkdir(parents=True, exist_ok=True)
        signal_backtest_csv = backtests_dir / self.backtest_orders_filename
        signal_backtest_json = backtests_dir / self.backtest_result_filename
        write_signal_outputs(
            csv_path=signal_backtest_csv,
            json_path=signal_backtest_json,
            orders=result.get("orders") or [],
            trade_date=today,
            as_backtest_result=True,
        )

        return {
            "status": "generated",
            "tradeDate": today,
            "count": len(result.get("orders") or []),
            "strategy": self.name,
            "orders_file": str(csv_path),
            "latestOrdersCsv": str(csv_path),
            "latestOrdersJson": str(json_path),
            "deals_actioned": result.get("deals_actioned") or [],
            "signal": {
                "regime": result.get("regime"),
                "candidates": result.get("candidates") or [],
                "entries": result.get("entries") or [],
                "exits": result.get("exits") or [],
                "blocked": result.get("blocked") or [],
                "nav": result.get("nav"),
                "notes": result.get("notes") or [],
            },
        }

    def format_cycle_summary(
        self,
        result: dict[str, Any],
        portfolio: dict[str, Any] | None = None,
    ) -> str:
        """Telegram summary for the signal-driven momentum cycle."""
        signal = result.get("signal") or {}
        lines: list[str] = [
            "📈 Vietnam Algo (momentum): signal-driven EOD",
            f"📅 {result.get('tradeDate') or '?'}",
        ]
        regime = signal.get("regime")
        lines.append(f"regime: {regime or '?'}")
        if signal.get("nav"):
            lines.append(f"NAV: {signal['nav']:,.0f} VND")

        entries = signal.get("entries") or []
        exits = signal.get("exits") or []
        lines.append(f"entries: {len(entries)}  exits: {len(exits)}")

        for e in entries:
            lines.append(
                f"  ➕ {e.get('symbol')} {e.get('quantity')} "
                f"@ {e.get('price'):,.0f} ({e.get('reason')})"
            )
        for e in exits:
            lines.append(
                f"  ➖ {e.get('symbol')} {e.get('quantity')} "
                f"@ {e.get('price'):,.0f} ({e.get('reason')})"
            )
        blocked = signal.get("blocked") or []
        for b in blocked:
            lines.append(f"  ⛔ {b.get('symbol')}: {b.get('reason')}")

        notes = signal.get("notes") or []
        for n in notes:
            lines.append(f"  ℹ️ {n}")
        return "\n".join(lines)


__all__ = ["MomentumSignalAdapter"]
