from __future__ import annotations

import sys
import unittest
from datetime import date as date_type
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.universe import (
    bars_until_list,
    build_candidate_price_table,
    build_liquid_universe_as_of,
)


class FakePriceBar:
    def __init__(self, time: str, close: float, volume: float) -> None:
        self.time = time
        self.open = close
        self.high = close
        self.low = close
        self.close = close
        self.volume = volume


def _table_from_bars(bars_by_symbol: dict[str, list[FakePriceBar]]) -> dict[str, list[FakePriceBar]]:
    return bars_by_symbol


def _bars(start: str, n: int, *, close: float = 50_000.0, volume: float = 1_000_000.0) -> list[FakePriceBar]:
    start_d = date_type.fromisoformat(start)
    return [
        FakePriceBar(date_type.fromordinal(start_d.toordinal() + i).isoformat(), close, volume)
        for i in range(n)
    ]


class BarsUntilListTests(unittest.TestCase):
    def test_returns_prefix_up_to_as_of(self) -> None:
        bars = _bars("2024-01-01", 10)
        result = bars_until_list(bars, "2024-01-05")
        self.assertEqual([b.time for b in result], [b.time for b in bars[:5]])

    def test_stops_when_time_exceeds(self) -> None:
        bars = _bars("2024-01-01", 10)
        result = bars_until_list(bars, "2024-01-03")
        self.assertEqual(len(result), 3)


class BuildLiquidUniverseAsOfTests(unittest.TestCase):
    def test_only_uses_bars_up_to_as_of(self) -> None:
        start = "2023-01-01"
        bars_a = _bars(start, 900, close=40_000, volume=500_000)
        bars_b = _bars(start, 900, close=80_000, volume=2_000_000)
        table = _table_from_bars({"AAA": bars_a, "BBB": bars_b})

        result_early = build_liquid_universe_as_of(table, "2024-06-01", max_stale_days=10000)
        result_late = build_liquid_universe_as_of(table, "2025-12-01", max_stale_days=10000)

        early_syms = {s.symbol for s in result_early}
        late_syms = {s.symbol for s in result_late}
        self.assertIn("BBB", early_syms)
        self.assertIn("AAA", early_syms)
        # The universe is a list of LiquidStock with metadata
        self.assertGreater(len(result_late), 0)

        bars_c = _bars("2025-01-01", 900, close=80_000, volume=2_000_000)
        table_with_c = _table_from_bars({"CCC": bars_c})
        result_before_c = build_liquid_universe_as_of(table_with_c, "2024-06-01", max_stale_days=10000)
        self.assertEqual({s.symbol for s in result_before_c}, set())

    def test_excludes_stale_symbols(self) -> None:
        start = "2023-01-01"
        bars_fresh = _bars(start, 900, close=80_000, volume=2_000_000)
        bars_stale = _bars(start, 365, close=80_000, volume=2_000_000)
        table = _table_from_bars({"FRESH": bars_fresh, "STALE": bars_stale})

        as_of = "2024-12-01"
        stale_last = bars_stale[-1].time
        gap_days = (date_type.fromisoformat(as_of) - date_type.fromisoformat(stale_last)).days
        if gap_days > 30:
            result = build_liquid_universe_as_of(table, as_of, max_stale_days=30)
            self.assertIn("FRESH", {s.symbol for s in result})
            self.assertNotIn("STALE", {s.symbol for s in result})

    def test_excludes_low_liquidity(self) -> None:
        start = "2023-01-01"
        bars_liquid = _bars(start, 900, close=80_000, volume=2_000_000)
        bars_illiquid = _bars(start, 900, close=80_000, volume=10_000)
        table = _table_from_bars({"LIQUID": bars_liquid, "ILLIQ": bars_illiquid})

        result = build_liquid_universe_as_of(table, "2025-06-01", max_stale_days=10000)
        syms = {s.symbol for s in result}
        self.assertIn("LIQUID", syms)
        self.assertNotIn("ILLIQ", syms)

    def test_top_n_by_avg_value(self) -> None:
        start = "2023-01-01"
        bars_big = _bars(start, 900, close=200_000, volume=5_000_000)
        bars_mid = _bars(start, 900, close=50_000, volume=2_000_000)
        bars_small = _bars(start, 900, close=20_000, volume=1_500_000)
        table = _table_from_bars({"BIG": bars_big, "MID": bars_mid, "SMALL": bars_small})

        result = build_liquid_universe_as_of(table, "2025-06-01", max_symbols=2, max_stale_days=10000)
        self.assertEqual([s.symbol for s in result], ["BIG", "MID"])

    def test_metadata_attached_when_provided(self) -> None:
        start = "2023-01-01"
        bars = _bars(start, 900, close=80_000, volume=2_000_000)
        table = _table_from_bars({"FOO": bars})
        meta = {"FOO": {"companyName": "Foo Corp", "exchangeCode": "HOSE", "sectorName": "Tech"}}
        result = build_liquid_universe_as_of(table, "2025-06-01", metadata_by_symbol=meta, max_stale_days=10000)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].companyName, "Foo Corp")
        self.assertEqual(result[0].exchangeCode, "HOSE")
        self.assertEqual(result[0].sectorName, "Tech")


class BuildCandidatePriceTableTests(unittest.TestCase):
    def test_loads_only_exchange_matches(self) -> None:
        data_dir = Path(__file__).resolve().parents[1] / "data"
        symbols_json = data_dir / "symbols.json"
        if not symbols_json.exists():
            self.skipTest("data/symbols.json not available in this checkout")
        table = build_candidate_price_table(data_dir)
        self.assertGreater(len(table), 0)
        for symbol, bars in table.items():
            self.assertGreater(len(bars), 0, f"{symbol} has no bars")
            # Bars should be sorted by time
            times = [b.time for b in bars]
            self.assertEqual(times, sorted(times), f"{symbol} bars not sorted")


if __name__ == "__main__":
    unittest.main()