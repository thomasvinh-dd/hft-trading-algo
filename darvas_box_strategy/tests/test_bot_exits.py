"""Tests for the live exit-event evaluator and position tracker.

These cover the Telegram notification path for TIER / TRAIL / SL_SCALE /
SL_HARD events — the exit notifications that were previously missing
entirely (the bot only had a breakout-scan command).
"""
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from darvas_box_strategy.bot.exits import evaluate_exits
from darvas_box_strategy.bot.positions import (
    LivePosition,
    find_position_for_ticker,
    load_state,
    mark_notified,
    position_from_signal,
    save_state,
    upsert_position,
)


def _cfg(t_plus=0):
    from darvas_box_strategy.darvas.config import (
        StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig,
        ExitsConfig, TrailingConfig, UniverseConfig,
    )
    return StrategyConfig(
        base=BaseConfig(),
        breakout=BreakoutConfig(),
        exits=ExitsConfig(t_plus_n_bars=t_plus),
        trailing=TrailingConfig(trail_from_peak_pct=0.10),
        universe=UniverseConfig(),
        backtest=BacktestConfig(start_nav=1e9, cost_bps=0.0, board_lot=100),
    )


def _bar(d, o, h, l, c, v=100_000):
    from darvas_box_strategy.darvas.data_loader import PriceBar
    return PriceBar(time=d.isoformat(), open=o, high=h, low=l, close=c, volume=v)


def _pos(**kw):
    base = dict(
        ticker="X", entry_date="2026-01-02", entry_price=10_000.0,
        shares=1_000, remaining=1_000, sl_T=9_000.0, sl_F=8_200.0,
        tiers=[[1.10, 0.30], [1.20, 0.30]],
        peak_close=10_000.0, last_close=10_000.0,
    )
    base.update(kw)
    return LivePosition(**base)


class ExitEventTests(unittest.TestCase):
    """The exit ladder must fire the same events the backtest does."""

    def test_tier_fires_when_price_reaches_target(self):
        pos = _pos()
        bars = [_bar(date(2026, 1, 3), 10_000, 11_500, 9_900, 11_400)]
        events = evaluate_exits(pos, bars, _cfg())
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.kind, "TIER")
        self.assertEqual(ev.shares, 300)      # 30% of 1000
        self.assertEqual(ev.price, 11_000.0)   # 10% above entry
        self.assertEqual(ev.remaining_after, 700)
        self.assertEqual(ev.tier_index, 0)

    def test_tier_cut_respects_board_lot(self):
        """A 13% tier on 1000 shares = 130 → rounds down to 100."""
        pos = _pos(remaining=1_000, shares=1_000,
                   tiers=[[1.10, 0.13]])
        bars = [_bar(date(2026, 1, 3), 10_000, 11_500, 9_900, 11_400)]
        events = evaluate_exits(pos, bars, _cfg())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].shares, 100)
        self.assertEqual(events[0].remaining_after, 900)

    def test_tier_sells_all_when_cut_would_strand_lot(self):
        """A 5% tier on 1000 shares = 50 → < 1 lot → sell all 1000."""
        pos = _pos(remaining=1_000, shares=1_000, tiers=[[1.10, 0.05]])
        bars = [_bar(date(2026, 1, 3), 10_000, 11_500, 9_900, 11_400)]
        events = evaluate_exits(pos, bars, _cfg())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].shares, 1_000)
        self.assertEqual(events[0].remaining_after, 0)

    def test_no_tier_when_price_below_target(self):
        pos = _pos()
        bars = [_bar(date(2026, 1, 3), 10_000, 10_500, 9_900, 10_200)]
        events = evaluate_exits(pos, bars, _cfg())
        self.assertEqual(events, [])

    def test_trail_fires_after_tier_activated(self):
        """Trail activates on TP1, then fires when price drops 10% from peak.

        The bar gaps through the trail: peak 12,000 → trail threshold
        10,800, but the bar's low is 10,500. We cannot sell above the
        market, so the fill is 10,500 — the bar's low.
        """
        pos = _pos(remaining=700, tier_idx=1, trailing_active=True,
                   peak_close=12_000.0, last_close=12_000.0)
        bars = [_bar(date(2026, 1, 3), 12_000, 12_000, 10_500, 10_600)]
        events = evaluate_exits(pos, bars, _cfg())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "TRAIL")
        self.assertEqual(events[0].shares, 700)
        self.assertEqual(events[0].price, 10_500.0)

    def test_trail_fills_at_the_trail_price_not_the_close(self):
        """A trail that is not gapped fills at the trail price.

        Regression: the trail used to fill at the bar's CLOSE, which gave
        away more than the stop width on any close below the threshold.
        Peak 12,000 → trail threshold 10,800. The bar's low is 10,900 and
        its close is 10,700 — the close is below the threshold but the
        market never traded that low, so the stop was touched intraday
        at 10,800 and that is where the fill belongs.
        """
        pos = _pos(remaining=700, tier_idx=1, trailing_active=True,
                   peak_close=12_000.0, last_close=12_000.0)
        bars = [_bar(date(2026, 1, 3), 12_000, 12_000, 10_900, 10_700)]
        events = evaluate_exits(pos, bars, _cfg())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "TRAIL")
        # 12,000 * (1 - 0.10) = 10,800 — not the 10,700 close.
        self.assertEqual(events[0].price, 10_800.0)

    def test_trail_not_active_before_first_tier(self):
        """Trail is inactive until TP1 is hit — no TRAIL event yet."""
        pos = _pos(trailing_active=False, peak_close=12_000.0)
        bars = [_bar(date(2026, 1, 3), 12_000, 12_000, 10_500, 10_600)]
        events = evaluate_exits(pos, bars, _cfg())
        kinds = [e.kind for e in events]
        self.assertNotIn("TRAIL", kinds)
        # No SL either (price 10600 is above sl_T 9000)
        self.assertNotIn("SL_SCALE", kinds)
        self.assertNotIn("SL_HARD", kinds)

    def test_sl_scale_fires_on_partial_stop(self):
        pos = _pos()
        # low touches sl_T (9000), close 9050 (above sl_T)
        bars = [_bar(date(2026, 1, 3), 10_000, 10_200, 8_900, 9_050)]
        events = evaluate_exits(pos, bars, _cfg())
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.kind, "SL_SCALE")
        self.assertEqual(ev.shares, 500)      # 50% of 1000
        self.assertEqual(ev.price, 9_000.0)   # sl_T
        self.assertEqual(ev.remaining_after, 500)

    def test_sl_hard_fires_when_price_breaks_hard_stop(self):
        """A bar that breaks sl_F triggers the whole ladder, ending in SL_HARD.

        This mirrors the executor: sl_T → sl_mid → sl_F are checked in
        order and all can fire on the same bar.
        """
        pos = _pos()
        # low breaks sl_F (8200)
        bars = [_bar(date(2026, 1, 3), 10_000, 10_100, 8_100, 8_150)]
        events = evaluate_exits(pos, bars, _cfg())
        kinds = [e.kind for e in events]
        self.assertEqual(kinds, ["SL_SCALE", "SL_SCALE", "SL_HARD"])
        # sl_T cut: 50% of 1000 = 500
        self.assertEqual(events[0].shares, 500)
        self.assertEqual(events[0].price, 9_000.0)
        # sl_mid cut: 50% of 500 = 250 → rounded down to 200
        self.assertEqual(events[1].shares, 200)
        self.assertEqual(events[1].price, 8_600.0)
        # sl_HARD closes the rest
        self.assertEqual(events[2].shares, 300)
        self.assertEqual(events[2].price, 8_200.0)
        self.assertEqual(events[2].remaining_after, 0)

    def test_sl_scale_fires_before_sl_hard(self):
        """Order matters: the partial stop is taken first."""
        pos = _pos()
        bars = [_bar(date(2026, 1, 3), 10_000, 10_100, 8_100, 8_150)]
        events = evaluate_exits(pos, bars, _cfg())
        self.assertEqual([e.kind for e in events],
                         ["SL_SCALE", "SL_SCALE", "SL_HARD"])
        # And then SL_HARD closes the rest.
        self.assertEqual(events[-1].remaining_after, 0)

    def test_t_plus_25_blocks_exits(self):
        """T+2.5: no exit events during the settlement lock-up."""
        pos = _pos()
        # Would hit sl_T immediately
        bars = [_bar(date(2026, 1, 3), 10_000, 10_100, 8_100, 8_150)]
        events = evaluate_exits(pos, bars, _cfg(t_plus=3))
        self.assertEqual(events, [])
        # bars_held should still have advanced
        self.assertEqual(pos.bars_held, 1)

    def test_t_plus_25_allows_exit_on_third_bar(self):
        pos = _pos()
        bars = [
            _bar(date(2026, 1, 3), 10_000, 10_100, 8_100, 8_150),
            _bar(date(2026, 1, 4), 10_000, 10_100, 8_100, 8_150),
            _bar(date(2026, 1, 5), 10_000, 10_100, 8_100, 8_150),
        ]
        events = evaluate_exits(pos, bars, _cfg(t_plus=3))
        self.assertTrue(events)
        self.assertEqual(events[-1].kind, "SL_HARD")

    def test_dedupe_key_blocks_repeat_notification(self):
        pos = _pos()
        bars = [_bar(date(2026, 1, 3), 10_000, 11_500, 9_900, 11_400)]
        events = evaluate_exits(pos, bars, _cfg())
        self.assertEqual(len(events), 1)
        key = events[0].dedupe_key
        # Second evaluation with the same dedupe set → no new events
        pos2 = _pos()
        events2 = evaluate_exits(pos2, bars, _cfg(), already_notified={key})
        self.assertEqual(events2, [])

    def test_peak_close_tracks_highest_close(self):
        pos = _pos()
        bars = [_bar(date(2026, 1, 3), 10_000, 12_500, 9_900, 12_400)]
        evaluate_exits(pos, bars, _cfg())
        self.assertEqual(pos.peak_close, 12_400.0)
        self.assertEqual(pos.last_close, 12_400.0)

    def test_position_state_is_updated(self):
        pos = _pos()
        bars = [_bar(date(2026, 1, 3), 10_000, 11_500, 9_900, 11_400)]
        evaluate_exits(pos, bars, _cfg())
        self.assertEqual(pos.remaining, 700)
        self.assertEqual(pos.tier_idx, 1)
        self.assertTrue(pos.trailing_active)
        self.assertEqual(pos.bars_held, 1)
        self.assertGreater(pos.cumulative_pnl, 0)


class PositionStateTests(unittest.TestCase):
    """Position state must survive between cron runs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "positions.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_load_roundtrip(self):
        positions = {}
        upsert_position(positions, _pos())
        save_state(positions, self.path)
        loaded = load_state(self.path)
        self.assertEqual(len(loaded), 1)
        pos = list(loaded.values())[0]
        self.assertEqual(pos.ticker, "X")
        self.assertEqual(pos.shares, 1_000)
        self.assertEqual(pos.tiers, [[1.10, 0.30], [1.20, 0.30]])

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(load_state(self.tmp / "nope.json"), {})

    def test_load_corrupt_file_returns_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_state(self.path), {})

    def test_notified_dedupe_list(self):
        pos = _pos()
        mark_notified(pos, "TIER:0")
        self.assertIn("TIER:0", pos.notified)
        # Marking twice doesn't duplicate
        mark_notified(pos, "TIER:0")
        self.assertEqual(pos.notified.count("TIER:0"), 1)

    def test_find_position_for_ticker_picks_latest(self):
        positions = {}
        upsert_position(positions, _pos(entry_date="2026-01-02"))
        upsert_position(positions, _pos(entry_date="2026-03-04"))
        found = find_position_for_ticker(positions, "X")
        self.assertEqual(found.entry_date, "2026-03-04")
        self.assertIsNone(find_position_for_ticker(positions, "ZZZ"))

    def test_position_from_signal(self):
        from darvas_box_strategy.darvas.base_detector import Base
        from darvas_box_strategy.darvas.signal_generator import Signal
        b = Base(ticker="X", base_low=9_800, base_high=10_000, end_idx=0,
                 days_in_base=10, volume_compression=1.0)
        sig = Signal(
            ticker="X", signal_date=date(2026, 1, 1), entry_date=date(2026, 1, 2),
            entry_price=10_000.0, base=b, sl_F=8_200.0, sl_T=9_000.0,
            tiers=((1.063, 0.10), (1.125, 0.30)), conviction="low",
            size_pct=0.15, shares=1_000, max_volume=99_999, bars_remaining=10,
        )
        pos = position_from_signal(sig)
        self.assertEqual(pos.ticker, "X")
        self.assertEqual(pos.entry_date, "2026-01-02")
        self.assertEqual(pos.shares, 1_000)
        self.assertEqual(pos.remaining, 1_000)
        self.assertEqual(pos.tiers, [[1.063, 0.10], [1.125, 0.30]])

class ExitMessageTests(unittest.TestCase):
    """The Telegram messages must show the right sign and labels."""

    def test_tier_message_contains_ticker_and_shares(self):
        from darvas_box_strategy.bot.messages import format_exit_event
        pos = _pos()
        from darvas_box_strategy.bot.exits import ExitEvent
        ev = ExitEvent(kind="TIER", shares=300, price=11_000.0,
                       remaining_after=700, pnl_delta=300_000.0,
                       cumulative_pnl=300_000.0, tier_index=0)
        msg = format_exit_event(pos, ev)
        self.assertIn("X", msg)
        self.assertIn("TIER", msg)
        self.assertIn("300", msg)
        self.assertIn("Take-profit", msg)

    def test_sl_hard_message_labels_correctly(self):
        from darvas_box_strategy.bot.messages import format_exit_event
        from darvas_box_strategy.bot.exits import ExitEvent
        pos = _pos()
        ev = ExitEvent(kind="SL_HARD", shares=1_000, price=8_200.0,
                       remaining_after=0, pnl_delta=-1_800_000.0,
                       cumulative_pnl=-1_800_000.0)
        msg = format_exit_event(pos, ev)
        self.assertIn("Hard stop", msg)
        self.assertIn("closed", msg.lower())


class ExecutorParityTests(unittest.TestCase):
    """The Telegram exit path must agree with the backtest executor.

    This is the critical guarantee: whatever the dashboard shows for a
    deal (TIER / TRAIL / SL_SCALE / SL_HARD) must be the SAME event the
    Telegram notification reports — otherwise the bot would tell the
    user to sell at the wrong time or the wrong size.
    """


    def _run_both(self, ticker, entry_date, entry_price, shares, tiers,
                  sl_pct_t=0.10, sl_pct_f=0.18):
        from darvas_box_strategy.bot.cli import _bars_after_entry
        from darvas_box_strategy.bot.exits import evaluate_exits
        from darvas_box_strategy.bot.positions import LivePosition
        from darvas_box_strategy.darvas.base_detector import Base
        from darvas_box_strategy.darvas.executor import simulate_trade
        from darvas_box_strategy.darvas.signal_generator import Signal

        cfg = _cfg()
        bars = _bars_after_entry(ticker, entry_date)
        if not bars:
            self.skipTest(f"no price data for {ticker}")

        # --- backtest path ---
        base = Base(ticker=ticker, base_low=entry_price * 0.85,
                    base_high=entry_price * 0.95, end_idx=0,
                    days_in_base=10, volume_compression=1.0)
        sig = Signal(
            ticker=ticker, signal_date=date(2025, 12, 29),
            entry_date=date.fromisoformat(entry_date),
            entry_price=entry_price, base=base,
            sl_F=entry_price * (1 - sl_pct_f), sl_T=entry_price * (1 - sl_pct_t),
            tiers=tiers, conviction="low", size_pct=0.15, shares=shares,
            max_volume=99_999, bars_remaining=200,
        )
        result = simulate_trade(sig, bars, cfg)
        bt = [(f.kind, abs(f.shares), round(f.level, 2)) for f in result.fills
              if f.kind != "DIVIDEND"]

        # --- bot path ---
        pos = LivePosition(
            ticker=ticker, entry_date=entry_date, entry_price=entry_price,
            shares=shares, remaining=shares, size_pct=0.15,
            sl_T=entry_price * (1 - sl_pct_t), sl_F=entry_price * (1 - sl_pct_f),
            tiers=[[m, p] for m, p in tiers],
            conviction="low", peak_close=entry_price, last_close=entry_price,
        )
        already = set()
        bot = []
        # Feed the bars one at a time — the way cmd_scan_exits does it
        # on each cron run (one new bar per day).
        for b in bars:
            for ev in evaluate_exits(pos, [b], cfg, already_notified=already):
                already.add(ev.dedupe_key)
                bot.append((ev.kind, ev.shares, round(ev.price, 2)))
            if pos.remaining <= 0:
                break
        return bt, bot

    def test_pow_2025_12_30_matches_executor(self):
        """Real POW trade: TP1, TP2, TP3, then trail out."""
        bt, bot = self._run_both(
            "POW", "2025-12-30", 11_500.0, 11_500,
            ((1.063, 0.10), (1.125, 0.30), (1.210, 0.30)),
        )
        self.assertTrue(bt, "backtest produced no fills")
        self.assertEqual(
            [(k, s) for k, s, _ in bt], [(k, s) for k, s, _ in bot],
            f"exit events differ:\n  backtest={bt}\n  bot={bot}",
        )
        self.assertEqual(
            [p for _, _, p in bt], [p for _, _, p in bot],
            f"fill prices differ:\n  backtest={bt}\n  bot={bot}",
        )

    def test_multi_bar_single_call_matches_executor(self):
        """Feeding ALL bars in one call must give the same result.

        This is the path used when a position is first seeded and the
        scanner walks its full history.
        """
        from darvas_box_strategy.bot.cli import _bars_after_entry
        from darvas_box_strategy.bot.exits import evaluate_exits
        from darvas_box_strategy.bot.positions import LivePosition
        from darvas_box_strategy.darvas.base_detector import Base
        from darvas_box_strategy.darvas.executor import simulate_trade
        from darvas_box_strategy.darvas.signal_generator import Signal

        cfg = _cfg()
        ticker, entry_date = "POW", "2025-12-30"
        entry_price, shares = 11_500.0, 11_500
        tiers = ((1.063, 0.10), (1.125, 0.30), (1.210, 0.30))
        bars = _bars_after_entry(ticker, entry_date)
        if not bars:
            self.skipTest("no price data for POW")

        base = Base(ticker=ticker, base_low=entry_price * 0.85,
                    base_high=entry_price * 0.95, end_idx=0,
                    days_in_base=10, volume_compression=1.0)
        sig = Signal(
            ticker=ticker, signal_date=date(2025, 12, 29),
            entry_date=date.fromisoformat(entry_date),
            entry_price=entry_price, base=base,
            sl_F=entry_price * 0.82, sl_T=entry_price * 0.90,
            tiers=tiers, conviction="low", size_pct=0.15, shares=shares,
            max_volume=99_999, bars_remaining=200,
        )
        result = simulate_trade(sig, bars, cfg)
        bt = [(f.kind, abs(f.shares), round(f.level, 2)) for f in result.fills
              if f.kind != "DIVIDEND"]

        pos = LivePosition(
            ticker=ticker, entry_date=entry_date, entry_price=entry_price,
            shares=shares, remaining=shares, size_pct=0.15,
            sl_T=entry_price * 0.90, sl_F=entry_price * 0.82,
            tiers=[[m, p] for m, p in tiers],
            conviction="low", peak_close=entry_price, last_close=entry_price,
        )
        evs = evaluate_exits(pos, bars, cfg)
        bot = [(ev.kind, ev.shares, round(ev.price, 2)) for ev in evs]

        self.assertEqual(
            [(k, s) for k, s, _ in bt], [(k, s) for k, s, _ in bot],
            f"exit events differ:\n  backtest={bt}\n  bot={bot}",
        )

    def test_trail_exit_matches_executor(self):
        """A position that exits via the trailing stop."""
        bt, bot = self._run_both(
            "MSN", "2025-10-17", 34_000.0, 3_400,
            ((1.063, 0.10), (1.125, 0.30), (1.210, 0.30)),
        )
        if not bt:
            self.skipTest("no fills for MSN trade")
        self.assertEqual(
            [(k, s) for k, s, _ in bt], [(k, s) for k, s, _ in bot],
            f"exit events differ:\n  backtest={bt}\n  bot={bot}",
        )


class DryRunStateTests(unittest.TestCase):
    """A --dry-run must never consume an event.

    Regression: `scan-exits --dry-run` used to mark the detected
    events as notified and persist the state file. The follow-up real
    run then found no new events and sent nothing — the operator
    never got the notification.
    """

    def _run_scan_exits(self, state_path, dry_run):
        """Run cmd_scan_exits against a temp state file + real data."""
        import darvas_box_strategy.bot.cli as cli_mod
        from darvas_box_strategy.bot import positions as pos_mod

        # Point the CLI at our temp state file.
        orig_state = cli_mod.STATE_PATH
        cli_mod.STATE_PATH = state_path
        sent = []

        def fake_send(config, text, **kw):
            sent.append(text)
            return {"ok": True}

        orig_send = cli_mod.send_telegram
        cli_mod.send_telegram = fake_send
        try:
            class Args:
                pass
            a = Args()
            a.dry_run = dry_run
            rc = cli_mod.cmd_scan_exits(a)
        finally:
            cli_mod.send_telegram = orig_send
            cli_mod.STATE_PATH = orig_state
        return rc, sent

    def test_dry_run_does_not_consume_events(self):
        import tempfile
        import shutil
        from pathlib import Path
        from darvas_box_strategy.bot.positions import LivePosition, save_state

        tmp = Path(tempfile.mkdtemp())
        try:
            state_path = tmp / "positions.json"
            # A position whose sl_T is above the last close, so a
            # partial stop fires on the final bar.
            positions = {
                "VNM_TEST": LivePosition(
                    ticker="VNM", entry_date="2026-08-10", entry_price=62_000.0,
                    shares=2_400, remaining=2_400, size_pct=0.15,
                    sl_T=60_000.0, sl_F=58_000.0,
                    tiers=[[1.063, 0.10], [1.125, 0.30], [1.210, 0.30]],
                    conviction="low", peak_close=64_000.0, last_close=58_200.0,
                ),
            }
            save_state(positions, state_path)

            rc, sent = self._run_scan_exits(state_path, dry_run=True)
            self.assertEqual(rc, 0)
            # Dry run sent nothing over the wire.
            self.assertEqual(sent, [])

            # The state file must still contain the position, and the
            # 'notified' list must be EMPTY so the real run can notify.
            reloaded = load_state(state_path)
            self.assertIn("VNM_TEST", reloaded,
                          "dry-run removed the position from the state file")
            self.assertEqual(reloaded["VNM_TEST"].notified, [],
                             "dry-run marked events as notified")

            # Now the real run must actually notify.
            rc2, sent2 = self._run_scan_exits(state_path, dry_run=False)
            self.assertEqual(rc2, 0)
            self.assertEqual(len(sent2), 1, "real run sent no notification")
            self.assertIn("Partial stop", sent2[0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
