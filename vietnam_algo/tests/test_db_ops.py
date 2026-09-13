"""Tests for the operational Postgres tables: order_ledger,
daily_reports, cycle_events, latest_orders.

These connect to a real Postgres when ``PG_TEST_DSN`` is set. They
use the same truncate-before-each-test pattern as ``test_db.py``.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from vietnam_algo.libs.db import DbClient, DbConfig


def _pg_test_dsn() -> str | None:
    return os.environ.get("PG_TEST_DSN")


@pytest.mark.skipif(_pg_test_dsn() is None, reason="PG_TEST_DSN not set")
class OrderLedgerTests(unittest.TestCase):
    """order_ledger: lifecycle tracking for every order."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = DbClient(DbConfig(dsn=_pg_test_dsn(), pool_min=1, pool_max=2))
        cls.db.init_schema_via_pool()

    def setUp(self) -> None:
        with self.db.cursor(commit=True) as cur:
            cur.execute("TRUNCATE TABLE order_ledger CASCADE")

    def _sample_row(self, **overrides):
        row = {
            "runId": "run-2026-09-13-1",
            "sourceOrderId": 1,
            "sourceDate": "2026-09-13",
            "symbol": "AAA",
            "side": "BUY",
            "quantity": 100,
            "price": 12345.67,
            "reason": "test",
            "status": "submitted",
            "dnseOrderId": "abc-123",
            "createdAt": "2026-09-13T09:16:00Z",
            "updatedAt": "2026-09-13T09:16:30Z",
            "lastDetail": {"foo": "bar"},
            "error": "",
        }
        row.update(overrides)
        return row

    def test_upsert_basic(self) -> None:
        n = self.db.upsert_order_ledger([self._sample_row()])
        self.assertEqual(n, 1)
        self.assertEqual(self.db.count_order_ledger(), 1)

    def test_upsert_status_transition(self) -> None:
        row = self._sample_row()
        self.db.upsert_order_ledger([row])

        # status moves submitted → filled
        row["status"] = "filled"
        row["updatedAt"] = "2026-09-13T15:30:00Z"
        self.db.upsert_order_ledger([row])

        rows = self.db.read_order_ledger(run_id=row["runId"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "filled")

    def test_replace_for_run(self) -> None:
        # First write inserts row for run-1
        self.db.upsert_order_ledger([self._sample_row(runId="run-1", symbol="AAA")])
        self.db.upsert_order_ledger([self._sample_row(runId="run-2", symbol="BBB")])
        self.assertEqual(self.db.count_order_ledger(), 2)

        # Replace run-1: only BBB should remain
        self.db.replace_order_ledger_for_run(
            "run-1",
            [self._sample_row(runId="run-1", symbol="CCC")],
        )
        self.assertEqual(self.db.count_order_ledger(), 2)
        rows = self.db.read_order_ledger(run_id="run-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "CCC")

    def test_read_filter_by_symbol_and_status(self) -> None:
        self.db.upsert_order_ledger([
            self._sample_row(symbol="AAA", status="filled"),
            self._sample_row(sourceOrderId=2, symbol="BBB", status="submitted"),
            self._sample_row(sourceOrderId=3, symbol="AAA", status="submitted"),
        ])
        aaa = self.db.read_order_ledger(symbol="AAA")
        self.assertEqual(len(aaa), 2)
        filled = self.db.read_order_ledger(status="filled")
        self.assertEqual(len(filled), 1)


@pytest.mark.skipif(_pg_test_dsn() is None, reason="PG_TEST_DSN not set")
class DailyReportsTests(unittest.TestCase):
    """daily_reports: one row per (strategy, date, source)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = DbClient(DbConfig(dsn=_pg_test_dsn(), pool_min=1, pool_max=2))
        cls.db.init_schema_via_pool()

    def setUp(self) -> None:
        with self.db.cursor(commit=True) as cur:
            cur.execute("TRUNCATE TABLE daily_reports CASCADE")

    def _report(self, **overrides):
        out = {
            "strategy": "momentum",
            "report_date": "2026-09-13",
            "source": "eod",
            "cash": 80_000_000,
            "nav": 90_000_000,
            "realized_pnl": -3_000_000,
            "unrealized_pnl": 0,
            "total_pnl": -3_000_000,
            "positions": {"PVT": {"quantity": 290, "avg_price": 21721.7}},
            "closed_trades": [],
            "planned_orders_count": 2,
            "applied_orders_count": 2,
            "clipped_orders_count": 0,
            "skipped_orders_count": 0,
        }
        out.update(overrides)
        return out

    def test_upsert_eod(self) -> None:
        n = self.db.upsert_daily_report(self._report())
        self.assertEqual(n, 1)
        rows = self.db.read_daily_reports("momentum")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "eod")

    def test_upsert_overwrites_with_same_key(self) -> None:
        self.db.upsert_daily_report(self._report(cash=80_000_000))
        self.db.upsert_daily_report(self._report(cash=85_000_000))
        rows = self.db.read_daily_reports("momentum")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cash"], 85_000_000.0)

    def test_two_sources_for_same_day(self) -> None:
        self.db.upsert_daily_report(self._report(source="start_of_day", cash=100_000_000))
        self.db.upsert_daily_report(self._report(source="eod", cash=80_000_000))
        rows = self.db.read_daily_reports("momentum")
        self.assertEqual(len(rows), 2)


@pytest.mark.skipif(_pg_test_dsn() is None, reason="PG_TEST_DSN not set")
class CycleEventsTests(unittest.TestCase):
    """cycle_events: audit trail."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = DbClient(DbConfig(dsn=_pg_test_dsn(), pool_min=1, pool_max=2))
        cls.db.init_schema_via_pool()

    def setUp(self) -> None:
        with self.db.cursor(commit=True) as cur:
            cur.execute("TRUNCATE TABLE cycle_events RESTART IDENTITY CASCADE")

    def test_insert_and_read(self) -> None:
        eid = self.db.insert_cycle_event(
            strategy="momentum", event_type="eod",
            local_date="2026-09-13",
            payload={"count": 2},
            message="EOD done",
        )
        self.assertGreater(eid, 0)
        rows = self.db.read_cycle_events(strategy="momentum")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "eod")
        self.assertEqual(rows[0]["payload"], {"count": 2})

    def test_filter_by_event_type(self) -> None:
        self.db.insert_cycle_event(strategy="momentum", event_type="heartbeat", local_date="2026-09-13")
        self.db.insert_cycle_event(strategy="momentum", event_type="eod", local_date="2026-09-13")
        rows = self.db.read_cycle_events(strategy="momentum", event_type="heartbeat")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "heartbeat")


@pytest.mark.skipif(_pg_test_dsn() is None, reason="PG_TEST_DSN not set")
class LatestOrdersTests(unittest.TestCase):
    """latest_orders: today's plan per strategy."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = DbClient(DbConfig(dsn=_pg_test_dsn(), pool_min=1, pool_max=2))
        cls.db.init_schema_via_pool()

    def setUp(self) -> None:
        with self.db.cursor(commit=True) as cur:
            cur.execute("TRUNCATE TABLE latest_orders CASCADE")

    def test_replace_latest_orders(self) -> None:
        items1 = [
            {"symbol": "AAA", "side": "BUY", "quantity": 100, "price": 1234.5,
             "reason": "top_rank", "sourceOrderId": 1},
            {"symbol": "BBB", "side": "BUY", "quantity": 200, "price": 6789.0,
             "reason": "top_rank", "sourceOrderId": 2},
        ]
        n = self.db.replace_latest_orders(
            strategy="momentum", trade_date="2026-09-13", items=items1,
        )
        self.assertEqual(n, 2)

        # Replace with a different plan
        items2 = [{"symbol": "CCC", "side": "BUY", "quantity": 50, "price": 100.0,
                  "reason": "top_rank", "sourceOrderId": 3}]
        self.db.replace_latest_orders(
            strategy="momentum", trade_date="2026-09-13", items=items2,
        )
        rows = self.db.read_latest_orders(strategy="momentum")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "CCC")

    def test_replace_empty(self) -> None:
        """Empty plan is a valid replacement — should not raise."""
        n = self.db.replace_latest_orders(
            strategy="momentum", trade_date="2026-09-13", items=[],
        )
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
