"""Regression tests for the 2026-09-22 momentum EOD bug.

User feedback 2026-09-22: 'for momentum bot, I got this EOD
report ... BUY 1400 GAS @ 91,691 VND ... BUY 4300 BSR @
30,980 VND, we do not have that much of cash to do the order,
seem like it is still calculate with 1B nav' AND
'firstly seem like it's stuck in 2026-09-16 trade date'.

Two layered issues:

1. **Cash-scaling bug** — the momentum backtest defaulted to
   ``initialCash=1B VND`` (``StrategyConfig().initialCash``).
   The dry-run state uses ``DRY_RUN_INITIAL_NAV=100M VND``. The
   post-hoc ``qty_scale`` in ``_apply_dry_run_fills`` scaled the
   orders DOWN by 0.1× in memory before applying them to the
   state (so the state correctly showed 100 GAS, 400 BSR), but
   the EOD report read the ORIGINAL unscaled quantities from
   ``latest_orders.json`` (1400 GAS, 4300 BSR) and showed them
   as "✓ filled" — even though the state actually held 100/400.

   Fix: runner overrides ``strategy_config.initialCash`` to the
   dry-run ``initial_nav`` BEFORE calling
   ``strategy_adapter.generate_orders``. The backtest then emits
   orders natively at the dry-run scale (100 GAS, 400 BSR) and
   no post-hoc scaling is needed.

2. **Stale ``tradeDate`` bug** — the momentum backtest's
   ``generate_live_orders`` falls back to
   ``latest_order_date(orders_csv)`` when ``trade_date`` is
   ``None``. That returns the latest DATE WITH TRADES, which is
   often several days stale when the strategy found no signals
   on recent trading days (e.g. 2026-09-16 when today is
   2026-09-22). The EOD report then said ``tradeDate: 2026-09-16``
   even though the bot's clock said Sep 22 — confusing.

   Fix: runner passes ``trade_date=today`` to the strategy
   adapter. When the backtest has no signals for today, the CSV
   filter returns 0 rows and the report says ``orderCount: 0``
   with today's date — the truthful answer.

These tests assert the structural separation so a future "let's
share one config" tweak doesn't regress either bug.
"""
from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent


def _runner_source() -> str:
    return (_PROJECT_ROOT / "vietnam_algo" / "bots" / "runner.py").read_text(
        encoding="utf-8"
    )


def test_runner_overrides_initial_cash_for_dry_run():
    """``runner.run_auto_bot`` must override ``strategy_config.initialCash``
    to ``dry_run_state.initial_nav`` BEFORE calling
    ``strategy_adapter.generate_orders`` — for both the adapter path
    (momentum) and the legacy ``generate_live_orders`` path (hybrid).

    Without this, the momentum backtest runs at 1B VND and emits
    order quantities (e.g. 1400 GAS) that the dry-run cash (100M)
    can never afford. The state-level scale-down produces a 100-GAS
    position but the EOD report still shows 1400 because it reads
    the original (unscaled) JSON.
    """
    import re

    src = _runner_source()
    # Scope the check to the EOD block — there's also a SOD-path
    # replace() that we'd otherwise match first.
    eod_start = src.find("Running EOD data update and order generation")
    assert eod_start > 0, "EOD marker missing"
    eod_block = src[eod_start:]

    # The override uses _dc_replace(strat_cfg, initialCash=...) — find
    # the call and confirm the target is the local ``strat_cfg`` (so
    # the live ``strategy_config`` isn't mutated). Whitespace-tolerant.
    replace_pattern = re.compile(
        r"replace\s*\(\s*strat_cfg\s*,\s*initialCash\s*=",
        re.MULTILINE,
    )
    override_match = replace_pattern.search(eod_block)
    assert override_match, (
        "EOD block must dataclass-replace ``strat_cfg`` (the override "
        "copy) with the dry-run initialCash — the load-bearing fix for "
        "the 1B-vs-100M NAV scaling bug. Found no "
        "_dc_replace(strat_cfg, initialCash=...) call in the EOD path."
    )
    # And the override must happen BEFORE the strategy_adapter call.
    adapter_call = eod_block.find("strategy_adapter.generate_orders(")
    assert adapter_call > 0, "EOD block must call strategy_adapter.generate_orders"
    assert override_match.start() < adapter_call, (
        f"the initialCash override must precede the "
        f"strategy_adapter.generate_orders call (override at "
        f"{override_match.start()}, adapter call at {adapter_call}); "
        f"otherwise the backtest still runs at 1B VND and emits orders "
        f"the dry-run cash can't afford."
    )


def test_runner_passes_today_to_strategy_adapter():
    """``runner.run_auto_bot`` must pass ``trade_date=today`` to the
    momentum strategy adapter so the report's ``tradeDate`` field
    reflects TODAY, not the latest backtest CSV row (which can be
    days stale when the strategy found no recent signals)."""
    import re

    src = _runner_source()
    # Both call sites must pass trade_date=today. Whitespace-tolerant.
    adapter_pattern = re.compile(
        r"strategy_adapter\.generate_orders\s*\([^)]*trade_date\s*=\s*today",
        re.DOTALL,
    )
    legacy_pattern = re.compile(
        r"generate_live_orders\s*\([^)]*trade_date\s*=\s*today",
        re.DOTALL,
    )
    assert adapter_pattern.search(src), (
        "strategy_adapter.generate_orders must receive trade_date=today — "
        "without it the momentum backtest silently picks the latest CSV "
        "row (often days stale) and reports that as today's tradeDate."
    )
    assert legacy_pattern.search(src), (
        "generate_live_orders must also receive trade_date=today for the "
        "hybrid strategy fallback path."
    )


def test_momentum_adapter_threads_trade_date():
    """``MomentumRotationAdapter.generate_orders`` must thread
    ``trade_date`` through to ``generate_live_orders`` — otherwise
    the runner's new ``trade_date=today`` argument is dropped."""
    import re

    src = (
        _PROJECT_ROOT / "vietnam_algo" / "algos" / "momentum.py"
    ).read_text(encoding="utf-8")
    pattern = re.compile(
        r"generate_live_orders\s*\([^)]*trade_date\s*=\s*trade_date",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "MomentumRotationAdapter.generate_orders must pass "
        "trade_date=trade_date through to generate_live_orders — "
        "otherwise the runner's trade_date=today argument is silently "
        "dropped and the stale tradeDate bug returns."
    )


def test_momentum_backtest_orders_scale_with_initial_cash():
    """Sanity-check that ``StrategyConfig(initialCash=...)`` is the
    single source of truth for the backtest's NAV scale — a guard
    against future "let's restore the 1B default" tweaks that
    would re-introduce the cash-scaling bug.

    We don't run the full 6-year backtest here (it takes ~3 min
    and would dominate the test suite). Instead we just assert
    that ``StrategyConfig.initialCash`` is mutable and that the
    default is 1B VND — the structural pre-condition for the
    runner's override to matter.
    """
    from vietnam_algo.libs.config import StrategyConfig

    config_default = StrategyConfig()
    assert config_default.initialCash == 1_000_000_000, (
        f"StrategyConfig.default.initialCash is "
        f"{config_default.initialCash:,} — the runner's dry-run "
        f"NAV override is calibrated against the 1B VND default. "
        f"If the default changed, the override's "
        f"< 0.99× heuristic must be re-tuned."
    )
    # And the dataclass-replace pattern used by the runner must
    # produce a config with the new initialCash.
    from dataclasses import replace as _dc_replace
    overridden = _dc_replace(config_default, initialCash=100_000_000)
    assert overridden.initialCash == 100_000_000
    assert config_default.initialCash == 1_000_000_000, (
        "StrategyConfig.replace must return a new instance — the "
        "runner relies on this to avoid mutating the live config."
    )
