"""Regression test for save_order_list returning Path.

Background
----------
On 2026-09-11 the momentum bot's submit cycle failed with
``'NoneType' object has no attribute 'open'`` because
``save_order_list`` stopped returning its output path. The caller
``generate_live_orders`` then passed ``None`` to
``export_orders_for_date`` which tried ``None.open(...)``.

The regression was introduced when a new helper function was added
just below ``save_order_list`` and the original ``return path``
statement was accidentally absorbed into the next function's body
(so ``save_order_list`` returned ``None`` silently). This test
ensures the return value is always the destination Path.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.backtest import BacktestResult, save_order_list


class SaveOrderListReturnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("/tmp/opencode/save_order_list_test")
        self.tmp.mkdir(parents=True, exist_ok=True)

    def test_returns_destination_path(self) -> None:
        result = BacktestResult(
            strategy="test",
            generatedAt="",
            config={},
            symbols=[],
            metrics={},
            equityCurve=[],
            trades=[],
        )
        path = save_order_list(result, self.tmp)
        self.assertIsInstance(path, Path)
        self.assertEqual(path.name, "test_orders.csv")
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
