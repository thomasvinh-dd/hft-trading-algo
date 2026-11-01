"""Postgres-backed price loading for the Darvas Box strategy.

The VPS keeps market data in two places:

  1. ``vietnam_algo/data/prices/<TICKER>.json`` — the JSON files the
     price-sync job writes.
  2. The Postgres ``prices_dnse`` table — the intended source of truth
     (see ``vietnam_algo/db.py``: "Postgres is the new source of truth
     for downstream readers (backtests, algos)").

Both are kept in sync by the ``common_sync`` cron job, which mirrors
the refreshed JSON into Postgres. But the JSON files are the ones that
get overwritten by a careless deploy (see ``scripts/deploy.sh``), and
they were the only source this module read — which is how a stale
local copy ended up marking the dashboard's open deals.

This module reads from Postgres when it's available and falls back to
the JSON files otherwise. The fallback matters: the bots must keep
working during a Postgres maintenance window.

Usage::

    from darvas_box_strategy.darvas.db_prices import load_bars

    bars = load_bars("VNM")        # Postgres first, JSON fallback
    bars = load_bars("VNM", source="json")   # force JSON
    bars = load_bars("VNM", source="db")     # force Postgres
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .data_loader import PriceBar

log = logging.getLogger("darvas.db_prices")

ROOT = Path(__file__).resolve().parents[2]
JSON_PRICES_DIR = ROOT / "vietnam_algo" / "data" / "prices"

# Cache of DbClient instances, keyed by DSN. Creating a
# ThreadedConnectionPool per call would be wasteful — the web app
# loads many tickers.
_DB_CLIENT: Any = None
_DB_CLIENT_FAILED = False


def _db_client():
    """Return a shared DbClient, or None if Postgres isn't usable.

    Never raises — the JSON fallback is the whole point.
    """
    global _DB_CLIENT, _DB_CLIENT_FAILED
    if _DB_CLIENT is not None:
        return _DB_CLIENT
    if _DB_CLIENT_FAILED:
        return None
    if os.environ.get("DARVAS_PRICES_SOURCE", "").lower() == "json":
        return None
    try:
        from vietnam_algo.libs.db import DbClient
        _DB_CLIENT = DbClient.from_default()
        return _DB_CLIENT
    except Exception as exc:
        log.info("Postgres unavailable, using JSON prices: %s", exc)
        _DB_CLIENT_FAILED = True
        return None


def _rows_to_bars(rows: List[Dict[str, Any]]) -> List[PriceBar]:
    out: List[PriceBar] = []
    for row in rows or []:
        try:
            t = row.get("time")
            if not t:
                continue
            out.append(
                PriceBar(
                    time=str(t),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0.0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda b: b.time)
    return out


def load_bars_from_db(ticker: str) -> List[PriceBar]:
    """Read one ticker's bars from Postgres. Returns [] on any failure."""
    db = _db_client()
    if db is None:
        return []
    try:
        rows = db.read_prices_dnse(ticker)
    except Exception as exc:
        log.warning("Postgres read failed for %s: %s", ticker, exc)
        return []
    return _rows_to_bars(rows)


def load_bars_from_json(
    ticker: str, prices_dir: Optional[Path] = None,
) -> List[PriceBar]:
    """Read one ticker's bars from the on-disk JSON file."""
    from .data_loader import load_price_bars

    directory = prices_dir or JSON_PRICES_DIR
    return load_price_bars(directory / f"{ticker.upper()}.json")


def load_bars(
    ticker: str,
    prices_dir: Optional[Path] = None,
    *,
    source: str = "auto",
) -> List[PriceBar]:
    """Load bars for ``ticker``.

    ``source``:
      - ``"auto"``  — Postgres first, JSON fallback (default)
      - ``"db"``    — Postgres only
      - ``"json"``  — JSON only
    """
    ticker = ticker.upper()
    if source == "json":
        return load_bars_from_json(ticker, prices_dir)

    bars = load_bars_from_db(ticker)
    if bars:
        return bars
    if source == "db":
        log.warning("Postgres has no bars for %s", ticker)
        return []
    # Fall back to JSON (Postgres down, or the symbol isn't mirrored).
    return load_bars_from_json(ticker, prices_dir)


def db_price_stats() -> Dict[str, Any]:
    """Summary of the Postgres price mirror — for the dashboard/health UI.

    Returns ``{"available": bool, "bars": int, "symbols": int,
    "latest": str}``. Never raises.
    """
    db = _db_client()
    if db is None:
        return {"available": False, "bars": 0, "symbols": 0, "latest": ""}
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT count(*), count(DISTINCT symbol), max(time) FROM prices_dnse"
            )
            row = cur.fetchone()
        if not row:
            return {"available": False, "bars": 0, "symbols": 0, "latest": ""}
        bars, symbols, latest = row
        return {
            "available": True,
            "bars": int(bars or 0),
            "symbols": int(symbols or 0),
            "latest": str(latest or ""),
        }
    except Exception as exc:
        log.warning("Postgres stats failed: %s", exc)
        return {"available": False, "bars": 0, "symbols": 0, "latest": ""}
