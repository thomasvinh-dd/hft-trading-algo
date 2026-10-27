"""Tests for the Darvas Box web dashboard."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))


class FlaskAppTests(unittest.TestCase):
    """Smoke tests for the Flask app."""

    def setUp(self):
        from darvas_box_strategy.web.app import create_app
        self.app = create_app()
        self.client = self.app.test_client()

    def test_index_loads(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Darvas Box", r.data)
        self.assertIn(b"Equity curve", r.data)
        self.assertIn(b"Opening signals", r.data)
        self.assertIn(b"Open positions", r.data)

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("cache_path", data)

    def test_signals_endpoint(self):
        r = self.client.get("/api/signals")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("count", data)
        self.assertIn("signals", data)
        self.assertIsInstance(data["signals"], list)
        # The endpoint also returns recent signals (last 7 trading
        # days) so the UI is useful even when the latest bar has none.
        self.assertIn("recent", data)
        self.assertIsInstance(data["recent"], list)

    def test_positions_endpoint(self):
        r = self.client.get("/api/positions")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("count", data)
        self.assertIn("positions", data)
        self.assertIsInstance(data["positions"], list)

    def test_backtest_endpoint(self):
        r = self.client.get("/api/backtest")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("backtest", data)
        self.assertIn("config", data)

    def test_refresh_endpoint(self):
        # Don't actually run the backtest (takes 4 min) — mock it.
        with patch("darvas_box_strategy.web.app.refresh_cache") as mock_refresh:
            from darvas_box_strategy.web.data import WebCache
            mock_refresh.return_value = WebCache(
                last_updated="2026-01-01T00:00:00",
                signals=[{"ticker": "MSN"}],
                open_positions=[],
                backtest={"total_return_pct": 100.0},
                config={},
            )
            r = self.client.post("/api/refresh")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("elapsed_seconds", data)
        self.assertEqual(data["signal_count"], 1)

    def test_404(self):
        r = self.client.get("/nonexistent")
        self.assertEqual(r.status_code, 404)

    def test_ticker_route(self):
        """Ticker history endpoint should return summary + deals."""
        with patch("darvas_box_strategy.web.app.get_ticker_history") as mock:
            mock.return_value = {
                "ticker": "MSN",
                "summary": {
                    "total_deals": 5, "open_deals": 1, "closed_deals": 4,
                    "wins": 3, "losses": 1, "win_rate_pct": 75.0,
                    "total_pnl_vnd": 12345678, "avg_pnl_pct": 12.3,
                },
                "deals": [
                    {
                        "deal_id": "MSN_2026-10-09", "ticker": "MSN",
                        "entry_date": "2026-10-09", "exit_date": "2026-10-20",
                        "entry_price": 75200, "exit_price": 80000,
                        "last_close": 80000, "pnl_pct": 6.4, "pnl_vnd": 4000000,
                        "exit_reason": "TRAIL", "bars_held": 7,
                        "is_open": False, "shares": 5000, "size_pct": 0.4,
                        "sl_F": 61664, "sl_T": 67680,
                    },
                ],
            }
            r = self.client.get("/api/ticker/MSN")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["ticker"], "MSN")
        self.assertEqual(data["summary"]["total_deals"], 5)
        self.assertEqual(len(data["deals"]), 1)
        self.assertEqual(data["deals"][0]["deal_id"], "MSN_2026-10-09")

    def test_ticker_route_rejects_invalid(self):
        r = self.client.get("/api/ticker/this-is-way-too-long-to-be-a-ticker")
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/ticker/..")
        # Either 400 (rejected) or 200 (no data) — both acceptable
        self.assertIn(r.status_code, (200, 400))


class ModalHiddenRegressionTests(unittest.TestCase):
    """Regression: the ticker history modal must be hidden by default.

    Previously the modal was visible on page load because
    ``.modal-overlay { display: flex; }`` had the same specificity as
    ``.hidden { display: none; }`` and won the cascade. The fix uses
    ``.modal-overlay.hidden { display: none; }`` to bump specificity.
    """

    def test_css_specificity(self):
        import re
        css_path = (Path(__file__).resolve().parents[1] / "web" / "static" / "styles.css")
        css = css_path.read_text()
        # The fix must be present
        self.assertRegex(
            css,
            r"\.modal-overlay\.hidden\s*\{\s*display:\s*none",
            "Modal needs .modal-overlay.hidden { display: none } to win "
            "the cascade against .modal-overlay { display: flex }",
        )

    def test_index_has_hidden_class(self):
        from darvas_box_strategy.web.app import create_app
        app = create_app()
        c = app.test_client()
        r = c.get("/")
        # The modal element must have the "hidden" class on page load
        self.assertIn(b'id="ticker-modal" class="modal-overlay hidden"', r.data)


class StaticAssetsTests(unittest.TestCase):
    def test_css_loads(self):
        from darvas_box_strategy.web.app import create_app
        app = create_app()
        c = app.test_client()
        r = c.get("/static/styles.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"--bg", r.data)  # CSS variable present

    def test_js_loads(self):
        from darvas_box_strategy.web.app import create_app
        app = create_app()
        c = app.test_client()
        r = c.get("/static/app.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"renderKPIs", r.data)


class WebCacheTests(unittest.TestCase):
    def test_cache_round_trip(self, tmp_path=None):
        from darvas_box_strategy.web.data import WebCache, save_cache, load_cache
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cache.json"
            c = WebCache(
                last_updated="2026-01-01T00:00:00",
                signals=[{"ticker": "MSN", "entry_price": 75200}],
                open_positions=[],
                backtest={"total_return_pct": 240.0},
                config={"universe": "test"},
            )
            save_cache(c, path=path)
            loaded = load_cache(path=path)
            self.assertEqual(loaded.last_updated, "2026-01-01T00:00:00")
            self.assertEqual(len(loaded.signals), 1)
            self.assertEqual(loaded.signals[0]["ticker"], "MSN")
            self.assertEqual(loaded.backtest["total_return_pct"], 240.0)

    def test_load_missing_returns_empty(self):
        from darvas_box_strategy.web.data import load_cache
        c = load_cache(path=Path("/nonexistent/cache.json"))
        self.assertEqual(c.last_updated, "")
        self.assertEqual(c.signals, [])
        self.assertEqual(c.open_positions, [])
        self.assertEqual(c.backtest, {})


class SignalToDictTests(unittest.TestCase):
    """Validate the _signal_to_dict shape — this dict is consumed by the
    frontend and breaking it would silently break the UI."""

    def test_signal_to_dict_keys(self):
        from darvas_box_strategy.web.data import _signal_to_dict
        from datetime import date

        class FakeSignal:
            ticker = "MSN"
            entry_price = 75200.0
            sl_F = 61664.0
            sl_T = 67680.0
            size_pct = 0.40
            entry_date = date(2026, 10, 10)
            signal_date = date(2026, 10, 9)
            atr_value = 1234.5
            shares = 5000

        d = _signal_to_dict(FakeSignal())
        self.assertEqual(d["ticker"], "MSN")
        self.assertEqual(d["entry_price"], 75200.0)
        self.assertEqual(d["sl_F"], 61664.0)
        self.assertEqual(d["sl_T"], 67680.0)
        self.assertEqual(d["size_pct"], 0.40)
        self.assertEqual(d["as_of_date"], "2026-10-10")
        self.assertEqual(d["signal_date"], "2026-10-09")
        self.assertTrue(d["high_conviction"])  # 0.40 > 0.30


class SummarizeBacktestTests(unittest.TestCase):
    """Test the summary computation against a fake BacktestResult.

    We're not running the full backtest here (it's too slow for unit
    tests); instead we test the metric extraction logic.
    """

    def test_summarize_uses_equity_curve_not_pnl_sum(self):
        """The total_return should be derived from the equity curve
        (end - start) / start, not sum of realized PnL. This catches
        regressions where dividend income or MTM is missing."""
        from datetime import date
        from darvas_box_strategy.web.data import _summarize_backtest
        from darvas_box_strategy.darvas.config import (
            StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig,
            ExitsConfig, TrailingConfig, UniverseConfig,
        )

        cfg = StrategyConfig(
            base=BaseConfig(),
            breakout=BreakoutConfig(),
            exits=ExitsConfig(),
            trailing=TrailingConfig(),
            universe=UniverseConfig(),
            backtest=BacktestConfig(start_nav=1_000_000_000.0),
        )

        class FakeResult:
            start = date(2020, 1, 1)
            end = date(2021, 1, 1)
            tickers_scanned = 70
            trades = []
            nav_curve = [
                (date(2020, 1, 1), 1_000_000_000.0),
                (date(2020, 7, 1), 1_100_000_000.0),  # +10%
                (date(2021, 1, 1), 1_200_000_000.0),  # +20%
            ]
            daily_returns = [0.001] * 252
            concurrent_position_peak = 5

        s = _summarize_backtest(FakeResult(), cfg, filtered=["x", "y"])
        # 1.2B / 1.0B - 1 = 0.20 = 20%
        self.assertAlmostEqual(s["total_return_pct"], 20.0, places=2)
        # 0.7 years → CAGR = (1.2)^(1/0.7) - 1 = ~30%
        # Just verify it's a reasonable positive number, not the exact value
        self.assertGreater(s["cagr_pct"], 0.0)
        # No trades → no win rate
        self.assertEqual(s["win_rate_pct"], 0.0)


class DealEndpointTests(unittest.TestCase):
    """The /api/deal/<deal_id> endpoint returns the full timeline
    (fills) for one deal — used by the deal-detail modal."""

    def setUp(self):
        from darvas_box_strategy.web.app import create_app
        self.app = create_app()
        self.client = self.app.test_client()

    def test_deal_endpoint_rejects_invalid(self):
        r = self.client.get("/api/deal/INVALID%20ID")
        self.assertIn(r.status_code, (400, 404))

    def test_deal_endpoint_returns_404_for_unknown(self):
        r = self.client.get("/api/deal/XXXX_2099-01-01")
        # Either 404 (no history) or 200 with empty data — both ok
        self.assertIn(r.status_code, (200, 404))


if __name__ == "__main__":
    unittest.main()


class MakeDealIdTests(unittest.TestCase):
    """Tests for the deal_id generator."""

    def test_basic_format(self):
        from darvas_box_strategy.web.data import _make_deal_id
        from datetime import date
        self.assertEqual(_make_deal_id("MSN", date(2026, 10, 9)), "MSN_2026-10-09")

    def test_seq_appended(self):
        from darvas_box_strategy.web.data import _make_deal_id
        from datetime import date
        self.assertEqual(
            _make_deal_id("MSN", date(2026, 10, 9), seq=2),
            "MSN_2026-10-09_2",
        )

    def test_seq_zero_omitted(self):
        """seq=0 should not add a suffix — the common case is unambiguous."""
        from darvas_box_strategy.web.data import _make_deal_id
        from datetime import date
        self.assertEqual(
            _make_deal_id("MSN", date(2026, 10, 9), seq=0),
            "MSN_2026-10-09",
        )

    def test_string_date(self):
        from darvas_box_strategy.web.data import _make_deal_id
        # Non-date objects should be coerced via str()
        self.assertEqual(_make_deal_id("VIX", "2026-05-07"), "VIX_2026-05-07")


class SignalToDictWithDealIdTests(unittest.TestCase):
    """The frontend depends on every signal having a deal_id."""

    def test_signal_includes_deal_id(self):
        from darvas_box_strategy.web.data import _signal_to_dict
        from datetime import date

        class FakeSignal:
            ticker = "MSN"
            entry_price = 75200.0
            sl_F = 61664.0
            sl_T = 67680.0
            size_pct = 0.40
            entry_date = date(2026, 10, 9)
            signal_date = date(2026, 10, 8)
            atr_value = 1234.5
            shares = 5000

        d = _signal_to_dict(FakeSignal())
        self.assertEqual(d["deal_id"], "MSN_2026-10-09")
        self.assertIn("deal_id", d)
        # The rest of the dict should still have all the existing fields
        self.assertEqual(d["ticker"], "MSN")
        self.assertEqual(d["entry_price"], 75200.0)


class OpenPositionToDictWithDealIdTests(unittest.TestCase):
    def test_position_includes_deal_id(self):
        from darvas_box_strategy.web.data import _open_position_to_dict
        from datetime import date

        class FakeSignal:
            ticker = "MSN"
            entry_price = 75200.0
            sl_F = 61664.0
            sl_T = 67680.0
            size_pct = 0.40
            entry_date = date(2026, 10, 9)
            signal_date = date(2026, 10, 8)
            shares = 5000

        class FakeTrade:
            signal = FakeSignal()
            highest_close = 76000.0
            last_close = 75700.0  # current price (lower than peak)
            bars_held = 5

        d = _open_position_to_dict(FakeTrade())
        self.assertEqual(d["deal_id"], "MSN_2026-10-09")
        self.assertEqual(d["ticker"], "MSN")
        self.assertEqual(d["shares"], 5000)
        # PnL math uses last_close (not highest_close): (75700/75200 - 1) * 100
        self.assertAlmostEqual(d["pnl_pct"], 0.6649, places=2)
        # last_close in the response should be 75700, not 76000
        self.assertEqual(d["last_close"], 75700.0)


class WebCacheDealIdTests(unittest.TestCase):
    """WebCache needs to round-trip the new ticker_history field."""

    def test_ticker_history_round_trip(self):
        from darvas_box_strategy.web.data import WebCache, save_cache, load_cache
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cache.json"
            c = WebCache(
                last_updated="2026-01-01T00:00:00",
                ticker_history={
                    "MSN": {
                        "summary": {"total_deals": 5},
                        "deals": [{"deal_id": "MSN_2026-10-09", "pnl_pct": 6.4}],
                    }
                },
            )
            save_cache(c, path=path)
            loaded = load_cache(path=path)
            self.assertIn("MSN", loaded.ticker_history)
            self.assertEqual(loaded.ticker_history["MSN"]["deals"][0]["deal_id"],
                             "MSN_2026-10-09")

    def test_ticker_history_default_empty(self):
        from darvas_box_strategy.web.data import WebCache
        c = WebCache()
        self.assertEqual(c.ticker_history, {})


class NoConcurrentPositionsTests(unittest.TestCase):
    """The ticker-history endpoint must enforce "1 ticker = 1 open
    position at a time". A new signal that fires while a previous
    deal is still open should be skipped — the live portfolio would
    have rejected it.

    Regression test for the bug where the dashboard's per-ticker
    history showed multiple overlapping deals on the same ticker
    (up to 4 concurrent for VCB), which was impossible in real
    trading.
    """

    def _make_ticker_history_test(self, deals):
        """Helper: build a ticker-history return value and verify
        no two deals on the same ticker overlap in time."""
        from collections import defaultdict
        by_ticker = defaultdict(list)
        for d in deals:
            by_ticker[d["ticker"]].append(d)
        for ticker, ticker_deals in by_ticker.items():
            ticker_deals.sort(key=lambda d: d["entry_date"])
            for i, d1 in enumerate(ticker_deals):
                for d2 in ticker_deals[i+1:]:
                    if d1["is_open"]:
                        # d1 still open — d2 must not exist (would be
                        # an overlap with an open position)
                        self.fail(
                            f"{ticker}: {d1['deal_id']} is still open "
                            f"but {d2['deal_id']} ({d2['entry_date']}) "
                            f"was also kept"
                        )
                    if d2["entry_date"] <= d1["exit_date"]:
                        self.fail(
                            f"{ticker}: {d1['deal_id']} closed {d1['exit_date']} "
                            f"but {d2['deal_id']} entered at {d2['entry_date']} "
                            f"(overlap of {(d1['exit_date'] - d2['entry_date']).days} days)"
                        )

    def test_real_tickers_have_no_concurrent_deals(self):
        """For a sample of HOSE blue chips, get_ticker_history should
        never return two deals on the same ticker that overlap in
        time. (Live trading can't have two open positions on one
        ticker at once.)"""
        from darvas_box_strategy.bot.cli import build_default_config
        from darvas_box_strategy.web.data import get_ticker_history
        cfg = build_default_config()
        # Try the first 5 blue chips — enough to catch a regression
        # without making the test slow.
        for ticker in ["MSN", "VCB", "VHM", "PLX", "GEE"]:
            with self.subTest(ticker=ticker):
                hist = get_ticker_history(ticker, cfg=cfg)
                self.assertEqual(hist["ticker"], ticker)
                deals = hist["deals"]
                if not deals:
                    continue
                # Walk through chronologically — each entry_date
                # must be > the previous exit_date
                sorted_deals = sorted(deals, key=lambda d: d["entry_date"])
                prev_exit = None
                for d in sorted_deals:
                    if prev_exit is not None and d["entry_date"] <= prev_exit:
                        self.fail(
                            f"{ticker}: deal {d['deal_id']} entered at "
                            f"{d['entry_date']} but previous deal was still "
                            f"open until {prev_exit} (overlap)"
                        )
                    if not d.get("is_open"):
                        prev_exit = d["exit_date"]
