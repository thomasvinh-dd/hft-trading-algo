"""Tests for the 2026-09-25 review fix #1: split ``generate_live_orders``.

Before this fix, both the 09:16 submit cycle and the 15:15 EOD
cycle ran the full momentum backtest (~6 years, ~3 minutes
wall-clock). The EOD run produced the same result as the 09:16
run — the only difference was that EOD also called
``export_orders_for_date`` to filter by today's date and write
``latest_orders.<strategy>.{csv,json}``.

After this fix, ``generate_live_orders`` accepts ``skip_backtest=True``:

  * If the cached orders CSV (``data/backtests/{adapter.backtest_orders_filename}``)
    exists, the function reads it and only runs the cheap
    ``export_orders_for_date`` step. No backtest execution.
  * If the cache is missing (cold VPS), it falls back to running
    the full backtest. The skip is best-effort.

The EOD call site in ``bots/runner.py`` now passes
``skip_backtest=True``, saving ~3 min/day of compute.

Tests
-----

* ``test_skip_backtest_reuses_cached_orders`` — skip path reads
  the existing CSV without invoking ``strategy_runner``.
* ``test_skip_backtest_payload_omits_metrics`` — when skipping
  the backtest the payload doesn't carry ``metrics`` (the EOD
  Telegram formatter hides the metrics block in dry-run mode
  anyway, so this is the safe behaviour).
* ``test_skip_backtest_falls_back_when_cache_missing`` — if the
  cached CSV isn't on disk, the function runs the full backtest.
* ``test_full_path_passes_skip_backtest_through`` —
  ``MomentumRotationAdapter.generate_orders(skip_backtest=True)``
  forwards the flag to ``generate_live_orders``.
* ``test_skip_backtest_preserves_trade_date_filter`` — the
  skip path still respects the ``trade_date`` arg when filtering.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["orderId", "date", "symbol", "side", "price",
                  "quantity", "value", "fee", "reason"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})


def _make_orders_csv(backtests_dir: Path) -> Path:
    """Mimic the ``liquid_momentum_rotation_orders.csv`` the
    09:16 submit cycle would have written. Two trades, one for
    2026-09-25 and one for 2026-09-24."""
    orders = backtests_dir / "liquid_momentum_rotation_orders.csv"
    _write_csv(orders, [
        {"orderId": "1", "date": "2026-09-24", "symbol": "PVT",
         "side": "BUY", "price": "22000", "quantity": "100",
         "value": "2200000", "fee": "3300", "reason": "top_rank"},
        {"orderId": "2", "date": "2026-09-25", "symbol": "BSR",
         "side": "BUY", "price": "28000", "quantity": "600",
         "value": "16800000", "fee": "25200", "reason": "top_rank"},
    ])
    return orders


def test_skip_backtest_reuses_cached_orders(tmp_path: Path) -> None:
    """``skip_backtest=True`` reads the cached orders CSV without
    invoking ``strategy_runner`` (the heavy ~3-min backtest).

    We patch ``strategy_runner`` to a sentinel that raises — if
    the function tried to run the backtest the test fails.
    """
    from vietnam_algo.libs.daily_engine import generate_live_orders
    from vietnam_algo.libs.config import StrategyConfig

    backtests_dir = tmp_path / "backtests"
    cached_orders = _make_orders_csv(backtests_dir)

    engine_config = type("E", (), {
        "dataDir": tmp_path,
        "strategy": "momentum",
        "liveDirName": "live",
    })()
    strategy_config = StrategyConfig()

    def _must_not_run(*args, **kwargs):
        raise AssertionError(
            "strategy_runner must NOT be called when skip_backtest=True "
            "and a cached orders CSV is available."
        )

    with patch(
        "vietnam_algo.libs.daily_engine.strategy_runner",
        side_effect=_must_not_run,
    ):
        payload = generate_live_orders(
            engine_config, strategy_config,
            trade_date="2026-09-25",
            strategy_name="momentum",
            skip_backtest=True,
        )

    # Sanity: the orders CSV was reused, the per-date export wrote
    # today's row only.
    assert payload["latestOrdersJson"].endswith("latest_orders.momentum.json")
    json_path = Path(payload["latestOrdersJson"])
    assert json_path.exists()
    items = json.loads(json_path.read_text())["items"]
    assert len(items) == 1
    assert items[0]["symbol"] == "BSR"
    assert items[0]["date"] == "2026-09-25"
    assert items[0]["quantity"] == "600"


def test_skip_backtest_payload_omits_metrics(tmp_path: Path) -> None:
    """Skipping the backtest means ``metrics`` isn't computed and
    shouldn't appear in the payload (the EOD Telegram formatter
    hides the metrics block in dry-run mode anyway, so this is
    the safe behaviour)."""
    from vietnam_algo.libs.daily_engine import generate_live_orders
    from vietnam_algo.libs.config import StrategyConfig

    backtests_dir = tmp_path / "backtests"
    _make_orders_csv(backtests_dir)

    engine_config = type("E", (), {
        "dataDir": tmp_path,
        "strategy": "momentum",
        "liveDirName": "live",
    })()
    strategy_config = StrategyConfig()

    with patch("vietnam_algo.libs.daily_engine.strategy_runner") as mock_runner:
        payload = generate_live_orders(
            engine_config, strategy_config,
            trade_date="2026-09-25",
            strategy_name="momentum",
            skip_backtest=True,
        )

    assert "metrics" not in payload, (
        f"skip_backtest=True must omit metrics; got {payload.get('metrics')}"
    )
    assert "result" not in payload, (
        f"skip_backtest=True must omit 'result' (backtest JSON path); "
        f"got {payload.get('result')}"
    )
    # strategy_runner was NOT called.
    mock_runner.assert_not_called()


def test_skip_backtest_falls_back_when_cache_missing(tmp_path: Path) -> None:
    """If the cached orders CSV doesn't exist (fresh VPS, never
    ran 09:16 yet), ``skip_backtest=True`` falls back to running
    the full backtest. The skip is best-effort — no crash."""
    from vietnam_algo.libs.daily_engine import generate_live_orders
    from vietnam_algo.libs.config import StrategyConfig
    from vietnam_algo.libs.backtest import BacktestResult

    # No cached CSV.
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)

    engine_config = type("E", (), {
        "dataDir": tmp_path,
        "strategy": "momentum",
        "liveDirName": "live",
    })()
    strategy_config = StrategyConfig()

    fake_result = BacktestResult(
        strategy="liquid_momentum_rotation",
        generatedAt="2026-09-25T15:15:00Z",
        config={"initialCash": 1_000_000_000},
        symbols=["BSR"],
        metrics={"initialCash": 1_000_000_000, "finalEquity": 1_050_000_000,
                 "totalReturnPercent": 5.0, "maxDrawdownPercent": -2.0},
        equityCurve=[{"date": "2026-09-25", "cash": 1_000_000_000.0}],
        trades=[],
    )

    # Pre-create the fake orders CSV so ``export_orders_for_date``
    # can open it (the function reads it to filter rows by date).
    fake_orders_csv = backtests_dir / "fake_orders.csv"
    _write_csv(fake_orders_csv, [
        {"orderId": "1", "date": "2026-09-25", "symbol": "BSR",
         "side": "BUY", "price": "28000", "quantity": "600",
         "value": "16800000", "fee": "25200", "reason": "top_rank"},
    ])
    fake_result_json = backtests_dir / "fake_result.json"

    with patch(
        "vietnam_algo.libs.daily_engine._run_strategy_backtest",
        return_value=(
            fake_result,
            fake_result_json,
            fake_orders_csv,
        ),
    ) as mock_runner:
        payload = generate_live_orders(
            engine_config, strategy_config,
            trade_date="2026-09-25",
            strategy_name="momentum",
            skip_backtest=True,
        )

    # The full backtest ran as fallback.
    mock_runner.assert_called_once()
    # Payload now has metrics (from the fallback backtest).
    assert "metrics" in payload
    assert payload["metrics"]["totalReturnPercent"] == 5.0


def test_full_path_passes_skip_backtest_through(tmp_path: Path) -> None:
    """``MomentumRotationAdapter.generate_orders(skip_backtest=True)``
    forwards the flag to ``generate_live_orders``."""
    from vietnam_algo.algos.momentum import MomentumRotationAdapter
    from vietnam_algo.libs.config import StrategyConfig

    backtests_dir = tmp_path / "backtests"
    _make_orders_csv(backtests_dir)

    engine_config = type("E", (), {
        "dataDir": tmp_path,
        "strategy": "momentum",
        "liveDirName": "live",
    })()
    strategy_config = StrategyConfig()

    adapter = MomentumRotationAdapter()
    with patch("vietnam_algo.libs.daily_engine.strategy_runner") as mock_runner:
        adapter.generate_orders(
            engine_config, strategy_config,
            trade_date="2026-09-25",
            skip_backtest=True,
        )
    # If skip_backtest was forwarded, strategy_runner was NOT called
    # because the cached CSV exists.
    mock_runner.assert_not_called()


def test_skip_backtest_preserves_trade_date_filter(tmp_path: Path) -> None:
    """Even on the skip path, the ``trade_date`` arg still filters
    the cached orders to just today's row."""
    from vietnam_algo.libs.daily_engine import generate_live_orders
    from vietnam_algo.libs.config import StrategyConfig

    backtests_dir = tmp_path / "backtests"
    _make_orders_csv(backtests_dir)

    engine_config = type("E", (), {
        "dataDir": tmp_path,
        "strategy": "momentum",
        "liveDirName": "live",
    })()
    strategy_config = StrategyConfig()

    with patch("vietnam_algo.libs.daily_engine.strategy_runner"):
        # Explicitly ask for 2026-09-24 instead of 2026-09-25.
        payload = generate_live_orders(
            engine_config, strategy_config,
            trade_date="2026-09-24",
            strategy_name="momentum",
            skip_backtest=True,
        )

    items = json.loads(Path(payload["latestOrdersJson"]).read_text())["items"]
    assert len(items) == 1
    assert items[0]["symbol"] == "PVT"  # the 09-24 row
    assert items[0]["date"] == "2026-09-24"
