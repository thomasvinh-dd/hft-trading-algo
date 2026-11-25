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
    _parse_partial_close,
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
        """🛡️ Chốt lãi X % bảo toàn vốn tại P → SELL ALL at the
        suggested price.

        2026-10-18 user feedback (re-confirmed 2026-10-20 against
        BAF): the X% in this recomment is the P&L PERCENTAGE, NOT
        a qty fraction. ``Chốt lãi 28.57 % bảo toàn vốn tại
        15,000`` means "the position is up 28.57% — sell everything
        at 15,000 to lock in the profit." This mirrors the
        ``_plan_partial_close_orders`` semantics; the notification
        path and the EOD path must agree.
        """
        body = "[HII] 🛡️ Chốt lãi 28.57 % bảo toàn vốn tại 15,000"
        ev = _event(body=body, tags=("SIGNAL", "TRADINGSTOP", "TAK"))
        our = _our_deal("HII", qty=1600)
        orders = _event_to_partial_orders(ev.title, ev.body, our)
        self.assertEqual(len(orders), 1)
        sell = orders[0]
        # Full close — sell all 1,600 (already board-lot aligned).
        self.assertEqual(sell["price"], 15000)
        self.assertEqual(sell["quantity"], 1600)
        # ``kind`` is ``full_close`` (consistent with the EOD-path
        # decision in ``_plan_partial_close_orders``).
        self.assertEqual(sell["kind"], "full_close")

    def test_reduce_event(self):
        """⚠️ Hạ 50 % → SELL 50% of position (round-up to board lot)."""
        # Use autofin's actual wording — the OLD regex required
        # "Hạ tỷ trọng lần 1" (no %), which never matched real
        # autofin output. autofin always emits the percent BETWEEN
        # "Hạ" and "tỷ/tỉ trọng". See the 2026-09-24 HNM incident.
        body = "[HII] ⚠️ Hạ 50 % tỷ trọng lần 1\nGiá: 9,500 - Nắm giữ: 6 ngày"
        ev = _event(body=body, tags=("SIGNAL", "TAK", "TRAILINGSTOP"))
        our = _our_deal("HII", qty=1600)
        orders = _event_to_partial_orders(ev.title, ev.body, our)
        self.assertEqual(len(orders), 1)
        # Reduce: 50 % × 1600 = 800, round-up to 100-lots = 800.
        self.assertEqual(orders[0]["quantity"], 800)

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

    def test_capital_protect_event_negative_pnl_full_close(self):
        """2026-10-20 BAF incident: 'Chốt lãi -1.75 % bảo toàn vốn
        tại 30,950' (NEGATIVE P&L) must still be recognised as a
        full-close SELL at the suggested price. The previous
        regex ``[\\d,\\.]+`` did not accept the leading ``-``, so
        the parser returned 'unknown' and the order was silently
        dropped. The action semantics ('sell all at P') are
        identical regardless of P&L sign."""
        body = "[BAF] 🛡️ Chốt lãi -1.75 % bảo toàn vốn tại 30,950\nNắm giữ: 51 ngày"
        ev = _event(body=body, tags=("SIGNAL", "TRADINGTIME", "TRAILINGSTOP"))
        our = _our_deal("BAF", qty=200)
        orders = _event_to_partial_orders(ev.title, ev.body, our)
        self.assertEqual(len(orders), 1)
        sell = orders[0]
        # Full close at the suggested price (NOT 200 × 0.0175 = 3.5 → 0 lots).
        self.assertEqual(sell["quantity"], 200)
        self.assertEqual(sell["price"], 30950)
        self.assertEqual(sell["kind"], "full_close")
        # ``percent`` is a tiny fraction stored on the action object
        # (sanity check the parser kept the negative sign).
        action = _parse_partial_close(body)
        self.assertEqual(action.action, "capital_protect")
        self.assertAlmostEqual(action.percent, -1.75, places=2)
        self.assertEqual(action.price, 30950)


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
