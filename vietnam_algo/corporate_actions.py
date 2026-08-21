from __future__ import annotations

import json
import re
import subprocess
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

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
) -> dict[str, Any]:
    path = corporate_action_path(data_dir)
    existing = load_corporate_actions(path)
    from_date = latest_corporate_action_date(existing) or "2012-07-13"
    end_date = to_date or datetime.now().date().isoformat()
    session = fetch_vietstock_session(timeout) if not request_verification_token else None
    fetched: list[CorporateAction] = []
    page = 1
    total: int | None = None
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


def non_cash_events_in_trading_window(
    actions: list[CorporateAction],
    trade_date: str,
    trading_dates: list[str],
    window_days: int = 3,
    include_ex_date: bool = True,
) -> dict[str, list[CorporateAction]]:
    if trade_date not in trading_dates:
        return non_cash_events_in_window(actions, trade_date, window_days=window_days, include_ex_date=include_ex_date)
    trade_index = trading_dates.index(trade_date)
    risky: dict[str, list[CorporateAction]] = {}
    for action in actions:
        if not action.isNonCash or not action.exDate:
            continue
        ex_trading_dates = [date for date in trading_dates if date >= action.exDate]
        if not ex_trading_dates:
            continue
        ex_index = trading_dates.index(ex_trading_dates[0])
        sessions_until = ex_index - trade_index
        lower = 0 if include_ex_date else 1
        if lower <= sessions_until <= window_days:
            risky.setdefault(action.symbol, []).append(action)
    return risky
