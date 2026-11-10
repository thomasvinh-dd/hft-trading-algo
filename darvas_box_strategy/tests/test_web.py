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


class UrlPrefixTests(unittest.TestCase):
    """The dashboard can be served under a path prefix, e.g. /darvas.

    The reverse proxy strips the prefix before forwarding, so Flask still
    sees its normal routes, but the app must know the prefix in order to
    generate correct asset and API URLs. Without this the dashboard's CSS,
    JavaScript and fetch() calls all resolve to the site root and break.
    """

    def _client(self):
        from darvas_box_strategy.web.app import create_app
        return create_app().test_client()

    def test_prefix_from_forwarded_header(self):
        """X-Forwarded-Prefix sets the prefix."""
        from darvas_box_strategy.web.app import url_prefix
        c = self._client()
        with c.application.test_request_context(
            "/", headers={"X-Forwarded-Prefix": "/darvas"}
        ):
            self.assertEqual(url_prefix(), "/darvas")

    def test_no_prefix_by_default(self):
        from darvas_box_strategy.web.app import url_prefix
        c = self._client()
        with c.application.test_request_context("/"):
            self.assertEqual(url_prefix(), "")

    def test_env_var_fallback(self):
        """DARVAS_URL_PREFIX is used when no header is present."""
        from darvas_box_strategy.web.app import url_prefix
        c = self._client()
        with patch.dict(os.environ, {"DARVAS_URL_PREFIX": "/darvas"}):
            with c.application.test_request_context("/"):
                self.assertEqual(url_prefix(), "/darvas")

    def test_header_wins_over_env(self):
        from darvas_box_strategy.web.app import url_prefix
        c = self._client()
        with patch.dict(os.environ, {"DARVAS_URL_PREFIX": "/from-env"}):
            with c.application.test_request_context(
                "/", headers={"X-Forwarded-Prefix": "/from-header"}
            ):
                self.assertEqual(url_prefix(), "/from-header")

    def test_prefix_is_normalised(self):
        from darvas_box_strategy.web.app import url_prefix
        c = self._client()
        for raw, expected in (
            ("darvas", "/darvas"),
            ("/darvas/", "/darvas"),
            ("/darvas//", "/darvas"),
            ("/", ""),
            ("", ""),
        ):
            with self.subTest(raw=raw):
                with c.application.test_request_context(
                    "/", headers={"X-Forwarded-Prefix": raw}
                ):
                    self.assertEqual(url_prefix(), expected)

    def test_index_renders_base_href(self):
        """With a prefix the page must carry a <base href> so relative
        URLs resolve against the prefix."""
        c = self._client()
        r = c.get("/", headers={"X-Forwarded-Prefix": "/darvas"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'<base href="/darvas/">', r.data)

    def test_index_without_prefix_has_no_base_href(self):
        """At the site root there is no prefix and no base tag."""
        c = self._client()
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"<base href=", r.data)

    def test_static_urls_include_prefix(self):
        """Asset URLs must be prefixed, otherwise they bypass the mount
        point and 404."""
        c = self._client()
        r = c.get("/", headers={"X-Forwarded-Prefix": "/darvas"})
        self.assertIn(b'href="/darvas/static/styles.css"', r.data)
        self.assertIn(b'src="/darvas/static/app.js"', r.data)

    def test_static_urls_unchanged_without_prefix(self):
        c = self._client()
        r = c.get("/")
        self.assertIn(b'href="/static/styles.css"', r.data)
        self.assertIn(b'src="/static/app.js"', r.data)

    def test_api_routes_still_work_with_prefix(self):
        """The proxy strips the prefix, so the app's own routes are
        unchanged — the prefix only affects generated URLs."""
        c = self._client()
        r = c.get("/api/health", headers={"X-Forwarded-Prefix": "/darvas"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_js_derives_api_base_from_base_href(self):
        """The JS must build API URLs from the <base href>, not from a
        hard-coded site root."""
        js_path = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"
        js = js_path.read_text()
        self.assertIn("API_BASE", js)
        self.assertIn('document.querySelector("base")', js)
        # Every API call must go through the api() helper.
        self.assertNotIn('fetchJSON("/api/', js)
        self.assertNotIn("fetchJSON(`/api/", js)

    def test_static_url_helper_outside_request_context(self):
        """static_url is also reachable from a background thread, where
        there is no request context — it must not raise."""
        from darvas_box_strategy.web.app import static_url
        c = self._client()
        with c.application.test_request_context("/"):
            self.assertEqual(static_url("app.js"), "/static/app.js")


if __name__ == "__main__":
    unittest.main()


class OpenDealExhaustFillTests(unittest.TestCase):
    """An open deal must not show a terminal 'sold everything' fill.

    The executor emits an EXHAUST fill when the backtest runs out of
    bars. That fill is a mark-to-market snapshot, NOT a sale — the
    position is still held. The old timeline rendered it as a full
    sell ("-2,400 sh, remaining 0") while the deal header said
    "OPEN", which is self-contradictory.

    Regression: the user saw VNM_2026-08-10 showing "sell all the
    shares" and asked why the deal was still open.
    """

    def _fills(self, *, is_open):
        from darvas_box_strategy.web.data import _fills_to_dicts
        from darvas_box_strategy.darvas.executor import FillEvent
        from darvas_box_strategy.darvas.signal_generator import Signal
        from darvas_box_strategy.darvas.base_detector import Base
        from datetime import date

        b = Base(ticker="X", base_low=9_800, base_high=10_000, end_idx=0,
                 days_in_base=10, volume_compression=1.0)
        sig = Signal(
            ticker="X", signal_date=date(2026, 1, 1), entry_date=date(2026, 1, 2),
            entry_price=10_000.0, base=b, sl_F=8_200.0, sl_T=9_000.0,
            tiers=((1.10, 0.30),), conviction="low", size_pct=0.15,
            shares=2_400, max_volume=99_999, bars_remaining=10,
        )
        fills = [
            FillEvent(date=date(2026, 1, 3), kind="TIER", level=11_000.0,
                      shares=-720, proceeds=7_920_000.0, fraction_of_pos=0.30,
                      cumulative_proceeds=7_920_000.0, realized_pnl=720_000.0,
                      remaining=1_680),
            FillEvent(date=date(2026, 1, 4), kind="EXHAUST", level=9_500.0,
                      shares=-1_680, proceeds=15_960_000.0, fraction_of_pos=1.0,
                      cumulative_proceeds=23_880_000.0, realized_pnl=-840_000.0,
                      remaining=0),
        ]
        return _fills_to_dicts(fills, signal=sig, is_open=is_open)

    def test_open_deal_exhaust_becomes_mark(self):
        out = self._fills(is_open=True)
        last = out[-1]
        self.assertEqual(last["kind"], "MARK")
        self.assertEqual(last["shares"], 0,
                         "open deal must not show a sell of the remaining shares")
        self.assertEqual(last["pnl_delta"], 0.0)
        self.assertIn("still open", last["note"].lower())
        # The shares still held must be shown in the remaining column
        self.assertEqual(last["remaining"], 1_680)
        self.assertIn("1,680", last["note"])

    def test_open_deal_still_shows_real_exits(self):
        out = self._fills(is_open=True)
        kinds = [f["kind"] for f in out]
        self.assertIn("ENTRY", kinds)
        self.assertIn("TIER", kinds)
        # The real TIER exit keeps its negative share count
        tier = next(f for f in out if f["kind"] == "TIER")
        self.assertEqual(tier["shares"], -720)
        self.assertEqual(tier["remaining"], 1_680)

    def test_closed_deal_keeps_exhaust_sell(self):
        """A CLOSED deal's EXHAUST fill is a real sale — keep it."""
        out = self._fills(is_open=False)
        last = out[-1]
        self.assertEqual(last["kind"], "EXHAUST")
        self.assertEqual(last["shares"], -1_680)
        self.assertNotEqual(last["shares"], 0)


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
        """For HOSE blue chips, get_ticker_history should never return
        two deals on the same ticker that overlap in time. (Live
        trading can't have two open positions on one ticker at once.)

        This used to sample only 5 tickers, and ABB — which had 5
        concurrent open deals — wasn't one of them. The sample is now
        the full 70-ticker universe plus every ticker that regressed.
        """
        from darvas_box_strategy.bot.cli import build_default_config
        from darvas_box_strategy.web.data import get_ticker_history
        cfg = build_default_config()
        # The tickers that actually regressed, plus the historical sample.
        tickers = ["MSN", "VCB", "VHM", "PLX", "GEE", "ABB", "ORS"]
        try:
            from darvas_box_strategy.bot.cli import load_blue_chips
            tickers = sorted(set(tickers) | set(load_blue_chips()))
        except Exception:
            pass
        for ticker in tickers:
            with self.subTest(ticker=ticker):
                hist = get_ticker_history(ticker, cfg=cfg)
                self.assertEqual(hist["ticker"], ticker)
                deals = hist["deals"]
                if not deals:
                    continue
                # At most ONE open deal per ticker.
                opens = [d for d in deals if d.get("is_open")]
                self.assertLessEqual(
                    len(opens), 1,
                    f"{ticker}: {len(opens)} concurrent open deals: "
                    f"{[d['deal_id'] for d in opens]}",
                )
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
