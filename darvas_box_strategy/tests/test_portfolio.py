"""Tests for the new Portfolio class and pure Signal type."""
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))


class PureSignalTests(unittest.TestCase):
    """The clean Signal in darvas.signals has no portfolio concerns."""

    def test_signal_has_no_sizing_fields(self):
        from darvas_box_strategy.darvas.signals import Signal
        from dataclasses import fields
        field_names = {f.name for f in fields(Signal)}
        # Pure Signal must NOT have these (those are portfolio concerns)
        for forbidden in ("size_pct", "shares", "max_volume"):
            self.assertNotIn(
                forbidden, field_names,
                f"Pure Signal should not have {forbidden} (portfolio concern)",
            )
        # But it should have these
        for required in ("ticker", "signal_date", "entry_date",
                         "entry_price", "sl_F", "sl_T", "tiers",
                         "conviction", "atr_value"):
            self.assertIn(required, field_names)


class SignalHistoryTests(unittest.TestCase):
    """SignalHistory is a thin list with filtering helpers."""

    def test_add_and_query(self):
        from darvas_box_strategy.darvas.signals import Signal, SignalHistory
        from dataclasses import dataclass, field
        @dataclass
        class FakeBase:
            ticker: str = "X"
            base_low: float = 0.0
            base_high: float = 0.0
            end_idx: int = 0
            days_in_base: int = 5
            volume_compression: float = 1.0
        base = FakeBase()
        history = SignalHistory()
        history.add(Signal(
            ticker="MSN", signal_date=date(2026, 1, 9),
            entry_date=date(2026, 1, 10), entry_price=100.0,
            base=base, sl_F=80, sl_T=90, tiers=((1.1, 0.5),),
            conviction="low",
        ))
        history.add(Signal(
            ticker="VIX", signal_date=date(2026, 2, 5),
            entry_date=date(2026, 2, 6), entry_price=200.0,
            base=base, sl_F=160, sl_T=180, tiers=((1.1, 0.5),),
            conviction="high",
        ))
        self.assertEqual(len(history), 2)
        self.assertEqual(len(history.by_ticker("MSN")), 1)
        self.assertEqual(len(history.by_ticker("VIX")), 1)
        # since(date(2026, 2, 1)) returns both
        self.assertEqual(len(history.since(date(2026, 2, 1))), 1)
        # between
        self.assertEqual(len(history.between(date(2026, 1, 1), date(2026, 1, 31))), 1)
        # most_recent
        self.assertEqual(history.most_recent(1)[0].ticker, "VIX")

    def test_clear(self):
        from darvas_box_strategy.darvas.signals import Signal, SignalHistory
        from dataclasses import dataclass
        @dataclass
        class FakeBase:
            ticker: str = "X"
            base_low: float = 0.0
            base_high: float = 0.0
            end_idx: int = 0
            days_in_base: int = 5
            volume_compression: float = 1.0
        history = SignalHistory()
        history.add(Signal(
            ticker="X", signal_date=date(2026, 1, 1),
            entry_date=date(2026, 1, 2), entry_price=100.0,
            base=FakeBase(), sl_F=80, sl_T=90, tiers=((1.1, 0.5),),
            conviction="low",
        ))
        self.assertEqual(len(history), 1)
        history.clear()
        self.assertEqual(len(history), 0)


class PortfolioSizePositionTests(unittest.TestCase):
    """The sizing helper in darvas.portfolio computes shares from a Signal."""

    def test_size_position_basic(self):
        from darvas_box_strategy.darvas.signals import Signal
        from darvas_box_strategy.darvas.portfolio import size_position
        from darvas_box_strategy.darvas.config import (
            StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig,
            ExitsConfig, TrailingConfig, UniverseConfig,
        )
        from dataclasses import dataclass
        @dataclass
        class FakeBase:
            ticker: str = "X"
            base_low: float = 0.0
            base_high: float = 0.0
            end_idx: int = 0
            days_in_base: int = 5
            volume_compression: float = 1.0
        cfg = StrategyConfig(
            base=BaseConfig(), breakout=BreakoutConfig(),
            exits=ExitsConfig(), trailing=TrailingConfig(),
            universe=UniverseConfig(),
            backtest=BacktestConfig(
                start_nav=1e9, size_low=0.20, size_high=0.40,
                cost_bps=0.0, board_lot=100,
            ),
        )
        sig = Signal(
            ticker="X", signal_date=date(2026, 1, 9),
            entry_date=date(2026, 1, 10), entry_price=10_000.0,
            base=FakeBase(), sl_F=8_000, sl_T=9_000,
            tiers=((1.1, 0.5),), conviction="high",
            extra={"avg_vol": 10_000_000},  # 10M volume; 5% cap = 500K
        )
        shares, size_pct = size_position(sig, nav=1e9, cfg=cfg)
        # 0.40 * 1B = 400M / 10,000 = 40,000 shares (clean lot, below cap)
        self.assertEqual(size_pct, 0.40)
        self.assertEqual(shares, 40_000)
        self.assertEqual(shares % 100, 0)

    def test_size_position_lot_rounding(self):
        from darvas_box_strategy.darvas.signals import Signal
        from darvas_box_strategy.darvas.portfolio import size_position
        from darvas_box_strategy.darvas.config import (
            StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig,
            ExitsConfig, TrailingConfig, UniverseConfig,
        )
        from dataclasses import dataclass
        @dataclass
        class FakeBase:
            ticker: str = "X"
            base_low: float = 0.0
            base_high: float = 0.0
            end_idx: int = 0
            days_in_base: int = 5
            volume_compression: float = 1.0
        cfg = StrategyConfig(
            base=BaseConfig(), breakout=BreakoutConfig(),
            exits=ExitsConfig(), trailing=TrailingConfig(),
            universe=UniverseConfig(),
            backtest=BacktestConfig(
                start_nav=10_000.0, size_low=0.20, size_high=0.40,
                cost_bps=0.0, board_lot=100,
            ),
        )
        # 10K * 0.20 = 2K; 2K / 7,331 = 0 shares → drop
        sig = Signal(
            ticker="X", signal_date=date(2026, 1, 9),
            entry_date=date(2026, 1, 10), entry_price=7_331.0,
            base=FakeBase(), sl_F=5_000, sl_T=6_000,
            tiers=((1.1, 0.5),), conviction="low",
        )
        shares, size_pct = size_position(sig, nav=10_000.0, cfg=cfg)
        self.assertEqual(shares, 0)
        self.assertEqual(size_pct, 0.0)


class PortfolioBasicTests(unittest.TestCase):
    """The Portfolio class manages cash, positions, and history."""

    def setUp(self):
        from darvas_box_strategy.darvas.config import (
            StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig,
            ExitsConfig, TrailingConfig, UniverseConfig,
        )
        self.cfg = StrategyConfig(
            base=BaseConfig(),
            breakout=BreakoutConfig(),
            exits=ExitsConfig(tiers=((1.10, 1.0),)),  # 10% TP, 100% sell
            trailing=TrailingConfig(),
            universe=UniverseConfig(),
            backtest=BacktestConfig(
                start_nav=1e9, size_low=0.20, size_high=0.40,
                cost_bps=0.0, board_lot=100,
            ),
        )
        from dataclasses import dataclass
        @dataclass
        class FakeBase:
            ticker: str = "X"
            base_low: float = 9_800
            base_high: float = 10_000
            end_idx: int = 0
            days_in_base: int = 5
            volume_compression: float = 1.0
        self.FakeBase = FakeBase

    def _make_signal(self, ticker="X", price=10_000, conviction="low"):
        from darvas_box_strategy.darvas.signals import Signal
        return Signal(
            ticker=ticker, signal_date=date(2026, 1, 9),
            entry_date=date(2026, 1, 10), entry_price=price,
            base=self.FakeBase(),
            sl_F=price * 0.8, sl_T=price * 0.9,
            tiers=self.cfg.exits.tiers, conviction=conviction,
            atr_value=None, bars_remaining=100,
            extra={"avg_vol": 10_000_000},  # high enough to not cap
        )

    def test_process_signal_opens_position(self):
        from darvas_box_strategy.darvas.portfolio import Portfolio
        portfolio = Portfolio(self.cfg, start_nav=1e9)
        order = portfolio.process_signal(self._make_signal())
        self.assertIsNotNone(order)
        self.assertEqual(order.side, "buy")
        self.assertEqual(order.kind, "ENTRY")
        self.assertEqual(portfolio.cash, 1e9 - order.shares * order.price)
        self.assertEqual(len(portfolio.open_positions), 1)
        self.assertIn("X", portfolio.open_positions)

    def test_process_signal_skips_duplicate_ticker(self):
        from darvas_box_strategy.darvas.portfolio import Portfolio
        portfolio = Portfolio(self.cfg, start_nav=1e9)
        order1 = portfolio.process_signal(self._make_signal())
        order2 = portfolio.process_signal(self._make_signal())
        self.assertIsNotNone(order1)
        self.assertIsNone(order2, "second signal for same ticker should be skipped")
        self.assertEqual(len(portfolio.open_positions), 1)

    def test_process_bar_triggers_tp_exit(self):
        from darvas_box_strategy.darvas.portfolio import Portfolio
        portfolio = Portfolio(self.cfg, start_nav=1e9)
        portfolio.process_signal(self._make_signal())
        # Bar with close at 1.10x entry → TP hit
        orders = portfolio.process_bar(date(2026, 1, 11), {"X": 11_000.0})
        # Should get at least one sell order
        sell_orders = [o for o in orders if o.side == "sell"]
        self.assertGreater(len(sell_orders), 0)
        # After TP hit, position should be closed
        self.assertEqual(len(portfolio.open_positions), 0)
        # History should have the trade
        self.assertEqual(len(portfolio.history), 1)
        # Trade should be a winner
        self.assertTrue(portfolio.history[0].is_winner)

    def test_process_bar_triggers_sl_hard(self):
        from darvas_box_strategy.darvas.portfolio import Portfolio
        portfolio = Portfolio(self.cfg, start_nav=1e9)
        portfolio.process_signal(self._make_signal())
        # Bar with close at 0.7x entry → hard stop hit
        orders = portfolio.process_bar(date(2026, 1, 11), {"X": 7_000.0})
        sell_orders = [o for o in orders if o.side == "sell"]
        self.assertGreater(len(sell_orders), 0)
        self.assertEqual(len(portfolio.open_positions), 0)
        self.assertEqual(len(portfolio.history), 1)
        self.assertFalse(portfolio.history[0].is_winner)
        # After SL_HARD exit, cooldown should be set
        self.assertIn("X", portfolio._cooldown_until)

    def test_max_concurrent_positions(self):
        from darvas_box_strategy.darvas.portfolio import Portfolio
        # Set max to 1
        from dataclasses import replace
        cfg = replace(self.cfg, backtest=replace(self.cfg.backtest, max_concurrent_positions=1))
        portfolio = Portfolio(cfg, start_nav=1e9)
        order1 = portfolio.process_signal(self._make_signal("X", 10_000))
        self.assertIsNotNone(order1)
        # With max=1, no second position is allowed (regardless of ticker)
        order2 = portfolio.process_signal(self._make_signal("Y", 10_000))
        self.assertIsNone(order2, "Y should be skipped (max 1 position)")
        self.assertEqual(len(portfolio.open_positions), 1)

    def test_close_all_marks_exhaust(self):
        from darvas_box_strategy.darvas.portfolio import Portfolio
        portfolio = Portfolio(self.cfg, start_nav=1e9)
        portfolio.process_signal(self._make_signal())
        portfolio.process_bar(date(2026, 1, 11), {"X": 10_100.0})  # MTM
        portfolio.close_all()
        self.assertEqual(len(portfolio.open_positions), 0)
        self.assertEqual(len(portfolio.history), 1)
        self.assertEqual(portfolio.history[0].exit_reason, "EXHAUST")

    def test_get_nav(self):
        from darvas_box_strategy.darvas.portfolio import Portfolio
        portfolio = Portfolio(self.cfg, start_nav=1e9)
        self.assertEqual(portfolio.get_nav(), 1e9)
        portfolio.process_signal(self._make_signal())
        # After buying, NAV should still be ~1e9 (cash - position = position MTM)
        nav = portfolio.get_nav()
        self.assertAlmostEqual(nav, 1e9, delta=1e3)


class OrderTradeTests(unittest.TestCase):
    """Order and Trade are the public types emitted by the Portfolio."""

    def test_order_str(self):
        from darvas_box_strategy.darvas.portfolio import Order
        o = Order(ticker="MSN", side="buy", shares=1000, price=75_200,
                  kind="ENTRY", reason="signal")
        s = str(o)
        self.assertIn("buy", s)
        self.assertIn("1000", s)
        self.assertIn("MSN", s)
        self.assertIn("ENTRY", s)

    def test_trade_deal_id(self):
        from darvas_box_strategy.darvas.portfolio import Trade
        from darvas_box_strategy.darvas.signals import Signal
        from dataclasses import dataclass
        @dataclass
        class FakeBase:
            ticker: str = "X"
            base_low: float = 0.0
            base_high: float = 0.0
            end_idx: int = 0
            days_in_base: int = 5
            volume_compression: float = 1.0
        sig = Signal(
            ticker="MSN", signal_date=date(2026, 1, 9),
            entry_date=date(2026, 1, 10), entry_price=100.0,
            base=FakeBase(), sl_F=80, sl_T=90, tiers=((1.1, 0.5),),
            conviction="low",
        )
        t = Trade(
            ticker="MSN", signal=sig,
            entry_date=date(2026, 1, 10), entry_price=100.0,
            shares=1000, exit_date=date(2026, 1, 15),
            exit_price=110.0, exit_reason="TRAIL",
            realized_pnl=10_000.0, return_pct=0.10,
            cost_paid=0.0, bars_held=5,
            highest_close=110.0, last_close=110.0,
        )
        self.assertEqual(t.deal_id, "MSN_2026-01-10")
        self.assertTrue(t.is_winner)


if __name__ == "__main__":
    unittest.main()
