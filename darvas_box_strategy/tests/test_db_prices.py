"""Tests for the Postgres-backed price loader.

The VPS mirrors market data into the Postgres ``prices_dnse`` table,
which is the intended source of truth (see vietnam_algo/db.py). The
JSON files under vietnam_algo/data/prices/ are the fallback.

These tests use a fake DbClient so they run without Postgres.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from darvas_box_strategy.darvas import db_prices
from darvas_box_strategy.darvas.data_loader import PriceBar


class FakeDbClient:
    """Stands in for vietnam_algo.libs.db.DbClient."""

    def __init__(self, bars_by_symbol=None, fail=False):
        self._bars = bars_by_symbol or {}
        self._fail = fail

    def read_prices_dnse(self, symbol):
        if self._fail:
            raise RuntimeError("postgres down")
        return list(self._bars.get(symbol.upper(), []))


def _row(t, o, h, l, c, v=1000):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


class LoadBarsTests(unittest.TestCase):
    def setUp(self):
        self._orig = db_prices._DB_CLIENT
        self._orig_failed = db_prices._DB_CLIENT_FAILED
        self.tmp = Path(tempfile.mkdtemp())
        db_prices._DB_CLIENT = None
        db_prices._DB_CLIENT_FAILED = False

    def tearDown(self):
        db_prices._DB_CLIENT = self._orig
        db_prices._DB_CLIENT_FAILED = self._orig_failed
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_json(self, ticker, rows):
        p = self.tmp / f"{ticker}.json"
        p.write_text(json.dumps({"items": rows}), encoding="utf-8")
        return p

    def test_prefers_postgres(self):
        db_prices._DB_CLIENT = FakeDbClient({
            "VNM": [_row("2026-10-29", 1, 2, 0.5, 1.5), _row("2026-10-30", 2, 3, 1, 2.5)],
        })
        self._write_json("VNM", [_row("2026-01-01", 9, 9, 9, 9)])

        bars = db_prices.load_bars("VNM", self.tmp)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1].time, "2026-10-30")
        self.assertEqual(bars[-1].close, 2.5)

    def test_falls_back_to_json_when_db_empty(self):
        db_prices._DB_CLIENT = FakeDbClient({})
        self._write_json("VNM", [_row("2026-10-30", 1, 2, 0.5, 1.5)])

        bars = db_prices.load_bars("VNM", self.tmp)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[-1].time, "2026-10-30")

    def test_falls_back_to_json_when_db_errors(self):
        db_prices._DB_CLIENT = FakeDbClient(fail=True)
        self._write_json("VNM", [_row("2026-10-30", 1, 2, 0.5, 1.5)])

        bars = db_prices.load_bars("VNM", self.tmp)

        self.assertEqual(len(bars), 1)

    def test_source_json_forces_json(self):
        db_prices._DB_CLIENT = FakeDbClient({
            "VNM": [_row("2026-10-30", 1, 2, 0.5, 1.5)],
        })
        self._write_json("VNM", [_row("2020-01-01", 9, 9, 9, 9)])

        bars = db_prices.load_bars("VNM", self.tmp, source="json")

        self.assertEqual(bars[-1].time, "2020-01-01")

    def test_source_db_does_not_fall_back(self):
        db_prices._DB_CLIENT = FakeDbClient({})
        self._write_json("VNM", [_row("2026-10-30", 1, 2, 0.5, 1.5)])

        bars = db_prices.load_bars("VNM", self.tmp, source="db")

        self.assertEqual(bars, [])

    def test_bars_are_sorted_by_time(self):
        db_prices._DB_CLIENT = FakeDbClient({
            "VNM": [_row("2026-10-30", 1, 2, 0.5, 1.5),
                    _row("2026-10-29", 1, 2, 0.5, 1.4)],
        })
        bars = db_prices.load_bars("VNM", self.tmp)
        self.assertEqual([b.time for b in bars], ["2026-10-29", "2026-10-30"])

    def test_db_values_survive_numeric_rounding(self):
        """Postgres stores NUMERIC(18,4), so prices round to 4dp.

        The DB and the JSON files must still agree to within that
        tolerance — otherwise a strategy that reads prices from either
        source computes different trades (which is exactly how the
        max drawdown went from -21% to -70%).
        """
        db_prices._DB_CLIENT = FakeDbClient({
            "VNM": [_row("2026-10-30", 55516.22260273973, 56279.33219178082,
                         55230.05650684931, 55707.0, 454070.0)],
        })
        bars = db_prices.load_bars("VNM", self.tmp)
        self.assertEqual(len(bars), 1)
        b = bars[0]
        self.assertAlmostEqual(b.open, 55516.2226, places=4)
        self.assertAlmostEqual(b.high, 56279.3322, places=4)
        self.assertAlmostEqual(b.close, 55707.0, places=4)
        # The rounding must be tiny relative to the price.
        self.assertLess(abs(b.high - 56279.33219178082), 1e-3)

    def test_malformed_db_rows_are_skipped(self):
        db_prices._DB_CLIENT = FakeDbClient({
            "VNM": [_row("2026-10-30", 1, 2, 0.5, 1.5),
                    {"time": "2026-10-29"},          # missing OHLC
                    {"open": 1, "high": 2, "low": 0, "close": 1}],  # no time
        })
        bars = db_prices.load_bars("VNM", self.tmp)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].time, "2026-10-30")

    def test_ticker_is_upper_cased(self):
        db_prices._DB_CLIENT = FakeDbClient({
            "VNM": [_row("2026-10-30", 1, 2, 0.5, 1.5)],
        })
        bars = db_prices.load_bars("vnm", self.tmp)
        self.assertEqual(len(bars), 1)


class DbUnavailableTests(unittest.TestCase):
    """The loader must degrade gracefully with no Postgres at all."""

    def setUp(self):
        self._orig = db_prices._DB_CLIENT
        self._orig_failed = db_prices._DB_CLIENT_FAILED
        db_prices._DB_CLIENT = None
        db_prices._DB_CLIENT_FAILED = False

    def tearDown(self):
        db_prices._DB_CLIENT = self._orig
        db_prices._DB_CLIENT_FAILED = self._orig_failed

    def test_db_client_returns_none_when_unavailable(self):
        db_prices._DB_CLIENT_FAILED = True
        self.assertIsNone(db_prices._db_client())

    def test_stats_report_unavailable(self):
        db_prices._DB_CLIENT_FAILED = True
        stats = db_prices.db_price_stats()
        self.assertFalse(stats["available"])
        self.assertEqual(stats["bars"], 0)


if __name__ == "__main__":
    unittest.main()
