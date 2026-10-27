"""Tests for the corporate-action handler."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
PARENT = os.path.dirname(ROOT)
for p in (ROOT, PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

from darvas_box_strategy.darvas import (  # noqa: E402
    CAEvent, PriceBar, adjust_bars, build_event_index, parse_event,
)


def make_bar(date_str, close, *, high=None, low=None, open_=None):
    return PriceBar(
        time=date_str,
        open=open_ if open_ is not None else close,
        high=high if high is not None else close + 1,
        low=low if low is not None else close - 1,
        close=close, volume=100_000.0,
    )


class ParseEventTests(unittest.TestCase):
    def test_cash_dividend(self):
        row = {"symbol": "AAA", "exDate": "2024-06-15",
               "note": "Trả cổ tức đợt 1/2024 bằng tiền, 1,500 đồng/CP",
               "name": "Trả cổ tức bằng tiền mặt"}
        ev = parse_event(row)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.cash_per_share, 1500.0)

    def test_stock_dividend(self):
        row = {"symbol": "BBB", "exDate": "2024-09-20",
               "note": "Trả cổ tức bằng cổ phiếu, tỷ lệ 100:25",
               "name": "Trả cổ tức bằng cổ phiếu"}
        ev = parse_event(row)
        self.assertIsNotNone(ev)
        self.assertAlmostEqual(ev.share_multiplier, 1.25)

    def test_unknown_event(self):
        self.assertIsNone(parse_event({"symbol": "CCC", "exDate": "2024-01-01",
                                        "note": "Hoán đổi cổ phiếu", "name": "Hoán đổi cổ phiếu"}))


class AdjustBarsTests(unittest.TestCase):
    def test_cash_dividend_continuous(self):
        bars = [make_bar("2024-06-13", 11000), make_bar("2024-06-14", 11000),
                make_bar("2024-06-15", 10000), make_bar("2024-06-16", 10100)]
        events = [CAEvent(ticker="X", ex_date=date(2024, 6, 15), cash_per_share=1000.0)]
        adj, div = adjust_bars(bars, events)
        self.assertAlmostEqual(adj[0].close, 12100.0, places=2)
        self.assertAlmostEqual(adj[2].close, 10000.0, places=2)
        self.assertEqual(div, {date(2024, 6, 15): 1000.0})

    def test_stock_dividend_continuous(self):
        bars = [make_bar("2024-09-18", 10000), make_bar("2024-09-19", 10000),
                make_bar("2024-09-20", 8000), make_bar("2024-09-21", 8100)]
        events = [CAEvent(ticker="Y", ex_date=date(2024, 9, 20), share_multiplier=1.25)]
        adj, div = adjust_bars(bars, events)
        self.assertAlmostEqual(adj[0].close, 12500.0, places=2)
        self.assertEqual(div, {})

    def test_no_events_returns_original(self):
        bars = [make_bar("2024-01-01", 10000)]
        adj, div = adjust_bars(bars, [])
        self.assertEqual(len(adj), 1)
        self.assertEqual(adj[0].close, 10000)
        self.assertEqual(div, {})


if __name__ == "__main__":
    unittest.main()
