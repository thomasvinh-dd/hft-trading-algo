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
from datetime import date, datetime, timezone
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
# Operational dual-write helpers
# ---------------------------------------------------------------------- #

def mirror_order_ledger(
    db: DbClient,
    rows: Iterable[dict[str, Any]],
    *,
    run_id: str | None = None,
) -> int | None:
    """Mirror the bot's order ledger into ``order_ledger``.

    ``run_id`` triggers a delete-then-insert for that run (mirrors
    how ``save_ledger`` rewrites the whole per-run slice); pass
    ``None`` to use plain UPSERT (additive).
    """
    if run_id:
        return _safe(db.replace_order_ledger_for_run, run_id, list(rows))
    return _safe(db.upsert_order_ledger, rows)


def mirror_latest_orders(
    db: DbClient,
    *,
    strategy: str,
    trade_date: str,
    items: Iterable[dict[str, Any]],
) -> int | None:
    """Mirror today's planned orders into ``latest_orders``.

    Replaces the previous set for ``(strategy, trade_date)`` so the
    plan stays in sync with the JSON file written by
    ``export_orders_for_date``.
    """
    return _safe(db.replace_latest_orders, strategy=strategy,
                 trade_date=trade_date, items=list(items))


def mirror_daily_report(
    db: DbClient,
    *,
    strategy: str,
    source: str,
    report: dict[str, Any],
    local_date: str | None = None,
) -> int | None:
    """Upsert a structured EOD / start-of-day snapshot.

    ``report`` is the structured payload extracted from the bot's
    snapshot (cash, nav, positions JSON, etc). The ``source`` arg
    is one of ``start_of_day``, ``eod``, ``heartbeat``.
    """
    payload = dict(report)
    payload["strategy"] = strategy
    payload["source"] = source
    if local_date and "report_date" not in payload:
        payload["report_date"] = local_date
    return _safe(db.upsert_daily_report, payload)


def emit_cycle_event(
    db: DbClient | None,
    *,
    strategy: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    message: str | None = None,
    local_date: str | None = None,
    occurred_at: datetime | None = None,
) -> int | None:
    """Emit a single cycle event to ``cycle_events``.

    Returns the new event_id on success, ``None`` if Postgres is
    unavailable. Safe to call with ``db=None`` — no-op.
    """
    if db is None:
        return None
    return _safe(
        db.insert_cycle_event,
        strategy=strategy,
        event_type=event_type,
        occurred_at=occurred_at,
        local_date=local_date,
        payload=payload,
        message=message,
    )


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


def read_order_ledger(
    *,
    db: DbClient | None = None,
    run_id: str | None = None,
    symbol: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Read the order ledger, preferring Postgres."""
    if db is not None:
        try:
            return db.read_order_ledger(run_id=run_id, symbol=symbol, limit=limit)
        except DbError as exc:
            logger.warning("Postgres read failed, falling back to JSON: %s", exc)

    # JSON fallback: load the on-disk ledger
    from pathlib import Path
    from .data import DEFAULT_DATA_DIR
    from .order_ledger import load_ledger

    path = Path(DEFAULT_DATA_DIR) / "live" / "order_ledger.json"
    if not path.exists():
        return []
    ledger = load_ledger(DEFAULT_DATA_DIR)
    rows = [
        {
            "runId": item.runId,
            "sourceOrderId": item.sourceOrderId,
            "sourceDate": item.sourceDate,
            "symbol": item.symbol,
            "side": item.side,
            "quantity": item.quantity,
            "price": item.price,
            "reason": item.reason,
            "status": item.status,
            "dnseOrderId": item.dnseOrderId,
            "createdAt": item.createdAt,
            "updatedAt": item.updatedAt,
            "lastDetail": item.lastDetail,
            "error": item.error,
        }
        for item in ledger.values()
    ]
    if run_id:
        rows = [r for r in rows if r["runId"] == run_id]
    if symbol:
        rows = [r for r in rows if r["symbol"] == symbol]
    rows.sort(key=lambda r: r.get("updatedAt") or "", reverse=True)
    return rows[:limit]


def read_daily_reports(
    strategy: str,
    *,
    db: DbClient | None = None,
    since: str | None = None,
    source: str | None = None,
    limit: int = 365,
) -> list[dict[str, Any]]:
    """Read structured daily reports for a strategy."""
    if db is not None:
        try:
            return db.read_daily_reports(
                strategy=strategy,
                since=date.fromisoformat(since) if since else None,
                source=source,
                limit=limit,
            )
        except DbError as exc:
            logger.warning("Postgres read failed, falling back to dry-run state: %s", exc)

    # JSON fallback: dry_run_state.<strategy>.json
    from pathlib import Path
    from .data import DEFAULT_DATA_DIR

    path = Path(DEFAULT_DATA_DIR) / "live" / f"dry_run_state.{strategy}.json"
    if not path.exists():
        return []
    import json
    payload = json.loads(path.read_text())
    closed = payload.get("closed_trades") or []
    return [
        {
            "report_date": str(_to_date(payload.get("updated_at")) or ""),
            "source": source or "fallback",
            "initial_nav": payload.get("initial_nav"),
            "cash": payload.get("cash"),
            "cash_total": payload.get("cash"),
            "stock_value": None,
            "nav": None,
            "realized_pnl": payload.get("realized_pnl"),
            "unrealized_pnl": 0.0,
            "total_pnl": payload.get("realized_pnl"),
            "pnl_pct_vs_initial": None,
            "planned_orders_count": 0,
            "applied_orders_count": 0,
            "clipped_orders_count": 0,
            "skipped_orders_count": 0,
            "fetched_at": payload.get("updated_at"),
        }
    ]


def read_cycle_events(
    *,
    db: DbClient | None = None,
    strategy: str | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read cycle events from the audit log."""
    if db is not None:
        try:
            return db.read_cycle_events(
                strategy=strategy, event_type=event_type, since=since, limit=limit,
            )
        except DbError as exc:
            logger.warning("Postgres read failed, no events available: %s", exc)
    return []


def _to_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None
