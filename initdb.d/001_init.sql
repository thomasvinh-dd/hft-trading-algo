-- 001_init.sql — initial Postgres schema for the trading bot data layer.
--
-- Loaded automatically by the postgres image on first start
-- (anything in /docker-entrypoint-initdb.d runs after the cluster
-- initialises, before the server accepts connections).
--
-- Idempotent (CREATE TABLE IF NOT EXISTS) so it's safe to run again.

-- ---------------------------------------------------------------------------
-- symbols — universe of tradable tickers.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS symbols (
    symbol         TEXT        PRIMARY KEY,
    exchange       TEXT,
    company_name   TEXT,
    industry       TEXT,
    payload        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS symbols_exchange_idx ON symbols (exchange);

-- ---------------------------------------------------------------------------
-- prices_dnse — daily OHLCV bars from the DNSE openapi.
-- One row per (symbol, time). Partition-friendly — would partition by year
-- once we hit ~50M rows. For now a plain table with a covering index.
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- prices_fiin — daily OHLCV from fiin-trade / fiin.x.run (VietnamFinance).
-- Mirrors prices_dnse so consumers can pick which source they want.
-- Empty by default; populated by an opt-in sync command.
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- corporate_actions — Vietstock events (cash div, stock split, etc.).
-- Primary key on eventId so re-syncs are idempotent (UPSERT).
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- market_holidays — Vietnam stock market holidays.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_holidays (
    date         DATE         PRIMARY KEY,
    name         TEXT,
    holiday_type TEXT,
    source       TEXT,
    fetched_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- sync_state — last-sync timestamps per (source, kind). Lets the dual-write
-- adapter skip work that has already been done.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_state (
    source      TEXT NOT NULL,    -- 'dnse', 'fiin', 'vietstock', 'market_holiday'
    kind        TEXT NOT NULL,    -- 'symbols', 'prices', 'corporate_actions', ...
    symbol      TEXT,             -- nullable; symbols sync is global
    last_synced TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, kind, COALESCE(symbol, ''))
);
