"""autofin_signals.py — login + signal fetcher for autofin.vn.

Authentication uses an email/password POST to /gateway/api/auth/sign-in,
returns {accessToken, refreshToken, userId}. Tokens are JWTs (~7d expiry).
We cache them on disk so subsequent bot cycles don't re-login.

Signal API: GET /gateway/api/gw/v1/signals/signal-static-suggest
Returns a list of signals with:
  symbol, companyName, lastRecomment (text)
  supply_F / supply_T: recommended buy zone (RAW VND)
  tP_F / tP_T: take-profit target (RAW VND)
  sL_F / sL_T: stop-loss (RAW VND)
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
    supply_from: float  # buy zone low (VND)
    supply_to: float    # buy zone high (VND)
    tp_from: float      # take-profit target low (VND)
    tp_to: float        # take-profit target high (VND)
    sl_from: float      # stop-loss low (VND)
    sl_to: float        # stop-loss high (VND)
    rate_for_nav: float # target portfolio allocation
    is_waitting_buy: bool
    is_waitting_sell: bool
    market_type: int    # 1=HOSE, 2=HNX, 3=UPCOM
    raw: dict[str, Any]


def parse_signal(raw: dict[str, Any]) -> AutofinSignal:
    """Convert a raw autofin signal dict into a typed AutofinSignal.

    All price fields from the autofin API are RAW VND (e.g. supply_F=5910
    means 5,910 VND), so we do NOT multiply by 1000 here.
    """
    sd = raw.get("statusDeal", {}) or {}
    return AutofinSignal(
        symbol=str(raw.get("symbol") or "").strip().upper(),
        company_name=str(raw.get("companyName") or ""),
        last_recomment=str(raw.get("lastRecomment") or ""),
        supply_from=float(raw.get("supply_F") or 0),
        supply_to=float(raw.get("supply_T") or 0),
        tp_from=float(raw.get("tP_F") or 0),
        tp_to=float(raw.get("tP_T") or 0),
        sl_from=float(raw.get("sL_F") or 0),
        sl_to=float(raw.get("sL_T") or 0),
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
        """Fetch autofin's position history (deals) for one symbol.

        If ``symbol`` is None, the ``signal-deals`` endpoint returns a
        server error (500) — autofin requires the ``symbol`` parameter.
        Use ``fetch_all_deals_pages()`` or ``fetch_all_deals()`` to
        iterate across all symbols.

        With ``only_open=True`` (default), filters to ``statusDeal.isClosed == False``.
        """
        items = self._fetch_paginated("signal-deals", page_size, page_index, symbol=symbol)
        if only_open:
            items = [
                it for it in items
                if isinstance(it, dict)
                and not ((it.get("statusDeal") or {}).get("isClosed"))
            ]
        return items

    def fetch_deals_for_symbol_all_pages(
        self,
        symbol: str,
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch ALL deals for one symbol, paginating through every page.

        Returns both open and closed deals. Use ``statusDeal.isClosed``
        to filter.
        """
        all_items: list[dict[str, Any]] = []
        page = 1
        while True:
            items = self.fetch_deals(
                symbol=symbol, page_size=page_size, page_index=page,
                only_open=False,
            )
            if not items:
                break
            all_items.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return all_items

    def fetch_all_symbols(self, max_pages: int = 50) -> list[str]:
        """Get all unique symbols that appear in ``/closing-signals``.

        Each symbol appears once in closing-signals (one row per
        symbol — typically the most recent closed deal). For the full
        deal history of each symbol, use ``fetch_deals_for_symbol_all_pages``.

        Returns sorted list of unique symbols.
        """
        symbols: set[str] = set()
        for page in range(1, max_pages + 1):
            items = self.fetch_closing(page_size=50, page_index=page)
            if not items:
                break
            for it in items:
                sym = it.get("symbol")
                if sym:
                    symbols.add(str(sym).upper())
        return sorted(symbols)

    def fetch_all_deals_pages(
        self,
        symbols: list[str] | None = None,
        page_size: int = 50,
        closed_only: bool = True,
        progress: bool = False,
        default_rate_for_nav: float = 0.15,
    ) -> list[dict[str, Any]]:
        """Fetch full closed-deal history across all symbols.

        Combines two endpoints:
          1. ``/closing-signals`` (paginated) to enumerate symbols.
          2. ``/signal-deals?symbol=X`` (paginated) per symbol.

        The ``/signal-deals`` endpoint does NOT include some fields
        that ``/closing-signals`` does (rateForNav, supply_F/T, tP_F/T,
        sL_F/T). For backtesting, we synthesize defaults so the
        strategy has data to work with:
          - rateForNav: defaults to ``default_rate_for_nav`` (0.15)
          - supply_F, supply_T: defaults to statusDeal.price_Open
          - tP_F, tP_T: defaults to statusDeal.price_Closed
          - sL_F, sL_T: defaults to statusDeal.price_Open * 0.90
            (i.e. autofin's typical 10% SL)

        With ``closed_only=True`` (default), filters to
        ``statusDeal.isClosed == True`` deals.

        Returns a list of raw autofin deal dicts (compatible with the
        ``run_autofin_backtest`` and ``run_daily_replay_backtest``
        callers).

        For 250+ symbols this can take 5-10 minutes. ``progress=True``
        prints progress to stderr.
        """
        if symbols is None:
            symbols = self.fetch_all_symbols()
        if progress:
            import sys
            print(f"[fetch_all_deals_pages] scanning {len(symbols)} symbols...",
                  file=sys.stderr, flush=True)
        all_items: list[dict[str, Any]] = []
        for i, sym in enumerate(symbols):
            if progress and (i + 1) % 10 == 0:
                import sys
                print(f"  {i+1}/{len(symbols)} scanned, {len(all_items)} deals so far",
                      file=sys.stderr, flush=True)
            try:
                deals = self.fetch_deals_for_symbol_all_pages(sym, page_size=page_size)
            except Exception as e:
                if progress:
                    print(f"  {sym}: ERROR {e}", file=sys.stderr, flush=True)
                continue
            if closed_only:
                deals = [d for d in deals if (d.get("statusDeal") or {}).get("isClosed")]
            # Synthesize missing fields for backtest compatibility
            for d in deals:
                sd = d.get("statusDeal") or {}
                if not d.get("rateForNav"):
                    d["rateForNav"] = default_rate_for_nav
                p_open = float(sd.get("price_Open") or 0)
                p_close = float(sd.get("price_Closed") or 0)
                if not d.get("supply_F"):
                    d["supply_F"] = p_open
                if not d.get("supply_T"):
                    d["supply_T"] = p_open
                if not d.get("tP_F"):
                    d["tP_F"] = p_close
                if not d.get("tP_T"):
                    d["tP_T"] = p_close
                if not d.get("sL_F"):
                    d["sL_F"] = p_open * 0.90
                if not d.get("sL_T"):
                    d["sL_T"] = p_open * 0.90
            all_items.extend(deals)
        if progress:
            import sys
            print(f"[fetch_all_deals_pages] done: {len(all_items)} deals from "
                  f"{len(symbols)} symbols", file=sys.stderr, flush=True)
        return all_items

    def fetch_holding(
        self, page_size: int = 50, page_index: int = 1,
    ) -> list[dict[str, Any]]:
        """Fetch autofin's currently-HELD positions (deal_id keyed)."""
        return self._fetch_paginated("signal-static-holding", page_size, page_index)

    def fetch_closing(
        self, page_size: int = 50, page_index: int = 1,
    ) -> list[dict[str, Any]]:
        """Fetch autofin's RECENTLY CLOSED positions (one record per symbol).

        NOTE: this endpoint returns ONE deal per unique symbol (the most
        recent closed one). For full per-symbol deal history, use
        ``fetch_deals(symbol=X, only_open=False)`` or the higher-level
        ``fetch_all_deals()`` helper.
        """
        return self._fetch_paginated("closing-signals", page_size, page_index)

    def fetch_risk(
        self, page_size: int = 50, page_index: int = 1,
    ) -> list[dict[str, Any]]:
        """Fetch autofin's RISK-flagged positions."""
        return self._fetch_paginated("signal-static-risk", page_size, page_index)

    def _fetch_paginated(
        self,
        endpoint: str,
        page_size: int,
        page_index: int,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        token = self.get_token()
        params: dict[str, object] = {"pageSize": page_size, "pageIndex": page_index}
        if symbol:
            params["symbol"] = symbol.upper()
        url = (
            f"https://api.autofin.vn/gateway/api/gw/v1/signals/{endpoint}?"
            + urllib.parse.urlencode(params)
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
        return items if isinstance(items, list) else []
