-- 002_ops.sql — operational data: order ledger, daily reports, cycle
-- events, latest orders.
--
-- Loaded automatically by the postgres image on first start
-- (anything in /docker-entrypoint-initdb.d runs after the cluster
-- initialises, before the server accepts connections). Idempotent
-- (CREATE TABLE IF NOT EXISTS) so it's safe to re-run.

-- ---------------------------------------------------------------------------
-- order_ledger — every order the bot has prepared or submitted,
-- tracked through its full lifecycle.
--
-- Primary key is the same composite used by libs.order_ledger.ledger_key:
--   (run_id, source_order_id, symbol, side)
-- so a fresh ``INSERT ... ON CONFLICT DO UPDATE`` is a natural UPSERT.
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

-- ---------------------------------------------------------------------------
-- daily_reports — one row per (strategy, date) with the structured
-- snapshot from start-of-day and EOD. The positions / closed trades
-- JSONB columns let us answer P&L questions without joining tables.
--
-- Two reports per day per strategy: source='start_of_day' and source='eod'.
-- UPSERT on (strategy, date, source) is the natural key.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_reports (
    strategy        TEXT        NOT NULL,
    report_date     DATE        NOT NULL,
    source          TEXT        NOT NULL,   -- 'start_of_day' | 'eod' | 'heartbeat'
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

-- ---------------------------------------------------------------------------
-- cycle_events — audit trail of bot state transitions.
--
-- One row per event the bot emits (heartbeat, start-of-day, submit
-- cycle, eod, error, etc). ``payload`` is a JSONB blob carrying
-- any structured context.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cycle_events (
    event_id        BIGSERIAL   PRIMARY KEY,
    strategy        TEXT        NOT NULL,
    event_type      TEXT        NOT NULL,   -- 'start_of_day' | 'submit' | 'eod' | 'heartbeat' | 'error' | 'token_refresh' | ...
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

-- ---------------------------------------------------------------------------
-- latest_orders — daily plan. Mirrors data/live/latest_orders.{name}.json
-- so the bot's EOD plan is queryable in Postgres.
--
-- Each (strategy, trade_date) keeps one row per order in the plan.
-- Composite primary key includes ``symbol + side + index_in_plan``
-- so we don't collide on duplicate symbols.
-- ---------------------------------------------------------------------------
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
