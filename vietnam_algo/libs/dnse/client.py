from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

_PROXY_OPENER: urllib.request.OpenerDirector | None = None


def configure_http_proxy_from_env() -> bool:
    """Configure HTTP/HTTPS proxy from `DNSE_PROXY_HTTP_URL` env var.

    Format: ``http://user:pass@host:port`` (user/pass optional).
    Replaces ``urllib.request.urlopen`` globally so every request goes
    through the proxy. Idempotent: re-applying is a no-op.

    Returns True when proxy is now active, False when no env var set.
    """
    global _PROXY_OPENER
    if _PROXY_OPENER is not None:
        return True
    url = os.environ.get("DNSE_PROXY_HTTP_URL", "").strip()
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError(f"Only http(s):// supported, got {parsed.scheme}")
    host = parsed.hostname
    if not host:
        raise RuntimeError(f"Missing host in DNSE_PROXY_HTTP_URL: {url}")
    proxy_url = (
        f"http://{urllib.parse.quote(parsed.username or '', safe='')}:"
        f"{urllib.parse.quote(parsed.password or '', safe='')}@"
        f"{host}:{parsed.port or 8080}"
    ) if parsed.username else url
    # Note: urllib ProxyHandler sends auth via base64 in HTTP headers;
    # putting credentials in the URL works for basic auth proxies.
    proxy_handler = urllib.request.ProxyHandler({
        "http": proxy_url,
        "https": proxy_url,
    })
    _PROXY_OPENER = urllib.request.build_opener(proxy_handler)
    return True


def use_proxy_opener() -> bool:
    """Install the proxy opener as the global urllib opener (if configured)."""
    if _PROXY_OPENER is None:
        return False
    urllib.request.install_opener(_PROXY_OPENER)
    return True


def is_proxy_active() -> bool:
    return _PROXY_OPENER is not None


DEFAULT_DNSE_BASE_URL = "https://openapi.dnse.com.vn"
DEFAULT_DNSE_VERSION = "2026-07-23"
DEFAULT_PRICE_DIR = "prices_dnse"
DEFAULT_INSTRUMENTS_FILE = "instruments_dnse.json"
DEFAULT_PAGE_SLEEP_SECONDS = 0.6
DEFAULT_DAILY_BAR_TYPE = "STOCK"


def http_date() -> str:
    return datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S %z")


def signing_string(method: str, path: str, date_value: str, nonce: str) -> str:
    # DNSE spec: signing string uses path-only (no query). Caller passes the
    # path-with-query for URL building; we strip before signing.
    path_only = path.split("?", 1)[0]
    return (
        f"(request-target): {method.lower()} {path_only}\n"
        f"date: {date_value}\n"
        f"nonce: {nonce}"
    )


def signature_header(api_key: str, api_secret: str, method: str, path: str, date_value: str, nonce: str) -> str:
    payload = signing_string(method, path, date_value, nonce)
    digest = hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    sig = urllib.parse.quote(base64.b64encode(digest).decode("utf-8"), safe="")
    return (
        f'Signature keyId="{api_key}",algorithm="hmac-sha256",'
        f'headers="(request-target) date",signature="{sig}",nonce="{nonce}"'
    )


def sign_url_path(method: str, path: str) -> str:
    """Strip the query string off a path before passing to the signer.

    Helper for places that already hold a path-with-query.
    """
    return path.split("?", 1)[0]


@dataclass
class DnseAccount:
    id: str
    deal_account: bool
    derivative_account: bool
    custody_code: str | None = None
    derivative_status: str | None = None


def parse_accounts(payload: Any) -> tuple[str | None, list[DnseAccount]]:
    investor_id: str | None = None
    accounts: list[DnseAccount] = []
    if not isinstance(payload, dict):
        return investor_id, accounts
    investor_id = payload.get("investorId") or payload.get("investor_id")
    raw_accounts = payload.get("accounts")
    if isinstance(raw_accounts, list):
        for raw in raw_accounts:
            if not isinstance(raw, dict):
                continue
            derivative = raw.get("derivative") if isinstance(raw.get("derivative"), dict) else {}
            accounts.append(
                DnseAccount(
                    id=str(raw.get("id") or raw.get("accountNo") or ""),
                    deal_account=bool(raw.get("dealAccount")),
                    derivative_account=bool(raw.get("derivativeAccount")),
                    custody_code=payload.get("custodyCode") or payload.get("custody_code"),
                    derivative_status=str(derivative.get("status")) if derivative else None,
                )
            )
    return investor_id, accounts


def parse_price_bars(payload: Any) -> list[dict[str, Any]]:
    """Return a list of {time, open, high, low, close, volume} dicts from any DNSE bar response.

    DNSE returns bars as a dict ``{t: [...], o: [...], h: [...], l: [...], c: [...], v: [...]}``
    with positional alignment. Also tolerates a list-of-dicts with any of those keys.
    """
    if payload is None:
        return []
    if isinstance(payload, dict):
        candidates = (payload.get(key) for key in ("data", "bars", "candles", "items", "result"))
        if isinstance(payload.get("t"), list):
            t = payload["t"]
            opens = payload.get("o") or []
            highs = payload.get("h") or []
            lows = payload.get("l") or []
            closes = payload.get("c") or []
            vols = payload.get("v") or []
            out: list[dict[str, Any]] = []
            for idx, ts_value in enumerate(t):
                try:
                    if isinstance(ts_value, (int, float)):
                        seconds = float(ts_value) / 1000.0 if float(ts_value) > 1e15 else float(ts_value)
                        ts_iso = datetime.fromtimestamp(seconds, tz=UTC).date().isoformat()
                    else:
                        ts_iso = str(ts_value).split("T")[0][:10]
                    close = closes[idx] if idx < len(closes) else None
                    if close is None:
                        continue
                    open_v = opens[idx] if idx < len(opens) else close
                    high_v = highs[idx] if idx < len(highs) else close
                    low_v = lows[idx] if idx < len(lows) else close
                    vol_v = vols[idx] if idx < len(vols) else 0
                    out.append(
                        {
                            "time": ts_iso,
                            "open": float(open_v),
                            "high": float(high_v),
                            "low": float(low_v),
                            "close": float(close),
                            "volume": float(vol_v or 0),
                        }
                    )
                except (TypeError, ValueError, IndexError):
                    continue
            out.sort(key=lambda item: item["time"])
            return out

        for candidate in candidates:
            if isinstance(candidate, list):
                rows = candidate
                break
        else:
            return []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []

    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ts_value = row.get("time") or row.get("t") or row.get("date") or row.get("tradingDate")
            if ts_value is None:
                continue
            if isinstance(ts_value, (int, float)):
                seconds = float(ts_value) / 1000.0 if float(ts_value) > 1e15 else float(ts_value)
                ts_iso = datetime.fromtimestamp(seconds, tz=UTC).date().isoformat()
            else:
                ts_iso = str(ts_value).split("T")[0][:10]
            close = row.get("close") or row.get("c") or row.get("closePrice")
            if close is None:
                continue
            open_v = row.get("open") or row.get("o") or row.get("openPrice")
            high_v = row.get("high") or row.get("h") or row.get("highPrice")
            low_v = row.get("low") or row.get("l") or row.get("lowPrice")
            vol_v = row.get("volume") or row.get("v") or 0
            out.append(
                {
                    "time": ts_iso,
                    "open": float(open_v if open_v is not None else close),
                    "high": float(high_v if high_v is not None else close),
                    "low": float(low_v if low_v is not None else close),
                    "close": float(close),
                    "volume": float(vol_v or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda item: item["time"])
    # Dedup on time — last-wins (so a re-fetch replaces stale values)
    deduped: dict[str, dict[str, Any]] = {}
    for bar in out:
        deduped[bar["time"]] = bar
    return sorted(deduped.values(), key=lambda item: item["time"])


def parse_instruments(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw = payload.get("data")
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        if not symbol:
            continue
        market = item.get("marketId") or item.get("market") or ""
        if market and market != "STO":
            continue
        out.append(
            {
                "symbol": symbol,
                "isin": item.get("isin"),
                "shortName": item.get("shortName"),
                "name": item.get("name"),
                "symbolType": item.get("symbolType"),
                "listedDate": item.get("listedDate"),
            }
        )
    return out


def utc_timestamp(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp())


def load_trading_token(path: "Path") -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def is_trading_token_fresh(payload: dict[str, Any], now: datetime | None = None, leeway_seconds: int = 600) -> bool:
    if not isinstance(payload, dict):
        return False
    expires_at = payload.get("expiresAt")
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current + timedelta(seconds=leeway_seconds) < expires


class DnseClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = DEFAULT_DNSE_BASE_URL,
        version: str = DEFAULT_DNSE_VERSION,
        date_header_name: str | None = None,
        request_timeout: float = 20.0,
        max_retries: int = 5,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.version = version
        env_name = os.environ.get("DATE_HEADER")
        self.date_header_name = date_header_name or env_name or "Date"
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        # Optional HTTP/HTTPS proxy for geo-restricted endpoints (e.g. Vietnamese IPs)
        if os.environ.get("DNSE_PROXY_HTTP_URL"):
            if configure_http_proxy_from_env():
                use_proxy_opener()

    def _signed_headers(self, method: str, path: str) -> dict[str, str]:
        date_value = http_date()
        nonce = secrets.token_hex(16)
        header_value = signature_header(
            self.api_key, self.api_secret, method, path, date_value, nonce
        )
        return {
            self.date_header_name: date_value,
            "x-api-key": self.api_key,
            "version": self.version,
            "X-Signature": header_value,
            "Accept": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        query: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
        extra_headers: dict[str, str] | None = None,
        version: str | None = None,
    ) -> dict[str, Any] | list[Any]:
        # NOTE: query string is included in the URL but NOT inside the signing string (DNSE spec)
        url = self.base_url + path
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = self._signed_headers(method, path)
        if version is not None:
            headers["version"] = version
        if body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        data = json.dumps(body).encode("utf-8") if body is not None else None

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                    raw = response.read().decode("utf-8")
                    if not raw:
                        return {}
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_exc = RuntimeError(
                    f"DNSE {method} {path} -> HTTP {exc.code} {detail[:200]}"
                )
                if exc.code in (429, 503) and attempt < self.max_retries:
                    sleep_s = min(60.0, 2.0 ** (attempt + 1))
                    time.sleep(sleep_s)
                    continue
                raise last_exc from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                last_exc = RuntimeError(f"DNSE {method} {path} -> {exc.reason if hasattr(exc, 'reason') else exc}")
                if attempt < self.max_retries:
                    time.sleep(2.0 ** (attempt + 1))
                    continue
                raise last_exc from exc
        raise last_exc if last_exc else RuntimeError(f"DNSE {method} {path} -> exhausted retries")

    # =========================================================================
    # Account / Trading endpoints
    # =========================================================================
    def get_accounts(self) -> dict[str, Any] | list[Any]:
        return self.request("GET", "/accounts")

    def get_balances(self, account_no: str) -> dict[str, Any] | list[Any]:
        return self.request("GET", f"/accounts/{account_no}/balances")

    def get_loan_packages(self, account_no: str, market_type: str = "STOCK", symbol: str | None = None) -> dict[str, Any] | list[Any]:
        query: dict[str, object] = {"marketType": market_type}
        if symbol:
            query["symbol"] = symbol
        return self.request("GET", f"/accounts/{account_no}/loan-packages", query)

    def list_loan_packages(self, account_no: str, market_type: str = "STOCK", symbol: str | None = None) -> list[dict[str, Any]]:
        """Return the flattened list of loan packages (DNSE wraps them in `{loanPackages: [...]}`)."""
        response = self.get_loan_packages(account_no, market_type=market_type, symbol=symbol)
        if isinstance(response, dict):
            nested = response.get("loanPackages")
            if isinstance(nested, list):
                return nested
            if isinstance(nested, dict) and "data" in nested and isinstance(nested["data"], list):
                return nested["data"]
            if isinstance(response.get("data"), list):
                return response["data"]
        if isinstance(response, list):
            return response
        return []

    def get_positions(self, account_no: str, market_type: str = "STOCK", page_size: int = 100) -> dict[str, Any] | list[Any]:
        return self.request(
            "GET",
            f"/accounts/{account_no}/positions",
            {"marketType": market_type, "pageSize": page_size},
        )

    def get_ppse(
        self, account_no: str, market_type: str, symbol: str, price: float, loan_package_id: int
    ) -> dict[str, Any] | list[Any]:
        return self.request(
            "GET",
            f"/accounts/{account_no}/ppse",
            {
                "marketType": market_type,
                "symbol": symbol.upper(),
                "loanPackageId": loan_package_id,
                "price": int(round(price)),
            },
        )

    # =========================================================================
    # Market data endpoints
    # =========================================================================
    def get_instruments(
        self,
        symbol: str | None = None,
        market_id: str | None = None,
        security_group_id: str | None = None,
        index_name: str | None = None,
        limit: int = 100,
        page: int = 1,
    ) -> dict[str, Any] | list[Any]:
        query: dict[str, object] = {"limit": limit, "page": page}
        if symbol:
            query["symbol"] = symbol
        if market_id:
            query["marketId"] = market_id
        if security_group_id:
            query["securityGroupId"] = security_group_id
        if index_name:
            query["indexName"] = index_name
        return self.request("GET", "/market/instruments", query)

    def list_instruments_paginated(
        self,
        limit: int = 100,
        page_size_max: int = 30,
    ) -> list[dict[str, Any]]:
        page = 1
        all_rows: list[dict[str, Any]] = []
        while True:
            response = self.get_instruments(limit=page_size_max, page=page)
            rows = parse_instruments(response)
            if not rows:
                break
            all_rows.extend(rows)
            meta_total = 0
            if isinstance(response, dict):
                meta_total = int(response.get("total") or 0)
            page += 1
            if len(rows) < page_size_max:
                break
            if meta_total and len(all_rows) >= meta_total:
                break
            if page > limit:
                break
        seen: set[str] = set()
        dedup: list[dict[str, Any]] = []
        for row in all_rows:
            symbol = row.get("symbol")
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            dedup.append(row)
        return dedup

    def get_working_dates(self) -> dict[str, Any] | list[Any]:
        return self.request("GET", "/market/working-dates")

    def get_latest_quote(self, symbol: str, board_id: str | None = None) -> dict[str, Any] | list[Any]:
        query: dict[str, object] = {}
        if board_id:
            query["boardId"] = board_id
        return self.request("GET", f"/price/{symbol.upper()}/quotes/latest", query)

    def get_ohlc(
        self,
        bar_type: str = DEFAULT_DAILY_BAR_TYPE,
        symbol: str | None = None,
        resolution: str = "1",
        from_ts: int | None = None,
        to_ts: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any] | list[Any]:
        """Fetch OHLC bars for ``symbol``.

        DNSE accepts the URL parameter name as ``type`` (not ``barType``), and accepts
        unix-seconds for ``from``/``to``. Resolution: ``1`` for daily, ``5``/``15``/``60``
        for intraday.
        """
        query: dict[str, object] = {"type": bar_type}
        if symbol:
            query["symbol"] = symbol.upper()
        query["resolution"] = resolution
        if from_ts is not None:
            query["from"] = from_ts
        elif from_date:
            query["from"] = utc_timestamp(from_date)
        if to_ts is not None:
            query["to"] = to_ts
        elif to_date:
            # DNSE ``to`` is exclusive; pass midnight at start of (to_date + 1)
            query["to"] = utc_timestamp(to_date) + 86400
        return self.request("GET", "/price/ohlc", query)

    def get_close_price(self, symbol: str, board_id: str | None = None) -> dict[str, Any] | list[Any]:
        query: dict[str, object] = {}
        if board_id:
            query["boardId"] = board_id
        return self.request("GET", f"/price/{symbol.upper()}/close", query)

    def get_security_definition(self, symbol: str, board_id: str | None = None) -> dict[str, Any] | list[Any]:
        query: dict[str, object] = {}
        if board_id:
            query["boardId"] = board_id
        return self.request("GET", f"/price/{symbol.upper()}/secdef", query)

    # =========================================================================
    # Registration / OTP
    # =========================================================================
    def send_email_otp(self) -> dict[str, Any] | list[Any]:
        return self.request("POST", "/registration/send-email-otp", body={})

    def create_trading_token(self, otp_type: str, passcode: str) -> str:
        response = self.request(
            "POST",
            "/registration/trading-token",
            body={"otpType": otp_type, "passcode": passcode},
        )
        if not isinstance(response, dict) or "tradingToken" not in response:
            raise RuntimeError(f"Missing tradingToken in response: {response}")
        return str(response["tradingToken"])

    def trading_token(self, otp_type: str, passcode: str) -> str:
        """Backward-compatible alias used by ``daily_engine`` and the ``dnse-trading-token`` CLI."""
        return self.create_trading_token(otp_type, passcode)


def sync_dnse_instruments(
    client: DnseClient,
    data_dir: Path = Path(__file__).resolve().parent / "data",
) -> Path:
    data_dir = Path(data_dir)
    rows = client.list_instruments_paginated()
    payload = {
        "source": DEFAULT_DNSE_BASE_URL + "/market/instruments",
        "fetchedAt": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "count": len(rows),
        "items": rows,
    }
    path = data_dir / DEFAULT_INSTRUMENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def fetch_dnse_history_chunked(
    client: DnseClient,
    symbol: str,
    from_date: str,
    to_date: str,
    resolution: str = "1D",
    chunk_days: int = 180,
    sleep_seconds: float = 0.4,
) -> list[dict[str, Any]]:
    """Fetch full daily OHLCV history for ``symbol``.

    NOTE: ``resolution="1D"`` is the default (daily bars). The DNSE OpenAPI treats
    ``resolution="1"`` as **1-minute intraday** which is rarely what backtests want
    and will return a tiny (most-recent-only) window because DNSE caps intraday
    response size.
    """
    start = datetime.strptime(from_date, "%Y-%m-%d").date()
    end = datetime.strptime(to_date, "%Y-%m-%d").date()
    combined: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=chunk_days - 1))
        response = client.get_ohlc(
            bar_type=DEFAULT_DAILY_BAR_TYPE,
            symbol=symbol,
            resolution=resolution,
            from_date=cursor.isoformat(),
            to_date=chunk_end.isoformat(),
        )
        combined.extend(parse_price_bars(response))
        cursor = chunk_end + timedelta(days=1)
        if sleep_seconds > 0 and cursor <= end:
            time.sleep(sleep_seconds)
    dedup: dict[str, dict[str, Any]] = {}
    for bar in combined:
        dedup[bar["time"]] = bar
    return sorted(dedup.values(), key=lambda item: item["time"])


def sync_dnse_prices(
    client: DnseClient,
    symbols: Iterable[str],
    from_date: str,
    to_date: str,
    data_dir: Path = Path(__file__).resolve().parent / "data",
    chunk_days: int = 180,
    resolution: str = "1D",
    sleep_seconds: float = 0.5,
    skip_existing: bool = True,
) -> dict[str, Path]:
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not symbols:
        return {}
    target_dir = Path(data_dir) / DEFAULT_PRICE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    total = len(symbols)
    for index, symbol in enumerate(symbols, start=1):
        path = target_dir / f"{symbol}.json"
        if skip_existing and path.exists() and path.stat().st_size > 50:
            print(f"[{index}/{total}] skip existing {symbol}")
            paths[symbol] = path
            continue
        try:
            bars = fetch_dnse_history_chunked(
                client=client,
                symbol=symbol,
                from_date=from_date,
                to_date=to_date,
                resolution=resolution,
                chunk_days=chunk_days,
                sleep_seconds=sleep_seconds,
            )
        except RuntimeError as exc:
            print(f"[{index}/{total}] {symbol} failed: {exc}")
            continue
        payload = {
            "source": DEFAULT_DNSE_BASE_URL + "/price/ohlc",
            "symbol": symbol,
            "fetchedAt": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "count": len(bars),
            "items": bars,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths[symbol] = path
        print(f"[{index}/{total}] {symbol}: {len(bars)} bars")
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return paths


def load_saved_dnse_prices(
    ticker: str,
    data_dir: Path = Path(__file__).resolve().parent / "data",
) -> list[dict[str, Any]]:
    path = Path(data_dir) / DEFAULT_PRICE_DIR / f"{ticker.strip().upper()}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = payload.get("items") or []
    return [bar for bar in items if isinstance(bar, dict)]
