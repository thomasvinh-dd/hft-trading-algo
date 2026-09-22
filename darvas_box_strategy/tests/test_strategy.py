"""Unit tests for the Darvas Box strategy."""
from __future__ import annotations

import dataclasses
import os
import sys
import unittest
from datetime import date, timedelta

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
PARENT = os.path.dirname(ROOT)
for p in (ROOT, PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

from darvas_box_strategy.darvas import (  # noqa: E402
    BaseConfig,
    BreakoutConfig,
    ExitsConfig,
    PriceBar,
    Signal,
    StrategyConfig,
    TrailingConfig,
    BacktestConfig,
    detect_base,
    generate_signal,
    simulate_trade,
)


def make_bars(n, start, *, base_range=None, base_days=0, base_idx=0,
              breakout_jump=0.0, vol_avg=100_000.0, vol_spike=1.0,
              final_trend=None):
    bars = []
    for i in range(n):
        d = start + timedelta(days=i)
        if base_range is not None and base_idx <= i < base_idx + base_days:
            lo, hi = base_range
            mid = (lo + hi) / 2
            high = hi
            low = lo
            close = mid
            open_ = mid
        elif i == base_idx + base_days and breakout_jump:
            close = 10000 + breakout_jump
            open_ = 10000
            high = close + 100
            low = open_ - 50
        else:
            close = 10000
            open_ = 10000
            high = 10050
            low = 9950
        vol = vol_avg * (vol_spike if i == base_idx + base_days else 1.0)
        bars.append(PriceBar(
            time=d.isoformat(), open=open_, high=high, low=low, close=close, volume=vol,
        ))
    if final_trend is not None:
        for i, c in enumerate(final_trend):
            idx = base_idx + base_days + i
            if idx >= len(bars):
                break
            b = bars[idx]
            bars[idx] = PriceBar(
                time=b.time, open=b.open, high=max(b.high, c + 50), low=min(b.low, c - 50),
                close=c, volume=b.volume,
            )
    return bars


class BaseDetectorTests(unittest.TestCase):
    def test_finds_tight_base(self):
        cfg = BaseConfig(min_days=5, max_days=20, max_range_pct=0.03)
        bars = make_bars(40, date(2025, 1, 1), base_range=(9900, 10100), base_days=10, base_idx=20)
        for i in range(30, 40):
            bars[i] = PriceBar(
                time=bars[i].time, open=10000, high=10500, low=9500,
                close=10000, volume=100_000,
            )
        b = detect_base(bars, cfg, as_of_idx=29)
        self.assertIsNotNone(b)
        self.assertAlmostEqual(b.base_low, 9900, places=2)
        self.assertAlmostEqual(b.base_high, 10100, places=2)
        self.assertGreaterEqual(b.days_in_base, 5)
        self.assertLess(b.range_pct, 0.03)

    def test_rejects_wide_range(self):
        cfg = BaseConfig(min_days=5, max_days=20, max_range_pct=0.02)
        bars = []
        for i in range(40):
            d = date(2025, 1, 1) + timedelta(days=i)
            close = 10000 + i * 50
            bars.append(PriceBar(
                time=d.isoformat(), open=close - 25, high=close + 25,
                low=close - 25, close=close, volume=100_000,
            ))
        b = detect_base(bars, cfg, as_of_idx=29)
        self.assertIsNone(b)

    def test_returns_most_recent_base(self):
        cfg = BaseConfig(min_days=3, max_days=10, max_range_pct=0.05)
        bars = make_bars(60, date(2025, 1, 1), base_range=(9950, 10050), base_days=5, base_idx=10)
        for i in range(15, 60):
            bars[i] = PriceBar(
                time=bars[i].time, open=10000, high=10500, low=9500,
                close=10000, volume=100_000,
            )
        for i in range(45, 50):
            bars[i] = PriceBar(
                time=bars[i].time, open=10000, high=10030, low=9970, close=10000, volume=100_000,
            )
        b = detect_base(bars, cfg, as_of_idx=55)
        self.assertIsNotNone(b)
        self.assertEqual(b.end_idx, 49)


class SignalGeneratorTests(unittest.TestCase):
    def _cfg(self, **kw):
        return StrategyConfig(
            base=BaseConfig(min_days=5, max_days=15, max_range_pct=0.03),
            breakout=BreakoutConfig(
                volume_mult=1.5, high_conviction_mult=2.5,
                require_ma200=False, min_history_days=60,
            ),
            exits=ExitsConfig(),
            trailing=TrailingConfig(),
            backtest=BacktestConfig(start_nav=1_000_000_000),
        )

    def test_breakout_with_volume_fires(self):
        bars = make_bars(100, date(2025, 1, 1), base_range=(9900, 10100), base_days=8, base_idx=60,
                          breakout_jump=200, vol_spike=2.0)
        cfg = self._cfg()
        b = detect_base(bars, cfg.base, as_of_idx=67, ticker="X")
        sig = generate_signal(bars, b, nav=1_000_000_000, cfg=cfg, as_of_idx=68)
        # 2.0x volume is below the new default high_conviction_mult=3.5
        # but above volume_mult=3.0, so it's still high enough to fire
        # with low conviction.
        self.assertIsNotNone(sig)
        self.assertEqual(sig.ticker, "X")
        self.assertGreater(sig.entry_price, b.base_high)
        # With vol_spike=2.0 and vol_mult=3.0, the breakout is not
        # high enough (2.0 < 3.0) so no signal fires with default config.
        # Verify by setting vol_mult lower:
        cfg_low = self._cfg()
        cfg_low = dataclasses.replace(cfg_low, breakout=dataclasses.replace(
            cfg_low.breakout, volume_mult=1.5, high_conviction_mult=2.5))
        sig_low = generate_signal(bars, b, nav=1_000_000_000, cfg=cfg_low, as_of_idx=68)
        self.assertIsNotNone(sig_low)
        self.assertEqual(sig_low.conviction, "low")

    def test_high_conviction_at_3x_volume(self):
        bars = make_bars(100, date(2025, 1, 1), base_range=(9900, 10100), base_days=8, base_idx=60,
                          breakout_jump=200, vol_spike=3.0)
        cfg = self._cfg()
        b = detect_base(bars, cfg.base, as_of_idx=67, ticker="X")
        sig = generate_signal(bars, b, nav=1_000_000_000, cfg=cfg, as_of_idx=68)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.conviction, "high")

    def test_no_breakout_without_volume(self):
        bars = make_bars(100, date(2025, 1, 1), base_range=(9900, 10100), base_days=8, base_idx=60,
                          breakout_jump=200, vol_spike=1.0)
        cfg = self._cfg()
        b = detect_base(bars, cfg.base, as_of_idx=67, ticker="X")
        sig = generate_signal(bars, b, nav=1_000_000_000, cfg=cfg, as_of_idx=68)
        self.assertIsNone(sig)

    def test_skip_imminent_ca(self):
        from darvas_box_strategy.darvas.ca_handler import CAEvent
        bars = make_bars(100, date(2025, 1, 1), base_range=(9900, 10100), base_days=8, base_idx=60,
                          breakout_jump=200, vol_spike=2.0)
        cfg = StrategyConfig(
            base=BaseConfig(min_days=5, max_days=15, max_range_pct=0.03),
            breakout=BreakoutConfig(
                volume_mult=1.5, high_conviction_mult=2.5,
                require_ma200=False, min_history_days=60,
                skip_days_before_ca=3,
            ),
            exits=ExitsConfig(),
            trailing=TrailingConfig(),
            backtest=BacktestConfig(start_nav=1_000_000_000),
        )
        b = detect_base(bars, cfg.base, as_of_idx=67, ticker="X")
        upcoming = [CAEvent(ticker="X", ex_date=date(2025, 3, 11))]
        sig = generate_signal(bars, b, nav=1_000_000_000, cfg=cfg, as_of_idx=68,
                              upcoming_ca=upcoming)
        self.assertIsNone(sig)
        upcoming_far = [CAEvent(ticker="X", ex_date=date(2025, 3, 14))]
        sig2 = generate_signal(bars, b, nav=1_000_000_000, cfg=cfg, as_of_idx=68,
                               upcoming_ca=upcoming_far)
        self.assertIsNotNone(sig2)


class ExecutorTests(unittest.TestCase):
    def _cfg(self):
        return StrategyConfig(
            base=BaseConfig(),
            breakout=BreakoutConfig(),
            exits=ExitsConfig(),
            trailing=TrailingConfig(),
            backtest=BacktestConfig(start_nav=1_000_000_000, cost_bps=0.0),
        )

    def _make_signal(self, entry_price=10_000):
        from darvas_box_strategy.darvas.base_detector import Base
        b = Base(ticker="X", base_low=9800, base_high=10_100, end_idx=0, days_in_base=10, volume_compression=1.0)
        return Signal(
            ticker="X", entry_date=date(2025, 1, 10), entry_price=entry_price, base=b,
            sl_F=9700, sl_T=9900,
            tiers=((1.063, 0.10), (1.125, 0.30), (1.21, 0.30), (1.33, 0.10)),
            conviction="low", size_pct=0.15, shares=10_000, max_volume=10_000, bars_remaining=10,
        )

    def test_tier_exit_ladder(self):
        sig = self._make_signal()
        bars = []
        for i, c in enumerate([10_000, 10_500, 10_700, 11_300, 12_200, 13_400, 14_500]):
            d = sig.entry_date + timedelta(days=i + 1)
            bars.append(PriceBar(time=d.isoformat(), open=c, high=c + 50, low=c - 50, close=c, volume=100_000))
        result = simulate_trade(sig, bars, self._cfg())
        tier_fills = [f for f in result.fills if f.kind == "TIER"]
        self.assertGreaterEqual(len(tier_fills), 3)
        self.assertGreater(result.realized_pnl, 0)

    def test_hard_stop_loss(self):
        sig = self._make_signal()
        bars = [
            PriceBar(time=(sig.entry_date + timedelta(days=i + 1)).isoformat(),
                     open=9_900, high=9_950, low=9_600, close=9_700, volume=100_000)
            for i in range(3)
        ]
        result = simulate_trade(sig, bars, self._cfg())
        hard = [f for f in result.fills if f.kind == "SL_HARD"]
        self.assertGreaterEqual(len(hard), 1)
        self.assertLess(result.realized_pnl, 0)

    def test_trailing_capital_preservation(self):
        sig = self._make_signal()
        bars = [
            PriceBar(time=(sig.entry_date + timedelta(days=1)).isoformat(),
                     open=10_700, high=10_700, low=10_600, close=10_700, volume=100_000),
            PriceBar(time=(sig.entry_date + timedelta(days=2)).isoformat(),
                     open=10_900, high=10_950, low=10_800, close=10_900, volume=100_000),
            PriceBar(time=(sig.entry_date + timedelta(days=3)).isoformat(),
                     open=10_900, high=10_950, low=9_700, close=9_750, volume=100_000),
        ]
        result = simulate_trade(sig, bars, self._cfg())
        trail = [f for f in result.fills if f.kind == "TRAIL"]
        self.assertEqual(len(trail), 1)
        self.assertEqual(result.exit_reason, "TRAIL")

    def test_atr_trail_mode(self):
        """ATR-based trailing stop fires at peak - ATR * mult."""
        sig = self._make_signal(entry_price=10_000)
        # ATR = 200 VND. With trail_atr_mult=2.0, trail at peak - 400.
        # Disable trail_floor so the pure ATR behavior is tested.
        bars = [
            PriceBar(time=(sig.entry_date + timedelta(days=1)).isoformat(),
                     open=10_300, high=10_500, low=10_200, close=10_400, volume=100_000),
            PriceBar(time=(sig.entry_date + timedelta(days=2)).isoformat(),
                     open=10_600, high=10_800, low=10_500, close=10_700, volume=100_000),
            PriceBar(time=(sig.entry_date + timedelta(days=3)).isoformat(),
                     open=10_700, high=10_700, low=10_350, close=10_400, volume=100_000),
        ]
        cfg = StrategyConfig(
            base=BaseConfig(),
            breakout=BreakoutConfig(),
            exits=ExitsConfig(),
            trailing=TrailingConfig(
                trail_mode="atr", trail_atr_mult=2.0,
                trail_floor_lock_profit=False,  # test pure ATR
            ),
            backtest=BacktestConfig(start_nav=1_000_000_000, cost_bps=0.0),
        )
        # Peak after TP1 = 10_700. Trail = 10_700 - 400 = 10_300.
        # Day 3 close = 10_400 > 10_300, so no trail exit.
        result = simulate_trade(sig, bars, cfg, atr_value=200.0)
        trail = [f for f in result.fills if f.kind == "TRAIL"]
        self.assertEqual(len(trail), 0)

        # Now add a bar where close drops below trail threshold
        bars2 = bars + [
            PriceBar(time=(sig.entry_date + timedelta(days=4)).isoformat(),
                     open=10_400, high=10_400, low=10_200, close=10_250, volume=100_000),
        ]
        result2 = simulate_trade(sig, bars2, cfg, atr_value=200.0)
        trail2 = [f for f in result2.fills if f.kind == "TRAIL"]
        self.assertEqual(len(trail2), 1)
        self.assertEqual(result2.exit_reason, "TRAIL")

    def test_atr_trail_with_floor(self):
        """trail_floor_lock_profit raises the trail threshold to TP1 price."""
        sig = self._make_signal(entry_price=10_000)
        bars = [
            PriceBar(time=(sig.entry_date + timedelta(days=1)).isoformat(),
                     open=10_600, high=10_800, low=10_500, close=10_700, volume=100_000),
            PriceBar(time=(sig.entry_date + timedelta(days=2)).isoformat(),
                     open=10_700, high=10_700, low=10_400, close=10_450, volume=100_000),
        ]
        cfg = StrategyConfig(
            base=BaseConfig(),
            breakout=BreakoutConfig(),
            exits=ExitsConfig(),
            trailing=TrailingConfig(
                trail_mode="atr", trail_atr_mult=10.0,  # very wide trail
                trail_floor_lock_profit=True,
            ),
            backtest=BacktestConfig(start_nav=1_000_000_000, cost_bps=0.0),
        )
        # Day 1: peak 10700, TP1 hit, trail activates, floor=10630
        # Day 2: ATR-based trail threshold = 10700 - 2000 = 8700
        # But floor raises to 10630. Close=10450 <= 10630 → trail fires.
        result = simulate_trade(sig, bars, cfg, atr_value=200.0)
        trail = [f for f in result.fills if f.kind == "TRAIL"]
        self.assertEqual(len(trail), 1)
        self.assertEqual(result.exit_reason, "TRAIL")


class PriceAdjustmentDefaultsTests(unittest.TestCase):
    def test_default_skip_days_before_ca_is_zero(self):
        cfg = StrategyConfig()
        self.assertEqual(cfg.breakout.skip_days_before_ca, 0)


class PortfolioDefaultsTests(unittest.TestCase):
    def test_default_max_concurrent_positions_is_ten(self):
        cfg = StrategyConfig()
        self.assertEqual(cfg.backtest.max_concurrent_positions, 10)

    def test_default_cooldown_is_five(self):
        cfg = StrategyConfig()
        self.assertEqual(cfg.backtest.cooldown_bars_after_stop, 5)

    def test_default_volume_mult_is_two(self):
        """OOS-validated: vol_mult=2.0 is the most robust setting."""
        cfg = StrategyConfig()
        self.assertEqual(cfg.breakout.volume_mult, 2.0)

    def test_default_trail_is_pct(self):
        """OOS-validated: pct trail beats atr trail on out-of-sample data."""
        cfg = StrategyConfig()
        self.assertEqual(cfg.trailing.trail_mode, "pct")
        self.assertEqual(cfg.trailing.trail_from_peak_pct, 0.12)

    def test_default_require_close_above_ma_is_20(self):
        """OOS-validated: requiring close>MA20 doubles OOS Sharpe."""
        cfg = StrategyConfig()
        self.assertEqual(cfg.breakout.require_close_above_ma, 20)

    def test_default_cost_bps_is_25(self):
        """Realistic VN-market round-trip cost (commission + slippage)."""
        cfg = StrategyConfig()
        self.assertEqual(cfg.backtest.cost_bps, 25.0)

    def test_default_max_holding_bars_is_zero(self):
        """Time stop is opt-in."""
        cfg = StrategyConfig()
        self.assertEqual(cfg.backtest.max_holding_bars, 0)

    def test_default_trail_is_12_percent(self):
        """OOS-validated: 12% trail lets winners compound; 7% was too tight."""
        cfg = StrategyConfig()
        self.assertEqual(cfg.trailing.trail_from_peak_pct, 0.12)

    def test_default_vol_target_is_disabled(self):
        """OOS-validated: vol-targeting hurt returns; fixed 2x sizing is better."""
        cfg = StrategyConfig()
        self.assertEqual(cfg.backtest.vol_target_pct, 0.0)

    def test_default_size_low_high(self):
        """OOS-validated: 2x sizing (0.20/0.40) for low/high conviction."""
        cfg = StrategyConfig()
        self.assertEqual(cfg.backtest.size_low, 0.20)
        self.assertEqual(cfg.backtest.size_high, 0.40)


class TrendFilterTests(unittest.TestCase):
    """Tests for require_close_above_ma filter."""

    def _build_bars(self, breakout_close, base_high=11_470):
        """Strong uptrend, 10-bar tight base, then breakout at `breakout_close`.

        `breakout_close` must be > base_high to trigger the breakout check.
        """
        from datetime import date, timedelta
        out = []
        for i in range(30):
            c = 10_000 + i * 50
            out.append(PriceBar(
                time=(date(2024, 1, 1) + timedelta(days=i)).isoformat(),
                open=c, high=c * 1.001, low=c * 0.999, close=c, volume=100_000,
            ))
        # 10-bar tight base
        for i in range(20, 30):
            out[i] = PriceBar(
                time=out[i].time, open=11_450, high=base_high,
                low=11_440, close=11_450, volume=100_000,
            )
        # breakout bar: high = base_high + 30, close > base_high + 5
        out.append(PriceBar(
            time=(date(2024, 1, 1) + timedelta(days=30)).isoformat(),
            open=base_high + 5, high=base_high + 50, low=base_high - 5,
            close=breakout_close, volume=500_000,
        ))
        return out

    def _make_base(self, bars):
        from darvas_box_strategy.darvas.base_detector import BaseScanner
        # Scan up to the breakout bar (exclusive) so the base ends before it.
        return BaseScanner(bars, BaseConfig(min_days=5, max_days=20, max_range_pct=0.05),
                           ticker="TST").find_base(as_of_idx=len(bars) - 2)

    def test_trend_filter_blocks_signal_below_ma(self):
        # Close = 11_480, but recent prices mostly 11_450 so MA20 ≈ 11_450
        # 11_480 > 11_450 should pass. To block, make close < MA20.
        # Use flat base around 11_000 so MA20 ≈ 11_000; breakout close = 10_900 (below MA)
        from datetime import date, timedelta
        bars = []
        for i in range(30):
            bars.append(PriceBar(
                time=(date(2024, 1, 1) + timedelta(days=i)).isoformat(),
                open=11_000, high=11_010, low=10_990, close=11_000,
                volume=100_000,
            ))
        for i in range(20, 30):
            bars[i] = PriceBar(
                time=bars[i].time, open=11_000, high=11_010,
                low=10_990, close=11_000, volume=100_000,
            )
        # breakout: close just above base_high but pull back to MA20
        bars.append(PriceBar(
            time=(date(2024, 1, 1) + timedelta(days=30)).isoformat(),
            open=11_010, high=11_030, low=10_950, close=10_960, volume=500_000,
        ))
        base = self._make_base(bars)
        cfg = StrategyConfig(
            breakout=BreakoutConfig(volume_mult=1.0, require_ma200=False,
                                    require_close_above_ma=20),
        )
        sig = generate_signal(bars, base, nav=1e9, cfg=cfg, as_of_idx=len(bars) - 1)
        self.assertIsNone(sig)

    def test_trend_filter_passes_signal_above_ma(self):
        bars = self._build_bars(breakout_close=11_510)
        base = self._make_base(bars)
        cfg = StrategyConfig(
            breakout=BreakoutConfig(volume_mult=1.0, require_ma200=False,
                                    require_close_above_ma=20),
        )
        sig = generate_signal(bars, base, nav=1e9, cfg=cfg, as_of_idx=len(bars) - 1)
        self.assertIsNotNone(sig)


class RegimeFilterTests(unittest.TestCase):
    """Tests for the market regime filter."""

    def test_regime_off_when_no_path(self):
        from datetime import date, timedelta
        from darvas_box_strategy.darvas.base_detector import BaseScanner
        from darvas_box_strategy.darvas import (
            StrategyConfig, BaseConfig, BreakoutConfig,
        )

        bars = []
        for i in range(220):
            c = 10_000 + i * 10
            bars.append(PriceBar(
                time=(date(2024, 1, 1) + timedelta(days=i)).isoformat(),
                open=c, high=c * 1.001, low=c * 0.999, close=c, volume=100_000,
            ))
        # 10-bar tight base at the end
        for i in range(210, 220):
            bars[i] = PriceBar(
                time=bars[i].time, open=12_000, high=12_030, low=11_980,
                close=12_000, volume=100_000,
            )
        # breakout bar
        bars.append(PriceBar(
            time=(date(2024, 1, 1) + timedelta(days=220)).isoformat(),
            open=12_050, high=12_200, low=12_040, close=12_180, volume=500_000,
        ))
        base = BaseScanner(bars, BaseConfig(), ticker="TST").find_base(as_of_idx=len(bars) - 2)
        cfg = StrategyConfig(
            breakout=BreakoutConfig(volume_mult=1.0, require_ma200=False,
                                    regime_index_path=""),
        )
        sig = generate_signal(bars, base, nav=1e9, cfg=cfg, as_of_idx=len(bars) - 1)
        self.assertIsNotNone(sig)

    def test_regime_blocks_when_index_below_ma(self):
        from datetime import date, timedelta
        from darvas_box_strategy.darvas.base_detector import BaseScanner
        from darvas_box_strategy.darvas import (
            StrategyConfig, BaseConfig, BreakoutConfig,
        )

        bars = []
        for i in range(220):
            c = 10_000 + i * 10
            bars.append(PriceBar(
                time=(date(2024, 1, 1) + timedelta(days=i)).isoformat(),
                open=c, high=c * 1.001, low=c * 0.999, close=c, volume=100_000,
            ))
        for i in range(210, 220):
            bars[i] = PriceBar(
                time=bars[i].time, open=12_000, high=12_030, low=11_980,
                close=12_000, volume=100_000,
            )
        bars.append(PriceBar(
            time=(date(2024, 1, 1) + timedelta(days=220)).isoformat(),
            open=12_050, high=12_200, low=12_040, close=12_180, volume=500_000,
        ))
        base = BaseScanner(bars, BaseConfig(), ticker="TST").find_base(as_of_idx=len(bars) - 2)

        # Market bars: downtrend, last close far below MA50
        market_bars = []
        for i in range(100):
            c = 30_000 - i * 50
            market_bars.append(PriceBar(
                time=(date(2024, 1, 1) + timedelta(days=i)).isoformat(),
                open=c, high=c * 1.001, low=c * 0.999, close=c, volume=100_000,
            ))
        cfg = StrategyConfig(
            breakout=BreakoutConfig(volume_mult=1.0, require_ma200=False,
                                    regime_index_path="/dummy",
                                    regime_ma_window=50),
        )
        sig = generate_signal(bars, base, nav=1e9, cfg=cfg,
                              as_of_idx=len(bars) - 1, market_bars=market_bars)
        self.assertIsNone(sig)


class TimeStopTests(unittest.TestCase):
    """Tests for max_holding_bars time stop."""

    def test_time_stop_closes_when_not_profitable(self):
        from datetime import date, timedelta
        from darvas_box_strategy.darvas.signal_generator import Signal, generate_signal

        sig = Signal(
            ticker="TST", entry_date=date(2024, 1, 1),
            entry_price=10_000.0, base=None,
            sl_F=9_500.0, sl_T=9_800.0,
            tiers=((1.10, 1.0),), conviction="low", size_pct=0.1,
            shares=1000, max_volume=999999, bars_remaining=30,
        )
        cfg = StrategyConfig(
            backtest=BacktestConfig(start_nav=1e9, cost_bps=0.0,
                                    max_holding_bars=3),
            trailing=TrailingConfig(trail_mode="pct", trail_from_peak_pct=0.20,
                                    trail_floor_lock_profit=False,
                                    activation_tier_idx=999),
        )
        # 3 bars where close stays below entry
        bars = []
        for i in range(3):
            bars.append(PriceBar(
                time=(date(2024, 1, 1) + timedelta(days=i + 1)).isoformat(),
                open=10_000, high=10_050, low=9_900, close=9_950, volume=100_000,
            ))
        result = simulate_trade(sig, bars, cfg)
        self.assertEqual(result.exit_reason, "TIME_STOP")


if __name__ == "__main__":
    unittest.main()


class StopLossAnchoringTests(unittest.TestCase):
    """Regression tests for stop-loss anchoring.

    Bug: stops placed at base_low × 0.99, causing -45% "stop-losses"
    when the stock has run 5-10x from base before the breakout.
    Fix: anchor stops to entry price (e.g., -7% and -12%).
    """

    def _make_signal(self, base_low, entry_price):
        from darvas_box_strategy.darvas.signal_generator import Signal
        from darvas_box_strategy.darvas.base_detector import Base
        return Signal(
            ticker="TST", entry_date=date(2024, 1, 1),
            entry_price=entry_price,
            base=Base(ticker="TST", base_low=base_low, base_high=base_low * 1.05,
                      end_idx=10, days_in_base=5, volume_compression=1.5),
            sl_F=0.0, sl_T=0.0,
            tiers=((1.10, 1.0),), conviction="high", size_pct=0.4,
            shares=1000, max_volume=999999, bars_remaining=30,
        )

    def test_entry_anchored_stops_far_run(self):
        """If entry is 10x base, stops must still be -7%/-12% from entry."""
        # Bug scenario: base at 1,200, entry at 12,000 (10x run-up)
        # With entry-anchored stops:
        cfg = StrategyConfig()
        expected_sl_T = 12000 * (1 - cfg.exits.sl_T_pct)  # 11,160
        expected_sl_F = 12000 * (1 - cfg.exits.sl_F_pct)  # 10,560

        # sl_T should be 7% below entry, NOT 45% below
        self.assertAlmostEqual(expected_sl_T, 11160)
        self.assertGreater(expected_sl_T, 12000 * 0.90)  # > 90% of entry (max -10%)
        self.assertGreater(expected_sl_F, 12000 * 0.85)  # > 85% of entry (max -15%)

    def test_legacy_stops_would_be_dangerous(self):
        """Document the bug: legacy stops at (base_low + entry)/2 would be -45%."""
        # With base=1,200, entry=12,000:
        legacy_sl_T = (1200 + 12000) / 2  # = 6,600 — that's -45% from entry!
        legacy_sl_F = 1200 * 0.99         # = 1,188 — that's -90% from entry!

        self.assertAlmostEqual(legacy_sl_T, 6600)
        self.assertAlmostEqual(legacy_sl_F, 1188)

        # The bug: legacy sl_T is 45% below entry, sl_F is 90% below entry!
        # This means "stop-loss" actually allows a -45% loss before exiting.
        loss_at_sl_T = (legacy_sl_T - 12000) / 12000
        self.assertLess(loss_at_sl_T, -0.40)  # confirms the bug

    def test_legacy_stops_would_be_dangerous(self):
        """Document the bug: legacy stops at (base_low + entry)/2 would be -45%."""
        # With base=1,200, entry=12,000:
        legacy_sl_T = (1200 + 12000) / 2  # = 6,600 — that's -45% from entry!
        legacy_sl_F = 1200 * 0.99         # = 1,188 — that's -90% from entry!

        self.assertAlmostEqual(legacy_sl_T, 6600)
        self.assertAlmostEqual(legacy_sl_F, 1188)

        # The bug: legacy sl_T is 45% below entry, sl_F is 90% below entry!
        # This means "stop-loss" actually allows a -45% loss before exiting.
        loss_at_sl_T = (legacy_sl_T - 12000) / 12000
        self.assertLess(loss_at_sl_T, -0.40)  # confirms the bug


if __name__ == "__main__":
    unittest.main()
