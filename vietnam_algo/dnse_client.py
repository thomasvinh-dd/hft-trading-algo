from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any


DEFAULT_DNSE_BASE_URL = "https://api.dnse.com.vn/openapi"
DEFAULT_DNSE_VERSION = "2026-07-23"


def http_date() -> str:
    return datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")


def request_target(method: str, path: str, query: dict[str, object] | None = None) -> str:
    target = path
    if query:
        target = f"{target}?{urllib.parse.urlencode(query)}"
    return f"{method.lower()} {target}"


def signature_header(api_key: str, api_secret: str, method: str, path: str, date: str, nonce: str, query: dict[str, object] | None = None) -> str:
    signing_string = f"(request-target): {request_target(method, path, query)}\ndate: {date}\nnonce: {nonce}"
    digest = hmac.new(api_secret.encode("utf-8"), signing_string.encode("utf-8"), hashlib.sha256).digest()
    signature = urllib.parse.quote(base64.b64encode(digest).decode("utf-8"), safe="")
    return f'Signature keyId="{api_key}",algorithm="hmac-sha256",headers="(request-target) date",signature="{signature}",nonce="{nonce}"'


class DnseClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = DEFAULT_DNSE_BASE_URL,
        version: str = DEFAULT_DNSE_VERSION,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.version = version

    def _headers(self, method: str, path: str, query: dict[str, object] | None = None, trading_token: str | None = None) -> dict[str, str]:
        date = http_date()
        nonce = secrets.token_hex(16)
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "X-Aux-Date": date,
            "X-Signature": signature_header(self.api_key, self.api_secret, method, path, date, nonce, query),
            "version": self.version,
        }
        if trading_token:
            headers["trading-token"] = trading_token
        return headers

    def request(
        self,
        method: str,
        path: str,
        query: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
        trading_token: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any] | list[Any]:
        url = self.base_url + path
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=self._headers(method, path, query, trading_token), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DNSE request failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DNSE request failed: {exc.reason}") from exc
        return json.loads(response_body) if response_body else {}

    def accounts(self) -> dict[str, Any] | list[Any]:
        return self.request("GET", "/accounts")

    def ppse(self, account_no: str, symbol: str, loan_package_id: int, price: float) -> dict[str, Any] | list[Any]:
        return self.request(
            "GET",
            f"/accounts/{account_no}/ppse",
            {"marketType": "STOCK", "symbol": symbol.upper(), "loanPackageId": loan_package_id, "price": int(price)},
        )

    def positions(self, account_no: str, page_size: int = 100) -> dict[str, Any] | list[Any]:
        return self.request("GET", f"/accounts/{account_no}/positions", {"marketType": "STOCK", "pageSize": page_size})

    def latest_quote(self, symbol: str) -> dict[str, Any] | list[Any]:
        return self.request("GET", f"/price/{symbol.upper()}/quotes/latest")

    def send_email_otp(self) -> dict[str, Any] | list[Any]:
        return self.request("POST", "/registration/send-email-otp")

    def trading_token(self, otp_type: str, passcode: str) -> str:
        response = self.request("POST", "/registration/trading-token", body={"otpType": otp_type, "passcode": passcode})
        if not isinstance(response, dict) or "tradingToken" not in response:
            raise RuntimeError(f"DNSE trading token response missing tradingToken: {response}")
        return str(response["tradingToken"])

    def place_stock_order(
        self,
        account_no: str,
        trading_token: str,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        loan_package_id: int,
        order_type: str = "LO",
    ) -> dict[str, Any] | list[Any]:
        body = {
            "symbol": symbol.upper(),
            "loanPackageId": loan_package_id,
            "orderType": order_type,
            "price": int(round(price)),
            "quantity": quantity,
            "side": side,
        }
        return self.request(
            "POST",
            f"/accounts/{account_no}/orders",
            {"marketType": "STOCK", "orderCategory": "NORMAL"},
            body,
            trading_token=trading_token,
        )

    def order_detail(self, account_no: str, order_id: str) -> dict[str, Any] | list[Any]:
        return self.request("GET", f"/accounts/{account_no}/orders/{order_id}")

    def replace_order(
        self,
        account_no: str,
        trading_token: str,
        order_id: str,
        price: float,
        quantity: int,
        order_type: str = "LO",
    ) -> dict[str, Any] | list[Any]:
        return self.request(
            "PUT",
            f"/accounts/{account_no}/orders/{order_id}",
            body={"orderType": order_type, "price": int(round(price)), "quantity": quantity},
            trading_token=trading_token,
        )

    def cancel_order(self, account_no: str, trading_token: str, order_id: str) -> dict[str, Any] | list[Any]:
        return self.request("DELETE", f"/accounts/{account_no}/orders/{order_id}", trading_token=trading_token)
