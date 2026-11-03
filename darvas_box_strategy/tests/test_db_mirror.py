"""Regression tests for the Postgres price mirror in common_sync.

Incident (2026-11-03): the Darvas Box dashboard's max drawdown went
from -21.34% to -69.95% and the win rate from 71.5% to 55.3% the
moment the strategy started reading prices from Postgres.

Root cause: ``_mirror_prices_to_db`` iterated ``zip(symbols, paths)``.
``sync_prices_incremental`` returns ONLY the files it changed, in its
own iteration order — so zipping the two lists paired every symbol
with a DIFFERENT symbol's price file. Ticker A's prices were written
under ticker B (MWG showed 8,300 VND when it trades at 72,300).

These tests pin the invariant: the ticker written to the DB must come
from the price file's own name.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class MirrorPriceTickerPairingTests(unittest.TestCase):
    """The mirrored ticker must match the file being read."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mirrored = {}     # symbol -> bars
        self.reads = []

        # Fake DbClient whose upsert records what it was given.
        mirrored = self.mirrored

        class FakeDb:
            def upsert_prices_dnse(self, symbol, bars):
                mirrored[symbol] = bars
                return len(bars)

        self.fake_db = FakeDb()

        # Patch the imports the mirror pulls in.
        self.db_mod = mock.MagicMock()
        self.db_mod.DbClient.from_default.return_value = self.fake_db
        self.sync_mod = mock.MagicMock()
        self.sync_mod.mirror_prices = lambda db, symbol, bars: (
            db.upsert_prices_dnse(symbol, bars)
        )

        self._orig = {}
        import vietnam_algo.libs.db as real_db
        import vietnam_algo.libs.db_sync as real_db_sync
        self._orig["db"] = real_db.DbClient
        self._orig["db_sync"] = real_db_sync.mirror_prices
        real_db.DbClient = self.db_mod.DbClient
        real_db_sync.mirror_prices = self.sync_mod.mirror_prices
        self.real_db = real_db
        self.real_db_sync = real_db_sync

    def tearDown(self):
        import shutil
        self.real_db.DbClient = self._orig["db"]
        self.real_db_sync.mirror_prices = self._orig["db_sync"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, ticker, rows):
        p = self.tmp / "prices" / f"{ticker}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"items": rows}), encoding="utf-8")
        return p

    def _mirror(self, symbols, paths):
        from vietnam_algo.jobs.common_sync import _mirror_prices_to_db
        return _mirror_prices_to_db(symbols, self.tmp, paths)

    def test_ticker_comes_from_the_file_not_the_zip_position(self):
        """THE regression test.

        ``symbols`` is the full universe in universe order; ``paths``
        holds only the changed files, in a different order. Zipping
        them cross-wires the data.
        """
        a = self._write("AAA", [{"time": "2026-01-01", "open": 1, "high": 1,
                                 "low": 1, "close": 111, "volume": 1}])
        b = self._write("BBB", [{"time": "2026-01-01", "open": 2, "high": 2,
                                 "low": 2, "close": 222, "volume": 2}])

        # Universe order: AAA, BBB, CCC... but only BBB changed.
        symbols = ["AAA", "BBB", "CCC", "DDD"]
        paths = [b]          # only BBB was rewritten

        self._mirror(symbols, paths)

        # The DB must have BBB's prices under BBB — not under AAA.
        self.assertIn("BBB", self.mirrored)
        self.assertNotIn("AAA", self.mirrored,
                         "AAA's row was written from BBB's price file")
        self.assertEqual(
            self.mirrored["BBB"][0]["close"], 222,
            "BBB's row got the wrong price file's data",
        )

    def test_multiple_changed_files_keep_their_own_tickers(self):
        a = self._write("AAA", [{"time": "2026-01-01", "open": 1, "high": 1,
                                 "low": 1, "close": 111, "volume": 1}])
        b = self._write("BBB", [{"time": "2026-01-01", "open": 2, "high": 2,
                                 "low": 2, "close": 222, "volume": 2}])
        c = self._write("CCC", [{"time": "2026-01-01", "open": 3, "high": 3,
                                 "low": 3, "close": 333, "volume": 3}])

        symbols = ["AAA", "BBB", "CCC"]
        paths = [c, a, b]      # changed, but in a shuffled order

        self._mirror(symbols, paths)

        self.assertEqual(self.mirrored["AAA"][0]["close"], 111)
        self.assertEqual(self.mirrored["BBB"][0]["close"], 222)
        self.assertEqual(self.mirrored["CCC"][0]["close"], 333)

    def test_empty_paths_is_a_noop(self):
        self.assertEqual(self._mirror(["AAA"], []), 0)
        self.assertEqual(self.mirrored, {})

    def test_ticker_is_upper_cased(self):
        p = self.tmp / "prices" / "aapl.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"items": [
            {"time": "2026-01-01", "open": 1, "high": 1, "low": 1,
             "close": 1, "volume": 1}]}), encoding="utf-8")
        self._mirror(["AAPL"], [p])
        self.assertIn("AAPL", self.mirrored)

    def test_env_flag_disables_the_mirror(self):
        import os
        os.environ["HFT_MIRROR_TO_DB"] = "0"
        try:
            p = self._write("AAA", [{"time": "2026-01-01", "open": 1,
                                     "high": 1, "low": 1, "close": 1,
                                     "volume": 1}])
            self.assertEqual(self._mirror(["AAA"], [p]), 0)
            self.assertEqual(self.mirrored, {})
        finally:
            os.environ.pop("HFT_MIRROR_TO_DB", None)


class MirrorSymbolTests(unittest.TestCase):
    """The symbols mirror reads symbols.json — no zip involved."""

    def test_symbols_mirror_reads_symbols_json(self):
        import shutil
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "symbols.json").write_text(json.dumps({"items": [
                {"ticker": "AAA", "companyName": "A"},
                {"ticker": "BBB", "companyName": "B"},
            ]}), encoding="utf-8")

            seen = {}

            class FakeDb:
                def upsert_symbols(self, items):
                    seen["items"] = list(items)
                    return len(items)

            import vietnam_algo.libs.db as real_db
            import vietnam_algo.libs.db_sync as real_db_sync
            orig_db = real_db.DbClient
            orig_mirror = real_db_sync.mirror_symbols
            real_db.DbClient = mock.MagicMock()
            real_db.DbClient.from_default.return_value = FakeDb()
            real_db_sync.mirror_symbols = lambda db, items: db.upsert_symbols(items)
            try:
                from vietnam_algo.jobs.common_sync import _mirror_symbols_to_db
                n = _mirror_symbols_to_db(tmp)
            finally:
                real_db.DbClient = orig_db
                real_db_sync.mirror_symbols = orig_mirror

            self.assertEqual(n, 2)
            self.assertEqual(
                [i["ticker"] for i in seen["items"]], ["AAA", "BBB"],
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
