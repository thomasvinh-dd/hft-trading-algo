"""Tests for the DNSE 'invalid symbol' denylist.

Background (2026-09-23): ``sync_prices_incremental`` was wasting ~50%
of its quota re-fetching tickers DNSE doesn't carry (bonds, warrants,
delisted names, etc.) — the broad HOSE/UPCoM universe has ~1100 of
these. The first tight-loop outage also revealed that the sentinel
"age" math was inflated by these permanent failures, so the bot
gated on the wrong threshold.

This module covers the three contracts that fix the problem:

  1. ``sync_prices_incremental`` skips denylisted tickers without
     making an HTTP call.
  2. ``sync_prices_incremental`` adds new 400 'invalid symbol'
     failures to the on-disk denylist and persists the updated set.
  3. The denylist helpers load/save idempotently and round-trip
     cleanly.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.data import (
    INVALID_SYMBOL_DENYLIST_FILENAME,
    PriceBar,
    is_invalid_symbol_error,
    load_invalid_symbol_denylist,
    save_invalid_symbol_denylist,
    sync_prices_incremental,
)


def _make_bar(t: str, close: float) -> PriceBar:
    return PriceBar(
        time=t, open=close, high=close, low=close, close=close, volume=1000.0,
    )


def _denylist_path(tmp: Path) -> Path:
    return tmp / "live" / INVALID_SYMBOL_DENYLIST_FILENAME


class InvalidSymbolErrorDetectionTests(unittest.TestCase):
    """is_invalid_symbol_error() must match the exact DNSE 400 pattern."""

    def test_matches_dnse_400_invalid_symbol(self) -> None:
        self.assertTrue(
            is_invalid_symbol_error(
                'RuntimeError: DNSE GET /price/ohlc -> HTTP 400 '
                '{"status":400,"code":"BAD_REQUEST","message":"invalid symbol"}'
            )
        )

    def test_does_not_match_429(self) -> None:
        self.assertFalse(is_invalid_symbol_error("HTTP 429 rate limit"))

    def test_does_not_match_500(self) -> None:
        self.assertFalse(is_invalid_symbol_error("HTTP 500 internal"))

    def test_does_not_match_other_400(self) -> None:
        # 400 with a different message is NOT a permanent reject
        self.assertFalse(is_invalid_symbol_error("HTTP 400 bad request body"))

    def test_does_not_match_network_error(self) -> None:
        self.assertFalse(is_invalid_symbol_error("TimeoutError: timed out"))


class DenylistPersistenceTests(unittest.TestCase):
    """load/save round-trip + missing-file handling."""

    def setUp(self) -> None:
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="denylist_test_"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_missing_returns_empty(self) -> None:
        self.assertEqual(load_invalid_symbol_denylist(self.tmp), set())

    def test_save_and_load_round_trip(self) -> None:
        denylist = {"AAA", "BBB", "ccc"}  # mix of upper/lower for case test
        save_invalid_symbol_denylist(self.tmp, denylist)
        loaded = load_invalid_symbol_denylist(self.tmp)
        # load normalizes to upper-case
        self.assertEqual(loaded, {"AAA", "BBB", "CCC"})

    def test_load_corrupt_file_returns_empty(self) -> None:
        path = _denylist_path(self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json {{{")
        self.assertEqual(load_invalid_symbol_denylist(self.tmp), set())

    def test_save_creates_live_dir(self) -> None:
        # No live/ dir yet
        self.assertFalse((self.tmp / "live").exists())
        save_invalid_symbol_denylist(self.tmp, {"X"})
        self.assertTrue(_denylist_path(self.tmp).exists())

    def test_save_is_sorted_for_diff_friendliness(self) -> None:
        save_invalid_symbol_denylist(self.tmp, {"ZZZ", "AAA", "MMM"})
        payload = json.loads(_denylist_path(self.tmp).read_text())
        self.assertEqual(payload["symbols"], ["AAA", "MMM", "ZZZ"])
        self.assertEqual(payload["count"], 3)


class SyncPricesDenylistIntegrationTests(unittest.TestCase):
    """sync_prices_incremental honours + extends the denylist."""

    def setUp(self) -> None:
        import shutil
        if Path("/tmp/opencode/incremental_denylist_test").exists():
            shutil.rmtree("/tmp/opencode/incremental_denylist_test")
        self.tmp = Path("/tmp/opencode/incremental_denylist_test")
        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.tmp / "prices").mkdir(exist_ok=True)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_denylisted_symbol_is_skipped_no_http_call(self) -> None:
        """Pre-seed the denylist with AAA. AAA must NOT trigger fetch_price_bars."""
        save_invalid_symbol_denylist(self.tmp, {"AAA"})
        with patch("vietnam_algo.libs.data.fetch_price_bars") as mock_fetch:
            paths, errors = sync_prices_incremental(
                ["AAA"], self.tmp, sleep_seconds=0,
            )
        # No HTTP call, no error tuple, no price file written
        mock_fetch.assert_not_called()
        self.assertEqual(paths, [])
        self.assertEqual(errors, [])

    def test_400_invalid_symbol_is_added_to_denylist(self) -> None:
        """A 400 'invalid symbol' error persists to the denylist."""
        bad_err = (
            'RuntimeError: DNSE GET /price/ohlc -> HTTP 400 '
            '{"status":400,"code":"BAD_REQUEST","message":"invalid symbol"}'
        )
        with patch(
            "vietnam_algo.libs.data.fetch_price_bars",
            side_effect=RuntimeError(bad_err),
        ):
            paths, errors = sync_prices_incremental(
                ["BSR"], self.tmp, sleep_seconds=0,
            )
        self.assertEqual(paths, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], "BSR")
        # On-disk denylist now contains BSR
        denylist = load_invalid_symbol_denylist(self.tmp)
        self.assertIn("BSR", denylist)

    def test_non_400_error_not_added_to_denylist(self) -> None:
        """Generic RuntimeError must NOT pollute the denylist."""
        with patch(
            "vietnam_algo.libs.data.fetch_price_bars",
            side_effect=RuntimeError("network blip"),
        ):
            paths, errors = sync_prices_incremental(
                ["GAS"], self.tmp, sleep_seconds=0,
            )
        self.assertEqual(len(errors), 1)
        denylist = load_invalid_symbol_denylist(self.tmp)
        self.assertNotIn("GAS", denylist)
        self.assertEqual(denylist, set())

    def test_denylist_grows_across_runs(self) -> None:
        """Two runs with different 400s build up the denylist cumulatively."""
        bad_err = lambda sym: (
            'RuntimeError: DNSE GET /price/ohlc -> HTTP 400 '
            '{"status":400,"code":"BAD_REQUEST","message":"invalid symbol"}'
        )
        # Run 1: AAA fails 400
        with patch(
            "vietnam_algo.libs.data.fetch_price_bars",
            side_effect=RuntimeError(bad_err("AAA")),
        ):
            sync_prices_incremental(["AAA"], self.tmp, sleep_seconds=0)
        # Run 2: BBB fails 400
        with patch(
            "vietnam_algo.libs.data.fetch_price_bars",
            side_effect=RuntimeError(bad_err("BBB")),
        ):
            sync_prices_incremental(["BBB"], self.tmp, sleep_seconds=0)
        # Both should be on the denylist
        denylist = load_invalid_symbol_denylist(self.tmp)
        self.assertEqual(denylist, {"AAA", "BBB"})

    def test_summary_total_excludes_denylisted(self) -> None:
        """The ``total = len(symbols)`` reported in the post-run summary
        reflects the filtered universe (no denylisted), so the per-N
        progress prints and the failure-rate denominator in callers
        (common_sync) are computed against the DNSE-accepted subset."""
        save_invalid_symbol_denylist(self.tmp, {"DENY1", "DENY2"})
        captured = []

        real_print = print

        def cap(*args, **kwargs):
            captured.append(" ".join(str(a) for a in args))
            real_print(*args, **kwargs)

        # AAA is valid, succeeds. The two denylisted should not show in
        # the [N/total] progress prints.
        with patch("vietnam_algo.libs.data.fetch_price_bars",
                   return_value=[_make_bar("2026-09-04", 100)]):
            with patch("builtins.print", side_effect=cap):
                paths, errors = sync_prices_incremental(
                    ["AAA", "DENY1", "DENY2"], self.tmp, sleep_seconds=0,
                )
        self.assertEqual(errors, [])
        self.assertEqual(len(paths), 1)
        # Confirm the post-run summary line says total=1 (not 3)
        summary_lines = [l for l in captured if "sync_prices_incremental done" in l]
        self.assertEqual(len(summary_lines), 1)
        self.assertIn("across 1 symbols", summary_lines[0])
        self.assertIn("2 skipped (denylisted)", summary_lines[0])


if __name__ == "__main__":
    unittest.main()
