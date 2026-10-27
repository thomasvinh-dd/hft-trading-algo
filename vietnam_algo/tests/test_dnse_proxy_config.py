"""Tests for the DNSE proxy config in ``vietnam_algo/libs/dnse/client.py``.

User-reported 2026-10-12: the bot was sending "market data STALE"
warnings because the ``DNSE_PROXY_HTTP_URL`` in
``vietnam_algo/env/.env.dnse-secrets`` was still pointing to a
defunct proxy. The cron tried to route through the proxy and got
``HTTP 407 Proxy Authentication Required``, so common_sync failed
and the ``data_ready_for_eod`` sentinel was never created. The
prices got stale (last bar = 2026-08-13) and the freshness
check fired.

Fix: set ``DNSE_PROXY_HTTP_URL=`` (empty) in
``.env.dnse-secrets``. With the empty string, the DnseClient
constructor does NOT install a proxy opener and all DNSE requests
go direct. The VPS IP ``185.227.135.227`` has been whitelisted
on the DNSE OpenAPI server since 2026-09-04 — see
``vietnam_algo/docs/2026-09-04-direct-dnse-access.md``.

These tests pin the proxy-activation logic so a future regression
that re-enables the proxy (or doesn't honour the empty string)
is caught at unit-test time.
"""
from __future__ import annotations

import sys

import pytest

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# We import the client module fresh in each test because
# ``configure_http_proxy_from_env`` mutates module-level state
# (``_PROXY_OPENER``). Resetting it before each test keeps the
# tests isolated.


@pytest.fixture
def fresh_client_module(monkeypatch):
    """Reload ``vietnam_algo.libs.dnse.client`` with a clean
    ``_PROXY_OPENER`` cache for each test."""
    import vietnam_algo.libs.dnse.client as c
    c._PROXY_OPENER = None
    yield c
    c._PROXY_OPENER = None


def test_empty_proxy_url_does_not_install_opener(fresh_client_module, monkeypatch) -> None:
    """The 2026-10-12 fix: with ``DNSE_PROXY_HTTP_URL=`` (empty),
    ``configure_http_proxy_from_env()`` returns ``False`` and no
    proxy opener is installed — all DNSE requests go direct.

    This is the default in production after the VPS IP was
    whitelisted in 2026-09-04.
    """
    monkeypatch.setenv("DNSE_PROXY_HTTP_URL", "")
    result = fresh_client_module.configure_http_proxy_from_env()
    assert result is False
    assert fresh_client_module._PROXY_OPENER is None
    # is_proxy_active() should also be False
    assert fresh_client_module.is_proxy_active() is False


def test_no_proxy_env_var_does_not_install_opener(fresh_client_module, monkeypatch) -> None:
    """If the env var is not set at all (no key in the env), no
    proxy is installed. Same as the empty-string case.
    """
    monkeypatch.delenv("DNSE_PROXY_HTTP_URL", raising=False)
    result = fresh_client_module.configure_http_proxy_from_env()
    assert result is False
    assert fresh_client_module._PROXY_OPENER is None


def test_proxy_url_starts_with_https_is_accepted(fresh_client_module, monkeypatch) -> None:
    """Sanity check: an http proxy URL is parsed and installed.
    This ensures the empty-string case is the ONLY short-circuit —
    other URLs go through the proxy.
    """
    monkeypatch.setenv("DNSE_PROXY_HTTP_URL", "http://user:pass@proxy.example.com:8080")
    result = fresh_client_module.configure_http_proxy_from_env()
    assert result is True
    assert fresh_client_module._PROXY_OPENER is not None
    # is_proxy_active() should now be True
    assert fresh_client_module.is_proxy_active() is True


def test_dnse_client_constructor_does_not_install_opener_with_empty_proxy(monkeypatch) -> None:
    """The end-to-end test the user reported: with the empty
    ``DNSE_PROXY_HTTP_URL`` env var, instantiating DnseClient
    must NOT call ``install_opener`` to set up a global proxy
    opener. This is the path that was failing the common_sync
    cron on 2026-10-12.
    """
    monkeypatch.setenv("DNSE_PROXY_HTTP_URL", "")
    import vietnam_algo.libs.dnse.client as c
    c._PROXY_OPENER = None
    # Patch ``urllib.request.install_opener`` to raise if called.
    # If the empty-URL path accidentally called install_opener,
    # the test would fail with the RuntimeError.
    import urllib.request
    def boom(*a, **kw):
        raise RuntimeError(
            "install_opener should NOT be called with empty proxy URL"
        )
    monkeypatch.setattr(urllib.request, "install_opener", boom)
    # Constructing DnseClient must not raise
    c.DnseClient(api_key="k" * 8, api_secret="s" * 16)
    assert c._PROXY_OPENER is None
