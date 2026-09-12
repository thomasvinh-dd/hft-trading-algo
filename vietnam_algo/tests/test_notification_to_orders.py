"""Tests for the notification-to-order conversion logic.

Per user feedback (2026-09-09: the autofin notification feed at
``/notification/get-notify-by-topics?topics=SIGNAL`` has TP/SL events.
We convert those events into SELL orders so we capture intraday TP/SL
without waiting for EOD numberOfProfit deltas.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest

from vietnam_algo.libs.autofin_signals import AutofinNotification, parse_notification
from vietnam_algo.algos.autofin_signal import (
    _event_to_partial_orders,
    _is_trading_event,
)


def _event(*, id_: int = 2026090909565119500, title: str = "", body: str = "", tags=()):
    raw = {
        "_id": id_,
        "timeRequest": "2026-09-09T09:56:51.1953687+07:00",
        "tag": ",".join(tags),
        "type": "SIGNAL",
        "notifyDetail": {"title": title, "body": body},
    }
    return parse_notification(raw)


def _our_deal(symbol: str, qty: int = 1600, did: str = "1787616000:HII"):
    return {did: {
        "_id": 1787616000,
        "symbol": symbol,
        "_our_qty": qty,
        "rateForNav": 0.15,
    }}


class EventToPartialOrdersTests(unittest.TestCase):
    def test_tp_event_with_symbol_match(self):
        """[HII] TP event → SELL 1/3 of position."""
        body = (
            "[HII] 💰 Chốt lãi lần 1 mức giá 10,000\n"
            "Tỷ trọng 10 % danh mục nắm giữ\n"
            "Ngày mua: 25/08/2026\n"
            "Giá mua: 9,000\n"
            "Nắm giữ: 9 ngày"
        )
        ev = _event(
            title="💰 CHỐT LÃI HII (10 %)",
            body=body,
            tags=("SIGNAL", "TRADINGTIME", "TAKEPROFIT"),
        )
        our = _our_deal("HII", qty=1600)
        orders = _event_to_partial_orders(ev.title, ev.body, our)
        self.assertEqual(len(orders), 1)
        sell = orders[0]
        self.assertEqual(sell["symbol"], "HII")
        self.assertEqual(sell["side"], "NS")
        self.assertEqual(sell["price"], 10000)
        # 1600 // 3 = 533.
        self.assertEqual(sell["quantity"], 500)
        self.assertEqual(sell["kind"], "partial_close")
        self.assertIn("autofin_partial_event", sell["reason"])

    def test_no_match_returns_empty(self):
        """Event for a symbol we don't track → empty."""
        body = "[XYZ] 💰 Chốt lãi lần 1 mức giá 10,000"
        ev = _event(body=body, tags=("SIGNAL", "TAKEPROFIT"))
        our = _our_deal("HII", qty=1600)
        self.assertEqual(_event_to_partial_orders(ev.title, ev.body, our), [])

    def test_zero_qty_skipped(self):
        """If _our_qty == 0, don't generate a SELL."""
        body = "[HII] 💰 Chốt lãi lần 1 mức giá 10,000"
        ev = _event(body=body, tags=("SIGNAL", "TAKEPROFIT"))
        our = _our_deal("HII", qty=0)
        self.assertEqual(_event_to_partial_orders(ev.title, ev.body, our), [])

    def test_capital_protect_event(self):
        """🛡️ Chốt lãi X % → SELL X% of position."""
        body = "[HII] 🛡️ Chốt lãi 28.57 % bảo toàn vốn tại 15,000"
        ev = _event(body=body, tags=("SIGNAL", "TRADINGSTOP", "TAK"))
        our = _our_deal("HII", qty=1600)
        orders = _event_to_partial_orders(ev.title, ev.body, our)
        self.assertEqual(len(orders), 1)
        sell = orders[0]
        # 28.57% of 1600 = 457. Rounded down to 100-lots = 400.
        self.assertEqual(sell["price"], 15000)
        self.assertEqual(sell["quantity"], 400)

    def test_reduce_event(self):
        """⚠️ Hạ 50 % → SELL 1/3 of position (default reduce delta)."""
        # Use the exact autofin wording — "Hạ tỷ trọng" (y + ơ).
        body = "[HII] ⚠️ Hạ tỷ trọng lần 1\nGiá: 9,500 - Nắm giữ: 6 ngày"
        ev = _event(body=body, tags=("SIGNAL", "TAK", "TRAILINGSTOP"))
        our = _our_deal("HII", qty=1600)
        orders = _event_to_partial_orders(ev.title, ev.body, our)
        self.assertEqual(len(orders), 1)
        # Reduce: delta = qty // 3 = 533.
        self.assertEqual(orders[0]["quantity"], 500)

    def test_no_symbol_in_body(self):
        """Body without [SYM] → no match → empty."""
        body = "💰 Some random notification without a ticker"
        ev = _event(body=body, tags=("SIGNAL", "TAKEPROFIT"))
        our = _our_deal("HII", qty=1600)
        self.assertEqual(_event_to_partial_orders(ev.title, ev.body, our), [])

    def test_sub_lot_delta_skipped(self):
        """If the delta < 1 board lot, skip the order entirely."""
        body = "[HII] 💰 Chốt lãi lần 1 mức giá 10,000"
        ev = _event(body=body, tags=("SIGNAL", "TAKEPROFIT"))
        # 100 shares, 100/3 = 33 → < 100 lot → skipped.
        our = _our_deal("HII", qty=100)
        self.assertEqual(_event_to_partial_orders(ev.title, ev.body, our), [])

    def test_no_price_skipped(self):
        """TP events without an explicit price → skip (can't place a limit)."""
        body = "[HII] 💰 Chốt lãi lần 1"  # no mức giá
        ev = _event(body=body, tags=("SIGNAL", "TAKEPROFIT"))
        our = _our_deal("HII", qty=1600)
        # _parse_partial_close returns "take_profit_lap" with price=0 → skipped.
        self.assertEqual(_event_to_partial_orders(ev.title, ev.body, our), [])


class NotificationTagClassifierTests(unittest.TestCase):
    def test_take_profit_tags(self):
        self.assertTrue(_is_trading_event(("SIGNAL", "TRADINGTIME", "TAKEPROFIT")))

    def test_stop_order_tags(self):
        self.assertTrue(_is_trading_event(("SIGNAL", "TRADINGTIME", "STOPORDER")))

    def test_trailingstop_tags(self):
        """TRAILINGSTOP is the most common — capital_protect / trailing trim."""
        self.assertTrue(_is_trading_event(("SIGNAL", "TRADINGTIME", "TRAILINGSTOP")))

    def test_combined_trading_tags(self):
        self.assertTrue(_is_trading_event(("SIGNAL", "EOD", "OPENDEAL", "TAKEPROFIT")))

    def test_unrelated_tags(self):
        """REPORT/REMIND/OPENDEAL without TP/SL markers → not a trading event."""
        self.assertFalse(_is_trading_event(("SIGNAL", "EOD")))
        self.assertFalse(_is_trading_event(("SIGNAL", "EOD", "REPORT")))
        self.assertFalse(_is_trading_event(("SIGNAL", "OPENDEAL", "REMIND")))


if __name__ == "__main__":
    unittest.main()
