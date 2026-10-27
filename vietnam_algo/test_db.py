"""Tests for libs/db.py and libs/db_sync.py.

These tests connect to a real Postgres instance when ``PG_TEST_DSN``
is set in the environment, otherwise they skip. To run locally::

    docker compose up -d
    export PG_TEST_DSN="host=127.0.0.1 port=5432 dbname=hft user=hft password=hft_dev_pw_change_me"
    python -m pytest vietnam_algo/tests/test_db.py -v

The tests share a single Postgres test database. ``setUp`` truncates
each table before every test, so order doesn't matter and there's no
need for separate schemas / drop-and-recreate logic.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from vietnam_algo.libs.db import DbClient, DbConfig, DbError


def _pg_test_dsn() -> str | None:
    """Return the PG_TEST_DSN env var, or None if not set.

    Lazy so callers can set the env var before pytest collects the
    module (e.g., ``PG_TEST_DSN=... pytest``).
    """
    return os.environ.get("PG_TEST_DSN")


@pytest.mark.skipif(_pg_test_dsn() is None, reason="PG_TEST_DSN not set")
class PostgresIntegrationTests(unittest.TestCase):
    """Hits a real Postgres. Each test starts with empty tables."""

    @classmethod
    def setUpClass(cls) -> None:
        dsn = _pg_test_dsn()
        if dsn is None:
            raise unittest.SkipTest("PG_TEST_DSN not set")
        cls.config = DbConfig(
            dsn=dsn,
            pool_min=1,
            pool_max=2,
        )
        cls.db = DbClient(cls.config)
        # Make sure the schema exists.
        cls.db.init_schema_via_pool()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def setUp(self) -> None:
        with self.db.cursor(commit=True) as cur:
            cur.execute("TRUNCATE TABLE symbols RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE TABLE prices_dnse CASCADE")
            cur.execute("TRUNCATE TABLE corporate_actions CASCADE")
            cur.execute("TRUNCATE TABLE market_holidays CASCADE")

    # ------------------------------------------------------------------ #
    # symbols
    # ------------------------------------------------------------------ #

    def test_upsert_symbols_basic(self) -> None:
        n = self.db.upsert_symbols([
            {"symbol": "AAA", "exchange": "HOSE", "companyName": "AAA Co"},
            {"symbol": "BBB", "exchange": "HNX", "companyName": "BBB Co"},
        ])
        self.assertEqual(n, 2)
        self.assertEqual(self.db.count_symbols(), 2)

    def test_upsert_symbols_updates_existing(self) -> None:
        self.db.upsert_symbols([{"symbol": "AAA", "companyName": "Old"}])
        self.db.upsert_symbols([{"symbol": "AAA", "companyName": "New"}])
        self.assertEqual(self.db.count_symbols(), 1)
        with self.db.cursor() as cur:
            cur.execute("SELECT company_name FROM symbols WHERE symbol = 'AAA'")
            self.assertEqual(cur.fetchone()[0], "New")

    # ------------------------------------------------------------------ #
    # prices
    # ------------------------------------------------------------------ #

    def test_upsert_prices_dnse_round_trip(self) -> None:
        bars = [
            {"time": "2024-01-02", "open": 100, "high": 110, "low": 95, "close": 105, "volume": 1000},
            {"time": "2024-01-03", "open": 105, "high": 115, "low": 100, "close": 112, "volume": 1500},
        ]
        self.db.upsert_prices_dnse("AAA", bars)
        rows = self.db.read_prices_dnse("AAA")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["time"], "2024-01-02")
        self.assertEqual(rows[1]["close"], 112.0)

    def test_copy_prices_dnse_fast_path(self) -> None:
        bars = [
            {
                "time": f"2024-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}",
                "open": i, "high": i + 1, "low": i - 1, "close": i, "volume": 100,
            }
            for i in range(50)
        ]
        self.db.copy_prices_dnse("BBB", bars)
        self.assertEqual(self.db.count_prices_dnse("BBB"), 50)

    def test_upsert_prices_dnse_is_idempotent(self) -> None:
        bars = [{"time": "2024-01-02", "open": 100, "high": 110, "low": 95, "close": 105, "volume": 1000}]
        self.db.upsert_prices_dnse("AAA", bars)
        self.db.upsert_prices_dnse("AAA", bars)
        self.assertEqual(self.db.count_prices_dnse("AAA"), 1)
        # Update via second upsert with different close
        bars2 = [{"time": "2024-01-02", "open": 100, "high": 110, "low": 95, "close": 999, "volume": 1000}]
        self.db.upsert_prices_dnse("AAA", bars2)
        rows = self.db.read_prices_dnse("AAA")
        self.assertEqual(rows[0]["close"], 999.0)

    def test_read_prices_dnse_with_since_filter(self) -> None:
        bars = [
            {"time": "2024-01-02", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100},
            {"time": "2024-01-05", "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 100},
            {"time": "2024-01-10", "open": 3, "high": 4, "low": 2.5, "close": 3.5, "volume": 100},
        ]
        self.db.upsert_prices_dnse("AAA", bars)
        rows = self.db.read_prices_dnse("AAA", since=date(2024, 1, 5))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["time"] for r in rows], ["2024-01-05", "2024-01-10"])

    # ------------------------------------------------------------------ #
    # corporate actions
    # ------------------------------------------------------------------ #

    def test_upsert_corporate_actions(self) -> None:
        events = [
            {
                "eventId": 1, "symbol": "AAA", "exDate": "2024-02-15",
                "recordDate": "2024-02-14", "paymentDate": "2024-03-01",
                "name": "Cash div", "title": "AAA pays 1000",
            },
            {
                "eventId": 2, "symbol": "BBB", "exDate": "2024-04-20",
                "name": "Stock split",
            },
        ]
        n = self.db.upsert_corporate_actions(events)
        self.assertEqual(n, 2)
        self.assertEqual(self.db.count_corporate_actions(), 2)

    def test_read_corporate_actions_filters(self) -> None:
        self.db.upsert_corporate_actions([
            {"eventId": 1, "symbol": "AAA", "exDate": "2024-02-15"},
            {"eventId": 2, "symbol": "BBB", "exDate": "2024-03-15"},
            {"eventId": 3, "symbol": "BBB", "exDate": "2024-04-15"},
        ])
        only_bbb = self.db.read_corporate_actions(symbol="BBB")
        self.assertEqual(len(only_bbb), 2)
        self.assertTrue(all(e["symbol"] == "BBB" for e in only_bbb))

    # ------------------------------------------------------------------ #
    # market holidays
    # ------------------------------------------------------------------ #

    def test_market_holidays_round_trip(self) -> None:
        self.db.upsert_market_holidays([
            {"date": "2026-01-01", "name": "New Year"},
            {"date": "2026-04-30", "name": "Reunification Day"},
        ])
        days = self.db.read_market_holidays()
        self.assertEqual(days, {"2026-01-01", "2026-04-30"})

    def test_market_holidays_idempotent(self) -> None:
        self.db.upsert_market_holidays([{"date": "2026-01-01", "name": "A"}])
        self.db.upsert_market_holidays([{"date": "2026-01-01", "name": "B"}])
        days = self.db.read_market_holidays()
        self.assertEqual(days, {"2026-01-01"})


@pytest.mark.skipif(_pg_test_dsn() is None, reason="PG_TEST_DSN not set")
class DbSyncMirrorTests(unittest.TestCase):
    """Verify the db_sync helpers don't crash on a DB outage."""

    def test_mirror_symbols_swallows_failure(self) -> None:
        """On a Postgres outage, mirror_* helpers return None and log."""
        bad_db = DbClient(DbConfig(
            dsn="host=127.0.0.1 port=1 dbname=nope user=nope password=nope",
            pool_min=0, pool_max=1,
        ))
        from vietnam_algo.libs.db_sync import mirror_symbols
        result = mirror_symbols(bad_db, [{"symbol": "X"}])
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
