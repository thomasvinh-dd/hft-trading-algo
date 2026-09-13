"""Postgres data layer for the trading bot.

The bot's read-heavy data (symbols, prices, corporate actions, market
holidays) is mirrored into a local Postgres container (see
``docker-compose.yml`` at the project root). State files (dry-run,
autofin signal actioned-deals, auto-bot state) stay as JSON on disk
because they're tiny and frequently mutated.

Design notes
------------

- ``DbClient`` opens a thread-safe psycopg2 ``ThreadedConnectionPool``
  on first use and reuses connections for the lifetime of the process.
  The two trading bots each get one ``DbClient`` instance, so we cap
  the pool at ~size 4 per process.
- ``sync_*`` functions dual-write: they continue to write the legacy
  JSON files AND mirror to Postgres. Postgres is the new source of
  truth for downstream readers (backtests, algos) but the JSON files
  are kept as a fallback during the transition.
- Reads default to Postgres when a connection is available; pass
  ``force_json=True`` to fall back to the on-disk JSON. This means
  the bots keep working even if Postgres is down for a maintenance
  window — they just lose the new mirror.
- Bulk loads use ``COPY`` from a buffer (not ``execute_values``) for
  the initial ~1.5GB price history. COPY is ~10x faster for big
  inserts.

Schema is defined in ``scripts/postgres/initdb.d/001_init.sql`` and
applied automatically on first container start. The same file is
re-runnable (CREATE TABLE IF NOT EXISTS) so it doubles as a manual
migration helper.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import execute_values, Json


logger = logging.getLogger(__name__)


DEFAULT_DSN = "host=127.0.0.1 port=5432 dbname=hft user=hft password=hft_dev_pw_change_me"
DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 4


class DbError(RuntimeError):
    """Raised when the Postgres data layer fails in a way callers
    should react to (e.g., by falling back to JSON)."""


@dataclass
class DbConfig:
    """Connection settings sourced from the environment.

    Override the defaults in production by exporting ``PG_DSN`` (or any
    of the ``PG_*`` settings) before the bot process starts.
    """

    dsn: str
    pool_min: int
    pool_max: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "DbConfig":
        e = env if env is not None else os.environ
        return cls(
            dsn=e.get("PG_DSN", DEFAULT_DSN),
            pool_min=int(e.get("PG_POOL_MIN", str(DEFAULT_POOL_MIN))),
            pool_max=int(e.get("PG_POOL_MAX", str(DEFAULT_POOL_MAX))),
        )


class DbClient:
    """Thread-safe wrapper around a psycopg2 ``ThreadedConnectionPool``.

    Use as a long-lived process-wide singleton::

        db = DbClient.from_default()
        with db.cursor() as cur:
            cur.execute("SELECT count(*) FROM prices_dnse")
            print(cur.fetchone())

    The pool is created lazily on first ``cursor()``/``commit()`` call
    so import-side-effect free. ``close()`` releases all connections;
    it's a no-op when the pool was never opened.
    """

    def __init__(self, config: DbConfig | None = None) -> None:
        self._config = config or DbConfig.from_env()
        self._pool: pg_pool.ThreadedConnectionPool | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_default(cls) -> "DbClient":
        return cls(DbConfig.from_env())

    @property
    def config(self) -> DbConfig:
        return self._config

    def _ensure_pool(self) -> pg_pool.ThreadedConnectionPool:
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is None:
                try:
                    self._pool = pg_pool.ThreadedConnectionPool(
                        minconn=self._config.pool_min,
                        maxconn=self._config.pool_max,
                        dsn=self._config.dsn,
                    )
                    logger.info(
                        "Postgres pool ready (min=%d max=%d)",
                        self._config.pool_min,
                        self._config.pool_max,
                    )
                except psycopg2.Error as exc:
                    raise DbError(f"could not open Postgres pool: {exc}") from exc
            return self._pool

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        pool = self._ensure_pool()
        conn = pool.getconn()
        try:
            yield conn
        finally:
            pool.putconn(conn)

    @contextmanager
    def cursor(self, *, commit: bool = False) -> Iterator[Any]:
        """Yield a cursor. ``commit=True`` commits the surrounding txn
        when the context exits cleanly (no exception)."""
        with self._conn() as conn:
            cur = conn.cursor()
            try:
                yield cur
                if commit:
                    conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()

    def commit(self) -> None:
        """Commit the most-recent txn on a checked-out connection.

        Use this if you opened a cursor without ``commit=True`` and
        later want to commit manually. Most callers should prefer
        ``with db.cursor(commit=True) as cur:``.
        """
        # psycopg2 connections commit themselves implicitly when
        # returned to the pool unless we explicitly roll back, so
        # this method mostly exists for symmetry with cursor().
        # No-op for now.
        return None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None

    # ------------------------------------------------------------------ #
    # Schema helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def init_schema(conn: Any) -> None:
        """Apply the SQL in ``scripts/postgres/initdb.d/001_init.sql``.

        Idempotent — every CREATE uses IF NOT EXISTS. We embed the
        SQL inline here (rather than reading from disk) so the schema
        travels with the code.
        """
        from vietnam_algo.libs import schema_sql

        with conn.cursor() as cur:
            cur.execute(schema_sql.INIT_SQL)
        conn.commit()

    def init_schema_via_pool(self) -> None:
        with self._conn() as conn:
            self.init_schema(conn)

    # ------------------------------------------------------------------ #
    # symbols
    # ------------------------------------------------------------------ #

    def upsert_symbols(self, symbols: Iterable[dict[str, Any]]) -> int:
        """Bulk-upsert rows into ``symbols``. Returns the row count.

        Each row must have at least a ``symbol`` key. Optional fields:
        ``exchange``, ``companyName``/``company_name``, ``industry``.
        The full payload is also stored in the ``payload`` JSONB column
        for forward compatibility.
        """
        rows = []
        for sym in symbols:
            symbol = sym.get("symbol") or sym.get("ticker")
            if not symbol:
                continue
            rows.append((
                symbol,
                sym.get("exchange"),
                sym.get("companyName") or sym.get("company_name"),
                sym.get("industry"),
                Json(sym),
            ))
        if not rows:
            return 0
        with self.cursor(commit=True) as cur:
            execute_values(
                cur,
                """
                INSERT INTO symbols (symbol, exchange, company_name, industry, payload)
                VALUES %s
                ON CONFLICT (symbol) DO UPDATE SET
                    exchange = EXCLUDED.exchange,
                    company_name = EXCLUDED.company_name,
                    industry = EXCLUDED.industry,
                    payload = EXCLUDED.payload,
                    fetched_at = now()
                """,
                rows,
            )
        return len(rows)

    def count_symbols(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT count(*) FROM symbols")
            return int(cur.fetchone()[0])

    # ------------------------------------------------------------------ #
    # prices
    # ------------------------------------------------------------------ #

    def upsert_prices_dnse(
        self,
        symbol: str,
        bars: Sequence[dict[str, Any]],
    ) -> int:
        """Bulk-upsert daily bars for one symbol. Returns row count.

        Bars shape: ``{"time": "YYYY-MM-DD", "open": float, ...}``.
        Uses ``execute_values`` for moderate batches; for the initial
        load use :func:`copy_prices_dnse` (10x faster).
        """
        rows = [_bar_to_row(symbol, b) for b in bars if b.get("time")]
        if not rows:
            return 0
        with self.cursor(commit=True) as cur:
            execute_values(
                cur,
                """
                INSERT INTO prices_dnse (symbol, "time", open, high, low, close, volume)
                VALUES %s
                ON CONFLICT (symbol, "time") DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
                """,
                rows,
            )
        return len(rows)

    def copy_prices_dnse(
        self,
        symbol: str,
        bars: Sequence[dict[str, Any]],
    ) -> int:
        """Bulk-load via ``COPY FROM STDIN``. Use for the initial
        ~3000 bars × 1500 symbols migration; ``upsert_prices_dnse``
        is fine for incremental daily syncs.
        """
        rows = [_bar_to_row(symbol, b) for b in bars if b.get("time")]
        if not rows:
            return 0
        with self.cursor(commit=True) as cur:
            from psycopg2 import sql
            from io import StringIO

            buf = StringIO()
            for r in rows:
                # COPY expects: symbol\tdate\topen\thigh\tlow\tclose\tvolume\n
                buf.write(f"{r[0]}\t{r[1]}\t{r[2]}\t{r[3]}\t{r[4]}\t{r[5]}\t{r[6]}\n")
            buf.seek(0)
            stmt = sql.SQL(
                "COPY prices_dnse (symbol, \"time\", open, high, low, close, volume) "
                "FROM STDIN WITH (FORMAT text)"
            )
            cur.copy_expert(stmt, buf)
        return len(rows)

    def read_prices_dnse(
        self,
        symbol: str,
        *,
        since: date | None = None,
    ) -> list[dict[str, Any]]:
        """Return all bars for ``symbol`` (optionally filtered by date).

        Empty list if the symbol isn't in the table.
        """
        with self.cursor() as cur:
            if since is None:
                cur.execute(
                    "SELECT \"time\", open, high, low, close, volume "
                    "FROM prices_dnse WHERE symbol = %s ORDER BY \"time\"",
                    (symbol,),
                )
            else:
                cur.execute(
                    "SELECT \"time\", open, high, low, close, volume "
                    "FROM prices_dnse WHERE symbol = %s AND \"time\" >= %s ORDER BY \"time\"",
                    (symbol, since),
                )
            return [
                {
                    "time": t.isoformat(),
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": float(v),
                }
                for (t, o, h, l, c, v) in cur.fetchall()
            ]

    def count_prices_dnse(self, symbol: str | None = None) -> int:
        with self.cursor() as cur:
            if symbol:
                cur.execute(
                    "SELECT count(*) FROM prices_dnse WHERE symbol = %s",
                    (symbol,),
                )
            else:
                cur.execute("SELECT count(*) FROM prices_dnse")
            return int(cur.fetchone()[0])

    # ------------------------------------------------------------------ #
    # corporate actions
    # ------------------------------------------------------------------ #

    def upsert_corporate_actions(
        self,
        events: Iterable[dict[str, Any]],
    ) -> int:
        """Bulk-upsert into ``corporate_actions`` keyed on ``eventId``.

        Each event must have ``eventId`` or ``event_id``. The full
        Vietstock payload is preserved in the ``payload`` JSONB column.
        """
        rows = []
        for ev in events:
            eid = ev.get("eventId") or ev.get("event_id")
            if eid is None:
                continue
            symbol = ev.get("symbol")
            rows.append((
                int(eid),
                symbol,
                _parse_date(ev.get("exDate") or ev.get("ex_date")),
                _parse_date(ev.get("recordDate") or ev.get("record_date")),
                _parse_date(ev.get("paymentDate") or ev.get("payment_date")),
                ev.get("name"),
                ev.get("title"),
                ev.get("exchange"),
                Json(ev),
            ))
        if not rows:
            return 0
        with self.cursor(commit=True) as cur:
            execute_values(
                cur,
                """
                INSERT INTO corporate_actions
                    (event_id, symbol, ex_date, record_date, payment_date,
                     name, title, exchange, payload)
                VALUES %s
                ON CONFLICT (event_id) DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    ex_date = EXCLUDED.ex_date,
                    record_date = EXCLUDED.record_date,
                    payment_date = EXCLUDED.payment_date,
                    name = EXCLUDED.name,
                    title = EXCLUDED.title,
                    exchange = EXCLUDED.exchange,
                    payload = EXCLUDED.payload,
                    fetched_at = now()
                """,
                rows,
            )
        return len(rows)

    def read_corporate_actions(
        self,
        *,
        symbol: str | None = None,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            clauses = []
            args: list[Any] = []
            if symbol:
                clauses.append("symbol = %s")
                args.append(symbol)
            if since:
                clauses.append("(ex_date >= %s OR record_date >= %s)")
                args.extend([since, since])
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            sql = (
                "SELECT event_id, symbol, ex_date, record_date, payment_date, "
                "name, title, exchange, payload "
                f"FROM corporate_actions {where} ORDER BY ex_date DESC NULLS LAST"
            )
            if limit:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql, args)
            return [
                {
                    "eventId": eid,
                    "symbol": sym,
                    "exDate": ex.isoformat() if ex else None,
                    "recordDate": rd.isoformat() if rd else None,
                    "paymentDate": pd.isoformat() if pd else None,
                    "name": name,
                    "title": title,
                    "exchange": exchange,
                    **payload,
                }
                for (eid, sym, ex, rd, pd, name, title, exchange, payload)
                in cur.fetchall()
            ]

    def count_corporate_actions(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT count(*) FROM corporate_actions")
            return int(cur.fetchone()[0])

    # ------------------------------------------------------------------ #
    # market holidays
    # ------------------------------------------------------------------ #

    def upsert_market_holidays(self, holidays: Iterable[dict[str, Any]]) -> int:
        rows = []
        for h in holidays:
            d = _parse_date(h.get("date") or h.get("holiday"))
            if d is None:
                continue
            rows.append((
                d,
                h.get("name") or h.get("holidayName"),
                h.get("type") or h.get("holidayType"),
                h.get("source", "market-holiday.com"),
            ))
        if not rows:
            return 0
        with self.cursor(commit=True) as cur:
            execute_values(
                cur,
                """
                INSERT INTO market_holidays (date, name, holiday_type, source)
                VALUES %s
                ON CONFLICT (date) DO UPDATE SET
                    name = EXCLUDED.name,
                    holiday_type = EXCLUDED.holiday_type,
                    source = EXCLUDED.source,
                    fetched_at = now()
                """,
                rows,
            )
        return len(rows)

    def read_market_holidays(self) -> set[str]:
        with self.cursor() as cur:
            cur.execute("SELECT date FROM market_holidays")
            return {row[0].isoformat() for row in cur.fetchall()}

    # ------------------------------------------------------------------ #
    # order_ledger
    # ------------------------------------------------------------------ #

    def upsert_order_ledger(
        self,
        rows: Iterable[dict[str, Any]],
    ) -> int:
        """Bulk-upsert into ``order_ledger``.

        Each dict must have at minimum ``runId``, ``sourceOrderId``,
        ``symbol``, ``side``. The full ``lastDetail`` payload is
        preserved in the ``last_detail`` JSONB column.
        """
        records = []
        for row in rows:
            required = ("runId", "sourceOrderId", "symbol", "side")
            if not all(row.get(k) for k in required):
                continue
            records.append((
                str(row["runId"]),
                int(row["sourceOrderId"]),
                _parse_date(row.get("sourceDate") or row.get("source_date")),
                str(row["symbol"]).upper(),
                str(row["side"]).upper(),
                int(row.get("quantity") or 0),
                float(row.get("price") or 0),
                row.get("reason"),
                str(row.get("status") or "unknown"),
                row.get("dnseOrderId") or row.get("dnse_order_id") or None,
                _parse_dt(row.get("createdAt") or row.get("created_at")) or _utc_now(),
                _parse_dt(row.get("updatedAt") or row.get("updated_at")) or _utc_now(),
                Json(row.get("lastDetail") or row.get("last_detail")) if (row.get("lastDetail") or row.get("last_detail")) else None,
                row.get("error"),
            ))
        if not records:
            return 0
        with self.cursor(commit=True) as cur:
            execute_values(
                cur,
                """
                INSERT INTO order_ledger (
                    run_id, source_order_id, source_date, symbol, side,
                    quantity, price, reason, status, dnse_order_id,
                    created_at, updated_at, last_detail, error
                ) VALUES %s
                ON CONFLICT (run_id, source_order_id, symbol, side) DO UPDATE SET
                    source_date = EXCLUDED.source_date,
                    quantity = EXCLUDED.quantity,
                    price = EXCLUDED.price,
                    reason = EXCLUDED.reason,
                    status = EXCLUDED.status,
                    dnse_order_id = COALESCE(EXCLUDED.dnse_order_id, order_ledger.dnse_order_id),
                    created_at = COALESCE(order_ledger.created_at, EXCLUDED.created_at),
                    updated_at = EXCLUDED.updated_at,
                    last_detail = COALESCE(EXCLUDED.last_detail, order_ledger.last_detail),
                    error = COALESCE(EXCLUDED.error, order_ledger.error)
                """,
                records,
            )
        return len(records)

    def replace_order_ledger_for_run(
        self,
        run_id: str,
        rows: Iterable[dict[str, Any]],
    ) -> int:
        """Replace the full set of ledger rows for ``run_id``.

        Used by ``save_ledger`` which owns the entire ledger per run.
        The transaction deletes the prior set and inserts the new
        set in a single transaction so we never see a half-written
        ledger.
        """
        record_list = list(rows)
        with self.cursor(commit=True) as cur:
            cur.execute("DELETE FROM order_ledger WHERE run_id = %s", (run_id,))
        if not record_list:
            return 0
        return self.upsert_order_ledger(record_list)

    def count_order_ledger(self, *, run_id: str | None = None) -> int:
        with self.cursor() as cur:
            if run_id:
                cur.execute("SELECT count(*) FROM order_ledger WHERE run_id = %s", (run_id,))
            else:
                cur.execute("SELECT count(*) FROM order_ledger")
            return int(cur.fetchone()[0])

    def read_order_ledger(
        self,
        *,
        run_id: str | None = None,
        symbol: str | None = None,
        status: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses, args = [], []
        if run_id:
            clauses.append("run_id = %s")
            args.append(run_id)
        if symbol:
            clauses.append("symbol = %s")
            args.append(symbol)
        if status:
            clauses.append("status = %s")
            args.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT run_id, source_order_id, source_date, symbol, side, "
            "quantity, price, reason, status, dnse_order_id, "
            "created_at, updated_at, last_detail, error "
            f"FROM order_ledger {where} ORDER BY updated_at DESC LIMIT {int(limit)}"
        )
        with self.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        return [
            {
                "runId": r[0],
                "sourceOrderId": r[1],
                "sourceDate": r[2].isoformat() if r[2] else None,
                "symbol": r[3],
                "side": r[4],
                "quantity": r[5],
                "price": float(r[6]),
                "reason": r[7],
                "status": r[8],
                "dnseOrderId": r[9] or "",
                "createdAt": r[10].isoformat() if r[10] else None,
                "updatedAt": r[11].isoformat() if r[11] else None,
                "lastDetail": r[12],
                "error": r[13] or "",
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # daily_reports
    # ------------------------------------------------------------------ #

    def upsert_daily_report(self, report: dict[str, Any]) -> int:
        """Upsert one row in ``daily_reports``.

        Required: ``strategy``, ``report_date`` (YYYY-MM-DD),
        ``source`` ('start_of_day' | 'eod' | 'heartbeat'), ``cash``,
        ``nav``.
        """
        if not all(report.get(k) for k in ("strategy", "report_date", "source")):
            return 0
        positions = report.get("positions") or {}
        closed_trades = report.get("closed_trades") or []
        with self.cursor(commit=True) as cur:
            execute_values(
                cur,
                """
                INSERT INTO daily_reports (
                    strategy, report_date, source,
                    initial_nav, cash, cash_total, stock_value, nav,
                    realized_pnl, unrealized_pnl, total_pnl, pnl_pct_vs_initial,
                    positions, closed_trades,
                    planned_orders_count, applied_orders_count,
                    clipped_orders_count, skipped_orders_count,
                    raw_message
                ) VALUES %s
                ON CONFLICT (strategy, report_date, source) DO UPDATE SET
                    initial_nav = EXCLUDED.initial_nav,
                    cash = EXCLUDED.cash,
                    cash_total = EXCLUDED.cash_total,
                    stock_value = EXCLUDED.stock_value,
                    nav = EXCLUDED.nav,
                    realized_pnl = EXCLUDED.realized_pnl,
                    unrealized_pnl = EXCLUDED.unrealized_pnl,
                    total_pnl = EXCLUDED.total_pnl,
                    pnl_pct_vs_initial = EXCLUDED.pnl_pct_vs_initial,
                    positions = EXCLUDED.positions,
                    closed_trades = EXCLUDED.closed_trades,
                    planned_orders_count = EXCLUDED.planned_orders_count,
                    applied_orders_count = EXCLUDED.applied_orders_count,
                    clipped_orders_count = EXCLUDED.clipped_orders_count,
                    skipped_orders_count = EXCLUDED.skipped_orders_count,
                    raw_message = EXCLUDED.raw_message,
                    fetched_at = now()
                """,
                [(
                    str(report["strategy"]),
                    _parse_date(report["report_date"]),
                    str(report["source"]),
                    _to_float(report.get("initial_nav")),
                    _to_float(report.get("cash"), 0.0),
                    _to_float(report.get("cash_total")),
                    _to_float(report.get("stock_value")),
                    _to_float(report.get("nav"), 0.0),
                    _to_float(report.get("realized_pnl"), 0.0),
                    _to_float(report.get("unrealized_pnl"), 0.0),
                    _to_float(report.get("total_pnl"), 0.0),
                    _to_float(report.get("pnl_pct_vs_initial")),
                    Json(positions),
                    Json(closed_trades),
                    int(report.get("planned_orders_count") or 0),
                    int(report.get("applied_orders_count") or 0),
                    int(report.get("clipped_orders_count") or 0),
                    int(report.get("skipped_orders_count") or 0),
                    report.get("raw_message"),
                )],
            )
        return 1

    def read_daily_reports(
        self,
        strategy: str,
        *,
        since: date | None = None,
        until: date | None = None,
        source: str | None = None,
        limit: int = 365,
    ) -> list[dict[str, Any]]:
        clauses, args = ["strategy = %s"], [strategy]
        if since:
            clauses.append("report_date >= %s")
            args.append(since)
        if until:
            clauses.append("report_date <= %s")
            args.append(until)
        if source:
            clauses.append("source = %s")
            args.append(source)
        where = "WHERE " + " AND ".join(clauses)
        sql = (
            "SELECT report_date, source, initial_nav, cash, cash_total, "
            "stock_value, nav, realized_pnl, unrealized_pnl, total_pnl, "
            "pnl_pct_vs_initial, planned_orders_count, applied_orders_count, "
            "clipped_orders_count, skipped_orders_count, fetched_at "
            f"FROM daily_reports {where} ORDER BY report_date DESC, source LIMIT {int(limit)}"
        )
        with self.cursor() as cur:
            cur.execute(sql, args)
            return [
                {
                    "report_date": r[0].isoformat(),
                    "source": r[1],
                    "initial_nav": _opt_float(r[2]),
                    "cash": float(r[3]),
                    "cash_total": _opt_float(r[4]),
                    "stock_value": _opt_float(r[5]),
                    "nav": float(r[6]),
                    "realized_pnl": float(r[7]),
                    "unrealized_pnl": float(r[8]),
                    "total_pnl": float(r[9]),
                    "pnl_pct_vs_initial": _opt_float(r[10]),
                    "planned_orders_count": r[11],
                    "applied_orders_count": r[12],
                    "clipped_orders_count": r[13],
                    "skipped_orders_count": r[14],
                    "fetched_at": r[15].isoformat(),
                }
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------ #
    # cycle_events
    # ------------------------------------------------------------------ #

    def insert_cycle_event(
        self,
        *,
        strategy: str,
        event_type: str,
        occurred_at: datetime | None = None,
        local_date: date | str | None = None,
        payload: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> int:
        when = occurred_at or datetime.now(tz=timezone.utc)
        ld = _parse_date(local_date) if local_date else None
        with self.cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO cycle_events
                    (strategy, event_type, occurred_at, local_date, payload, message)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING event_id
                """,
                (
                    str(strategy),
                    str(event_type),
                    when,
                    ld,
                    Json(payload or {}),
                    message,
                ),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def read_cycle_events(
        self,
        *,
        strategy: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses, args = [], []
        if strategy:
            clauses.append("strategy = %s")
            args.append(strategy)
        if event_type:
            clauses.append("event_type = %s")
            args.append(event_type)
        if since:
            clauses.append("occurred_at >= %s")
            args.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT event_id, strategy, event_type, occurred_at, local_date, "
            "payload, message "
            f"FROM cycle_events {where} ORDER BY occurred_at DESC LIMIT {int(limit)}"
        )
        with self.cursor() as cur:
            cur.execute(sql, args)
            return [
                {
                    "event_id": r[0],
                    "strategy": r[1],
                    "event_type": r[2],
                    "occurred_at": r[3].isoformat(),
                    "local_date": r[4].isoformat() if r[4] else None,
                    "payload": r[5],
                    "message": r[6],
                }
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------ #
    # latest_orders
    # ------------------------------------------------------------------ #

    def replace_latest_orders(
        self,
        *,
        strategy: str,
        trade_date: date | str,
        items: Iterable[dict[str, Any]],
    ) -> int:
        """Replace the entire plan for ``(strategy, trade_date)``.

        Used by ``export_orders_for_date`` which owns the full plan
        for one date per strategy. The previous set is wiped before
        the new one is written so we never see a half-written plan.
        """
        td = _parse_date(trade_date)
        if td is None:
            return 0
        items_list = list(items)
        with self.cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM latest_orders WHERE strategy = %s AND trade_date = %s",
                (strategy, td),
            )
        if not items_list:
            return 0
        rows = []
        for i, item in enumerate(items_list):
            rows.append((
                strategy,
                td,
                i,
                str(item.get("symbol") or "").upper(),
                str(item.get("side") or "").upper(),
                _to_int(item.get("quantity")),
                _to_float(item.get("price"), 0.0),
                item.get("reason"),
                _to_int(item.get("sourceOrderId") or item.get("source_order_id")),
                _to_float(item.get("value")),
                _to_float(item.get("fee")),
                item.get("kind"),
                Json(item),
            ))
        with self.cursor(commit=True) as cur:
            execute_values(
                cur,
                """
                INSERT INTO latest_orders (
                    strategy, trade_date, plan_index, symbol, side,
                    quantity, price, reason, source_order_id, value, fee, kind, payload
                ) VALUES %s
                """,
                rows,
            )
        return len(rows)

    def read_latest_orders(
        self,
        *,
        strategy: str,
        trade_date: date | str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses, args = ["strategy = %s"], [strategy]
        if trade_date:
            clauses.append("trade_date = %s")
            args.append(_parse_date(trade_date))
        where = "WHERE " + " AND ".join(clauses)
        sql = (
            "SELECT trade_date, plan_index, symbol, side, quantity, price, "
            "reason, source_order_id, value, fee, kind "
            f"FROM latest_orders {where} ORDER BY trade_date DESC, plan_index LIMIT {int(limit)}"
        )
        with self.cursor() as cur:
            cur.execute(sql, args)
            return [
                {
                    "trade_date": r[0].isoformat(),
                    "plan_index": r[1],
                    "symbol": r[2],
                    "side": r[3],
                    "quantity": r[4],
                    "price": float(r[5]),
                    "reason": r[6],
                    "source_order_id": r[7],
                    "value": _opt_float(r[8]),
                    "fee": _opt_float(r[9]),
                    "kind": r[10],
                }
                for r in cur.fetchall()
            ]


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #

def _bar_to_row(symbol: str, bar: dict[str, Any]) -> tuple:
    """Coerce a single OHLCV bar into the (symbol, time, o, h, l, c, v)
    tuple shape used by both ``upsert_prices_dnse`` and
    ``copy_prices_dnse``.
    """
    t = _parse_date(bar.get("time") or bar.get("date"))
    if t is None:
        raise ValueError(f"bar missing date: {bar!r}")
    return (
        symbol,
        t,
        float(bar.get("open", 0) or 0),
        float(bar.get("high", 0) or 0),
        float(bar.get("low", 0) or 0),
        float(bar.get("close", 0) or 0),
        float(bar.get("volume", 0) or 0),
    )


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _to_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _opt_float(value: Any) -> float | None:
    """Convert DB NUMERIC to float; returns None for NULL values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
