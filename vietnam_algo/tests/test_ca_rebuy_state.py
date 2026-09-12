"""Tests for the persistent CA-rebuy state module.

The state module is the live-path equivalent of the backtest's local
``pending_corporate_action_rebuys`` and ``reserved_corporate_action_cash``
dicts. Tests cover serialize/deserialize, the queue/reserve mutators, and
expiry logic.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vietnam_algo.libs.ca_rebuy_state import (
    CaRebuyState,
    PendingRebuy,
    ReservedCash,
)
from vietnam_algo.libs.data import DEFAULT_DATA_DIR


class PendingRebuyTests(unittest.TestCase):
    def test_to_from_dict_roundtrip(self) -> None:
        pending = PendingRebuy(
            symbol="VNM",
            exitDate="2020-09-29",
            exitPrice=80277.0,
            targetValue=300_000_000.0,
            exDate="2020-09-29",
            hasHighRisk=False,
            sector="consumer",
            queuedAt="2026-08-23T10:00:00Z",
            note="rebuy after cash dividend",
        )
        d = pending.to_dict()
        reloaded = PendingRebuy.from_dict(d)
        self.assertEqual(reloaded, pending)


class ReservedCashTests(unittest.TestCase):
    def test_to_from_dict_roundtrip(self) -> None:
        reserve = ReservedCash(
            symbol="SHS",
            amount=150_000_000.0,
            exDate="2021-08-20",
            expiryIndex=1234,
            sector="real-estate",
            reservedAt="2026-08-23T10:00:00Z",
        )
        reloaded = ReservedCash.from_dict(reserve.to_dict())
        self.assertEqual(reloaded, reserve)


class CaRebuyStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_returns_empty_when_no_file(self) -> None:
        state = CaRebuyState.load(self.tmp_dir)
        self.assertEqual(state.pendingRebuys, [])
        self.assertEqual(state.reservedCash, [])

    def test_save_and_load_roundtrip(self) -> None:
        state = CaRebuyState()
        state.enqueue(
            symbol="SHS",
            exit_date="2021-08-19",
            exit_price=16622.0,
            target_value=150_000_000.0,
            ex_date="2021-08-20",
            has_high_risk=True,
            sector="real-estate",
            expiry_index=999,
            note="rebuy after rights offering",
        )
        path = state.save(self.tmp_dir)
        self.assertTrue(path.exists())

        reloaded = CaRebuyState.load(self.tmp_dir)
        self.assertEqual(len(reloaded.pendingRebuys), 1)
        self.assertEqual(len(reloaded.reservedCash), 1)
        p = reloaded.pendingRebuys[0]
        self.assertEqual(p.symbol, "SHS")
        self.assertEqual(p.exitPrice, 16622.0)
        self.assertEqual(p.hasHighRisk, True)
        self.assertEqual(p.targetValue, 150_000_000.0)
        self.assertEqual(p.exDate, "2021-08-20")
        r = reloaded.reservedCash[0]
        self.assertEqual(r.symbol, "SHS")
        self.assertEqual(r.amount, 150_000_000.0)
        self.assertEqual(r.expiryIndex, 999)

    def test_enqueue_refreshes_existing(self) -> None:
        state = CaRebuyState()
        state.enqueue(symbol="VNM", exit_date="2020-09-29", exit_price=80277.0,
                      target_value=100.0, ex_date="2020-09-29", has_high_risk=False)
        state.enqueue(symbol="VNM", exit_date="2020-09-29", exit_price=80277.0,
                      target_value=999.0, ex_date="2020-09-29", has_high_risk=False)
        # Only one pending row, refreshed with new value
        self.assertEqual(len(state.pendingRebuys), 1)
        self.assertEqual(state.pendingRebuys[0].targetValue, 999.0)

    def test_enqueue_without_expiry_index_skips_reserve(self) -> None:
        state = CaRebuyState()
        state.enqueue(symbol="AAA", exit_date="2026-01-01", exit_price=100.0,
                      target_value=50_000_000.0, ex_date="2026-01-02", has_high_risk=False,
                      expiry_index=0)  # 0 means "don't reserve"
        self.assertEqual(len(state.pendingRebuys), 1)
        self.assertEqual(len(state.reservedCash), 0)

    def test_drop_removes_both(self) -> None:
        state = CaRebuyState()
        state.enqueue(symbol="AAA", exit_date="2026-01-01", exit_price=100.0,
                      target_value=10.0, ex_date="2026-01-02", has_high_risk=False,
                      expiry_index=999)
        state.enqueue(symbol="BBB", exit_date="2026-01-01", exit_price=100.0,
                      target_value=10.0, ex_date="2026-01-02", has_high_risk=False,
                      expiry_index=999)
        state.drop("AAA")
        self.assertEqual([p.symbol for p in state.pendingRebuys], ["BBB"])
        self.assertEqual([r.symbol for r in state.reservedCash], ["BBB"])

    def test_drop_expired_drops_expired_only(self) -> None:
        state = CaRebuyState()
        state.enqueue(symbol="AAA", exit_date="2026-01-01", exit_price=100.0,
                      target_value=10.0, ex_date="2026-01-02", has_high_risk=False,
                      expiry_index=100)
        state.enqueue(symbol="BBB", exit_date="2026-01-01", exit_price=100.0,
                      target_value=10.0, ex_date="2026-01-02", has_high_risk=False,
                      expiry_index=200)
        expired = state.drop_expired(current_trading_index=150)
        self.assertEqual(expired, ["AAA"])
        self.assertEqual([p.symbol for p in state.pendingRebuys], ["BBB"])
        self.assertEqual([r.symbol for r in state.reservedCash], ["BBB"])

    def test_reserved_total(self) -> None:
        state = CaRebuyState()
        state.enqueue(symbol="AAA", exit_date="2026-01-01", exit_price=100.0,
                      target_value=100.0, ex_date="2026-01-02", has_high_risk=False,
                      expiry_index=999)
        state.enqueue(symbol="BBB", exit_date="2026-01-01", exit_price=100.0,
                      target_value=200.0, ex_date="2026-01-02", has_high_risk=False,
                      expiry_index=999)
        self.assertEqual(state.reserved_total(), 300.0)

    def test_reserved_sector_counts(self) -> None:
        state = CaRebuyState()
        state.enqueue(symbol="AAA", exit_date="2026-01-01", exit_price=100.0,
                      target_value=100.0, ex_date="2026-01-02", has_high_risk=False,
                      sector="tech", expiry_index=999)
        state.enqueue(symbol="BBB", exit_date="2026-01-01", exit_price=100.0,
                      target_value=200.0, ex_date="2026-01-02", has_high_risk=False,
                      sector="real-estate", expiry_index=999)
        state.enqueue(symbol="CCC", exit_date="2026-01-01", exit_price=100.0,
                      target_value=200.0, ex_date="2026-01-02", has_high_risk=False,
                      sector="tech", expiry_index=999)
        counts = state.reserved_sector_counts({})
        self.assertEqual(counts, {"tech": 2, "real-estate": 1})

    def test_persistence_with_real_files(self) -> None:
        # End-to-end: save to disk, load from disk, ensure JSON well-formed.
        state = CaRebuyState()
        state.enqueue(symbol="VNM", exit_date="2020-09-29", exit_price=80277.0,
                      target_value=300_000_000.0, ex_date="2020-09-29", has_high_risk=False,
                      sector="consumer", expiry_index=999, note="test note")
        path = state.save(self.tmp_dir)
        raw = json.loads(path.read_text())
        self.assertIn("pendingRebuys", raw)
        self.assertIn("reservedCash", raw)
        self.assertEqual(raw["pendingRebuys"][0]["symbol"], "VNM")
        self.assertEqual(raw["reservedCash"][0]["amount"], 300_000_000.0)


if __name__ == "__main__":
    unittest.main()
