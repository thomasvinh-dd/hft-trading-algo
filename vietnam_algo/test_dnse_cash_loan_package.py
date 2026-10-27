"""Tests for ``DnseClient.get_cash_loan_package_id``.

2026-10-12: discovered during the live ABB submit at 13:48 ICT that
the DNSE server REQUIRES the ``loanPackageId`` field in every
``place_stock_order`` body, and that ``0`` is rejected as
"loanPackageId is required". The actual cash package id varies
per symbol (e.g. ``1036`` for ABB on account ``0003937868``).

This test pins the lookup logic so a future regression that picks
the wrong package (e.g. picking the first package instead of the
one with ``type == "N"``) is caught at unit-test time.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.dnse.client import DnseClient


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        if isinstance(self._payload, (dict, list)):
            import json as _json
            return _json.dumps(self._payload).encode("utf-8")
        return self._payload.encode("utf-8") if isinstance(self._payload, str) else self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class _FakeOpener:
    """A urllib opener that returns the next item in ``responses``."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, str | None, str | None]] = []

    def open(self, request, timeout=None):  # noqa: ANN001
        # Capture (method, url, body, trading-token)
        body = request.data.decode("utf-8") if request.data else None
        trading_token = request.get_header("trading-token")
        self.calls.append((request.method, request.full_url, body, trading_token))
        if not self._responses:
            raise RuntimeError("FakeOpener: no more responses queued")
        payload = self._responses.pop(0)
        # Accept dict, list, or str; serialize dicts/lists to JSON
        if isinstance(payload, (dict, list)):
            import json as _json
            text = _json.dumps(payload)
        else:
            text = str(payload)
        return _FakeResponse(text.encode("utf-8"))


def _build_client_with_opener(responses, monkeypatch, install_opener=True):
    """Build a DnseClient whose urllib.request is replaced with a fake."""
    import urllib.request
    fake = _FakeOpener(responses)
    if install_opener:
        monkeypatch.setattr(urllib.request, "install_opener", lambda o: None)
    monkeypatch.setattr(urllib.request, "urlopen", fake.open)
    c = DnseClient(api_key="k" * 8, api_secret="s" * 16, base_url="https://example.test")
    return c, fake


def test_get_cash_loan_package_id_returns_type_N(monkeypatch) -> None:
    """When the server returns multiple packages (margin + cash),
    the helper picks the one with ``type == "N"`` (normal/cash).
    """
    response = {
        "loanPackages": [
            {"id": 9999, "name": "Margin X", "type": "M"},
            {"id": 1036, "name": "GD tiền mặt", "type": "N"},
        ]
    }
    c, fake = _build_client_with_opener([response], monkeypatch)
    pkg_id = c.get_cash_loan_package_id("0003937868", "ABB")
    assert pkg_id == 1036
    # The call was a GET to the right path
    assert len(fake.calls) == 1
    method, url, body, _tok = fake.calls[0]
    assert method == "GET"
    assert "/accounts/0003937868/loan-packages" in url
    assert "marketType=STOCK" in url
    assert "symbol=ABB" in url
    assert body is None


def test_get_cash_loan_package_id_raises_when_no_cash(monkeypatch) -> None:
    """If the server returns only margin packages, raise a clear error."""
    response = {
        "loanPackages": [
            {"id": 9999, "name": "Margin X", "type": "M"},
        ]
    }
    c, _fake = _build_client_with_opener([response], monkeypatch)
    with pytest.raises(RuntimeError) as exc_info:
        c.get_cash_loan_package_id("0003937868", "ABB")
    assert "none has type='N'" in str(exc_info.value)


def test_get_cash_loan_package_id_raises_when_empty(monkeypatch) -> None:
    """If the server returns no packages, raise a clear error."""
    response = {"loanPackages": []}
    c, _fake = _build_client_with_opener([response], monkeypatch)
    with pytest.raises(RuntimeError) as exc_info:
        c.get_cash_loan_package_id("0003937868", "ABB")
    assert "no loan packages" in str(exc_info.value)


def test_place_stock_order_with_real_cash_package_id_works(monkeypatch) -> None:
    """The end-to-end path: lookup cash pkg id then place order with it."""
    lookup_response = {
        "loanPackages": [
            {"id": 1036, "name": "GD tiền mặt", "type": "N"},
        ]
    }
    place_response = {
        "id": "999",
        "orderStatus": "New",
        "loanPackageId": 1036,
    }
    c, fake = _build_client_with_opener([lookup_response, place_response], monkeypatch)
    pkg_id = c.get_cash_loan_package_id("0003937868", "ABB")
    assert pkg_id == 1036
    r = c.place_stock_order(
        account_no="0003937868",
        trading_token="tok",
        symbol="ABB",
        side="NB",
        quantity=100,
        price=17500,
        loan_package_id=pkg_id,
        order_type="LO",
    )
    assert r["orderStatus"] == "New"
    # Verify the body actually sent loanPackageId=1036 (not 0, not omitted)
    place_call = fake.calls[1]
    method, url, body, _tok = place_call
    assert method == "POST"
    import json
    parsed = json.loads(body)
    assert parsed["loanPackageId"] == 1036
    assert parsed["symbol"] == "ABB"
    assert parsed["side"] == "NB"
