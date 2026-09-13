"""Dual-write helpers that mirror the JSON files the bot writes
into the Postgres data layer.

We don't change ``libs.data`` or ``libs.corporate_actions`` to know
about Postgres — they keep writing JSON exactly as before. These
helpers are called by ``update_dataset`` and the CA sync path
*after* a successful JSON write, with the same data, to mirror the
result to Postgres.

Reads (``read_*``) prefer Postgres when a connection is available
and fall back to JSON on a ``DbError`` or empty result. That keeps
the bots working even during a Postgres maintenance window.

Usage::

    from vietnam_algo.libs.db import DbClient
    from vietnam_algo.libs.db_sync import mirror_symbols, mirror_prices

    db = DbClient.from_default()
    symbols = fetch_all_symbols(...)
    save_symbols(symbols, data_dir)        # JSON write
    mirror_symbols(db, symbols)            # mirror to Postgres

    bars = fetch_price_bars(symbol, ...)
    save_price_bars(symbol, bars, data_dir)
    mirror_prices(db, symbol, [asdict(b) for b in bars])
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from .db import DbClient, DbError


logger = logging.getLogger(__name__)


def _safe(fn, *args, **kwargs):
    """Run a DB op and log on failure — never raise.

    The bots fall back to JSON if Postgres is down, so a DB write
    failure must be visible but non-fatal. The JSON files are
    authoritative for the duration of any outage.
    """
    try:
        return fn(*args, **kwargs)
    except DbError as exc:
        logger.warning("DB write failed (will keep JSON): %s", exc)
        return None
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Unexpected DB write error: %s", exc)
        return None


def mirror_symbols(db: DbClient, symbols: Iterable[dict[str, Any]]) -> int | None:
    """Mirror a list of symbol dicts into ``symbols`` table.

    Returns the row count on success, ``None`` if Postgres is down.
    """
    return _safe(db.upsert_symbols, symbols)


def mirror_prices(db: DbClient, symbol: str, bars: Sequence[dict[str, Any]]) -> int | None:
    """Mirror one symbol's bars into ``prices_dnse``.

    ``bars`` is a sequence of dicts with at minimum a ``time`` key.
    Returns the row count on success, ``None`` if Postgres is down.
    """
    return _safe(db.upsert_prices_dnse, symbol, bars)


def mirror_corporate_actions(db: DbClient, events: Iterable[dict[str, Any]]) -> int | None:
    return _safe(db.upsert_corporate_actions, events)


def mirror_market_holidays(db: DbClient, holidays: Iterable[dict[str, Any]]) -> int | None:
    return _safe(db.upsert_market_holidays, holidays)


# ---------------------------------------------------------------------- #
# Read API — prefer Postgres, fall back to JSON on DbError / empty.
# ---------------------------------------------------------------------- #


def read_prices_dnse(symbol: str, *, db: DbClient | None = None) -> list[dict[str, Any]]:
    """Read bars for ``symbol``.

    Order of preference:
      1. If ``db`` is given AND it can answer, use Postgres.
      2. Otherwise fall back to ``data/prices/<SYMBOL>.json``.

    Returning an empty list is treated as "no data here"; callers
    that need to know which source was used should call the
    ``DbClient`` directly.

    Raises nothing on a Postgres outage — the JSON fallback is the
    point of this helper.
    """
    if db is not None:
        try:
            rows = db.read_prices_dnse(symbol)
            if rows:
                return rows
        except DbError as exc:
            logger.warning("Postgres read failed, falling back to JSON: %s", exc)

    # JSON fallback
    from pathlib import Path
    from .data import DEFAULT_DATA_DIR, load_saved_price_bars

    path = Path(DEFAULT_DATA_DIR) / "prices" / f"{symbol.upper()}.json"
    return load_saved_price_bars(path)


def read_symbols(*, db: DbClient | None = None) -> list[str]:
    """Read the full symbol universe, preferring Postgres."""
    if db is not None:
        try:
            with db.cursor() as cur:
                cur.execute("SELECT symbol FROM symbols ORDER BY symbol")
                rows = [r[0] for r in cur.fetchall()]
            if rows:
                return rows
        except DbError as exc:
            logger.warning("Postgres read failed, falling back to JSON: %s", exc)

    # JSON fallback
    from .data import load_saved_symbols
    return load_saved_symbols()


def read_corporate_actions(
    *, db: DbClient | None = None, symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Read corporate actions, preferring Postgres."""
    if db is not None:
        try:
            rows = db.read_corporate_actions(symbol=symbol)
            if rows:
                return rows
        except DbError as exc:
            logger.warning("Postgres read failed, falling back to JSON: %s", exc)

    # JSON fallback: corporate_action.json
    from pathlib import Path
    from .data import DEFAULT_DATA_DIR

    path = Path(DEFAULT_DATA_DIR) / "corporate_action.json"
    if not path.exists():
        return []
    import json

    payload = json.loads(path.read_text())
    items = payload.get("items") or []
    if symbol:
        items = [e for e in items if e.get("symbol") == symbol]
    return items
