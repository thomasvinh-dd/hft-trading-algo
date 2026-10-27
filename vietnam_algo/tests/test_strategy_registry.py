"""Tests for the strategy registry."""
from __future__ import annotations

import pytest

from vietnam_algo.algos.registry import (
    STRATEGY_REGISTRY,
    StrategyAdapter,
    get_strategy,
    list_strategies,
)


def test_registry_has_at_least_momentum():
    """The legacy momentum strategy must always be present."""
    assert "momentum" in STRATEGY_REGISTRY


def test_get_strategy_returns_registered():
    adapter = get_strategy("momentum")
    assert isinstance(adapter, StrategyAdapter)
    assert adapter.name == "momentum"


def test_get_strategy_rejects_unknown():
    with pytest.raises(ValueError) as exc:
        get_strategy("definitely-not-a-real-strategy")
    assert "Unknown strategy" in str(exc.value)
    assert "Available:" in str(exc.value)


def test_list_strategies_returns_dicts():
    items = list_strategies()
    assert isinstance(items, list)
    assert all("name" in e and "description" in e for e in items)
    # momentum entry should be present
    assert any(e["name"] == "momentum" for e in items)


def test_strategy_adapter_filenames_are_namespaced():
    a = get_strategy("momentum")
    assert a.orders_csv_filename == "latest_orders.momentum.csv"
    assert a.orders_json_filename == "latest_orders.momentum.json"
    assert a.state_filename == "auto_bot_state.momentum.json"


def test_empty_name_falls_back_to_legacy_filenames():
    """Empty-name strategy produces the legacy non-namespaced paths."""
    from vietnam_algo.algos.registry import _EmptyStrategy
    legacy = _EmptyStrategy()
    assert legacy.orders_csv_filename == "latest_orders.csv"
    assert legacy.orders_json_filename == "latest_orders.json"
    assert legacy.state_filename == "auto_bot_state.json"
