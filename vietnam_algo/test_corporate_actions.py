from __future__ import annotations

import sys
import unittest
from datetime import date as date_type
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.corporate_actions import (
    CorporateAction,
    build_corporate_action_lookup,
    non_cash_events_in_trading_window,
)


class FakeCorporateAction:
    def __init__(self, eventId, symbol, exDate, isNonCash=True):
        self.eventId = eventId
        self.symbol = symbol
        self.exDate = exDate
        self.isNonCash = isNonCash


def _real_corp(eventId, symbol, exDate, *, isNonCash=True, isCashOnly=False):
    return CorporateAction(
        eventId=eventId,
        eventTypeId=1,
        channelId=0,
        catId=0,
        symbol=symbol,
        companyName="Co",
        exchange="HOSE",
        exDate=exDate,
        recordDate=None,
        paymentDate=None,
        name="",
        note="",
        title="",
        fileUrl="",
        isCashOnly=isCashOnly,
        isNonCash=isNonCash,
    )


class NonCashEventsInWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trading_dates = [f"2026-01-{d:02d}" for d in range(5, 20)]  # 15 trading days

    def test_past_events_excluded(self) -> None:
        # event with ex-date already in the past
        actions = [_real_corp(1, "AAA", "2025-12-01")]
        result = non_cash_events_in_trading_window(actions, "2026-01-10", self.trading_dates)
        self.assertEqual(result, {})

    def test_future_event_inside_window(self) -> None:
        actions = [_real_corp(1, "AAA", "2026-01-12")]
        result = non_cash_events_in_trading_window(actions, "2026-01-10", self.trading_dates, window_days=3)
        self.assertIn("AAA", result)
        self.assertEqual(len(result["AAA"]), 1)

    def test_future_event_outside_window(self) -> None:
        actions = [_real_corp(1, "AAA", "2026-01-19")]
        result = non_cash_events_in_trading_window(actions, "2026-01-10", self.trading_dates, window_days=3)
        self.assertEqual(result, {})

    def test_cash_only_excluded(self) -> None:
        actions = [_real_corp(1, "AAA", "2026-01-12", isNonCash=False)]
        result = non_cash_events_in_trading_window(actions, "2026-01-10", self.trading_dates)
        self.assertEqual(result, {})

    def test_trade_date_not_in_trading_dates_falls_back_to_calendar(self) -> None:
        # trade_date not in trading_dates -> falls back to non_cash_events_in_window
        actions = [_real_corp(1, "AAA", "2026-01-12")]
        result = non_cash_events_in_trading_window(actions, "2026-01-12", self.trading_dates, window_days=3)
        # Calendar fallback says "12-12" -> days_until=0 (include_ex_date=True by default)
        self.assertIn("AAA", result)

    def test_ex_date_before_trading_dates_works(self) -> None:
        actions = [_real_corp(1, "AAA", "2025-06-01")]
        result = non_cash_events_in_trading_window(actions, "2025-12-01", self.trading_dates)
        # Far past, should be excluded
        self.assertEqual(result, {})

    def test_ex_date_not_a_trading_date_uses_next_trading_date(self) -> None:
        # ex_date is Saturday (a non-trading day); find next trading date
        actions = [_real_corp(1, "AAA", "2026-01-10")]
        # 2026-01-10 is in self.trading_dates. Add an ex-date on a weekend.
        weekend_date = "2026-01-11"  # a Sunday
        actions.append(_real_corp(2, "BBB", weekend_date))
        result = non_cash_events_in_trading_window(actions, "2026-01-10", self.trading_dates, window_days=2)
        # both events should be in window: 2026-01-10 is trading date 0 in series
        # AAA: ex_index=0, trade_index=0, sessions_until=0
        # BBB: ex_date=Sunday -> bisect finds next >= (2026-01-12 at index 2)
        self.assertIn("AAA", result)
        self.assertIn("BBB", result)

    def test_precomputed_returns_same_result(self) -> None:
        actions = [
            _real_corp(1, "AAA", "2026-01-12"),
            _real_corp(2, "BBB", "2026-01-14"),
        ]
        _, ex_index = build_corporate_action_lookup(actions, self.trading_dates, non_cash_only=True)
        without = non_cash_events_in_trading_window(actions, "2026-01-10", self.trading_dates, window_days=5)
        with_pre = non_cash_events_in_trading_window(
            actions, "2026-01-10", self.trading_dates, window_days=5,
            precomputed_ex_index=ex_index,
        )
        self.assertEqual(set(without), set(with_pre))

    def test_multiple_events_same_symbol(self) -> None:
        actions = [
            _real_corp(1, "AAA", "2026-01-12"),
            _real_corp(2, "AAA", "2026-01-13"),
        ]
        result = non_cash_events_in_trading_window(actions, "2026-01-10", self.trading_dates, window_days=5)
        self.assertEqual(len(result["AAA"]), 2)

    def test_ex_date_equals_trade_date_include_true(self) -> None:
        actions = [_real_corp(1, "AAA", "2026-01-10")]
        result = non_cash_events_in_trading_window(actions, "2026-01-10", self.trading_dates, window_days=1, include_ex_date=True)
        # sessions_until=0, 0<=0<=1 -> included
        self.assertIn("AAA", result)

    def test_ex_date_equals_trade_date_include_false(self) -> None:
        actions = [_real_corp(1, "AAA", "2026-01-10")]
        result = non_cash_events_in_trading_window(actions, "2026-01-10", self.trading_dates, window_days=1, include_ex_date=False)
        # sessions_until=0, lower=1 -> excluded
        self.assertEqual(result, {})


class BuildCorporateActionLookupTests(unittest.TestCase):
    def test_empty_actions(self) -> None:
        filtered, mapping = build_corporate_action_lookup([], ["2026-01-05"], non_cash_only=True)
        self.assertEqual(filtered, [])
        self.assertEqual(mapping, {})

    def test_empty_trading_dates(self) -> None:
        actions = [_real_corp(1, "AAA", "2026-01-12")]
        filtered, mapping = build_corporate_action_lookup(actions, [], non_cash_only=True)
        self.assertEqual(filtered, [])
        self.assertEqual(mapping, {})

    def test_filters_cash_only(self) -> None:
        actions = [
            _real_corp(1, "AAA", "2026-01-12"),
            _real_corp(2, "BBB", "2026-01-13", isNonCash=False),
        ]
        filtered, mapping = build_corporate_action_lookup(actions, ["2026-01-05", "2026-01-12", "2026-01-13"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].symbol, "AAA")
        self.assertIn(1, mapping)

    def test_maps_event_id_to_index(self) -> None:
        trading_dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
        actions = [_real_corp(42, "AAA", "2026-01-06")]
        _, mapping = build_corporate_action_lookup(actions, trading_dates)
        self.assertEqual(mapping[42], 1)

    def test_skips_events_beyond_calendar(self) -> None:
        trading_dates = ["2026-01-05"]
        actions = [_real_corp(42, "AAA", "2026-12-31")]
        filtered, mapping = build_corporate_action_lookup(actions, trading_dates)
        self.assertEqual(filtered, [])
        self.assertEqual(mapping, {})


class TradingWindowPerfRegressionTests(unittest.TestCase):
    """Sanity-check the perf fix: a call should be O(log N) per action, not O(N)."""

    def test_one_call_completes_quickly_on_large_input(self) -> None:
        import time
        trading_dates = [f"2026-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)][:365]
        actions = [_real_corp(i, f"SYM{i}", f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}") for i in range(13_000)]
        t0 = time.time()
        for _ in range(100):
            non_cash_events_in_trading_window(actions, "2026-06-01", trading_dates, window_days=5)
        elapsed = time.time() - t0
        # 100 calls × 13k actions × O(N) ~ 100 × 13k × 365 = 475M ops
        # 100 calls × 13k × log(365) ~ 100 × 13k × 9 = 12M ops (with bisect) or
        # even faster with precomputed map.
        # Allow generous 5s budget.
        self.assertLess(elapsed, 5.0)


class RebuyWindowRegressionTests(unittest.TestCase):
    """Regression tests for the high-risk-window rebuy-eligibility bug.

    The bug: forced exit at sessions_until=2 with a PURE STANDARD event was
    correctly triggered by high_risk_window=3, but the rebuy eligibility
    check only looked in sell_window=1 and saw no events, dropping the rebuy.
    """

    def test_rebuy_allowed_for_pure_standard_event_in_high_risk_window(self) -> None:
        """events at sessions_until=2 triggered by high_risk_window should still rebuy if pure standard."""
        from vietnam_algo.libs.backtest import corporate_action_exit_rebuy_allowed
        from vietnam_algo.libs.config import StrategyConfig
        from vietnam_algo.libs.corporate_actions import build_corporate_action_lookup

        # Trade dates: Mon..Wed..Fri
        trading_dates = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]
        # ex-date is 2 trading sessions after trade date (Friday)
        ex_date = "2026-09-25"
        standard_event = _real_corp(eventId=42, symbol="VNM", exDate=ex_date, isNonCash=True)

        ca_table = {"VNM": {ex_date: [standard_event]}}
        config = StrategyConfig(
            corporateActionSellWindowDays=1,
            corporateActionHighRiskSellWindowDays=5,
        )
        trade_date = "2026-09-23"  # sessions_until = 2
        _, ex_index = build_corporate_action_lookup([standard_event], trading_dates)
        result = corporate_action_exit_rebuy_allowed(
            "VNM", trade_date, ca_table, config,
            trading_dates=trading_dates, precomputed_ex_index=ex_index,
        )
        self.assertTrue(result, "Pure-standard event in high-risk window should allow rebuy")

    def test_rebuy_allowed_for_high_risk_event(self) -> None:
        """Policy update: high-risk events (rights offerings, share swaps) now
        also allow post-ex rebuy, because the adjusted price already reflects
        the dilution/swap/issuance. The rebuy is always fair at that price.
        """
        from vietnam_algo.libs.backtest import corporate_action_exit_rebuy_allowed
        from vietnam_algo.libs.config import StrategyConfig
        from vietnam_algo.libs.corporate_actions import build_corporate_action_lookup

        trading_dates = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]
        ex_date = "2026-09-25"
        high_risk_event = CorporateAction(
            eventId=99, eventTypeId=1, channelId=0, catId=0,
            symbol="SHS", companyName="Co", exchange="HOSE",
            exDate=ex_date, recordDate=None, paymentDate=None,
            name="Phát hành thêm",
            note="Phát hành thêm cổ phiếu",
            title="", fileUrl="",
            isCashOnly=False,
            isNonCash=True,
        )
        ca_table = {"SHS": {ex_date: [high_risk_event]}}
        config = StrategyConfig(
            corporateActionSellWindowDays=1,
            corporateActionHighRiskSellWindowDays=5,
        )
        trade_date = "2026-09-23"
        _, ex_index = build_corporate_action_lookup([high_risk_event], trading_dates)
        result = corporate_action_exit_rebuy_allowed(
            "SHS", trade_date, ca_table, config,
            trading_dates=trading_dates, precomputed_ex_index=ex_index,
        )
        self.assertTrue(result, "High-risk event should still allow rebuy at adjusted price")

    def test_next_ex_date_finds_event_outside_sell_window(self) -> None:
        """Regression: next_corporate_action_ex_date previously used sell_window=1
        so it returned None for ex-dates at sessions_until > 1.

        Also: previously it filtered to standard events only, so a pure
        high-risk event (rights offering) would yield None even when its
        ex-date is in the union window. Now it returns the soonest ex-date
        regardless of risk_type."""
        from vietnam_algo.libs.backtest import next_corporate_action_ex_date
        from vietnam_algo.libs.config import StrategyConfig
        from vietnam_algo.libs.corporate_actions import build_corporate_action_lookup

        trading_dates = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]
        ex_date = "2026-09-25"
        standard_event = _real_corp(eventId=1, symbol="VNM", exDate=ex_date, isNonCash=True)
        ca_table = {"VNM": {ex_date: [standard_event]}}
        config = StrategyConfig(
            corporateActionSellWindowDays=1,
            corporateActionHighRiskSellWindowDays=5,
        )
        trade_date = "2026-09-23"
        _, ex_index = build_corporate_action_lookup([standard_event], trading_dates)
        result = next_corporate_action_ex_date(
            "VNM", trade_date, ca_table, config,
            trading_dates=trading_dates, precomputed_ex_index=ex_index,
        )
        self.assertEqual(result, ex_date, "next_corporate_action_ex_date should find ex-date in high_risk_window")

    def test_next_ex_date_returns_date_for_high_risk_event(self) -> None:
        """Regression: previously the function filtered to standard events only,
        so a pure rights offering (high-risk) returned None even when it was the
        only upcoming event. Now it returns the ex-date regardless of risk_type."""
        from vietnam_algo.libs.backtest import next_corporate_action_ex_date
        from vietnam_algo.libs.config import StrategyConfig
        from vietnam_algo.libs.corporate_actions import build_corporate_action_lookup

        trading_dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        ex_date = "2024-01-03"
        rights_event = CorporateAction(
            eventId=2, eventTypeId=1, channelId=0, catId=0,
            symbol="SZC", companyName="Co", exchange="HOSE",
            exDate=ex_date, recordDate=None, paymentDate=None,
            name="Phát hành thêm cổ phiếu",
            note="", title="", fileUrl="",
            isCashOnly=False, isNonCash=True,
        )
        ca_table = {"SZC": {ex_date: [rights_event]}}
        config = StrategyConfig(
            corporateActionSellWindowDays=1,
            corporateActionHighRiskSellWindowDays=5,
        )
        _, ex_index = build_corporate_action_lookup([rights_event], trading_dates)
        result = next_corporate_action_ex_date(
            "SZC", "2024-01-02", ca_table, config,
            trading_dates=trading_dates, precomputed_ex_index=ex_index,
        )
        self.assertEqual(result, ex_date, "high-risk rights offering should yield its ex-date")


class CorporateActionRiskTypeTests(unittest.TestCase):
    """Tests for the risk classifier fix.

    The old classifier checked name+note+title and used bare "phát hành" /
    "chuyển đổi" terms. The title is a long summary that often mentions
    unrelated events (e.g. a stock-bonus whose title mentions stock issuance),
    causing false-positive high-risk classifications that blocked rebays.
    """

    def _ev(self, *, name, note="", title=""):
        return CorporateAction(
            eventId=1, eventTypeId=1, channelId=0, catId=0,
            symbol="SYM", companyName="Co", exchange="HOSE",
            exDate="2026-09-29", recordDate=None, paymentDate=None,
            name=name, note=note, title=title, fileUrl="",
            isCashOnly=False, isNonCash=True,
        )

    def test_stock_bonus_standard(self) -> None:
        from vietnam_algo.libs.backtest import corporate_action_risk_type
        ev = self._ev(
            name="Thưởng cổ phiếu",
            title="Thông báo phát hành cổ phiếu để tăng vốn cổ phần từ nguồn vốn chủ sở hữu và tạm ứng cổ tức bằng tiền",
        )
        self.assertEqual(corporate_action_risk_type(ev), "standard")

    def test_cash_dividend_standard(self) -> None:
        from vietnam_algo.libs.backtest import corporate_action_risk_type
        ev = self._ev(
            name="Trả cổ tức bằng tiền mặt",
            title="VNM: phát hành cổ phiếu để tăng vốn cổ phần từ nguồn vốn chủ sở hữu và tạm ứng cổ tức đợt 1 năm 2020 bằng tiền",
        )
        self.assertEqual(corporate_action_risk_type(ev), "standard")

    def test_stock_dividend_standard(self) -> None:
        from vietnam_algo.libs.backtest import corporate_action_risk_type
        ev = self._ev(name="Trả cổ tức bằng cổ phiếu")
        self.assertEqual(corporate_action_risk_type(ev), "standard")

    def test_additional_issuance_high(self) -> None:
        from vietnam_algo.libs.backtest import corporate_action_risk_type
        ev = self._ev(name="Phát hành thêm cổ phiếu")
        self.assertEqual(corporate_action_risk_type(ev), "high")

    def test_rights_offering_high(self) -> None:
        from vietnam_algo.libs.backtest import corporate_action_risk_type
        ev = self._ev(
            name="Phát hành cổ phiếu",
            note="Thực hiện quyền mua cổ phiếu phát hành thêm, tỷ lệ 2:1",
        )
        self.assertEqual(corporate_action_risk_type(ev), "high")

    def test_convertible_bond_high(self) -> None:
        from vietnam_algo.libs.backtest import corporate_action_risk_type
        ev = self._ev(name="Trái phiếu chuyển đổi")
        self.assertEqual(corporate_action_risk_type(ev), "high")

    def test_private_placement_high(self) -> None:
        from vietnam_algo.libs.backtest import corporate_action_risk_type
        ev = self._ev(name="Chào bán riêng lẻ")
        self.assertEqual(corporate_action_risk_type(ev), "high")

    def test_swap_high(self) -> None:
        from vietnam_algo.libs.backtest import corporate_action_risk_type
        ev = self._ev(name="Hoán đổi cổ phiếu")
        self.assertEqual(corporate_action_risk_type(ev), "high")


class RebuyAlwaysAllowedTests(unittest.TestCase):
    """Tests for the policy "always rebuild after a forced CA exit".

    Previously `corporate_action_exit_rebuy_allowed` returned False when any
    event in the union window was classified high-risk (rights offerings,
    share swaps). The policy is now: every forced exit is followed by a
    post-ex rebuy at the adjusted price, regardless of risk_type, since
    the adjusted price already reflects the dilution/swap/issuance.
    """

    def test_rebuy_blocked_when_no_events(self) -> None:
        from vietnam_algo.libs.backtest import corporate_action_exit_rebuy_allowed
        from vietnam_algo.libs.config import StrategyConfig

        config = StrategyConfig()
        result = corporate_action_exit_rebuy_allowed(
            "NTP", "2025-06-27", {}, config,
            trading_dates=["2025-06-25", "2025-06-26", "2025-06-27"],
        )
        self.assertFalse(result, "no upcoming events → no rebuy")


class RebuyDelayConfigTests(unittest.TestCase):
    """Tests for the rebuy-delay feature.

    The user observed that high-risk CA events often keep sliding for a few
    days post-ex. The rebuy-delay feature lets the strategy wait N trading
    days after the ex-date before re-entering the position, instead of
    firing the rebuy on the first eligible day.
    """

    def test_default_delay_is_zero(self) -> None:
        from vietnam_algo.libs.config import StrategyConfig
        self.assertEqual(StrategyConfig().corporateActionRebuyDelayDays, 0)
        self.assertFalse(StrategyConfig().corporateActionRebuyDelayHighRiskOnly)

    def test_delay_only_for_high_risk_default_off(self) -> None:
        from vietnam_algo.libs.config import StrategyConfig
        cfg = StrategyConfig(corporateActionRebuyDelayDays=3)
        self.assertFalse(cfg.corporateActionRebuyDelayHighRiskOnly)
        # When delay_high_risk_only=False, the delay applies to ALL events.


class PostCaRebuySignalTests(unittest.TestCase):
    """Tests for the post-CA rebuy signal logic.

    All six signal modes plus "fixed_delay" + a time-fallback max_days
    are tested so any future refactor of the live + backtest shared
    signal code breaks loudly here.
    """

    def _cfg(self, **overrides):
        from vietnam_algo.libs.config import StrategyConfig
        defaults = dict(
            corporateActionRebuySignalMode="combined",
            corporateActionRebuySignalRecoveryPct=0.005,
            corporateActionRebuySignalRangePct=0.025,
            corporateActionRebuySignalLookback=3,
            corporateActionRebuySignalMaPeriod=5,
        )
        defaults.update(overrides)
        return StrategyConfig(**defaults)

    def test_fixed_delay_mode_returns_false(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        closes = [100.0, 95.0, 96.0, 97.5]
        cfg = self._cfg(corporateActionRebuySignalMode="fixed_delay")
        triggered, reason = evaluate_post_ca_rebuy_signal(closes, cfg)
        self.assertFalse(triggered)
        self.assertEqual(reason, "fixed_delay_mode")

    def test_recovery_triggers_on_bounce(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        closes = [100.0, 95.0, 96.0, 95.5]  # min=95, current=95.5, +0.53%
        cfg = self._cfg(corporateActionRebuySignalMode="recovery")
        triggered, reason = evaluate_post_ca_rebuy_signal(closes, cfg)
        self.assertTrue(triggered, "0.5% bounce from low should trigger recovery")
        self.assertEqual(reason, "recovery")

    def test_recovery_does_not_trigger_on_insufficient_bounce(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        closes = [100.0, 95.0, 95.3, 95.4]  # min=95, current=95.4, +0.42%
        cfg = self._cfg(corporateActionRebuySignalMode="recovery")
        triggered, _ = evaluate_post_ca_rebuy_signal(closes, cfg)
        self.assertFalse(triggered, "0.4% bounce should not trigger 0.5% recovery")

    def test_stabilization_triggers_when_range_tight(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        closes = [100.0, 92.0, 92.5, 92.3]  # last 3: range=(92.5-92.0)/92.0=0.54%
        cfg = self._cfg(corporateActionRebuySignalMode="stabilization")
        triggered, reason = evaluate_post_ca_rebuy_signal(closes, cfg)
        self.assertTrue(triggered, "tight 0.5% range should trigger stabilization")
        self.assertEqual(reason, "stabilization")

    def test_stabilization_does_not_trigger_on_wide_range(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        closes = [100.0, 90.0, 95.0, 92.0]  # last 3: range=(95-90)/90=5.5%
        cfg = self._cfg(corporateActionRebuySignalMode="stabilization")
        triggered, _ = evaluate_post_ca_rebuy_signal(closes, cfg)
        self.assertFalse(triggered, "wide 5% range should not trigger 2.5% stabilization")

    def test_combined_requires_both(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        # Recovery met, but range too wide → no trigger
        closes_recover_only = [100.0, 90.0, 95.0, 95.5]
        cfg = self._cfg(corporateActionRebuySignalMode="combined")
        triggered, _ = evaluate_post_ca_rebuy_signal(closes_recover_only, cfg)
        self.assertFalse(triggered, "recovery alone should not trigger combined mode")
        # Both met → trigger
        closes_both = [100.0, 92.0, 92.5, 92.8]
        triggered, reason = evaluate_post_ca_rebuy_signal(closes_both, cfg)
        self.assertTrue(triggered)
        self.assertEqual(reason, "combined")

    def test_combined_stabilization_alone_insufficient(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        closes = [100.0, 92.0, 92.05, 92.1]  # stable but no bounce
        cfg = self._cfg(corporateActionRebuySignalMode="combined")
        triggered, _ = evaluate_post_ca_rebuy_signal(closes, cfg)
        self.assertFalse(triggered, "stabilization alone should not trigger combined mode")

    def test_stabilization_or_no_new_low_accepts_either_signal(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        cfg = self._cfg(corporateActionRebuySignalMode="stabilization_or_no_new_low")
        triggered, reason = evaluate_post_ca_rebuy_signal([100.0, 92.0, 92.1, 92.0], cfg)
        self.assertTrue(triggered)
        self.assertEqual(reason, "stabilization_or_no_new_low")

        triggered, reason = evaluate_post_ca_rebuy_signal([100.0, 92.0, 94.0, 93.0], cfg)
        self.assertTrue(triggered)
        self.assertEqual(reason, "stabilization_or_no_new_low")

    def test_signal_adapts_to_local_low(self) -> None:
        """A 0.5% bounce from the LOCAL low should trigger regardless of
        the absolute drop magnitude. Adapts to whatever the post-ex drop is."""
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        # Big drop: 100 → 50, then small bounce to 50.3 (+0.6%)
        big_drop = [100.0, 60.0, 50.0, 50.3]
        cfg = self._cfg(corporateActionRebuySignalMode="recovery")
        triggered, _ = evaluate_post_ca_rebuy_signal(big_drop, cfg)
        self.assertTrue(triggered, "0.6% bounce from post-ex low should trigger")
        # Small drop: 100 → 99, then small bounce to 99.5 (+0.5%)
        small_drop = [100.0, 99.2, 99.0, 99.5]
        triggered, _ = evaluate_post_ca_rebuy_signal(small_drop, cfg)
        self.assertTrue(triggered, "0.5% bounce from small post-ex drop should also trigger")

    def test_empty_window_returns_false(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        cfg = self._cfg(corporateActionRebuySignalMode="recovery")
        for mode in ("recovery", "stabilization", "combined"):
            cfg2 = self._cfg(corporateActionRebuySignalMode=mode)
            triggered, _ = evaluate_post_ca_rebuy_signal([], cfg2)
            self.assertFalse(triggered, f"empty window should not trigger {mode}")

    def test_all_six_signal_modes_are_wired(self) -> None:
        """Regression: every mode listed in the CLI choices must compute a real
        signal — none of them should fall through to ``unknown_mode``.

        Crafted windows so each mode triggers on its own preferred pattern.
        """
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal

        # Recovery: 0.5% bounce from a clear post-ex low.
        recovery_window = [100.0, 80.0, 80.4]
        cfg_recovery = self._cfg(corporateActionRebuySignalMode="recovery")
        triggered, reason = evaluate_post_ca_rebuy_signal(recovery_window, cfg_recovery)
        self.assertTrue(triggered, "recovery mode should trigger on bounce")
        self.assertEqual(reason, "recovery")

        # Stabilization: range over last 3 closes < 2.5%.
        stabilization_window = [100.0, 92.0, 92.05, 92.1]
        cfg_stab = self._cfg(corporateActionRebuySignalMode="stabilization")
        triggered, reason = evaluate_post_ca_rebuy_signal(stabilization_window, cfg_stab)
        self.assertTrue(triggered, "stabilization mode should trigger on tight range")
        self.assertEqual(reason, "stabilization")

        # new_high: today's close is the highest close since ex.
        # Need current close > all PRIOR closes, and the first close (ex-date close)
        # must NOT be the highest. Simulate ex-day drop followed by recovery.
        new_high_window = [80.0, 60.0, 70.0, 85.0]  # prior max=80, current=85
        cfg_nh = self._cfg(corporateActionRebuySignalMode="new_high")
        triggered, reason = evaluate_post_ca_rebuy_signal(new_high_window, cfg_nh)
        self.assertTrue(triggered, "new_high mode should trigger on absolute new high")
        self.assertEqual(reason, "new_high")

        # ma_cross: today's close is above the 5-day MA of the post-ex window.
        ma_window = [100.0, 80.0, 82.0, 81.0, 83.0, 95.0]  # 5-MA = 84.2, current=95
        cfg_ma = self._cfg(corporateActionRebuySignalMode="ma_cross", corporateActionRebuySignalMaPeriod=5)
        triggered, reason = evaluate_post_ca_rebuy_signal(ma_window, cfg_ma)
        self.assertTrue(triggered, "ma_cross mode should trigger when close > MA")
        self.assertEqual(reason, "ma_cross")

        # no_new_low: no new low in last 3 closes.
        nnl_window = [100.0, 80.0, 81.0, 82.0]  # no new low in last 3 (80, 81, 82)
        cfg_nnl = self._cfg(corporateActionRebuySignalMode="no_new_low")
        triggered, reason = evaluate_post_ca_rebuy_signal(nnl_window, cfg_nnl)
        self.assertTrue(triggered, "no_new_low mode should trigger when no new low")
        self.assertEqual(reason, "no_new_low")

        # combined: recovery AND stabilization must both fire.
        combined_window = [100.0, 92.0, 92.05, 92.6]  # recovery 0.65% + tight range <2.5%
        cfg_comb = self._cfg(corporateActionRebuySignalMode="combined")
        triggered, reason = evaluate_post_ca_rebuy_signal(combined_window, cfg_comb)
        self.assertTrue(triggered, "combined mode should trigger when both fire")
        self.assertEqual(reason, "combined")

    def test_ma_cross_does_not_trigger_below_ma(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        # Today's close is BELOW the 5-close MA — should NOT trigger.
        ma_window = [100.0, 80.0, 90.0, 85.0, 88.0, 70.0]  # MA=82.6, current=70
        cfg = self._cfg(corporateActionRebuySignalMode="ma_cross", corporateActionRebuySignalMaPeriod=5)
        triggered, _ = evaluate_post_ca_rebuy_signal(ma_window, cfg)
        self.assertFalse(triggered, "ma_cross should not trigger when close is below MA")

    def test_unknown_mode_returns_false(self) -> None:
        from vietnam_algo.libs.backtest import evaluate_post_ca_rebuy_signal
        cfg = self._cfg(corporateActionRebuySignalMode="bogus_mode")
        triggered, reason = evaluate_post_ca_rebuy_signal([100.0, 90.0, 95.0], cfg)
        self.assertFalse(triggered)
        self.assertEqual(reason, "unknown_mode")


class ClosesSinceExWindowTests(unittest.TestCase):
    """Test the live-accessible closes-since-ex helper.

    This is the function the live engine will use to load closes from
    ``data/prices/SYM.json`` for signal evaluation.
    """

    def test_excludes_dates_before_ex_date(self) -> None:
        from vietnam_algo.libs.corporate_actions import closes_since_ex_window
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        closes = {"AAA": {"2024-01-02": 100.0, "2024-01-03": 95.0, "2024-01-04": 96.0, "2024-01-05": 97.0}}
        out = closes_since_ex_window("AAA", "2024-01-03", dates, closes)
        self.assertEqual(out, [95.0, 96.0, 97.0])

    def test_excludes_dates_after_horizon(self) -> None:
        from vietnam_algo.libs.corporate_actions import closes_since_ex_window
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        closes = {"AAA": {d: 100.0 + i for i, d in enumerate(dates)}}
        out = closes_since_ex_window("AAA", "2024-01-03", dates, closes)
        self.assertEqual(out, [101.0, 102.0, 103.0])

    def test_skips_dates_not_in_closes_lookup(self) -> None:
        from vietnam_algo.libs.corporate_actions import closes_since_ex_window
        dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
        closes = {"AAA": {"2024-01-02": 100.0, "2024-01-04": 102.0}}  # 01-03 missing
        out = closes_since_ex_window("AAA", "2024-01-02", dates, closes)
        self.assertEqual(out, [100.0, 102.0])

    def test_unknown_symbol_returns_empty(self) -> None:
        from vietnam_algo.libs.corporate_actions import closes_since_ex_window
        dates = ["2024-01-02", "2024-01-03"]
        closes = {"AAA": {"2024-01-02": 100.0}}
        out = closes_since_ex_window("ZZZ", "2024-01-02", dates, closes)
        self.assertEqual(out, [])

    def test_ex_date_after_all_dates_returns_empty(self) -> None:
        from vietnam_algo.libs.corporate_actions import closes_since_ex_window
        dates = ["2024-01-02", "2024-01-03"]
        closes = {"AAA": {"2024-01-02": 100.0}}
        out = closes_since_ex_window("AAA", "2024-01-05", dates, closes)
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()

class PreExitSignalTests(unittest.TestCase):
    """Tests for the pre-exit signal logic.

    These mirror the post-CA rebuy-signal tests in spirit but cover the
    exit side. The runner decides when to force-sell by combining the
    window-check ("is there a CA in the watch window?") with a price-signal
    check ("has the price shown a topping pattern?").
    """

    def _cfg(self, **overrides):
        from vietnam_algo.libs.config import StrategyConfig
        defaults = dict(
            corporateActionPreExitSignalMode="drop_from_high",
            corporateActionPreExitSignalDropPct=0.02,
            corporateActionPreExitSignalMaPeriod=5,
            corporateActionPreExitSignalShortPeriod=3,
            corporateActionPreExitSignalLongPeriod=8,
            corporateActionPreExitSignalLookback=14,
        )
        defaults.update(overrides)
        return StrategyConfig(**defaults)

    def test_drop_from_high_triggers_on_2pct_drop(self) -> None:
        from vietnam_algo.libs.corporate_actions import evaluate_pre_exit_signal
        # Peak at 100, current at 97 → 3% drop → triggers.
        closes = [100.0, 99.0, 100.0, 98.0, 97.0]
        cfg = self._cfg()
        triggered, reason = evaluate_pre_exit_signal(closes, cfg)
        self.assertTrue(triggered)
        self.assertEqual(reason, "drop_from_high")

    def test_drop_from_high_does_not_trigger_within_band(self) -> None:
        from vietnam_algo.libs.corporate_actions import evaluate_pre_exit_signal
        # Peak at 100, current at 99 → 1% drop → does NOT trigger.
        closes = [100.0, 99.0, 100.0, 99.0, 99.0]
        cfg = self._cfg(corporateActionPreExitSignalDropPct=0.02)
        triggered, _ = evaluate_pre_exit_signal(closes, cfg)
        self.assertFalse(triggered)

    def test_drop_from_high_adapts_to_local_peak(self) -> None:
        from vietnam_algo.libs.corporate_actions import evaluate_pre_exit_signal
        # Higher peak but smaller absolute drop → still doesn't trigger.
        closes = [200.0, 150.0, 199.0, 195.0]
        cfg = self._cfg(corporateActionPreExitSignalDropPct=0.05)  # 5%
        triggered, _ = evaluate_pre_exit_signal(closes, cfg)
        # Peak=200, current=195, drop=2.5% → no trigger
        self.assertFalse(triggered)
        # Lower the peak in subsequent bars
        closes2 = [200.0, 150.0, 180.0, 195.0, 184.0]  # new peak = 200, current=184, drop=8%
        triggered, _ = evaluate_pre_exit_signal(closes2, cfg)
        self.assertTrue(triggered)

    def test_below_ma_triggers(self) -> None:
        from vietnam_algo.libs.corporate_actions import evaluate_pre_exit_signal
        # 5-day MA = 100, current = 95 → triggers.
        closes = [105.0, 102.0, 100.0, 98.0, 95.0]
        cfg = self._cfg(corporateActionPreExitSignalMode="below_ma",
                        corporateActionPreExitSignalMaPeriod=5)
        triggered, reason = evaluate_pre_exit_signal(closes, cfg)
        self.assertTrue(triggered)
        self.assertEqual(reason, "below_ma")

    def test_momentum_reversal_triggers(self) -> None:
        from vietnam_algo.libs.corporate_actions import evaluate_pre_exit_signal
        # bars 0-4 stable 100, bar 5 spike 110, bar 6 105, bar 7 90, bar 8 80
        # Short MA was 105 on bar 7 (> long MA at 100.6) → "above"
        # Short MA on bar 9 is 91.7, long MA 92.5 → crosses below.
        closes = [100, 100, 100, 100, 100, 110, 105, 90, 80]
        cfg = self._cfg(corporateActionPreExitSignalMode="momentum_reversal",
                        corporateActionPreExitSignalShortPeriod=3,
                        corporateActionPreExitSignalLongPeriod=8)
        triggered, reason = evaluate_pre_exit_signal(closes, cfg)
        self.assertTrue(triggered)
        self.assertEqual(reason, "momentum_reversal")

    def test_combined_requires_both(self) -> None:
        from vietnam_algo.libs.corporate_actions import evaluate_pre_exit_signal
        cfg = self._cfg(corporateActionPreExitSignalMode="combined")
        # No drop, no below_ma → combined does not trigger.
        closes = [100.0] * 14
        triggered, _ = evaluate_pre_exit_signal(closes, cfg)
        self.assertFalse(triggered, "no drop should not trigger combined")
        # Both drop from peak AND below MA → trigger.
        closes2 = [110, 108, 108, 109, 108, 109, 109, 108, 109, 100, 95, 90, 85, 80]
        triggered, _ = evaluate_pre_exit_signal(closes2, cfg)
        self.assertTrue(triggered)
        # Below MA only (no big drop) → combined does not trigger.
        closes3 = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 99, 99, 99, 99]
        triggered, _ = evaluate_pre_exit_signal(closes3, cfg)
        self.assertFalse(triggered, "below_ma alone should not trigger combined")

    def test_fixed_window_never_returns_true_from_signal(self) -> None:
        from vietnam_algo.libs.corporate_actions import evaluate_pre_exit_signal
        cfg = self._cfg(corporateActionPreExitSignalMode="fixed_window")
        closes = [100.0, 80.0, 75.0, 70.0]
        triggered, reason = evaluate_pre_exit_signal(closes, cfg)
        self.assertFalse(triggered)
        self.assertEqual(reason, "fixed_window_mode")

    def test_empty_window_returns_false(self) -> None:
        from vietnam_algo.libs.corporate_actions import evaluate_pre_exit_signal
        cfg = self._cfg()
        triggered, _ = evaluate_pre_exit_signal([], cfg)
        self.assertFalse(triggered)

    def test_all_signal_modes_wired(self) -> None:
        """Regression: every mode listed in the CLI choices must compute
        a real signal — none of them should fall through to unknown_mode."""
        from vietnam_algo.libs.corporate_actions import evaluate_pre_exit_signal
        # Each mode on a window crafted to trigger its own signal.
        cases = [
            ("drop_from_high", [100, 100, 100, 100, 100, 100, 100, 100, 100, 95, 94, 90]),
            ("below_ma", [110, 110, 110, 110, 110, 110, 110, 110, 105, 95, 88]),
            ("momentum_reversal", [100, 100, 100, 100, 100, 110, 105, 90, 80]),
            ("combined", [110, 108, 108, 109, 108, 109, 109, 108, 109, 100, 95, 90, 85, 80]),
        ]
        for mode, closes in cases:
            cfg = self._cfg(corporateActionPreExitSignalMode=mode)
            triggered, reason = evaluate_pre_exit_signal(closes, cfg)
            self.assertTrue(triggered, f"{mode} did not trigger")
            self.assertEqual(reason, mode)


class ClosesUntilDateTests(unittest.TestCase):
    """Tests for the live-accessible closes-until-date helper."""

    def test_includes_target_date(self) -> None:
        from vietnam_algo.libs.corporate_actions import closes_until_date
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        closes = {"AAA": {"2024-01-02": 100.0, "2024-01-03": 95.0, "2024-01-04": 96.0, "2024-01-05": 97.0}}
        out = closes_until_date("AAA", "2024-01-04", dates, closes, lookback=10)
        self.assertEqual(out, [100.0, 95.0, 96.0])

    def test_lookback_limits_window(self) -> None:
        from vietnam_algo.libs.corporate_actions import closes_until_date
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
        closes = {"AAA": {d: 100.0 + i for i, d in enumerate(dates)}}
        out = closes_until_date("AAA", "2024-01-08", dates, closes, lookback=3)
        self.assertEqual(out, [102.0, 103.0, 104.0])
