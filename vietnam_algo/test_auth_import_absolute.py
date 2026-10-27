"""Regression test for the 2026-10-07 auth.py import bug.

The lazy token-refresh path in ``ensure_fresh_trading_token`` does a
relative import ``from .daily_engine import trading_token_from_email``.
But ``auth.py`` lives in ``vietnam_algo.libs.dnse`` and
``daily_engine`` is in the parent ``vietnam_algo.libs`` — there's no
``vietnam_algo.libs.dnse.daily_engine`` module. The relative path
silently resolves to a non-existent module, and the FIRST time a
live-submit bot needed a token refresh the import raised
``ModuleNotFoundError`` and crashed the submit cycle.

The fix was to use the absolute path
``from vietnam_algo.libs.daily_engine import trading_token_from_email``.

This test pins that the absolute import resolves and that the
``ensure_fresh_trading_token`` happy path no longer trips the broken
relative import.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))

from vietnam_algo.libs.dnse import auth
from vietnam_algo.libs.dnse.auth import ensure_fresh_trading_token


def test_absolute_daily_engine_import_resolves():
    """The fixed import path must succeed without ModuleNotFoundError."""
    # If the file ever reverts to ``from .daily_engine``, this fails.
    from vietnam_algo.libs.daily_engine import trading_token_from_email
    assert callable(trading_token_from_email)


def test_no_dnse_subpackage_daily_engine():
    """Sanity check: ``vietnam_algo.libs.dnse.daily_engine`` does NOT
    exist. This is why ``from .daily_engine`` was wrong — it tried
    to import a module that was never there. If somebody later adds
    that module by accident, this test fails and the team should
    re-review the import path.
    """
    with pytest.raises(ImportError):
        import vietnam_algo.libs.dnse.daily_engine  # noqa: F401


def test_ensure_fresh_trading_token_uses_absolute_import(monkeypatch):
    """Stub is_token_fresh to return False, stub the OTP flow, and
    confirm ``ensure_fresh_trading_token`` doesn't crash with
    ModuleNotFoundError on the lazy refresh path.
    """
    # Token is stale → forces the refresh path.
    monkeypatch.setattr(auth, "is_token_fresh", lambda *a, **k: False)

    # Stub out the OTP flow so we don't call real DNSE.
    from vietnam_algo.libs import daily_engine
    monkeypatch.setattr(
        daily_engine, "trading_token_from_email",
        lambda *a, **k: "stubbed-token-from-absolute-import",
    )

    client = MagicMock()
    state = MagicMock()
    state.tradingToken = ""
    state.tokenExpiry = None
    state.tokenDate = None
    state.tokenInfo = {}

    token, was_refreshed = ensure_fresh_trading_token(
        client=client,
        state=state,
        account_no="0003937868",
        live_submit=True,
        refresh_func=None,
    )
    assert token == "stubbed-token-from-absolute-import"
    assert was_refreshed is True
    assert state.tradingToken == "stubbed-token-from-absolute-import"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))