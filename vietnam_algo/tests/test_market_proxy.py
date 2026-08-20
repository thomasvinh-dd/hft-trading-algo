from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.backtest import precompute_market_proxy, market_proxy_closes


class FakePriceBar:
    def __init__(self, time: str, close: float) -> None:
        self.time = time
        self.open = close
        self.high = close
        self.low = close
        self.close = close
        self.volume = 0.0


def _row(prices: list[tuple[str, float]]) -> dict[str, FakePriceBar]:
    return {time: FakePriceBar(time, close) for time, close in prices}


class PrecomputeMarketProxyTests(unittest.TestCase):
    def test_starts_at_one(self) -> None:
        table = {
            "A": _row([("2024-01-01", 100.0), ("2024-01-02", 110.0)]),
        }
        levels = precompute_market_proxy(table, ["2024-01-01", "2024-01-02"])
        self.assertEqual(levels[0], 1.0)

    def test_two_stocks_one_doubles(self) -> None:
        # Stock A goes 100 -> 200 (+100%), Stock B stays 100 -> 100 (0%)
        # Average return = 50%, index after = 1.5
        table = {
            "A": _row([("2024-01-01", 100.0), ("2024-01-02", 200.0)]),
            "B": _row([("2024-01-01", 100.0), ("2024-01-02", 100.0)]),
        }
        levels = precompute_market_proxy(table, ["2024-01-01", "2024-01-02"])
        self.assertAlmostEqual(levels[1], 1.5, places=6)

    def test_compound_returns(self) -> None:
        # 2 days, both stocks +10% each day
        # Day 1: 1.1, Day 2: 1.1 * 1.1 = 1.21
        table = {
            "A": _row([("2024-01-01", 100.0), ("2024-01-02", 110.0), ("2024-01-03", 121.0)]),
            "B": _row([("2024-01-01", 100.0), ("2024-01-02", 110.0), ("2024-01-03", 121.0)]),
        }
        levels = precompute_market_proxy(
            table, ["2024-01-01", "2024-01-02", "2024-01-03"]
        )
        self.assertAlmostEqual(levels[1], 1.1, places=6)
        self.assertAlmostEqual(levels[2], 1.21, places=6)

    def test_only_uses_stocks_with_both_dates(self) -> None:
        # A: trades both days, B: only day 1
        # Day 2: A contributes 10%, B does not contribute
        # Index day 2 = 1.10
        table = {
            "A": _row([("2024-01-01", 100.0), ("2024-01-02", 110.0)]),
            "B": _row([("2024-01-01", 100.0)]),  # no day 2
        }
        levels = precompute_market_proxy(table, ["2024-01-01", "2024-01-02"])
        self.assertAlmostEqual(levels[1], 1.10, places=6)

    def test_skips_zero_or_negative_prices(self) -> None:
        # A: bad close (0) on day 2, B: normal
        # Only B contributes, so day 2 return = 0%
        table = {
            "A": _row([("2024-01-01", 100.0), ("2024-01-02", 0.0)]),
            "B": _row([("2024-01-01", 100.0), ("2024-01-02", 100.0)]),
        }
        levels = precompute_market_proxy(table, ["2024-01-01", "2024-01-02"])
        self.assertAlmostEqual(levels[1], 1.0, places=6)

    def test_no_stocks_trading_returns_flat(self) -> None:
        # No stocks trade on day 2
        table = {
            "A": _row([("2024-01-01", 100.0)]),
        }
        levels = precompute_market_proxy(table, ["2024-01-01", "2024-01-02"])
        self.assertEqual(levels[1], 1.0)

    def test_round_trip_returns_to_one(self) -> None:
        # Stock goes 100 -> 50 -> 100, market return day1=-50%, day2=+100%
        # Day 1: 0.5, Day 2: 0.5 * 2.0 = 1.0
        table = {
            "A": _row([("2024-01-01", 100.0), ("2024-01-02", 50.0), ("2024-01-03", 100.0)]),
        }
        levels = precompute_market_proxy(
            table, ["2024-01-01", "2024-01-02", "2024-01-03"]
        )
        self.assertAlmostEqual(levels[1], 0.5, places=6)
        self.assertAlmostEqual(levels[2], 1.0, places=6)


class MarketProxyClosesTests(unittest.TestCase):
    def test_slices_precomputed(self) -> None:
        pre = [1.0, 1.1, 1.21, 1.331]
        out = market_proxy_closes({}, ["a", "b", "c", "d"], index=2, precomputed=pre)
        self.assertEqual(out, [1.0, 1.1, 1.21])

    def test_falls_back_to_compute_when_no_precomputed(self) -> None:
        table = {
            "A": _row([("2024-01-01", 100.0), ("2024-01-02", 110.0)]),
        }
        out = market_proxy_closes(table, ["2024-01-01", "2024-01-02"], index=1)
        self.assertEqual(out, [1.0, 1.1])


if __name__ == "__main__":
    unittest.main()