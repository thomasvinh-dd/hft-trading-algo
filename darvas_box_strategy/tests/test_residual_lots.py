"""Regression tests for the residual-lot bug in partial exits.

Incident (2026-11-07): the user asked why the ORS_2026-03-12 deal
carried a MARK event on 2026-11-04.

Root cause: repeated 50% scale-outs converge on a small stub that
has no exit left. ORS walked

    10,700 -> 5,400 -> 2,700 -> 1,400 -> 700 -> 400 -> 200

and then stopped. The last 200 shares were stranded because:

* the profit tiers were never reached (ORS never traded at 1.063x =
  14,882), so the trailing stop never armed;
* the price recovered and never revisited sl_T (12,600), sl_mid
  (12,040) or sl_F (11,480) again.

The stub therefore stayed OPEN for 8 months, blocking every later ORS
signal and showing up on the dashboard as a mark-to-market row for a
position that could not be exited.

Fix: when a partial exit would leave a residue below
``ExitsConfig.min_residual_lots`` board lots, liquidate the WHOLE
remaining position at that price instead of leaving a stub.

These tests cover the shared helper and all three code paths that
implement partial exits:
  - darvas_box_strategy/darvas/executor.py  (backtest / dashboard)
  - darvas_box_strategy/darvas/portfolio.py (authoritative live path)
  - darvas_box_strategy/bot/exits.py         (Telegram exit scanner)
"""
import unittest
from datetime import date

from darvas_box_strategy.darvas.executor import _scale_out_cut


class ScaleOutCutTests(unittest.TestCase):
    """The shared helper decides how much a partial exit sells."""

    def test_cut_is_rounded_down_to_board_lot(self):
        # 10,000 * 15% = 1,500 -> exactly 15 lots.
        self.assertEqual(_scale_out_cut(10_000, 0.15, 100, 0), 1_500)
        # 10,000 * 17% = 1,700 -> 17 lots.
        self.assertEqual(_scale_out_cut(10_000, 0.17, 100, 0), 1_700)

    def test_always_leaves_at_least_one_lot(self):
        """A cut that would leave < 1 lot sells everything."""
        # 300 * 50% = 150 -> rounds to 100, leaves 200 (2 lots) -> OK.
        self.assertEqual(_scale_out_cut(300, 0.5, 100, 0), 100)
        # 200 * 50% = 100 -> leaves 100 (1 lot) -> OK.
        self.assertEqual(_scale_out_cut(200, 0.5, 100, 0), 100)
        # 100 * 50% = 50 -> rounds to 0 -> sell everything.
        self.assertEqual(_scale_out_cut(100, 0.5, 100, 0), 100)

    def test_residue_below_threshold_liquidates_everything(self):
        """The bug: 400 shares, 50% cut, leaves 200 (2 lots)."""
        # With the rule OFF the old behaviour is kept.
        self.assertEqual(_scale_out_cut(400, 0.5, 100, 0), 200)
        # With min_residual_lots=2 a residue of 2 lots is too small to
        # carry its own exit -> liquidate the whole position.
        self.assertEqual(_scale_out_cut(400, 0.5, 100, 2), 400)
        # 3 lots is still fine -> keep the partial cut.
        self.assertEqual(_scale_out_cut(600, 0.5, 100, 2), 300)

    def test_residue_at_or_above_threshold_keeps_the_cut(self):
        """A residue big enough to keep trading is left alone."""
        # 1,000 shares, 50% cut -> leaves 500 (5 lots) -> keep the cut.
        self.assertEqual(_scale_out_cut(1_000, 0.5, 100, 2), 500)
        # 2,000 shares, 50% cut -> leaves 1,000 (10 lots) -> keep.
        self.assertEqual(_scale_out_cut(2_000, 0.5, 100, 2), 1_000)

    def test_zero_threshold_disables_the_rule(self):
        """0 = keep the old stub-leaving behaviour."""
        # 400 shares, 50% cut -> 200 sold, 200 left (the old behaviour).
        self.assertEqual(_scale_out_cut(400, 0.5, 100, 0), 200)
        # 700 shares, 50% cut -> 300 sold, 400 left.
        self.assertEqual(_scale_out_cut(700, 0.5, 100, 0), 300)
        # A fractional lot is still never stranded: 150 shares, 50% cut
        # rounds to 0 -> the whole (fractional) position is closed.
        self.assertEqual(_scale_out_cut(150, 0.5, 100, 0), 150)
        self.assertEqual(_scale_out_cut(100, 0.5, 100, 0), 100)

    def test_never_sells_more_than_remaining(self):
        for rem in (0, 100, 500, 10_000):
            cut = _scale_out_cut(rem, 0.5, 100, 2)
            self.assertLessEqual(cut, rem)
            self.assertGreaterEqual(cut, 0)

    def test_zero_remaining_sells_nothing(self):
        self.assertEqual(_scale_out_cut(0, 0.5, 100, 2), 0)


class ExitsConfigDefaultTests(unittest.TestCase):
    """The rule must be ON by default."""

    def test_default_min_residual_lots_is_2(self):
        from darvas_box_strategy.darvas.config import ExitsConfig
        self.assertEqual(ExitsConfig().min_residual_lots, 2)

    def test_build_default_config_keeps_the_rule(self):
        from darvas_box_strategy.bot.cli import build_default_config
        cfg = build_default_config()
        self.assertEqual(cfg.exits.min_residual_lots, 2)


class ExecutorResidueTests(unittest.TestCase):
    """The backtest executor must not strand an un-exitable stub."""

    def _cfg(self, **exits_kwargs):
        from darvas_box_strategy.darvas.config import (
            StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig,
            ExitsConfig, TrailingConfig, UniverseConfig,
        )
        return StrategyConfig(
            base=BaseConfig(),
            breakout=BreakoutConfig(require_ma200=False, require_close_above_ma=0),
            exits=ExitsConfig(
                tiers=(),  # no profit targets -> the trail never arms
                sl_T_pct=0.10, sl_F_pct=0.18,
                scale_out_pct_at_sl_T=0.5,
                scale_out_pct_at_midpoint=0.5,
                t_plus_n_bars=0,
                **exits_kwargs,
            ),
            trailing=TrailingConfig(trail_from_peak_pct=0.99),  # never trails
            universe=UniverseConfig(),
            backtest=BacktestConfig(
                start_nav=1e9, cost_bps=0.0, board_lot=100,
                size_low=0.15, size_high=0.30,
            ),
        )

    def _signal(self, shares):
        from darvas_box_strategy.darvas.base_detector import Base
        from darvas_box_strategy.darvas.signal_generator import Signal
        base = Base(ticker="X", base_low=9800, base_high=10000, end_idx=0,
                    days_in_base=5, volume_compression=1.0)
        return Signal(
            ticker="X", signal_date=date(2026, 1, 1), entry_date=date(2026, 1, 2),
            entry_price=10000.0, base=base, sl_F=8200, sl_T=9000,
            tiers=(), conviction="low", size_pct=0.15,
            shares=shares, max_volume=99999, bars_remaining=10,
        )

    def _ors_bars(self):
        """Reproduce the exact ORS_2026-03-12 bar sequence.

        ORS: entry 14,000 with 10,700 shares, sl_T 12,600, sl_mid 12,040,
        sl_F 11,480. The five stop-hit bars walk the position down

            10,700 -> 5,400 -> 2,700 -> 1,400 -> 700 -> 400 -> 200

        and then the price recovers above 12,600 and never revisits a
        stop, so the 200-share stub has no exit left.
        """
        from darvas_box_strategy.darvas.data_loader import PriceBar
        bars = [
            # low 12,550 -> sl_T only (above sl_mid 12,040)
            PriceBar(time=date(2026, 1, 3).isoformat(), open=14_000, high=14_000,
                     low=12_550, close=13_000, volume=100_000),
            # low 11,900 -> sl_T AND sl_mid
            PriceBar(time=date(2026, 1, 4).isoformat(), open=13_000, high=13_000,
                     low=11_900, close=12_200, volume=100_000),
            # low 12,500 -> sl_T only
            PriceBar(time=date(2026, 1, 5).isoformat(), open=12_500, high=12_500,
                     low=12_500, close=12_500, volume=100_000),
            # low 12,500 -> sl_T only
            PriceBar(time=date(2026, 1, 6).isoformat(), open=12_500, high=12_500,
                     low=12_500, close=12_500, volume=100_000),
            # low 12,550 -> sl_T only. 50% of 400 = 200 -> leaves 200.
            PriceBar(time=date(2026, 1, 7).isoformat(), open=12_550, high=12_550,
                     low=12_550, close=12_550, volume=100_000),
        ]
        # The price recovers and never revisits 12,600 again.
        for i in range(6, 20):
            bars.append(PriceBar(
                time=date(2026, 1, 3 + i).isoformat(), open=13_000, high=13_400,
                low=12_900, close=13_300, volume=100_000))
        return bars

    def test_position_closes_instead_of_stranding_a_stub(self):
        """The exact ORS shape: 10,700 shares, repeated 50% cuts, then the
        price recovers and never revisits a stop."""
        from darvas_box_strategy.darvas.executor import simulate_trade

        cfg = self._cfg()
        sig = self._signal(10_700)
        # ORS stops: sl_T 12,600 (10%), sl_F 11,480 (18%).
        sig = sig.__class__(
            ticker=sig.ticker, signal_date=sig.signal_date, entry_date=sig.entry_date,
            entry_price=14_000.0, base=sig.base, sl_F=11_480, sl_T=12_600,
            tiers=(), conviction=sig.conviction, size_pct=sig.size_pct,
            shares=sig.shares, max_volume=sig.max_volume,
            bars_remaining=sig.bars_remaining,
        )

        result = simulate_trade(sig, self._ors_bars(), cfg)

        self.assertEqual(
            result.exit_reason, "SL_SCALE",
            "the final scale-out should close the position, not leave a stub",
        )
        self.assertEqual(
            result.fills[-1].remaining, 0,
            "no shares may be stranded after the final cut",
        )
        self.assertEqual(
            sum(f.shares for f in result.fills if f.kind != "DIVIDEND"),
            -10_700,
            "every share must have been sold",
        )
        self.assertNotIn(
            "EXHAUST",
            [f.kind for f in result.fills],
            "EXHAUST means the position was still open at the end of the "
            "data — the stub was stranded",
        )

    def test_old_behaviour_would_strand_a_stub(self):
        """Guard the test itself: with the rule OFF the stub survives.

        This documents exactly what the fix changes.
        """
        from darvas_box_strategy.darvas.executor import simulate_trade

        cfg = self._cfg(min_residual_lots=0)
        sig = self._signal(10_700)
        sig = sig.__class__(
            ticker=sig.ticker, signal_date=sig.signal_date, entry_date=sig.entry_date,
            entry_price=14_000.0, base=sig.base, sl_F=11_480, sl_T=12_600,
            tiers=(), conviction=sig.conviction, size_pct=sig.size_pct,
            shares=sig.shares, max_volume=sig.max_volume,
            bars_remaining=sig.bars_remaining,
        )

        result = simulate_trade(sig, self._ors_bars(), cfg)

        # The last real fill leaves a 200-share stub behind ...
        scale_fills = [f for f in result.fills if f.kind == "SL_SCALE"]
        self.assertEqual(
            scale_fills[-1].remaining, 200,
            "without the rule a 200-share stub is left behind (the old bug)",
        )
        # ... and the position is only closed by the synthetic EXHAUST
        # mark-to-market fill at the end of the data.
        self.assertEqual(result.exit_reason, "EXHAUST")
        self.assertEqual(
            [f.kind for f in result.fills][-1], "EXHAUST",
            "the stub could only be closed by the end-of-data EXHAUST",
        )

    def test_exact_ors_fill_sequence(self):
        """Pin the ORS fill sequence to catch any regression."""
        from darvas_box_strategy.darvas.executor import simulate_trade

        cfg = self._cfg(min_residual_lots=0)
        sig = self._signal(10_700)
        sig = sig.__class__(
            ticker=sig.ticker, signal_date=sig.signal_date, entry_date=sig.entry_date,
            entry_price=14_000.0, base=sig.base, sl_F=11_480, sl_T=12_600,
            tiers=(), conviction=sig.conviction, size_pct=sig.size_pct,
            shares=sig.shares, max_volume=sig.max_volume,
            bars_remaining=sig.bars_remaining,
        )
        result = simulate_trade(sig, self._ors_bars(), cfg)
        got = [(f.kind, f.shares, f.remaining) for f in result.fills]
        self.assertEqual(got, [
            ("SL_SCALE", -5300, 5400),
            ("SL_SCALE", -2700, 2700),
            ("SL_SCALE", -1300, 1400),
            ("SL_SCALE", -700, 700),
            ("SL_SCALE", -300, 400),
            ("SL_SCALE", -200, 200),   # <-- the stranded residue
            ("EXHAUST", -200, 0),      # <-- synthetic mark-to-market
        ])


class PortfolioResidueTests(unittest.TestCase):
    """The authoritative live path must apply the same rule."""

    def test_portfolio_uses_the_shared_helper(self):
        """The Portfolio's partial cuts must be board-lot rounded AND
        must liquidate a small residue — it previously used a raw
        ``int(remaining * pct)`` with no board-lot rounding at all."""
        import inspect
        from darvas_box_strategy.darvas import portfolio
        src = inspect.getsource(portfolio.Portfolio._apply_exits)
        self.assertIn("_scale_out_cut", src)
        self.assertNotIn("max(int(pos.remaining * pct), 0)", src)
        self.assertNotIn(
            "max(int(pos.remaining * exits_cfg.scale_out_pct_at_sl_T), 0)", src)
        self.assertNotIn(
            "max(int(pos.remaining * exits_cfg.scale_out_pct_at_midpoint), 0)", src)


class BotExitsResidueTests(unittest.TestCase):
    """The Telegram exit scanner must apply the same rule."""

    def test_bot_exits_uses_the_shared_helper(self):
        import inspect
        from darvas_box_strategy.bot import exits
        src = inspect.getsource(exits.evaluate_exits)
        self.assertIn("_scale_out_cut", src)
        self.assertNotIn("_round_down_lot(remaining * pct, lot)", src)

    def test_bot_exits_liquidates_a_small_residue(self):
        """End-to-end through the live exit evaluator."""
        from darvas_box_strategy.bot.cli import build_default_config
        from darvas_box_strategy.bot.exits import evaluate_exits
        from darvas_box_strategy.bot.positions import LivePosition
        from darvas_box_strategy.darvas.data_loader import PriceBar

        cfg = build_default_config()
        pos = LivePosition(
            ticker="X", entry_date="2026-01-02",
            entry_price=14_000.0, shares=10_700, remaining=10_700,
            sl_T=12_600.0, sl_F=11_480.0,
            tiers=[], tier_idx=0, trailing_active=False, trail_floor=0.0,
            peak_close=14_000.0, last_close=14_000.0, bars_held=0,
        )
        bars = []
        # T+2.5 settlement: the first 2 bars are locked for exits, so
        # the first two stop bars below are consumed by the lock-up.
        # The effective sequence is then:
        #   10,700 -> 5,400 -> 2,700 -> 1,400 -> 700 -> 400 -> 0
        stop_bars = [
            (12_550, 13_000),   # locked (settlement)
            (12_550, 13_000),   # locked (settlement)
            (12_550, 13_000),   # sl_T only (above sl_mid 12,040)
            (11_900, 12_200),   # sl_T + sl_mid
            (12_500, 12_500),   # sl_T only
            (12_500, 12_500),   # sl_T only
            (12_550, 12_550),   # sl_T only -> 50% of 400 = 200 -> residue
        ]
        for i, (low, close) in enumerate(stop_bars):
            bars.append(PriceBar(
                time=date(2026, 1, 3 + i).isoformat(), open=14_000, high=14_000,
                low=low, close=close, volume=100_000))
        # The price recovers and never revisits 12,600 again.
        for i in range(len(stop_bars), 20):
            bars.append(PriceBar(
                time=date(2026, 1, 3 + i).isoformat(), open=13_000, high=13_400,
                low=12_900, close=13_300, volume=100_000))

        events = evaluate_exits(pos, bars, cfg)
        # ExitEvent.shares is POSITIVE (shares to sell).
        sold = sum(e.shares for e in events if e.kind != "DIVIDEND")
        self.assertEqual(
            sold, 10_700,
            "the live evaluator must liquidate the whole position, not "
            "strand a stub (events: %s)" % [(e.kind, e.shares) for e in events],
        )
        self.assertEqual(pos.remaining, 0)
        self.assertTrue(
            all(e.remaining_after == 0 for e in events[-1:]),
            "the final event must leave nothing behind",
        )


if __name__ == "__main__":
    unittest.main()
