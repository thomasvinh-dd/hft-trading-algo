"""autofin_signals.py — login + signal fetcher for autofin.vn.

Authentication uses an email/password POST to /gateway/api/auth/sign-in,
returns {accessToken, refreshToken, userId}. Tokens are JWTs (~7d expiry).
We cache them on disk so subsequent bot cycles don't re-login.

Signal API: GET /gateway/api/gw/v1/signals/signal-static-suggest
Returns a list of signals with:
  symbol, companyName, lastRecomment (text)
  supply_F / supply_T: recommended buy zone
  tP_F / tP_T: take-profit target
  sL_F / sL_T: stop-loss
  rateForNav: target portfolio allocation (0..1)
  isWaittingBuy / isWaittingSell: entry trigger
  statusDeal: live trade state (price_Open, time_Open, price_StopLoss, ...)
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_LOGIN_URL = "https://api.autofin.vn/gateway/api/auth/sign-in"
DEFAULT_SIGNALS_URL = "https://api.autofin.vn/gateway/api/gw/v1/signals/signal-static-suggest"
DEFAULT_TOKEN_CACHE_PATH = Path("vietnam_algo/data/live/autofin_token.json")


@dataclass(frozen=True)
class AutofinSignal:
    """A single autofin.vn trading signal."""

    symbol: str
    company_name: str
    last_recomment: str  # human-readable (e.g. "Khuyến nghị đầu tư")
    supply_from: float  # buy zone low
    supply_to: float    # buy zone high
    tp_from: float      # take-profit target low
    tp_to: float        # take-profit target high
    sl_from: float      # stop-loss low
    sl_to: float        # stop-loss high
    rate_for_nav: float # target portfolio allocation
    is_waitting_buy: bool
    is_waitting_sell: bool
    market_type: int    # 1=HOSE, 2=HNX, 3=UPCOM
    raw: dict[str, Any]


def parse_signal(raw: dict[str, Any]) -> AutofinSignal:
    """Convert a raw autofin signal dict into a typed AutofinSignal."""
    sd = raw.get("statusDeal", {}) or {}
    return AutofinSignal(
        symbol=str(raw.get("symbol") or "").strip().upper(),
        company_name=str(raw.get("companyName") or ""),
        last_recomment=str(raw.get("lastRecomment") or ""),
        supply_from=float(raw.get("supply_F") or 0) * 1000,
        supply_to=float(raw.get("supply_T") or 0) * 1000,
        tp_from=float(raw.get("tP_F") or 0) * 1000,
        tp_to=float(raw.get("tP_T") or 0) * 1000,
        sl_from=float(raw.get("sL_F") or 0) * 1000,
        sl_to=float(raw.get("sL_T") or 0) * 1000,
        rate_for_nav=float(raw.get("rateForNav") or 0),
        is_waitting_buy=bool(raw.get("isWaittingBuy")),
        is_waitting_sell=bool(raw.get("isWaittingSell")),
        market_type=int(sd.get("market_TypePrice") or raw.get("market_TypePrice") or 0),
        raw=raw,
    )


def _read_token_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_token_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["_savedAt"] = time.time()
    path.write_text(json.dumps(payload, indent=2))


def _token_still_valid(payload: dict[str, Any]) -> bool:
    """Refresh if older than 6 days or if missing."""
    return (time.time() - payload.get("_savedAt", 0)) < 6 * 86400


class AutofinClient:
    """Tiny autofin.vn API client (login + signals fetch)."""

    def __init__(
        self,
        email: str,
        password: str,
        login_url: str = DEFAULT_LOGIN_URL,
        signals_url: str = DEFAULT_SIGNALS_URL,
        token_cache_path: Path = DEFAULT_TOKEN_CACHE_PATH,
        request_timeout: float = 15.0,
    ) -> None:
        self.email = email
        self.password = password
        self.login_url = login_url
        self.signals_url = signals_url
        self.token_cache_path = token_cache_path
        self.request_timeout = request_timeout
        self._token: str | None = None

    def _load_cached_token(self) -> str | None:
        cached = _read_token_cache(self.token_cache_path)
        if cached and _token_still_valid(cached) and cached.get("accessToken"):
            self._token = cached["accessToken"]
            return self._token
        return None

    def _save_tokens(self, payload: dict[str, Any]) -> None:
        self._token = payload.get("accessToken")
        _write_token_cache(self.token_cache_path, payload)

    def _post_login(self) -> dict[str, Any]:
        body = json.dumps(
            {"type": "email", "email": self.email, "password": self.password}
        ).encode()
        req = urllib.request.Request(
            self.login_url,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "origin": "https://www.autofin.vn",
                "referer": "https://www.autofin.vn/",
                "user-agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _ensure_token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self._load_cached_token()
            if cached:
                return cached
        payload = self._post_login()
        if not payload.get("accessToken"):
            raise RuntimeError(f"Autofin login failed: {payload!r}")
        self._save_tokens(payload)
        return payload["accessToken"]

    def get_token(self) -> str:
        if self._token is None:
            self._ensure_token()
        return self._token  # type: ignore[return-value]

    def fetch_signals(
        self,
        page_size: int = 20,
        page_index: int = 1,
        sort_by: str = "Time_Open",
        sort_type: int = -1,
    ) -> list[AutofinSignal]:
        token = self.get_token()
        url = (
            f"{self.signals_url}?pageSize={page_size}&pageIndex={page_index}"
            f"&sortBy={sort_by}&sortType={sort_type}"
        )
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "accept": "*/*",
                "origin": "https://www.autofin.vn",
                "referer": "https://www.autofin.vn/",
                "user-agent": "Mozilla/5.0",
            },
        )
        req.add_header("Cookie", f"token={token}")
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as e:
                # 401 → token expired, force refresh and retry once
                if e.code == 401 and attempt == 0:
                    self._token = None
                    token = self._ensure_token(force_refresh=True)
                    req = urllib.request.Request(
                        url,
                        method="GET",
                        headers={
                            "accept": "*/*",
                            "origin": "https://www.autofin.vn",
                            "referer": "https://www.autofin.vn/",
                            "user-agent": "Mozilla/5.0",
                        },
                    )
                    req.add_header("Cookie", f"token={token}")
                    continue
                raise
        items = payload.get("data") or []
        if not isinstance(items, list):
            return []
        return [parse_signal(it) for it in items if isinstance(it, dict)]

    def fetch_deals(
        self,
        symbol: str | None = None,
        page_size: int = 50,
        page_index: int = 1,
        only_open: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch autofin's position history (deals).

        If ``symbol`` is None, returns all recent deals. With ``only_open=True``
        (default), filters to ``statusDeal.isClosed == False``.
        """
        token = self.get_token()
        params = {"pageSize": page_size, "pageIndex": page_index}
        if symbol:
            params["symbol"] = symbol.upper()
        url = "https://api.autofin.vn/gateway/api/gw/v1/signals/signal-deals?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "accept": "*/*",
                "origin": "https://www.autofin.vn",
                "referer": "https://www.autofin.vn/",
                "user-agent": "Mozilla/5.0",
            },
        )
        req.add_header("Cookie", f"token={token}")
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._token = None
                    token = self._ensure_token(force_refresh=True)
                    req = urllib.request.Request(
                        url,
                        method="GET",
                        headers={
                            "accept": "*/*",
                            "origin": "https://www.autofin.vn",
                            "referer": "https://www.autofin.vn/",
                            "user-agent": "Mozilla/5.0",
                        },
                    )
                    req.add_header("Cookie", f"token={token}")
                    continue
                raise
        items = payload.get("data") or []
        if not isinstance(items, list):
            return []
        if only_open:
            items = [
                it for it in items
                if isinstance(it, dict)
                and not ((it.get("statusDeal") or {}).get("isClosed"))
            ]
        return items
