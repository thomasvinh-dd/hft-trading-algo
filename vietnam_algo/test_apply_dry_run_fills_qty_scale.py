"""Tests for the 2026-09-25 review fix #3.

Before this fix, ``_apply_dry_run_fills`` took a full ``result``
dict (the same dict ``generate_live_orders`` returns) and pulled
two paths out of it:

  * ``result["latestOrdersJson"]`` — the orders JSON to replay
  * ``result["result"]`` — the backtest result JSON; only used to
    compute the qty_scale ratio (state.initial_nav / backtest_cash)

The SOD path doesn't run a backtest, so it had to synthesise a
``_fake_result`` dict just to satisfy the second lookup. That was
a code smell because:

  * The SOD caller knew nothing about backtest artefacts.
  * The literal filename ``liquid_momentum_rotation_orders.csv``
    was hardcoded in the synthesis (defeating the adapter
    abstraction — see fix #2).
  * The function signature gave no hint that ``result`` was only
    used as a side-channel to fetch two strings.

After the fix:
  * ``_apply_dry_run_fills`` takes the orders JSON path directly
    plus an explicit ``qty_scale`` kwarg.
  * ``_compute_dry_run_qty_scale`` is a small helper that does the
    backtest-result lookup, called by EOD only.
  * The SOD path no longer builds a ``_fake_result``.

These tests pin the new contract.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vietnam_algo.bots.runner import (
    _apply_dry_run_fills,
    _compute_dry_run_qty_scale,
)


class _FakeState:
    """Minimal stand-in for ``DryRunState`` — only needs
    ``initial_nav`` for ``_compute_dry_run_qty_scale``."""

    def __init__(self, initial_nav: float) -> None:
        self.initial_nav = initial_nav


def test_compute_qty_scale_returns_one_when_no_path() -> None:
    """``None`` backtest path → ``qty_scale = 1.0`` (no scaling)."""
    assert _compute_dry_run_qty_scale(None, _FakeState(100_000_000)) == 1.0


def test_compute_qty_scale_returns_one_when_path_missing(tmp_path: Path) -> None:
    """Non-existent backtest JSON → ``qty_scale = 1.0``.

    Defensive: the helper is called from EOD with the path the
    adapter just wrote. If the path is bogus (e.g. on a fresh VPS
    where EOD runs before any backtest ever ran), the apply must
    not crash — it should just skip the rescaling.
    """
    bogus = tmp_path / "does_not_exist.json"
    assert _compute_dry_run_qty_scale(bogus, _FakeState(100_000_000)) == 1.0


def test_compute_qty_scale_returns_one_when_backtest_already_at_scale(
    tmp_path: Path,
) -> None:
    """When the backtest was already run at the dry-run NAV scale
    (e.g. because the EOD path overrode ``initialCash`` to
    ``state.initial_nav``), the backtest's ``equityCurve[0].cash``
    equals the dry-run NAV and scaling would be a no-op anyway.

    The 1.5× guard prevents accidental re-scaling in this case:
    ``qty_scale = 1.0``.
    """
    backtest = tmp_path / "result.json"
    backtest.write_text(json.dumps({
        "equityCurve": [{"cash": 100_000_000.0}],
    }))
    assert _compute_dry_run_qty_scale(
        backtest, _FakeState(100_000_000),
    ) == 1.0


def test_compute_qty_scale_returns_ratio_when_backtest_larger(tmp_path: Path) -> None:
    """Backtest at 1B VND with dry-run NAV at 100M → ``qty_scale ≈ 0.1``.

    This is the typical momentum case where the legacy backtest
    runs at 1B VND and the user's dry-run is at 100M. Without the
    scale, the planned BUY 6,200 BSR order would be clipped as
    ``insufficient_cash`` on apply.
    """
    backtest = tmp_path / "result.json"
    backtest.write_text(json.dumps({
        "equityCurve": [{"cash": 1_000_000_000.0}],
    }))
    scale = _compute_dry_run_qty_scale(backtest, _FakeState(100_000_000))
    assert abs(scale - 0.1) < 1e-9


def test_compute_qty_scale_keeps_one_for_sub_1p5x(tmp_path: Path) -> None:
    """If the backtest cash is only marginally larger than the
    dry-run NAV (less than 1.5×), the helper returns 1.0 — the
    scaling is too small to bother with."""
    backtest = tmp_path / "result.json"
    backtest.write_text(json.dumps({
        "equityCurve": [{"cash": 140_000_000.0}],
    }))
    assert _compute_dry_run_qty_scale(
        backtest, _FakeState(100_000_000),
    ) == 1.0


def test_compute_qty_scale_handles_invalid_json(tmp_path: Path) -> None:
    """Garbage in the backtest file → ``qty_scale = 1.0`` (no crash)."""
    backtest = tmp_path / "result.json"
    backtest.write_text("not json at all")
    assert _compute_dry_run_qty_scale(
        backtest, _FakeState(100_000_000),
    ) == 1.0


def test_compute_qty_scale_handles_zero_dry_run_nav(tmp_path: Path) -> None:
    """``state.initial_nav = 0`` (uninitialised) → ``qty_scale = 1.0``."""
    backtest = tmp_path / "result.json"
    backtest.write_text(json.dumps({
        "equityCurve": [{"cash": 1_000_000_000.0}],
    }))
    assert _compute_dry_run_qty_scale(backtest, _FakeState(0)) == 1.0


def test_apply_dry_run_fills_takes_path_not_result_dict(
    tmp_path: Path,
) -> None:
    """``_apply_dry_run_fills`` accepts the orders JSON path directly;
    no ``result`` dict needed.

    This is the core API change for issue #3. The function reads
    ``items`` and ``tradeDate`` from the JSON, applies them, and
    writes the dry-run state — never needing to look up the
    backtest result path or any other side-channel.
    """
    from vietnam_algo.libs.config import StrategyConfig
    from vietnam_algo.libs.daily_engine import EngineConfig

    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    orders_path = live_dir / "latest_orders.autofin_signal.json"
    orders_path.write_text(json.dumps({
        "tradeDate": "2026-09-25",
        "items": [
            {"side": "NB", "symbol": "TEST", "price": 10000,
             "quantity": 100, "reason": "autofin_buy:test:nav15pct"},
        ],
    }))

    engine_config = EngineConfig(
        dataDir=tmp_path, strategy="autofin_signal",
        updateSymbols=False, updatePrices=False,
    )
    strategy_config = StrategyConfig()

    # No ``result`` dict, no ``backtest_result.json``. The function
    # must still apply the order. qty_scale=1.0 because the orders
    # were already sized for the current NAV.
    _apply_dry_run_fills(
        orders_path, engine_config, "0003937868", strategy_config,
        strategy_name="autofin_signal", qty_scale=1.0,
    )

    from vietnam_algo.libs.dry_run_state import load_dry_run_state
    state = load_dry_run_state(tmp_path, strategy_name="autofin_signal")
    assert state is not None
    assert "TEST" in state.positions
    assert state.positions["TEST"].quantity == 100


def test_apply_dry_run_fills_scales_quantities_down(
    tmp_path: Path,
) -> None:
    """When ``qty_scale < 1.0``, BUY quantities are scaled DOWN and
    rounded to the nearest board lot (default 100). This is the
    safety net for callers that don't override ``initialCash`` to
    the dry-run NAV.

    Example: 6200 shares × 0.1 = 620 → round down to 600 (6 lots).
    """
    from vietnam_algo.libs.config import StrategyConfig
    from vietnam_algo.libs.daily_engine import EngineConfig

    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    orders_path = live_dir / "latest_orders.momentum.json"
    orders_path.write_text(json.dumps({
        "tradeDate": "2026-09-25",
        "items": [
            {"side": "BUY", "symbol": "BSR", "price": 28000,
             "quantity": 6200, "reason": "top_rank"},
        ],
    }))

    engine_config = EngineConfig(
        dataDir=tmp_path, strategy="momentum",
        updateSymbols=False, updatePrices=False,
    )
    strategy_config = StrategyConfig()

    _apply_dry_run_fills(
        orders_path, engine_config, "0003937868", strategy_config,
        strategy_name="momentum", qty_scale=0.1,
    )

    from vietnam_algo.libs.dry_run_state import load_dry_run_state
    state = load_dry_run_state(tmp_path, strategy_name="momentum")
    assert state is not None
    assert "BSR" in state.positions
    # 6200 × 0.1 = 620 → round-down to nearest 100 = 600.
    assert state.positions["BSR"].quantity == 600


def test_apply_dry_run_fills_no_op_on_empty_items(
    tmp_path: Path,
) -> None:
    """When the orders JSON has ``items: []`` the apply is a no-op
    (positions / cash unchanged). The function returns early before
    doing any scaling."""
    from vietnam_algo.libs.config import StrategyConfig
    from vietnam_algo.libs.daily_engine import EngineConfig

    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    orders_path = live_dir / "latest_orders.autofin_signal.json"
    orders_path.write_text(json.dumps({
        "tradeDate": "2026-09-25", "items": [],
    }))

    engine_config = EngineConfig(
        dataDir=tmp_path, strategy="autofin_signal",
        updateSymbols=False, updatePrices=False,
    )
    strategy_config = StrategyConfig()

    _apply_dry_run_fills(
        orders_path, engine_config, "0003937868", strategy_config,
        strategy_name="autofin_signal", qty_scale=1.0,
    )

    from vietnam_algo.libs.dry_run_state import load_dry_run_state
    state = load_dry_run_state(tmp_path, strategy_name="autofin_signal")
    assert state is not None
    assert state.positions == {}
    assert state.cash == 100_000_000
