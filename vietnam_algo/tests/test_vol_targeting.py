from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.backtest import (
    compute_exposure_cap,
    compute_realized_vol,
    compute_vol_scalar,
)
from vietnam_algo.config import StrategyConfig
from vietnam_algo.backtest import EquityPoint


class ComputeRealizedVolTests(unittest.TestCase):
    def test_constant_returns_zero_vol(self) -> None:
        equities = [100.0] * 25
        self.assertEqual(compute_realized_vol(equities, 20), 0.0)

    def test_insufficient_history_returns_zero(self) -> None:
        equities = [100.0, 101.0, 102.0]
        self.assertEqual(compute_realized_vol(equities, 20), 0.0)

    def test_window_too_small_returns_zero(self) -> None:
        equities = [100.0, 110.0, 121.0]
        self.assertEqual(compute_realized_vol(equities, 1), 0.0)

    def test_simple_daily_returns_annualized(self) -> None:
        # 21 days, constant +1% daily return
        # daily vol should be ~0, annualized ~0
        equities = [100.0 * (1.01 ** i) for i in range(21)]
        vol = compute_realized_vol(equities, 20)
        self.assertAlmostEqual(vol, 0.0, places=6)

    def test_known_volatility(self) -> None:
        # Construct returns of known stdev: alternating +x and -x
        # stdev = x, annualized = x * sqrt(252)
        x = 0.01
        equities = [100.0]
        for i in range(20):
            sign = 1 if i % 2 == 0 else -1
            equities.append(equities[-1] * (1 + sign * x))
        vol = compute_realized_vol(equities, 20)
        # sample std (n-1) of alternating +/- x is x * sqrt(n/(n-1)) when mean=0
        # For alternating +x, -x, the mean is ~0 and sample variance is x^2
        # So daily_vol ~ x, annualized ~ x * sqrt(252)
        expected = x * (252 ** 0.5)
        self.assertAlmostEqual(vol, expected, delta=expected * 0.05)


class ComputeVolScalarTests(unittest.TestCase):
    def test_one_to_one_when_equals_target(self) -> None:
        self.assertAlmostEqual(compute_vol_scalar(0.25, 0.25, 1.5), 1.0)

    def test_scale_down_when_realized_higher(self) -> None:
        # Realized 50%, target 25% → scalar = 0.5
        self.assertAlmostEqual(compute_vol_scalar(0.50, 0.25, 1.5), 0.5)

    def test_scale_up_when_realized_lower(self) -> None:
        # Realized 12.5%, target 25% → scalar = 2.0, but capped at 1.5
        self.assertAlmostEqual(compute_vol_scalar(0.125, 0.25, 1.5), 1.5)

    def test_capped_at_max_leverage(self) -> None:
        # Realized 1%, target 25% → raw 25, capped at 1.5
        self.assertAlmostEqual(compute_vol_scalar(0.01, 0.25, 1.5), 1.5)

    def test_returns_one_when_realized_zero(self) -> None:
        self.assertEqual(compute_vol_scalar(0.0, 0.25, 1.5), 1.0)

    def test_returns_one_when_target_zero(self) -> None:
        self.assertEqual(compute_vol_scalar(0.50, 0.0, 1.5), 1.0)

    def test_returns_one_when_max_leverage_zero(self) -> None:
        self.assertEqual(compute_vol_scalar(0.50, 0.25, 0.0), 1.0)


class ComputeExposureCapTests(unittest.TestCase):
    def _equity_curve(self, values: list[float]) -> list[EquityPoint]:
        return [EquityPoint(date=f"2024-01-{i+1:02d}", equity=v, cash=v, positionsValue=0.0) for i, v in enumerate(values)]

    def test_off_when_use_vol_targeting_false(self) -> None:
        config = StrategyConfig(useVolTargeting=False, bullExposurePercent=80.0)
        curve = self._equity_curve([100.0] * 25)
        cap, scalar = compute_exposure_cap(1_000_000.0, "bull", config, curve)
        self.assertAlmostEqual(cap, 800_000.0)
        self.assertEqual(scalar, 1.0)

    def test_on_with_high_realized_vol_scales_down(self) -> None:
        config = StrategyConfig(useVolTargeting=True, bullExposurePercent=100.0, volTargetAnnualized=0.25)
        # Construct a curve with ~50% annualized vol (alternating +/-3%)
        x = 0.03
        values = [1_000_000.0]
        for i in range(125):
            sign = 1 if i % 2 == 0 else -1
            values.append(values[-1] * (1 + sign * x))
        curve = self._equity_curve(values)
        cap, scalar = compute_exposure_cap(2_000_000.0, "bull", config, curve)
        # Scalar should be ~0.5 (target 25 / realized ~50)
        self.assertLess(scalar, 0.7)
        self.assertGreater(scalar, 0.3)
        self.assertLess(cap, 2_000_000.0 * 0.7)

    def test_on_with_low_realized_vol_scales_up_capped(self) -> None:
        config = StrategyConfig(useVolTargeting=True, bullExposurePercent=100.0, volTargetAnnualized=0.25, volMaxLeverage=1.5)
        curve = self._equity_curve([1_000_000.0] * 125)
        cap, scalar = compute_exposure_cap(2_000_000.0, "bull", config, curve)
        self.assertEqual(scalar, 1.0)
        self.assertAlmostEqual(cap, 2_000_000.0)

    def test_insufficient_history_returns_scalar_one(self) -> None:
        config = StrategyConfig(useVolTargeting=True, bullExposurePercent=100.0)
        # Only 5 values, less than window + 1
        curve = self._equity_curve([1_000_000.0, 1_010_000.0, 1_020_000.0, 1_015_000.0, 1_025_000.0])
        cap, scalar = compute_exposure_cap(2_000_000.0, "bull", config, curve)
        self.assertEqual(scalar, 1.0)


if __name__ == "__main__":
    unittest.main()