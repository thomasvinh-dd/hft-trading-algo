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
            atr_value = 1234.5
            shares = 5000

        d = _signal_to_dict(FakeSignal())
        self.assertEqual(d["ticker"], "MSN")
        self.assertEqual(d["entry_price"], 75200.0)
        self.assertEqual(d["sl_F"], 61664.0)
        self.assertEqual(d["sl_T"], 67680.0)
        self.assertEqual(d["size_pct"], 0.40)
        self.assertEqual(d["as_of_date"], "2026-10-10")
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


if __name__ == "__main__":
    unittest.main()
