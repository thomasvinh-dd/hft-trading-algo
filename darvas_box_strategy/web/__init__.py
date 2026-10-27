"""Darvas Box web dashboard.

Single-page Flask app that exposes:
  GET  /                 — HTML dashboard
  GET  /api/signals      — current opening signals (today)
  GET  /api/positions    — currently open positions (from latest backtest)
  GET  /api/backtest     — backtest summary + equity curve
  POST /api/refresh      — manually re-run scan + backtest
  GET  /api/health       — liveness check

Run via:
  python3 -m darvas_box_strategy.web.app
or via CLI:
  python3 -m darvas_box_strategy.web.cli serve --port 5001
"""
from .app import create_app, main
from .data import (
    WebCache,
    get_current_signals,
    get_open_positions,
    get_backtest_summary,
    refresh_cache,
    load_cache,
)

__all__ = [
    "create_app",
    "main",
    "WebCache",
    "get_current_signals",
    "get_open_positions",
    "get_backtest_summary",
    "refresh_cache",
    "load_cache",
]
