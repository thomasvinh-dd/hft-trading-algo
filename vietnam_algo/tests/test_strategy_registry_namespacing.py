"""Regression test for the MomentumRotationAdapter.generate_orders
namespace bug discovered 2026-09-11:
- Strategy adapter was calling ``generate_live_orders(...)`` without
  passing ``strategy_name=self.name``.
- That triggered the legacy ``latest_orders.csv/.json`` filename and
  left ``latest_orders.momentum.csv/.json`` stale.
- The start-of-day briefing then read the namespaced file (which was
  Sept 7 HCM/HHP rows) while the dry-run state had Sept 9 PVT/VIC —
  telegram message showed the wrong symbols.

Test verifies the adapter passes strategy_name so the namespaced files
get the same contents.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.algos.registry import MomentumRotationAdapter
from vietnam_algo.libs.daily_engine import generate_live_orders, EngineConfig


class MomentumAdapterNamespacingTests(unittest.TestCase):
    def test_generate_orders_passes_strategy_name(self) -> None:
        """MomentumRotationAdapter.generate_orders() must pass
        strategy_name=self.name so latest_orders.momentum.csv/.json
        gets the latest from the backtest CSV (not an empty list
        when the calendar date has no rows)."""
        from unittest.mock import patch

        captured: dict = {}

        def fake_generate_live_orders(config, strategy_config, **kwargs):
            captured.update(kwargs)
            return {
                "result": "/tmp/r.json",
                "orders": "/tmp/o.csv",
                "latestOrdersCsv": "/tmp/lo.csv",
                "latestOrdersJson": "/tmp/lo.json",
                "tradeDate": "2026-08-25",
                "count": 2,
                "items": [],
            }

        adapter = MomentumRotationAdapter()
        with patch(
            "vietnam_algo.algos.registry.generate_live_orders",
            side_effect=fake_generate_live_orders,
        ):
            adapter.generate_orders(
                EngineConfig(
                    dataDir=Path("/tmp"),
                    strategy="momentum",
                ),
                None,
            )
        self.assertEqual(
            captured.get("strategy_name"), "momentum",
            "MomentumRotationAdapter must propagate its name so "
            "latest_orders.momentum.csv/.json is written (and the "
            "start-of-day briefing reads the same file).",
        )


if __name__ == "__main__":
    unittest.main()
