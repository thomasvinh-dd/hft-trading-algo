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
"""
