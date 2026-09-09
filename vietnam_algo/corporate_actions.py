from __future__ import annotations

import bisect
import json
import re
import subprocess
import time
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

from .data import DEFAULT_DATA_DIR, write_json


VIETSTOCK_EVENTS_URL = "https://finance.vietstock.vn/data/eventstypedata"
VIETSTOCK_REFERER = "https://finance.vietstock.vn/lich-su-kien.htm"


@dataclass(frozen=True)
class CorporateAction:
    eventId: int
    eventTypeId: int
    channelId: int
    catId: int
    symbol: str
    companyName: str
    exchange: str
    exDate: str | None
    recordDate: str | None
    paymentDate: str | None
    name: str
    note: str
    title: str
    fileUrl: str
    isCashOnly: bool
    isNonCash: bool


@dataclass(frozen=True)
class VietstockSession:
    token: str
    opener: urllib.request.OpenerDirector | None = None
    cookie: str = ""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_vietstock_date(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        match = re.fullmatch(r"/Date\((-?\d+)\)/", value)
        if match:
            timestamp = int(match.group(1)) / 1000
            return datetime.fromtimestamp(timestamp).date().isoformat()
        try:
            return datetime.fromisoformat(value[:10]).date().isoformat()
        except ValueError:
            return None
    return None


def event_text(row: dict[str, Any]) -> str:
    parts = [row.get("Name"), row.get("Note"), row.get("Title"), row.get("Content")]
    return " ".join(str(part).lower() for part in parts if part)


def classify_event(row: dict[str, Any]) -> tuple[bool, bool]:
    text = event_text(row)
    cash = "tiền mặt" in text or " bằng tiền" in text
    non_cash_terms = [
        "bằng cổ phiếu",
        "cổ tức bằng cổ phiếu",
        "quyền mua",
        "phát hành",
        "thưởng cổ phiếu",
        "chia tách",
        "tách cổ phiếu",
        "gộp cổ phiếu",
        "trái phiếu chuyển đổi",
    ]
    non_cash = any(term in text for term in non_cash_terms)
    return cash and not non_cash, non_cash


def normalize_vietstock_event(row: dict[str, Any]) -> CorporateAction | None:
    symbol = str(row.get("Code") or "").strip().upper()
    if not symbol:
        return None
    is_cash_only, is_non_cash = classify_event(row)
    return CorporateAction(
        eventId=int(row.get("EventID") or 0),
        eventTypeId=int(row.get("EventTypeID") or 0),
        channelId=int(row.get("ChannelID") or 0),
        catId=int(row.get("CatID") or 0),
        symbol=symbol,
        companyName=str(row.get("CompanyName") or ""),
        exchange=str(row.get("Exchange") or ""),
        exDate=parse_vietstock_date(row.get("GDKHQDate")),
        recordDate=parse_vietstock_date(row.get("NDKCCDate")),
        paymentDate=parse_vietstock_date(row.get("Time")),
        name=str(row.get("Name") or ""),
        note=str(row.get("Note") or ""),
        title=str(row.get("Title") or ""),
        fileUrl=str(row.get("FileUrl") or ""),
        isCashOnly=is_cash_only,
        isNonCash=is_non_cash,
    )


def extract_request_verification_token(html: str) -> str:
    patterns = (
        r'name=__RequestVerificationToken\s+type=hidden\s+value=([^\s>]+)',
        r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)',
        r'value=["\']([^"\']+)["\'][^>]*name=["\']__RequestVerificationToken["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    raise RuntimeError("Vietstock request verification token was not found")


def fetch_vietstock_session(timeout: float = 30.0) -> VietstockSession:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    request = urllib.request.Request(
        VIETSTOCK_REFERER,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0",
        },
    )
    with opener.open(request, timeout=timeout) as response:
        html = response.read().decode("utf-8", errors="replace")
    return VietstockSession(token=extract_request_verification_token(html), opener=opener)


def fetch_vietstock_events(
    from_date: str,
    to_date: str,
    request_verification_token: str = "",
    cookie: str = "",
    page: int = 1,
    page_size: int = 100,
    timeout: float = 30.0,
    code: str = "",
    event_type_id: int = 1,
    session: VietstockSession | None = None,
) -> list[CorporateAction]:
    session = session or (fetch_vietstock_session(timeout) if not request_verification_token else None)
    token = request_verification_token or (session.token if session else "")
    body = urllib.parse.urlencode(
        {
            "eventTypeID": event_type_id,
            "channelID": 0,
            "code": code.strip().upper(),
            "catID": -1,
            "fDate": from_date,
            "tDate": to_date,
            "page": page,
            "pageSize": page_size,
            "orderBy": "Date1",
            "orderDir": "DESC",
            "__RequestVerificationToken": token,
        }
    ).encode("utf-8")
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://finance.vietstock.vn",
        "Referer": VIETSTOCK_REFERER,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0",
        "X-Requested-With": "XMLHttpRequest",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Microsoft Edge";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(
        VIETSTOCK_EVENTS_URL,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        opener = session.opener if session and session.opener else urllib.request
        with opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vietstock events HTTP {exc.code}: {detail[:500]}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        if cookie:
            payload = fetch_vietstock_events_with_curl(body, cookie, timeout)
        else:
            raise RuntimeError(f"Vietstock events returned non-JSON response: {text[:500]}")
    if isinstance(payload, list) and payload and isinstance(payload[0], list):
        rows = [row for row in payload[0] if isinstance(row, dict)]
        return [action for row in rows if (action := normalize_vietstock_event(row))]
    if isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
        return [action for row in rows if (action := normalize_vietstock_event(row))]
    raise RuntimeError(f"Unexpected Vietstock event payload: {type(payload).__name__}")


def fetch_vietstock_events_page_with_total(
    from_date: str,
    to_date: str,
    page: int,
    page_size: int,
    request_verification_token: str = "",
    cookie: str = "",
    timeout: float = 30.0,
    session: VietstockSession | None = None,
) -> tuple[list[CorporateAction], int | None]:
    session = session or (fetch_vietstock_session(timeout) if not request_verification_token else None)
    token = request_verification_token or (session.token if session else "")
    body = urllib.parse.urlencode(
        {
            "eventTypeID": 1,
            "channelID": 0,
            "code": "",
            "catID": -1,
            "fDate": from_date,
            "tDate": to_date,
            "page": page,
            "pageSize": page_size,
            "orderBy": "Date1",
            "orderDir": "DESC",
            "__RequestVerificationToken": token,
        }
    ).encode("utf-8")
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://finance.vietstock.vn",
        "Referer": VIETSTOCK_REFERER,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(VIETSTOCK_EVENTS_URL, data=body, method="POST", headers=headers)
    opener = session.opener if session and session.opener else urllib.request
    with opener.open(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if isinstance(payload, list) and payload and isinstance(payload[0], list):
        total = int(payload[1]) if len(payload) > 1 and isinstance(payload[1], int) else None
        actions = [action for row in payload[0] if isinstance(row, dict) and (action := normalize_vietstock_event(row))]
        return actions, total
    if isinstance(payload, list):
        return [action for row in payload if isinstance(row, dict) and (action := normalize_vietstock_event(row))], None
    raise RuntimeError(f"Unexpected Vietstock event payload: {type(payload).__name__}")


def fetch_vietstock_events_with_curl(body: bytes, cookie: str, timeout: float) -> Any:
    result = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--url",
            VIETSTOCK_EVENTS_URL,
            "-H",
            "Accept: */*",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-H",
            "Connection: keep-alive",
            "-H",
            "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
            "-b",
            cookie,
            "-H",
            "Origin: https://finance.vietstock.vn",
            "-H",
            f"Referer: {VIETSTOCK_REFERER}",
            "-H",
            "Sec-Fetch-Dest: empty",
            "-H",
            "Sec-Fetch-Mode: cors",
            "-H",
            "Sec-Fetch-Site: same-origin",
            "-H",
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0",
            "-H",
            "X-Requested-With: XMLHttpRequest",
            "-H",
            'sec-ch-ua: "Not=A?Brand";v="99", "Microsoft Edge";v="151", "Chromium";v="151"',
            "-H",
            "sec-ch-ua-mobile: ?0",
            "-H",
            'sec-ch-ua-platform: "Windows"',
            "--data-raw",
            body.decode("utf-8"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Vietstock curl fallback failed: {result.stderr[:500]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Vietstock events returned non-JSON response: {result.stdout[:500]}") from exc


def save_corporate_actions(actions: list[CorporateAction], data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    path = data_dir / "corporate_action.json"
    write_json(
        path,
        {
            "source": VIETSTOCK_EVENTS_URL,
            "fetchedAt": utc_now(),
            "count": len(actions),
            "items": [asdict(action) for action in actions],
        },
    )
    return path


def latest_corporate_action_date(actions: list[CorporateAction]) -> str | None:
    dates = [action.exDate for action in actions if action.exDate]
    return max(dates) if dates else None


def merge_corporate_actions(existing: list[CorporateAction], new_actions: list[CorporateAction]) -> tuple[list[CorporateAction], int]:
    by_id = {action.eventId: action for action in existing if action.eventId}
    added = 0
    for action in new_actions:
        if action.eventId and action.eventId not in by_id:
            added += 1
        if action.eventId:
            by_id[action.eventId] = action
    merged = sorted(by_id.values(), key=lambda action: (action.exDate or "", action.eventId), reverse=True)
    return merged, added


def sync_corporate_actions_incremental(
    data_dir: Path = DEFAULT_DATA_DIR,
    request_verification_token: str = "",
    cookie: str = "",
    to_date: str | None = None,
    page_size: int = 100,
    timeout: float = 30.0,
    max_attempts: int = 3,
    initial_backoff_seconds: float = 10.0,
) -> dict[str, Any]:
    path = corporate_action_path(data_dir)
    existing = load_corporate_actions(path)
    from_date = latest_corporate_action_date(existing) or "2012-07-13"
    end_date = to_date or datetime.now().date().isoformat()
# Retry the full fetch loop on transient errors (anti-bot rate-limits
    # occasionally return empty bodies mid-day, or Vietstock returns
    # HTML on a session-cookie glitch). Each attempt refreshes the
    # session so we re-bootstrap cookies and request token. Backoff
    # doubles each attempt up to 60s.
    #
    # Per user feedback 2026-09-09: 5 attempts was too noisy and the
    # upstream often fails all 5 identically (not transient, just
    # blocking). Default reduced to 3 with 10s initial backoff.
    last_error: Exception | None = None
    backoff = initial_backoff_seconds
    for attempt in range(1, max_attempts + 1):
        session = fetch_vietstock_session(timeout) if not request_verification_token else None
        fetched: list[CorporateAction] = []
        page = 1
        total: int | None = None
        try:
            while True:
                rows, total = fetch_vietstock_events_page_with_total(
                    from_date,
                    end_date,
                    page,
                    page_size,
                    request_verification_token=request_verification_token,
                    cookie=cookie,
                    timeout=timeout,
                    session=session,
                )
                fetched.extend(rows)
                if not rows:
                    break
                if total is not None and page * page_size >= total:
                    break
                page += 1
            # If we got no rows AT ALL on an early attempt, treat as transient.
            if not fetched:
                raise RuntimeError("Vietstock returned empty payload (transient)")
            break
        except (json.JSONDecodeError, RuntimeError, urllib.error.URLError) as exc:
            last_error = exc
            print(f"[CA sync] attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            raise
    if last_error is not None and not fetched:
        raise last_error
    merged, added = merge_corporate_actions(existing, fetched)
    save_corporate_actions(merged, data_dir)
    return {
        "path": str(path),
        "fromDate": from_date,
        "toDate": end_date,
        "fetched": len(fetched),
        "pages": page,
        "added": added,
        "total": len(merged),
    }


def load_corporate_actions(path: Path) -> list[CorporateAction]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("items", []) if isinstance(payload, dict) else payload
    actions: list[CorporateAction] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            actions.append(
                CorporateAction(
                    eventId=int(row.get("eventId") or 0),
                    eventTypeId=int(row.get("eventTypeId") or 0),
                    channelId=int(row.get("channelId") or 0),
                    catId=int(row.get("catId") or 0),
                    symbol=str(row.get("symbol") or "").upper(),
                    companyName=str(row.get("companyName") or ""),
                    exchange=str(row.get("exchange") or ""),
                    exDate=row.get("exDate"),
                    recordDate=row.get("recordDate"),
                    paymentDate=row.get("paymentDate"),
                    name=str(row.get("name") or ""),
                    note=str(row.get("note") or ""),
                    title=str(row.get("title") or ""),
                    fileUrl=str(row.get("fileUrl") or ""),
                    isCashOnly=bool(row.get("isCashOnly")),
                    isNonCash=bool(row.get("isNonCash")),
                )
            )
        except (TypeError, ValueError):
            continue
    return [action for action in actions if action.symbol]


def corporate_action_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir / "corporate_action.json"


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def non_cash_events_in_window(
    actions: list[CorporateAction],
    trade_date: str,
    window_days: int = 3,
    include_ex_date: bool = True,
) -> dict[str, list[CorporateAction]]:
    trade = parse_iso_date(trade_date)
    if trade is None:
        return {}
    risky: dict[str, list[CorporateAction]] = {}
    for action in actions:
        ex_date = parse_iso_date(action.exDate)
        if not action.isNonCash or ex_date is None:
            continue
        days_until = (ex_date - trade).days
        lower = 0 if include_ex_date else 1
        if lower <= days_until <= window_days:
            risky.setdefault(action.symbol, []).append(action)
    return risky


def build_corporate_action_lookup(
    actions: Iterable[CorporateAction],
    trading_dates: list[str],
    non_cash_only: bool = True,
) -> tuple[list[CorporateAction], dict[int, int]]:
    """Precompute a fast event_id -> trading-date index map.

    Returns (filtered_actions, ex_index_by_event_id). The map is built once
    per backtest / per day and lets ``non_cash_events_in_trading_window``
    avoid the O(M*N) scan inside that function.
    """
    if not trading_dates:
        return [], {}
    filtered: list[CorporateAction] = []
    ex_index_by_event_id: dict[int, int] = {}
    for action in actions:
        if non_cash_only and not action.isNonCash:
            continue
        if not action.exDate or not action.eventId:
            continue
        ex_index = bisect.bisect_left(trading_dates, action.exDate)
        if ex_index >= len(trading_dates):
            continue
        filtered.append(action)
        ex_index_by_event_id[action.eventId] = ex_index
    return filtered, ex_index_by_event_id


def non_cash_events_in_trading_window(
    actions: Iterable[CorporateAction],
    trade_date: str,
    trading_dates: list[str],
    window_days: int = 3,
    include_ex_date: bool = True,
    precomputed_ex_index: dict[int, int] | None = None,
) -> dict[str, list[CorporateAction]]:
    """Find non-cash events whose first trading date on/after ex-date falls
    within ``window_days`` sessions of ``trade_date``.

    ``precomputed_ex_index`` is an optional event_id -> trading_date_index
    map produced by ``build_corporate_action_lookup`` to skip the per-action
    O(log N) bisect in hot loops.
    """
    if not trading_dates:
        return {}
    try:
        trade_index = trading_dates.index(trade_date)
    except ValueError:
        return non_cash_events_in_window(list(actions), trade_date, window_days=window_days, include_ex_date=include_ex_date)
    lower = 0 if include_ex_date else 1
    risky: dict[str, list[CorporateAction]] = {}
    if precomputed_ex_index is not None:
        for action in actions:
            if not action.isNonCash or not action.eventId:
                continue
            ex_index = precomputed_ex_index.get(action.eventId)
            if ex_index is None:
                continue
            sessions_until = ex_index - trade_index
            if lower <= sessions_until <= window_days:
                risky.setdefault(action.symbol, []).append(action)
        return risky
    for action in actions:
        if not action.isNonCash or not action.exDate:
            continue
        ex_index = bisect.bisect_left(trading_dates, action.exDate)
        if ex_index >= len(trading_dates):
            continue
        sessions_until = ex_index - trade_index
        if lower <= sessions_until <= window_days:
            risky.setdefault(action.symbol, []).append(action)
    return risky


def closes_until_date(
    symbol: str,
    as_of_date: str,
    trading_dates: list[str],
    closes_by_symbol: dict[str, dict[str, float]],
    lookback: int,
) -> list[float]:
    """Return the closes for ``symbol`` from ``as_of_date`` looking back ``lookback`` days.

    Used by the pre-exit signal logic: we want to know whether the price has
    dropped from a recent peak (topping pattern) or fallen below a moving
    average (momentum roll-over). Looks at the most recent ``lookback``
    closes, ending at ``as_of_date`` inclusive.
    """
    if not trading_dates or not closes_by_symbol:
        return []
    sym_closes = closes_by_symbol.get(symbol, {})
    end_idx = next((i for i, d in enumerate(trading_dates) if d >= as_of_date), None)
    if end_idx is None:
        return []
    start_idx = max(0, end_idx - lookback + 1)
    closes: list[float] = []
    for i in range(start_idx, end_idx + 1):
        c = sym_closes.get(trading_dates[i])
        if c is not None:
            closes.append(float(c))
    return closes


def pre_exit_signal_drop_from_high(
    closes_window: list[float],
    threshold_pct: float,
) -> bool:
    """Fire when the current close has dropped by ``threshold_pct`` from the
    recent peak.

    Captures the "topping pattern" — price rallied (perhaps toward an ex-date
    expectation) and now starts drifting down. The threshold is relative to
    the local maximum, so it adapts to any post-ex drop magnitude.
    """
    if not closes_window:
        return False
    peak = max(closes_window)
    current = closes_window[-1]
    if peak <= 0:
        return False
    return current <= peak * (1.0 - threshold_pct)


def pre_exit_signal_below_ma(
    closes_window: list[float],
    ma_period: int,
) -> bool:
    """Fire when current close has fallen below the ``ma_period``-close moving average."""
    if len(closes_window) < ma_period:
        return False
    ma = sum(closes_window[-ma_period:]) / ma_period
    return closes_window[-1] < ma


def pre_exit_signal_momentum_reversal(
    closes_window: list[float],
    short_period: int,
    long_period: int,
) -> bool:
    """Fire when short MA crosses below long MA (bearish momentum reversal).

    Catches trend changes earlier than the drop-from-high signal because the
    MA-cross detects the turn even when the absolute level is still close to
    the peak.
    """
    if len(closes_window) < long_period + 1:
        return False
    short_now = sum(closes_window[-short_period:]) / short_period
    long_now = sum(closes_window[-long_period:]) / long_period
    short_prev = sum(closes_window[-(short_period+1):-1]) / short_period
    long_prev = sum(closes_window[-(long_period+1):-1]) / long_period
    return short_prev >= long_prev and short_now < long_now


def evaluate_pre_exit_signal(
    closes_window: list[float],
    config: Any,
) -> tuple[bool, str]:
    """Return (triggered, reason) for the configured pre-exit signal.

    Modes (read from ``config.corporateActionPreExitSignalMode``):

        "fixed_window"  — never returns True here; pre-exit handled by
                          ``corporateActionPreExitDays`` (legacy behaviour).
        "drop_from_high" — fire when close has dropped
                           ``PreExitSignalDropPct`` from the recent peak.
        "below_ma"       — fire when close has fallen below the
                           ``PreExitSignalMaPeriod``-close MA.
        "momentum_reversal" — fire on short/long MA bearish cross.
        "combined"       — fire when BOTH drop_from_high AND below_ma
                           trigger together.
    """
    mode = getattr(config, "corporateActionPreExitSignalMode", "fixed_window")
    if mode == "fixed_window":
        return False, "fixed_window_mode"
    drop_pct = max(0.0, getattr(config, "corporateActionPreExitSignalDropPct", 0.02))
    ma_period = max(2, getattr(config, "corporateActionPreExitSignalMaPeriod", 5))
    short_period = max(2, getattr(config, "corporateActionPreExitSignalShortPeriod", 3))
    long_period = max(short_period + 1, getattr(config, "corporateActionPreExitSignalLongPeriod", 8))
    lookback = max(ma_period, long_period + 1, getattr(config, "corporateActionPreExitSignalLookback", 14))
    if len(closes_window) < 3:
        return False, "not_enough_history"

    dropped = pre_exit_signal_drop_from_high(closes_window, drop_pct)
    below = pre_exit_signal_below_ma(closes_window, ma_period)
    reversed = pre_exit_signal_momentum_reversal(closes_window, short_period, long_period)

    if mode == "drop_from_high":
        return (dropped, "drop_from_high")
    if mode == "below_ma":
        return (below, "below_ma")
    if mode == "momentum_reversal":
        return (reversed, "momentum_reversal")
    if mode == "combined":
        return (dropped and below, "combined")
    return False, "unknown_mode"


# ---------------------------------------------------------------------------
# Risk classifier + post-CA rebuy signal logic
# ---------------------------------------------------------------------------
#
# These helpers were originally in ``backtest.py``. They are pure functions
# of (closes, corporate actions, config) and have no dependency on the
# backtest runner, so they live here so both the backtest AND the live engine
# can call the exact same code paths. Keeping a single source of truth is
# important: any divergence between engines is a real divergence in trading
# behaviour, not a refactor artefact.


def corporate_action_risk_type(action: CorporateAction) -> str:
    """Classify a CA event as ``standard`` or ``high``.

    ``high`` events (additional share issuance, rights offerings, share swaps,
    private placements, convertible bonds) mechanically change the share
    structure of the issuer. ``standard`` events (stock dividends, stock bonus,
    cash dividends) adjust the price without changing shareholder rights.

    The classifier only inspects the short ``name`` and ``note`` fields and
    uses multi-word keyword terms. It deliberately ignores ``title`` (a
    generic regulatory summary that frequently mentions unrelated events on
    the same ex-date) and avoids bare ``"phát hành"`` / ``"chuyển đổi"``
    substrings that match too broadly.
    """
    text = " ".join(part for part in (action.name or "", action.note or "") if part).lower()
    if not text:
        return "standard"
    high_risk_terms = (
        "phát hành thêm",
        "quyền mua",
        "trái phiếu chuyển đổi",
        "hoán đổi",
        "riêng lẻ",
    )
    return "high" if any(term in text for term in high_risk_terms) else "standard"


def closes_since_ex_window(
    symbol: str,
    ex_date: str,
    trading_dates: list[str],
    closes_by_symbol: dict[str, dict[str, float]],
) -> list[float]:
    """Return the sequence of closes for ``symbol`` from ``ex_date`` onward.

    ``trading_dates`` is the sorted list of session dates. ``closes_by_symbol``
    is a {symbol: {date_str: close}} lookup; the live path can build this
    from ``data/prices/SYM.json`` and the backtest passes its in-memory
    price_table directly.

    Returns an empty list if the symbol has no closes on/after ``ex_date``.
    """
    if not trading_dates or not closes_by_symbol:
        return []
    sym_closes = closes_by_symbol.get(symbol, {})
    ex_index = next((i for i, d in enumerate(trading_dates) if d >= ex_date), None)
    if ex_index is None:
        return []
    closes: list[float] = []
    for d in trading_dates[ex_index:]:
        c = sym_closes.get(d)
        if c is not None:
            closes.append(float(c))
    return closes


def rebuy_signal_recovery(closes_window: list[float], recovery_pct: float) -> bool:
    """Fire when current close has bounced by ``recovery_pct`` from the post-ex low.

    Adapts to the actual drop magnitude — the threshold is relative to the
    local minimum, so a 5% post-ex drop and a 30% drop can both fire when
    the price first shows real recovery.
    """
    if not closes_window:
        return False
    min_close = min(closes_window)
    if min_close <= 0:
        return False
    return closes_window[-1] >= min_close * (1.0 + recovery_pct)


def rebuy_signal_stabilization(
    closes_window: list[float],
    range_pct: float,
    lookback: int,
) -> bool:
    """Fire when the range over the last ``lookback`` closes is below ``range_pct``."""
    if len(closes_window) < lookback:
        return False
    recent = closes_window[-lookback:]
    low = min(recent)
    high = max(recent)
    if low <= 0:
        return False
    return (high - low) / low < range_pct


def rebuy_signal_new_high(closes_window: list[float]) -> bool:
    """Fire when current close is the highest close since the ex-date."""
    if len(closes_window) < 2:
        return False
    return closes_window[-1] >= max(closes_window[:-1])


def rebuy_signal_no_new_low(closes_window: list[float], lookback: int) -> bool:
    """Fire when no new low has been made over the last ``lookback`` closes."""
    if len(closes_window) < lookback + 1:
        return False
    window = closes_window[-(lookback + 1):]
    recent_min = min(window[:-1])
    return window[-1] >= recent_min


def rebuy_signal_ma_cross(closes_window: list[float], ma_period: int) -> bool:
    """Fire when the current close is above the ``ma_period``-close MA of the
    post-ex window.

    This is a simple "price is above the MA" proxy — a true golden/death
    cross requires the pre-ex tail of closes which the live path doesn't have
    access to. The proxy still captures the "post-ex recovery has begun"
    pattern: the price prints above its short-term average, which is what a
    classic MA-cross would converge to anyway.
    """
    if len(closes_window) < ma_period:
        return False
    ma = sum(closes_window[-ma_period:]) / ma_period
    return closes_window[-1] > ma


def evaluate_post_ca_rebuy_signal(
    closes_window: list[float],
    config: Any,
) -> tuple[bool, str]:
    """Return (triggered, reason) for the configured post-CA rebuy signal.

    Modes (read from ``config.corporateActionRebuySignalMode``):
        "fixed_delay"     — never returns True here; delay handled separately.
        "recovery"        — fire when post-ex recovery threshold is met.
        "stabilization"   — fire when post-ex range is tight.
        "new_high"        — fire when current close >= max closes since ex.
        "ma_cross"        — proxy: "price > MA" with a simple MA-cross-style
                              check (live back-compat is intentionally
                              conservative since proper MA-cross needs the
                              pre-ex tail of closes).
        "no_new_low"      — fire when no new low over the last ``lookback``.
         "combined"        — BOTH recovery AND stabilization must fire.
         "stabilization_or_no_new_low" — either stabilization or no new low.

    Confirm-days: require the signal to have been true at the close of each
    of the last ``confirm_days`` post-ex trading days. Filters single-day
    spikes / dead-cat bounces.
    """
    mode = getattr(config, "corporateActionRebuySignalMode", "fixed_delay")
    if mode == "fixed_delay":
        return False, "fixed_delay_mode"
    confirm = max(1, getattr(config, "corporateActionRebuySignalConfirmDays", 1))
    lookback = max(1, getattr(config, "corporateActionRebuySignalLookback", 3))
    if len(closes_window) < max(confirm, lookback):
        return False, "not_enough_history"
    recovery_pct = getattr(config, "corporateActionRebuySignalRecoveryPct", 0.005)
    range_pct = getattr(config, "corporateActionRebuySignalRangePct", 0.025)

    ma_period = max(2, getattr(config, "corporateActionRebuySignalMaPeriod", 5))
    triggers: dict[str, bool] = {
        "recovery": True,
        "stabilization": True,
        "new_high": True,
        "no_new_low": True,
        "ma_cross": True,
    }
    for k in range(confirm):
        sub = closes_window[: len(closes_window) - k]
        if not rebuy_signal_recovery(sub, recovery_pct):
            triggers["recovery"] = False
        if not rebuy_signal_stabilization(sub, range_pct, lookback):
            triggers["stabilization"] = False
        if not rebuy_signal_new_high(sub):
            triggers["new_high"] = False
        if not rebuy_signal_no_new_low(sub, lookback):
            triggers["no_new_low"] = False
        if not rebuy_signal_ma_cross(sub, ma_period):
            triggers["ma_cross"] = False

    if mode == "recovery":
        return (triggers["recovery"], "recovery")
    if mode == "stabilization":
        return (triggers["stabilization"], "stabilization")
    if mode == "new_high":
        return (triggers["new_high"], "new_high")
    if mode == "no_new_low":
        return (triggers["no_new_low"], "no_new_low")
    if mode == "ma_cross":
        return (triggers["ma_cross"], "ma_cross")
    if mode == "combined":
        return (triggers["recovery"] and triggers["stabilization"], "combined")
    if mode == "stabilization_or_no_new_low":
        return (
            triggers["stabilization"] or triggers["no_new_low"],
            "stabilization_or_no_new_low",
        )
    return False, "unknown_mode"
    return risky
