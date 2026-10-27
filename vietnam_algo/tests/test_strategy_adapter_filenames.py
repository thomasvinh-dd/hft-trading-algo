"""Tests for backtest_orders_filename / backtest_result_filename properties.

Added in 2026-09-25 review-fix pass to enforce the abstraction that
``runner.py`` used to hardcode (see issue #2). Before this fix, the
start-of-day briefing path synthesized the file names
``liquid_momentum_rotation_orders.csv`` and
``liquid_momentum_rotation.json`` directly in the bot runner, with
no adapter method to override. If momentum renamed its files the
runner would silently miss the cached backtest and re-run a full
~6-year backtest at SOD.

After the fix:
  - ``StrategyAdapter.backtest_orders_filename`` and
    ``backtest_result_filename`` properties exist on the base class.
  - ``MomentumRotationAdapter`` overrides them to match the actual
    filenames written by ``run_momentum_rotation`` (which sets
    ``BacktestResult(strategy="liquid_momentum_rotation", ...)``).
  - ``MomentumRotationAdapter.generate_orders`` accepts ``skip_backtest``.
"""
from vietnam_algo.algos.base import StrategyAdapter
from vietnam_algo.algos.momentum import MomentumRotationAdapter
from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter


class _ConcreteAdapter(StrategyAdapter):
    """Minimal concrete subclass for testing base-class defaults."""

    name = ""
    description = "test-only"

    def generate_orders(self, config, strategy_config, trade_date=None, *, skip_backtest=False):
        return {}


def test_base_adapter_filenames_fall_back_to_name():
    """Base defaults: ``{name}_orders.csv`` / ``{name}.json``.

    Matches the convention used by ``libs.backtest.save_order_list``
    and ``libs.backtest.save_backtest_result`` when the backtest
    names ``BacktestResult.strategy = "<name>"`` (the typical case
    for any new strategy that doesn't override these).
    """
    adapter = _ConcreteAdapter()
    adapter.name = "myalgo"
    assert adapter.backtest_orders_filename == "myalgo_orders.csv"
    assert adapter.backtest_result_filename == "myalgo.json"


def test_base_adapter_filenames_have_empty_string_fallback():
    """If ``name`` is empty, the base adapter falls back to a
    generic prefix so files are still deterministic."""
    adapter = _ConcreteAdapter()
    adapter.name = ""
    assert adapter.backtest_orders_filename == "backtest_orders.csv"
    assert adapter.backtest_result_filename == "backtest.json"


def test_momentum_adapter_overrides_filenames():
    """Momentum's backtest sets
    ``BacktestResult(strategy="liquid_momentum_rotation", ...)``, so
    ``save_backtest_result`` writes to
    ``liquid_momentum_rotation.json`` and ``save_order_list`` writes
    to ``liquid_momentum_rotation_orders.csv``. The adapter must
    expose those exact names so the runner / dry-run replay can
    locate the cached files.
    """
    mom = MomentumRotationAdapter()
    assert mom.name == "momentum"
    assert mom.backtest_orders_filename == "liquid_momentum_rotation_orders.csv"
    assert mom.backtest_result_filename == "liquid_momentum_rotation.json"


def test_momentum_generate_orders_accepts_skip_backtest():
    """The base ``generate_orders`` abstract method now takes a
    ``skip_backtest`` kwarg. Momentum forwards it through to
    ``generate_live_orders``; we don't run the actual backtest here,
    only check that the kwarg is accepted (no ``TypeError``).
    """
    import inspect
    sig = inspect.signature(MomentumRotationAdapter.generate_orders)
    assert "skip_backtest" in sig.parameters
    assert sig.parameters["skip_backtest"].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["skip_backtest"].default is False


def test_autofin_generate_orders_accepts_skip_backtest():
    """Autofin doesn't run a backtest, but its ``generate_orders``
    must still accept ``skip_backtest`` for signature compatibility
    with the abstract base method. The kwarg is silently ignored.
    """
    import inspect
    sig = inspect.signature(AutofinSignalAdapter.generate_orders)
    assert "skip_backtest" in sig.parameters
    assert sig.parameters["skip_backtest"].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["skip_backtest"].default is False


def test_base_generate_orders_signature_has_skip_backtest():
    """The ABC's abstract method signature must include
    ``skip_backtest`` so all subclasses can pass it through.
    """
    import inspect
    sig = inspect.signature(StrategyAdapter.generate_orders)
    assert "skip_backtest" in sig.parameters
    assert sig.parameters["skip_backtest"].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["skip_backtest"].default is False
