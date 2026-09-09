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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_LOGIN_URL = "https://api.autofin.vn/gateway/api/auth/sign-in"
DEFAULT_SIGNALS_URL = "https://api.autofin.vn/gateway/api/gw/v1/signals/signal-static-suggest"
DEFAULT_NOTIFICATIONS_URL = (
    "https://api.autofin.vn/gateway/api/gw/v1/notification/get-notify-by-topics"
)
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

    autofin nests the strategy fields under ``detail.*`` in some
    endpoints (/signal-deals) and at the top level in others
    (/closing-signals, /signal-static-holding, /signal-static-suggest).
    We read from ``detail.*`` first, then fall back to top level.
    """
    sd = raw.get("statusDeal", {}) or {}
    detail = raw.get("detail", {}) or {}
    # Use detail.* when present (signal-deals endpoint), else top-level
    def _g(field: str) -> float:
        v = detail.get(field)
        if v is None or v == 0 or v == "":
            v = raw.get(field)
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    return AutofinSignal(
        symbol=str(raw.get("symbol") or detail.get("symbol") or "").strip().upper(),
        company_name=str(raw.get("companyName") or detail.get("companyName") or ""),
        last_recomment=str(raw.get("lastRecomment") or detail.get("lastRecomment") or ""),
        supply_from=_g("supply_F"),
        supply_to=_g("supply_T"),
        tp_from=_g("tP_F"),
        tp_to=_g("tP_T"),
        sl_from=_g("sL_F"),
        sl_to=_g("sL_T"),
        rate_for_nav=_g("rateForNav"),
        is_waitting_buy=bool(detail.get("isWaittingBuy", raw.get("isWaittingBuy", False))),
        is_waitting_sell=bool(detail.get("isWaittingSell", raw.get("isWaittingSell", False))),
        market_type=int(sd.get("market_TypePrice") or detail.get("market_TypePrice") or raw.get("market_TypePrice") or 0),
        raw=raw,
    )


@dataclass(frozen=True)
class AutofinNotification:
    """A single autofin.vn notification event.

    Notifications are emitted by autofin whenever something happens:
    a new deal opens, a TP fires, an SL hits, a daily report is ready,
    etc. Each carries the EXACT timestamp of the event, the type tags,
    and a human-readable title+body with structured data (zones, prices,
    percentages) that we already parse from ``lastRecomment`` today.

    This is a different shape from ``AutofinSignal`` (which is a state
    snapshot of one deal): notifications are an EVENT STREAM. We can
    use them as a faster/replacement source for TP/SL detection that
    currently polls ``numberOfProfit`` deltas.
    """

    id: int                          # unique notification id (Unix-ns timestamp)
    time_request: str                # ISO timestamp from autofin
    tags: tuple[str, ...]            # e.g. ("SIGNAL", "TRADINGTIME", "TAKEPROFIT")
    title: str                       # Vietnamese title (e.g. "💰 CHỐT LÃI DRI")
    body: str                        # Event body (HTML stripped to plain text)
    image_url: str = ""              # company logo or autofin logo
    action_url: str = ""             # Deep link to dashboard
    has_read: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    # Convenience properties for tag-matching.
    @property
    def is_buy_opened(self) -> bool:
        return "OPENDEAL" in self.tags and "EOD" in self.tags

    @property
    def is_take_profit(self) -> bool:
        return "TAKEPROFIT" in self.tags

    @property
    def is_stop_order(self) -> bool:
        return "STOPORDER" in self.tags

    @property
    def is_close_deal(self) -> bool:
        return "CLOSEDEAL" in self.tags

    @property
    def is_daily_report(self) -> bool:
        return "REPORT" in self.tags and "EOD" in self.tags


def _strip_html(s: str) -> str:
    """Remove HTML tags AND decode HTML entities (&#xNN; and &name;)."""
    import re as _re
    import html as _html
    s = _re.sub(r"<[^>]+>", " ", s or "")
    s = _html.unescape(s)
    s = _re.sub(r"\s+", " ", s)
    return s.strip()


def _parse_vnd_int(s: str) -> int:
    """Parse '12,900' → 12900 (Vietnamese thousands separator)."""
    return int((s or "").replace(".", "").replace(",", "").strip() or "0")


def parse_notification(raw: dict[str, Any]) -> AutofinNotification:
    """Parse one autofin notification API response into ``AutofinNotification``.

    The endpoint ``/notification/get-notify-by-topics?topics=SIGNAL``
    returns ``{"data": [{_id, timeRequest, tag, type, notifyDetail, ...}]}``.
    Each notification is an EVENT, not a state — handle the various
    tag combinations (OPENDEAL, TAKEPROFIT, STOPORDER, CLOSEDEAL,
    REPORT, REMIND) via the ``AutofinNotification`` helper properties.
    """
    detail = raw.get("notifyDetail") or {}
    raw_tag = raw.get("tag") or ""
    tags = tuple(t.strip() for t in raw_tag.split(",") if t.strip())
    return AutofinNotification(
        id=int(raw.get("_id") or 0),
        time_request=str(raw.get("timeRequest") or ""),
        tags=tags,
        title=str(detail.get("title") or ""),
        body=_strip_html(str(detail.get("body") or "")),
        image_url=str(detail.get("imageURL") or ""),
        action_url=str(detail.get("action") or ""),
        has_read=bool(raw.get("hasRead", False)),
        raw=raw,
    )


def parse_recommendation_history(raw_deal: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse the ``hisRecomment`` array into a list of typed events.

    Each event dict has:
      - ``day_offset``: int, days from deal's time_Open (0 = same day)
      - ``action``: one of "tp", "reduce", "capital_protect", "close"
      - ``price``: int VND (recommended execution price)
      - ``fraction``: float, fraction of CURRENT position to sell (0..1)
      - ``raw``: the original recommendation text

    Patterns recognised:
      - "💰 Chốt lãi lần N mức giá P, Tỷ trọng X %" → TP lap, X% of position
      - "⚠️ Hạ X % tỉ trọng lần N, Giá: P" → reduce, X% of position
      - "🛡️ Chốt lãi X % bảo toàn vốn tại P" → capital protect, X% of position
      - "Đóng DEAL ..." → full close at P (uses last known price)
      - Entry: "Ngày khuyến nghị: ..." → returned with action="entry"
    """
    import re as _re
    sd = raw_deal.get("statusDeal", {}) or {}
    p_open = float(sd.get("price_Open") or 0)
    p_close = float(sd.get("price_Closed") or 0)
    his = raw_deal.get("hisRecomment") or []
    if not isinstance(his, list):
        return []

    events: list[dict[str, Any]] = []
    for entry in his:
        if not isinstance(entry, str):
            continue
        text = _strip_html(entry)
        if not text:
            continue

        # Parse day offset: "Nắm giữ: X ngày" (X = days from open)
        days_match = _re.search(r"Nắm giữ:\s*(\d+)\s*ngày", text)
        day_offset = int(days_match.group(1)) if days_match else 0

        # 1. TP lap: "💰 Chốt lãi lần N mức giá P ... Tỷ trọng X %"
        m = _re.search(r"Chốt lãi lần\s*\d+\s*mức giá\s*([\d,\.]+)", text)
        if m:
            price = _parse_vnd_int(m.group(1))
            pct = _re.search(r"Tỷ trọng\s*(\d+)\s*%", text)
            fraction = (int(pct.group(1)) / 100.0) if pct else 1 / 3
            events.append({
                "day_offset": day_offset,
                "action": "tp",
                "price": price,
                "fraction": fraction,
                "raw": text,
            })
            continue

        # 2. Capital protect: "🛡️ Chốt lãi X % bảo toàn vốn tại P"
        m = _re.search(r"Chốt lãi\s*([\d\.]+)\s*%\s*bảo toàn vốn\s*tại\s*([\d,\.]+)", text)
        if m:
            fraction = float(m.group(1)) / 100.0
            price = _parse_vnd_int(m.group(2))
            events.append({
                "day_offset": day_offset,
                "action": "capital_protect",
                "price": price,
                "fraction": fraction,
                "raw": text,
            })
            continue

        # 3. Reduce: "⚠️ Hạ X % tỉ trọng lần N Giá: P"
        m = _re.search(r"Hạ\s*(\d+)\s*%\s*tỉ trọng\s*lần\s*\d+", text)
        if m:
            fraction = int(m.group(1)) / 100.0
            price_m = _re.search(r"Giá:\s*([\d,\.]+)", text)
            price = _parse_vnd_int(price_m.group(1)) if price_m else (p_close or p_open)
            events.append({
                "day_offset": day_offset,
                "action": "reduce",
                "price": price,
                "fraction": fraction,
                "raw": text,
            })
            continue

        # 4. Full close: "Đóng DEAL ..."
        if _re.search(r"Đóng\s*DEAL", text):
            close_price_m = _re.search(r"Lãi:\s*[\d\.]+\s*%\)?", text)
            # Close uses final price_Close or last known price
            events.append({
                "day_offset": day_offset,
                "action": "close",
                "price": p_close or p_open,
                "fraction": 1.0,  # remaining position
                "raw": text,
            })
            continue

        # 5. Entry: "Ngày khuyến nghị: ..."
        if _re.search(r"khuyến nghị", text):
            # Extract buy-zone midpoint as the implicit BUY price
            m = _re.search(r"Vùng mua:\s*([\d,\.]+)\s*-\s*([\d,\.]+)", text)
            price = 0
            if m:
                lo = _parse_vnd_int(m.group(1))
                hi = _parse_vnd_int(m.group(2))
                price = (lo + hi) // 2
            events.append({
                "day_offset": 0,
                "action": "entry",
                "price": price or p_open,
                "fraction": 1.0,
                "raw": text,
            })
            continue

    return events


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
        notifications_url: str = DEFAULT_NOTIFICATIONS_URL,
        token_cache_path: Path = DEFAULT_TOKEN_CACHE_PATH,
        request_timeout: float = 15.0,
    ) -> None:
        self.email = email
        self.password = password
        self.login_url = login_url
        self.signals_url = signals_url
        self.notifications_url = notifications_url
        self.token_cache_path = token_cache_path
        self.request_timeout = request_timeout
        self._token: str | None = None
        self._user_id: int | None = None

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
        # Capture user id (used by notifications endpoint) if present.
        uid = payload.get("userId") or (payload.get("data") or {}).get("id")
        if isinstance(uid, int):
            self._user_id = uid
        elif isinstance(uid, str) and uid.isdigit():
            self._user_id = int(uid)
        return payload["accessToken"]

    def get_token(self) -> str:
        if self._token is None:
            self._ensure_token()
        return self._token  # type: ignore[return-value]

    def get_user_id(self) -> int:
        """Return the cached autofin user id, or 581 (default from sample URL).

        The notification endpoint requires ``uid`` as a query param.
        Default to 581 (the sample UID from autofin's web app) so the
        method works even if we haven't captured the real userId yet.
        """
        if self._user_id is not None:
            return self._user_id
        return 581

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

    def fetch_notifications(
        self,
        topics: str = "SIGNAL",
        number: int = 20,
        skip: int = 0,
    ) -> list[AutofinNotification]:
        """Fetch the user's notification feed from autofin's API.

        Endpoint: ``GET /gateway/api/gw/v1/notification/get-notify-by-topics``
        Params: ``uid``, ``topics`` (comma-separated), ``number`` (page size),
        ``skip`` (offset).

        Returns a paginated EVENT STREAM — each item has tags like
        ``SIGNAL,TRADINGTIME,TAKEPROFIT`` (a TP fired) or
        ``SIGNAL,EOD,OPENDEAL`` (a new buy recommendation) or
        ``SIGNAL,EOD,CLOSEDEAL`` (deal closed). The exact tags let us
        route each event to the right handler (see
        ``AutofinNotification.is_*`` helpers).

        Each event's body is HTML-stripped to plain text. We keep the
        raw payload so callers can inspect ``image_url`` (company logo)
        and ``action_url`` (deep link to autofin dashboard).

        Note: this endpoint is the SAME one autofin's web app uses to
        populate the bell-icon notification panel. Per user feedback
        2026-09-09: the events have the same fields as
        ``/signal-static-suggest`` (zone, TP, SL, %) plus exact event
        timestamps — useful as a faster TP/SL detector than polling
        ``numberOfProfit`` deltas.
        """
        # Trigger login if we don't have a token yet (also captures userId).
        token = self.get_token()
        uid = self.get_user_id()
        url = (
            f"{self.notifications_url}?uid={uid}&topics={topics}"
            f"&number={number}&skip={skip}"
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
        # API returns {"data": [...], "errorMessage": ..., "statusCode": ...}.
        items = payload.get("data") or []
        if not isinstance(items, list):
            return []
        return [parse_notification(it) for it in items if isinstance(it, dict)]

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
        default_rate_for_nav: float = 0.10,
        default_tp_pct_above_entry: float = 0.10,
        default_sl_pct_below_entry: float = 0.10,
    ) -> list[dict[str, Any]]:
        """Fetch full closed-deal history across all symbols.

        Combines two endpoints:
          1. ``/closing-signals`` (paginated) to enumerate symbols.
          2. ``/signal-deals?symbol=X`` (paginated) per symbol.

        The ``/signal-deals`` endpoint does NOT include some fields
        that ``/closing-signals`` does (rateForNav, supply_F/T, tP_F/T,
        sL_F/T). For backtesting, we synthesize defaults so the
        strategy has data to work with — but we use FORECAST-BASED
        defaults that DO NOT depend on future information
        (price_Close). Concretely:
          - rateForNav: defaults to ``default_rate_for_nav`` (0.10)
          - supply_F, supply_T: defaults to statusDeal.price_Open
          - tP_F, tP_T: defaults to price_Open × (1 + tp_pct_above_entry)
            i.e. autofin's typical forecast-based TP target ABOVE
            entry. NEVER below entry (would fire on day 1, look-ahead).
          - sL_F, sL_T: defaults to price_Open × (1 - sl_pct_below_entry)
            i.e. autofin's typical forecast-based SL target.

        Honesty note: this avoids look-ahead bias from using
        price_Close for TP/SL targets. For LOSERS, this means
        the TP never fires (price drops below entry), so we hold
        to the final close — capturing the full loss. The previous
        "TP = price_Close" approach was biased upward: for losers,
        it set TP below entry and fired day 1, capping losses
        artificially.

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
            # Backfill any remaining gaps from autofin's detail record.
            # Real /signal-deals data has these fields nested under detail.*
            # (which parse_signal already reads), so we just check for presence.
            for d in deals:
                detail = d.get("detail") or {}
                sd = d.get("statusDeal") or {}
                # If still missing (very old deal), apply the forecast-based
                # defaults — NEVER use price_Close (look-ahead bias).
                if not (d.get("rateForNav") or detail.get("rateForNav")):
                    d["rateForNav"] = default_rate_for_nav
                p_open = float(sd.get("price_Open") or 0)
                if not (d.get("supply_F") or detail.get("supply_F")):
                    d["supply_F"] = p_open
                if not (d.get("supply_T") or detail.get("supply_T")):
                    d["supply_T"] = p_open
                if not (d.get("tP_F") or detail.get("tP_F")):
                    d["tP_F"] = p_open * (1 + default_tp_pct_above_entry)
                if not (d.get("tP_T") or detail.get("tP_T")):
                    d["tP_T"] = p_open * (1 + default_tp_pct_above_entry)
                if not (d.get("sL_F") or detail.get("sL_F")):
                    d["sL_F"] = p_open * (1 - default_sl_pct_below_entry)
                if not (d.get("sL_T") or detail.get("sL_T")):
                    d["sL_T"] = p_open * (1 - default_sl_pct_below_entry)
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
