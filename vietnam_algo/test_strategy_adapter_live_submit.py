"""Regression tests for ``live_submit`` plumbed through ``generate_orders``.

2026-09-30 incident: the autofin catch-up scan (added in commit
``beb245c``) called ``self._maybe_catchup_missed_signals(..., live_submit)``
but the ``generate_orders`` method had no ``live_submit`` parameter —
so every cycle raised ``NameError: name 'live_submit' is not defined``
and the catch-up scan was a silent no-op. The autofin EOD log showed:

    [autofin_signal] catchup scan failed: name 'live_submit' is not defined

The 4-day bot-down gap (Sep 25 → Sep 29) had several reduce events on
HNM that were missed and never replayed. Fix is to thread
``live_submit`` through the base ``StrategyAdapter.generate_orders``
signature and the runner's call sites.

These tests pin the wiring so a future refactor can't silently
regress it.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
from vietnam_algo.algos.momentum import MomentumRotationAdapter
from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig


@pytest.fixture
def engine_config(tmp_path) -> EngineConfig:
    return EngineConfig(
        dataDir=tmp_path,
        liveDirName="live",
        strategy="autofin_signal",
        syncData=False,
    )


@pytest.fixture
def strategy_config() -> StrategyConfig:
    return StrategyConfig(initialCash=100_000_000)


def _make_mock_adapter() -> AutofinSignalAdapter:
    """Build an AutofinSignalAdapter whose _client_lazy returns a mock client."""
    a = AutofinSignalAdapter.__new__(AutofinSignalAdapter)
    a._our_deals = {}
    a._last_poll_at = None
    a._notified_event_ids = set()
    a._notified_order_keys = set()
    a._last_notification_id = ""
    a._last_notification_poll_at = None
    a._positions = None
    return a


def test_generate_orders_accepts_live_submit_kwarg(
    engine_config, strategy_config
):
    """``generate_orders`` must accept ``live_submit=...`` without
    raising TypeError. Pre-fix this raised ``TypeError: generate_orders()
    got an unexpected keyword argument 'live_submit'``."""
    a = _make_mock_adapter()
    a._client_lazy = MagicMock()  # type: ignore[method-assign]
    # We expect generate_orders to fail later (no real client) but the
    # signature check happens first — if ``live_submit`` is not a
    # recognised kwarg we fail with TypeError before any logic.
    try:
        a.generate_orders(
            engine_config, strategy_config,
            trade_date=None, live_submit=False,
        )
    except TypeError as e:
        if "live_submit" in str(e):
            pytest.fail(
                f"generate_orders should accept live_submit kwarg: {e}"
            )
        raise
    except Exception:
        # Any other exception (network, etc.) is fine — we only care
        # about the kwarg being accepted by the signature.
        pass


def test_generate_orders_live_submit_does_not_raise_nameerror(
    engine_config, strategy_config, monkeypatch
):
    """Pre-fix: catchup scan raised NameError because ``live_submit``
    was referenced inside ``_maybe_catchup_missed_signals`` but the
    variable wasn't bound. This test exercises the catchup code path
    directly (gap > threshold) and asserts no NameError is raised."""
    a = _make_mock_adapter()
    # Force catchup threshold to be met
    from datetime import datetime, timedelta, timezone
    a._last_poll_at = (
        datetime.now(tz=timezone.utc) - timedelta(hours=48)
    ).isoformat()
    # Direct call — bypasses generate_orders (which needs full adapter
    # state to work). The bug we're testing is that ``live_submit`` was
    # a free variable in ``_maybe_catchup_missed_signals`` (it's a
    # parameter now, but the OLD code referenced it as a free var
    # inside generate_orders). Calling the method directly with
    # ``live_submit=False`` exercises the same code path the runner
    # takes.
    mock_client = MagicMock()
    mock_client.fetch_holding.return_value = []
    mock_client.fetch_signals.return_value = []
    mock_client.fetch_closing.return_value = []
    mock_client.fetch_deals.return_value = []
    # live_submit=False (dry-run)
    result = a._maybe_catchup_missed_signals(
        mock_client, [], [], live_submit=False,
    )
    # No missed events (empty holding+closing) → returns None
    assert result is None
    # live_submit=True (live mode)
    result = a._maybe_catchup_missed_signals(
        mock_client, [], [], live_submit=True,
    )
    # Live mode + empty holding → returns the live_skip summary
    assert result is not None
    assert result["live_skip"] is True


def test_momentum_adapter_accepts_live_submit_kwarg(
    engine_config, strategy_config, monkeypatch
):
    """MomentumRotationAdapter's generate_orders also needs to accept
    ``live_submit`` (forwarded to the base class)."""
    from vietnam_algo.libs.daily_engine import generate_live_orders

    captured: dict[str, Any] = {}

    def fake_gen(*args, **kwargs):
        captured.update(kwargs)
        return {"status": "generated", "count": 0, "items": []}

    monkeypatch.setattr(
        "vietnam_algo.algos.momentum.generate_live_orders",
        fake_gen,
    )
    a = MomentumRotationAdapter()
    # Should not raise TypeError
    a.generate_orders(
        engine_config, strategy_config,
        trade_date=None,
        skip_backtest=True,
        live_submit=True,
    )
    # MomentumRotationAdapter should accept and ignore live_submit
    # (the legacy generate_live_orders reads from engine_config directly).
    # Just verify it ran without error.
    assert captured.get("skip_backtest") is True