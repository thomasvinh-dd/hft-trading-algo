"""Embedded SQL used by libs/db.py.

We embed the schema in Python so a code-only checkout can run
``DbClient.init_schema_via_pool()`` without needing to ship the
``scripts/postgres/initdb.d/001_init.sql`` file alongside. The two
files are kept in sync manually; the SQL is short.
"""
from __future__ import annotations

INIT_SQL = r"""
CREATE TABLE IF NOT EXISTS symbols (
    symbol         TEXT        PRIMARY KEY,
    exchange       TEXT,
    company_name   TEXT,
    industry       TEXT,
    payload        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS symbols_exchange_idx ON symbols (exchange);

CREATE TABLE IF NOT EXISTS prices_dnse (
    symbol  TEXT        NOT NULL,
    time    DATE        NOT NULL,
    open    NUMERIC(18, 4) NOT NULL,
    high    NUMERIC(18, 4) NOT NULL,
    low     NUMERIC(18, 4) NOT NULL,
    close   NUMERIC(18, 4) NOT NULL,
    volume  NUMERIC(20, 4) NOT NULL,
    PRIMARY KEY (symbol, time)
);

CREATE INDEX IF NOT EXISTS prices_dnse_time_idx ON prices_dnse (time);
CREATE INDEX IF NOT EXISTS prices_dnse_symbol_time_idx ON prices_dnse (symbol, time DESC);

CREATE TABLE IF NOT EXISTS prices_fiin (
    symbol  TEXT        NOT NULL,
    time    DATE        NOT NULL,
    open    NUMERIC(18, 4) NOT NULL,
    high    NUMERIC(18, 4) NOT NULL,
    low     NUMERIC(18, 4) NOT NULL,
    close   NUMERIC(18, 4) NOT NULL,
    volume  NUMERIC(20, 4) NOT NULL,
    PRIMARY KEY (symbol, time)
);

CREATE INDEX IF NOT EXISTS prices_fiin_time_idx ON prices_fiin (time);
CREATE INDEX IF NOT EXISTS prices_fiin_symbol_time_idx ON prices_fiin (symbol, time DESC);

CREATE TABLE IF NOT EXISTS corporate_actions (
    event_id        BIGINT      PRIMARY KEY,
    symbol          TEXT        NOT NULL,
    ex_date         DATE,
    record_date     DATE,
    payment_date    DATE,
    name            TEXT,
    title           TEXT,
    exchange        TEXT,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS corporate_actions_symbol_idx ON corporate_actions (symbol);
CREATE INDEX IF NOT EXISTS corporate_actions_ex_date_idx ON corporate_actions (ex_date);
CREATE INDEX IF NOT EXISTS corporate_actions_record_date_idx ON corporate_actions (record_date);

CREATE TABLE IF NOT EXISTS market_holidays (
    date         DATE         PRIMARY KEY,
    name         TEXT,
    holiday_type TEXT,
    source       TEXT,
    fetched_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sync_state (
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    symbol      TEXT NOT NULL DEFAULT '',
    last_synced TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, kind, symbol)
);

-- ---------------------------------------------------------------------------
-- 002_ops — operational data: order ledger, daily reports,
-- cycle events, latest orders.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS order_ledger (
    run_id          TEXT        NOT NULL,
    source_order_id BIGINT      NOT NULL,
    source_date     DATE        NOT NULL,
    symbol          TEXT        NOT NULL,
    side            TEXT        NOT NULL,
    quantity        BIGINT      NOT NULL,
    price           NUMERIC(18, 4) NOT NULL,
    reason          TEXT,
    status          TEXT        NOT NULL,
    dnse_order_id   TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    last_detail     JSONB,
    error           TEXT,
    PRIMARY KEY (run_id, source_order_id, symbol, side)
);

CREATE INDEX IF NOT EXISTS order_ledger_symbol_idx ON order_ledger (symbol);
CREATE INDEX IF NOT EXISTS order_ledger_status_idx ON order_ledger (status);
CREATE INDEX IF NOT EXISTS order_ledger_updated_at_idx ON order_ledger (updated_at DESC);
CREATE INDEX IF NOT EXISTS order_ledger_source_date_idx ON order_ledger (source_date);

CREATE TABLE IF NOT EXISTS daily_reports (
    strategy        TEXT        NOT NULL,
    report_date     DATE        NOT NULL,
    source          TEXT        NOT NULL,
    initial_nav     NUMERIC(20, 4),
    cash            NUMERIC(20, 4) NOT NULL,
    cash_total      NUMERIC(20, 4),
    stock_value     NUMERIC(20, 4),
    nav             NUMERIC(20, 4) NOT NULL,
    realized_pnl    NUMERIC(20, 4) NOT NULL DEFAULT 0,
    unrealized_pnl  NUMERIC(20, 4) NOT NULL DEFAULT 0,
    total_pnl       NUMERIC(20, 4) NOT NULL DEFAULT 0,
    pnl_pct_vs_initial NUMERIC(10, 6),
    positions       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    closed_trades   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    planned_orders_count  BIGINT NOT NULL DEFAULT 0,
    applied_orders_count  BIGINT NOT NULL DEFAULT 0,
    clipped_orders_count  BIGINT NOT NULL DEFAULT 0,
    skipped_orders_count  BIGINT NOT NULL DEFAULT 0,
    raw_message     TEXT,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, report_date, source)
);

CREATE INDEX IF NOT EXISTS daily_reports_strategy_date_idx
    ON daily_reports (strategy, report_date DESC);
CREATE INDEX IF NOT EXISTS daily_reports_fetched_at_idx
    ON daily_reports (fetched_at DESC);

CREATE TABLE IF NOT EXISTS cycle_events (
    event_id        BIGSERIAL   PRIMARY KEY,
    strategy        TEXT        NOT NULL,
    event_type      TEXT        NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    local_date      DATE,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    message         TEXT
);

CREATE INDEX IF NOT EXISTS cycle_events_strategy_time_idx
    ON cycle_events (strategy, occurred_at DESC);
CREATE INDEX IF NOT EXISTS cycle_events_event_type_idx
    ON cycle_events (event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS cycle_events_local_date_idx
    ON cycle_events (local_date);

CREATE TABLE IF NOT EXISTS latest_orders (
    strategy        TEXT        NOT NULL,
    trade_date      DATE        NOT NULL,
    plan_index      BIGINT      NOT NULL,
    symbol          TEXT        NOT NULL,
    side            TEXT        NOT NULL,
    quantity        BIGINT      NOT NULL,
    price           NUMERIC(18, 4) NOT NULL,
    reason          TEXT,
    source_order_id BIGINT,
    value           NUMERIC(20, 4),
    fee             NUMERIC(20, 4),
    kind            TEXT,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, trade_date, plan_index)
);

CREATE INDEX IF NOT EXISTS latest_orders_strategy_date_idx
    ON latest_orders (strategy, trade_date DESC);
CREATE INDEX IF NOT EXISTS latest_orders_symbol_idx
    ON latest_orders (symbol);
"""
