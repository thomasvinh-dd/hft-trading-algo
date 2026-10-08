"""Tests for the 4 order-management methods on DnseClient that were
missing as of 2026-10-08:

  - place_stock_order  (POST /accounts/{accountNo}/orders)
  - order_detail       (GET  /accounts/{accountNo}/orders/{orderId})
  - replace_order      (PUT  /accounts/{accountNo}/orders/{orderId})
  - cancel_order       (DELETE /accounts/{accountNo}/orders/{orderId})

These methods were the root cause of the 2026-10-08 04:30 ICT incident
where the bot's Telegram preview said
"→ DNSE: BUY 1700 ABB @ 17,400 VND" but the order was never placed
because ``DnseClient.place_stock_order`` did not exist; the call site
in ``daily_engine.execute_orders_with_ledger`` raised AttributeError,
silently swallowed by a try/except, and the ledger entry got stamped
with status="unknown".

The tests pin:
  1. The HTTP method + path (with path-var interpolation).
  2. The query parameters (marketType, orderCategory).
  3. The request body shape (NORMAL stock order).
  4. The ``trading-token`` HTTP HEADER (NOT body field).
  5. ``max_retries=0`` on writes (so a transient 503 does NOT
     re-place a real order and create a duplicate).
  6. The GET / PUT / DELETE shape of the other 3 methods.
  7. The pre-flight guard that fails loud if these methods are
     missing — the 2026-10-08 incident happened because the bot
     started --live-submit without the methods existing and nobody
     noticed.
"""
from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.dnse.client import DnseClient  # noqa: E402


# -----------------------------------------------------------------------------
# Shared fixtures (mirrors test_dnse_client.py)
# -----------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body: object, status: int = 200) -> None:
        self.body = body
        self.status_code = status

    def read(self) -> bytes:
        return json.dumps(self.body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, url: str, code: int, body: str) -> None:
        super().__init__(url, code, f"HTTP {code}", {}, None)
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body


def _client() -> DnseClient:
    return DnseClient(api_key="k" * 8, api_secret="s" * 16)


def _capture_request(monkeypatch: pytest.MonkeyPatch, response_body: object, fail_first_n: int = 0):
    """Patch ``urllib.request.urlopen`` so we can capture the request.

    Returns a dict that the test can read after the call: ``req`` (the
    last ``urllib.request.Request``) and ``attempts`` (the number of
    urlopen calls made). If ``fail_first_n > 0`` the first N calls
    raise ``_FakeHTTPError(503)``; the test asserts that a non-retrying
    write endpoint does NOT retry, but a retrying read endpoint DOES.
    """
    captured: dict = {"req": None, "attempts": 0}

    def fake_urlopen(req, timeout=20):
        captured["attempts"] += 1
        captured["req"] = req
        if captured["attempts"] <= fail_first_n:
            raise _FakeHTTPError(req.full_url, 503, "Service Unavailable")
        return _FakeResp(response_body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return captured


# -----------------------------------------------------------------------------
# place_stock_order
# -----------------------------------------------------------------------------


def test_place_stock_order_uses_correct_path_and_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /accounts/{accountNo}/orders?marketType=STOCK&orderCategory=NORMAL."""
    captured = _capture_request(monkeypatch, {"id": 12345, "status": "new"})
    client = _client()
    response = client.place_stock_order(
        account_no="0003937868",
        trading_token="abc-token-uuid",
        symbol="ABB",
        side="NB",
        quantity=1700,
        price=17400,
        loan_package_id=1775,
        order_type="LO",
    )
    assert response == {"id": 12345, "status": "new"}
    req = captured["req"]
    assert req.method == "POST"
    assert req.full_url.startswith("https://openapi.dnse.com.vn/accounts/0003937868/orders")
    # marketType + orderCategory in query
    assert "marketType=STOCK" in req.full_url
    assert "orderCategory=NORMAL" in req.full_url


def test_place_stock_order_body_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Body must be {symbol, side, orderType, price, quantity, loanPackageId}."""
    captured = _capture_request(monkeypatch, {"id": 1})
    client = _client()
    client.place_stock_order(
        account_no="0003937868",
        trading_token="abc-token-uuid",
        symbol="HPG",
        side="NB",
        quantity=100,
        price=25950,
        loan_package_id=5757,
        order_type="LO",
    )
    body = json.loads(captured["req"].data)
    assert body == {
        "symbol": "HPG",
        "side": "NB",
        "orderType": "LO",
        "price": 25950,
        "quantity": 100,
        "loanPackageId": 5757,
    }


def test_place_stock_order_sends_trading_token_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trading_token goes in the ``trading-token`` HEADER, NOT the body.

    The 2026-10-08 incident root cause: the call sites pass
    ``trading_token`` as a body kwarg (which the DNSE API ignores),
    so the server rejected every submit. Pin the header here so a
    future refactor can't regress.
    """
    captured = _capture_request(monkeypatch, {"id": 1})
    client = _client()
    client.place_stock_order(
        account_no="0003937868",
        trading_token="abc-token-uuid-1234",
        symbol="HPG",
        side="NB",
        quantity=100,
        price=25950,
        loan_package_id=5757,
        order_type="LO",
    )
    headers = {k.lower(): v for k, v in captured["req"].header_items()}
    assert headers.get("trading-token") == "abc-token-uuid-1234"
    # And NOT in the body
    body = json.loads(captured["req"].data)
    assert "tradingToken" not in body
    assert "trading_token" not in body


def test_place_stock_order_does_not_retry_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 5xx on place_stock_order must propagate as RuntimeError, NOT
    trigger a retry that could create a duplicate order.

    The instance default ``max_retries=5`` (read endpoints) is bypassed
    by ``max_retries=0`` on the write. Without this, a transient 503
    whose response was lost in transit would place the order twice
    and DNSE would not de-dupe (no idempotency key).
    """
    captured = _capture_request(monkeypatch, fail_first_n=999, response_body={})
    client = _client()
    with pytest.raises(RuntimeError, match="503"):
        client.place_stock_order(
            account_no="0003937868",
            trading_token="abc",
            symbol="HPG",
            side="NB",
            quantity=100,
            price=25950,
            loan_package_id=5757,
            order_type="LO",
        )
    assert captured["attempts"] == 1, "place_stock_order must not retry on write"


def test_place_stock_order_uppercases_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """symbol, side, orderType are normalized to upper-case before send."""
    captured = _capture_request(monkeypatch, {"id": 1})
    client = _client()
    client.place_stock_order(
        account_no="0003937868",
        trading_token="t",
        symbol="hpg",
        side="nb",
        quantity=100,
        price=25950,
        loan_package_id=5757,
        order_type="lo",
    )
    body = json.loads(captured["req"].data)
    assert body["symbol"] == "HPG"
    assert body["side"] == "NB"
    assert body["orderType"] == "LO"


def test_place_stock_order_supports_sell_side(monkeypatch: pytest.MonkeyPatch) -> None:
    """Side 'NS' (bán) passes through."""
    captured = _capture_request(monkeypatch, {"id": 1})
    client = _client()
    client.place_stock_order(
        account_no="0003937868",
        trading_token="t",
        symbol="HPG",
        side="NS",
        quantity=100,
        price=27000,
        loan_package_id=5757,
        order_type="LO",
    )
    body = json.loads(captured["req"].data)
    assert body["side"] == "NS"


# -----------------------------------------------------------------------------
# order_detail
# -----------------------------------------------------------------------------


def test_order_detail_uses_get_with_order_id_in_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_request(monkeypatch, {"id": 999, "orderStatus": "filled", "filledQuantity": 100})
    client = _client()
    detail = client.order_detail("0003937868", 999)
    assert detail["orderStatus"] == "filled"
    req = captured["req"]
    assert req.method == "GET"
    assert req.full_url == "https://openapi.dnse.com.vn/accounts/0003937868/orders/999"


def test_order_detail_sends_trading_token_header_when_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_request(monkeypatch, {"id": 1, "orderStatus": "new"})
    client = _client()
    client.order_detail("0003937868", "abc-123", trading_token="tkn-uuid")
    headers = {k.lower(): v for k, v in captured["req"].header_items()}
    assert headers.get("trading-token") == "tkn-uuid"


def test_order_detail_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Order-detail GETs are not retried (max_retries=0) because the
    bot calls this in a tight monitor loop and the response is read
    straight back into the ledger."""
    captured = _capture_request(monkeypatch, fail_first_n=999, response_body={})
    client = _client()
    with pytest.raises(RuntimeError):
        client.order_detail("0003937868", 1)
    assert captured["attempts"] == 1


# -----------------------------------------------------------------------------
# replace_order
# -----------------------------------------------------------------------------


def test_replace_order_uses_put_with_correct_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_request(monkeypatch, {"id": 999, "orderStatus": "pendingReplace"})
    client = _client()
    response = client.replace_order(
        account_no="0003937868",
        trading_token="tkn",
        order_id=999,
        price=26000,
        quantity=200,
    )
    assert response["orderStatus"] == "pendingReplace"
    req = captured["req"]
    assert req.method == "PUT"
    assert req.full_url.startswith("https://openapi.dnse.com.vn/accounts/0003937868/orders/999")
    body = json.loads(req.data)
    assert body == {"price": 26000, "quantity": 200}


def test_replace_order_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_request(monkeypatch, fail_first_n=999, response_body={})
    client = _client()
    with pytest.raises(RuntimeError):
        client.replace_order("0003937868", "tkn", 1, 100, 100)
    assert captured["attempts"] == 1


# -----------------------------------------------------------------------------
# cancel_order
# -----------------------------------------------------------------------------


def test_cancel_order_uses_delete_with_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_request(monkeypatch, {"id": 999, "orderStatus": "canceled"})
    client = _client()
    response = client.cancel_order(
        account_no="0003937868",
        trading_token="tkn",
        order_id=999,
    )
    assert response["orderStatus"] == "canceled"
    req = captured["req"]
    assert req.method == "DELETE"
    assert req.full_url == "https://openapi.dnse.com.vn/accounts/0003937868/orders/999"
    # Cancel has no body
    assert req.data is None


def test_cancel_order_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_request(monkeypatch, fail_first_n=999, response_body={})
    client = _client()
    with pytest.raises(RuntimeError):
        client.cancel_order("0003937868", "tkn", 1)
    assert captured["attempts"] == 1


# -----------------------------------------------------------------------------
# Pre-flight guard: DnseClient has the 4 order methods
# -----------------------------------------------------------------------------


def test_dnse_client_has_all_four_order_methods() -> None:
    """Pin the 4 method names so the 2026-10-08 incident cannot recur.

    The 2026-10-08 04:30 ICT incident happened because
    ``DnseClient.place_stock_order`` did not exist. The bot was
    started with ``--live-submit`` and the call site in
    ``daily_engine.execute_orders_with_ledger`` was calling
    ``client.place_stock_order(...)`` — Python raised AttributeError,
    the surrounding try/except silently swallowed it, and the user
    was told "→ DNSE: BUY ..." with no order actually placed.

    This test asserts the 4 method names exist on the class so that
    any future refactor that removes one (or any code path that
    imports a stale version) fails loud at unit-test time.
    """
    for name in ("place_stock_order", "order_detail", "replace_order", "cancel_order"):
        assert callable(getattr(DnseClient, name, None)), (
            f"DnseClient.{name} is missing — see 2026-10-08 incident "
            f"in vietnam_algo/docs/dnse-api/README.md"
        )


def test_dnse_client_request_max_retries_override_disables_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """``request(..., max_retries=0)`` must skip the retry loop even
    on a 5xx — a read endpoint that the caller wants to fail-fast on."""
    captured = _capture_request(monkeypatch, fail_first_n=999, response_body={})
    client = _client()
    with pytest.raises(RuntimeError):
        client.request("GET", "/accounts", max_retries=0)
    assert captured["attempts"] == 1
