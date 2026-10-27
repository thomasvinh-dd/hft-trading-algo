from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .data import DEFAULT_DATA_DIR, PriceBar, ensure_data_dir, read_json, write_json


INVESTING_BASE = "https://www.investing.com"
INVESTING_API_BASE = "https://api.investing.com"
INVESTING_VN_BASE = "https://vn.investing.com"
DEFAULT_INVESTING_PRICES_DIR = "prices_investing"
DEFAULT_DOMAIN_ID = "vn"
TIME_FRAME_DAILY = "Daily"
TIME_FRAME_WEEKLY = "Weekly"
TIME_FRAME_MONTHLY = "Monthly"
DEFAULT_CHUNK_DAYS = 365
DEFAULT_PAGE_SLEEP_SECONDS = 1.0

INVESTING_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class InvestingSymbol:
    ticker: str
    pair_id: int
    slug: str | None = None
    name: str | None = None
    exchange: str | None = None
    sector: str | None = None

    @property
    def historical_url(self) -> str:
        if self.slug:
            return f"{INVESTING_VN_BASE}/equities/{self.slug}-historical-data"
        return f"{INVESTING_VN_BASE}/equities/{self.ticker.lower()}-historical-data"

    @property
    def api_url_template(self) -> str:
        return (
            f"{INVESTING_API_BASE}/api/financialdata/historical/{self.pair_id}"
            "?start-date={start}&end-date={end}&time-frame={tf}&add-missing-rows=false"
        )


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def investing_headers(domain_id: str = DEFAULT_DOMAIN_ID, referer: str | None = None) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": INVESTING_USER_AGENT,
        "domain-id": domain_id,
        "origin": INVESTING_VN_BASE,
        "referer": referer or f"{INVESTING_VN_BASE}/",
    }


def investing_html_headers(domain_id: str = DEFAULT_DOMAIN_ID) -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": INVESTING_USER_AGENT,
        "origin": INVESTING_VN_BASE,
        "referer": f"{INVESTING_VN_BASE}/equities/vietnam",
    }


def fetch_investing_json(
    url: str,
    headers: dict[str, str],
    timeout: float = 30.0,
    max_retries: int = 4,
) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                if not raw:
                    raise RuntimeError(f"{url} -> empty body")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{url} -> non-JSON body ({raw[:120]!r})") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            if exc.code in (429, 503) and attempt < max_retries:
                sleep_s = min(60.0, 2.0 ** (attempt + 1))
                print(f"  rate-limited HTTP {exc.code}; sleeping {sleep_s:.0f}s and retrying")
                time.sleep(sleep_s)
                continue
            raise RuntimeError(f"{url} -> HTTP {exc.code} {detail}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt < max_retries:
                sleep_s = 2.0 ** (attempt + 1)
                print(f"  network error: {exc}; sleeping {sleep_s:.0f}s and retrying")
                time.sleep(sleep_s)
                continue
            raise RuntimeError(f"{url} -> {exc}") from exc
    raise RuntimeError(f"{url} -> exhausted retries")


def fetch_investing_html(
    url: str,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> str:
    request = urllib.request.Request(url, headers=investing_html_headers(), method="GET")
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, TimeoutError, urllib.error.URLError) as exc:
            if attempt < max_retries:
                time.sleep(2.0 ** (attempt + 1))
                continue
            raise RuntimeError(f"{url} -> {exc}") from exc
    raise RuntimeError(f"{url} -> exhausted retries")


def parse_investing_bars(payload: Any) -> list[PriceBar]:
    rows: Iterable[Any]
    if isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], list):
            rows = payload["data"]
        elif "candles" in payload and isinstance(payload["candles"], list):
            rows = payload["candles"]
        else:
            inner = payload.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("candles"), list):
                rows = inner["candles"]
            else:
                return []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []

    bars: list[PriceBar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            raw_date = row.get("date") or row.get("time") or row.get("tradeDate")
            if raw_date is None:
                continue
            if isinstance(raw_date, (int, float)):
                tick = float(raw_date)
                if tick > 1e12:
                    seconds = tick / 1000.0
                else:
                    seconds = tick
                time_iso = datetime.fromtimestamp(seconds, tz=UTC).date().isoformat()
            else:
                text = str(raw_date)
                if "T" in text:
                    time_iso = text.split("T")[0]
                else:
                    time_iso = text[:10]

            open_v = row.get("open") or row.get("openPrice") or row.get("o")
            high_v = row.get("high") or row.get("highPrice") or row.get("h")
            low_v = row.get("low") or row.get("lowPrice") or row.get("l")
            close_v = row.get("close") or row.get("closePrice") or row.get("c") or row.get("value")
            volume_v = row.get("volume") or row.get("v") or 0

            if open_v is None or high_v is None or low_v is None or close_v is None:
                continue

            bars.append(
                PriceBar(
                    time=time_iso,
                    open=float(open_v),
                    high=float(high_v),
                    low=float(low_v),
                    close=float(close_v),
                    volume=float(volume_v or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    return sorted(bars, key=lambda item: item.time)


def fetch_investing_history(
    pair_id: int,
    start_date: str,
    end_date: str,
    time_frame: str = TIME_FRAME_DAILY,
    timeout: float = 30.0,
) -> list[PriceBar]:
    if not start_date or not end_date:
        raise ValueError("start_date and end_date are required (YYYY-MM-DD)")
    if not _is_iso_date(start_date) or not _is_iso_date(end_date):
        raise ValueError("start_date and end_date must be YYYY-MM-DD")
    if start_date > end_date:
        raise ValueError("start_date must be <= end_date")

    url = (
        f"{INVESTING_API_BASE}/api/financialdata/historical/{pair_id}"
        + f"?start-date={start_date}&end-date={end_date}"
        + f"&time-frame={time_frame}&add-missing-rows=false"
    )
    headers = investing_headers(domain_id=DEFAULT_DOMAIN_ID)
    payload = fetch_investing_json(url, headers, timeout=timeout)
    return parse_investing_bars(payload)


def fetch_investing_history_chunked(
    pair_id: int,
    start_date: str,
    end_date: str,
    time_frame: str = TIME_FRAME_DAILY,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    sleep_seconds: float = DEFAULT_PAGE_SLEEP_SECONDS,
    timeout: float = 30.0,
) -> list[PriceBar]:
    if not _is_iso_date(start_date) or not _is_iso_date(end_date):
        raise ValueError("start_date and end_date must be YYYY-MM-DD")
    if start_date > end_date:
        raise ValueError("start_date must be <= end_date")
    if chunk_days <= 0:
        raise ValueError("chunk_days must be > 0")

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    combined: list[PriceBar] = []
    cursor = start_dt
    while cursor <= end_dt:
        chunk_end_dt = min(end_dt, cursor + timedelta(days=chunk_days - 1))
        chunk_end_iso = chunk_end_dt.isoformat()
        chunk_start_iso = cursor.isoformat()
        bars = fetch_investing_history(
            pair_id=pair_id,
            start_date=chunk_start_iso,
            end_date=chunk_end_iso,
            time_frame=time_frame,
            timeout=timeout,
        )
        combined.extend(bars)
        cursor = chunk_end_dt + timedelta(days=1)
        if sleep_seconds > 0 and cursor <= end_dt:
            time.sleep(sleep_seconds)

    deduped_map: dict[str, PriceBar] = {}
    for bar in sorted(combined, key=lambda item: item.time):
        deduped_map[bar.time] = bar
    return sorted(deduped_map.values(), key=lambda item: item.time)


def _is_iso_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


SLUG_RE = re.compile(r"/equities/([a-z0-9][a-z0-9\-]*?)(?:-historical-data(?:/?|$))?[\"'\\\s>]")


def slug_to_ticker(slug: str) -> str | None:
    if not slug:
        return None
    head = slug.split("-", 1)[0]
    if re.fullmatch(r"[a-z0-9]{3}", head):
        return head.upper()
    return None


def ticker_to_slug(ticker: str) -> str:
    text = (ticker or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def parse_investing_equities_list_html(html: str) -> list[InvestingSymbol]:
    pair_id_re = re.compile(r'data-pair(?:-id)?=.?(\d+)')

    symbols: list[InvestingSymbol] = []
    seen: set[int] = set()

    for match in pair_id_re.finditer(html):
        try:
            pair_id = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if pair_id in seen:
            continue

        anchor_span = _find_anchor_for_pair(html, match.start(), pair_id)
        slug = None
        text_hint: str | None = None
        if anchor_span:
            start, end = anchor_span
            anchor_html = html[start:end]
            opener_match = re.search(r"<a [^>]*>", anchor_html)
            slug_match = SLUG_RE.search(opener_match.group(0)) if opener_match else None
            if slug_match:
                slug = slug_match.group(1)
            text_hint = _strip_tags(anchor_html)
        ticker = slug_to_ticker(slug or "") if slug else None
        if pair_id in seen:
            continue
        seen.add(pair_id)
        symbols.append(
            InvestingSymbol(
                ticker=ticker or "",
                pair_id=pair_id,
                slug=slug,
                name=text_hint,
            )
        )
    return symbols


def _find_anchor_for_pair(html: str, idx: int, pair_id: int) -> tuple[int, int] | None:
    end = html.find("</a", idx)
    if end == -1:
        return None
    prev_end = html.rfind("</a>", 0, idx)
    start = html.find("<a", (prev_end + 1) if prev_end != -1 else 0, idx + 1)
    if start == -1 or start >= end:
        return None
    return start, end


def _strip_tags(anchor_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", anchor_html)
    return re.sub(r"\s+", " ", text).strip()


def fetch_investing_symbols(
    timeout: float = 30.0,
    url: str | None = None,
) -> list[InvestingSymbol]:
    target = url or f"{INVESTING_VN_BASE}/equities/vietnam"
    html = fetch_investing_html(target, timeout=timeout)
    return parse_investing_equities_list_html(html)


def load_investing_id_map(path: Path) -> dict[str, int]:
    payload = read_json(path)
    if isinstance(payload, dict) and "items" in payload and isinstance(payload["items"], list):
        items = payload["items"]
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    out: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_ticker = (
            item.get("ticker")
            or item.get("symbol")
            or item.get("Ticker")
            or item.get("Symbol")
        )
        raw_id = (
            item.get("pair_id")
            or item.get("pairId")
            or item.get("id")
            or item.get("ID")
        )
        if not raw_ticker or raw_id is None:
            continue
        try:
            pair_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        out[str(raw_ticker).strip().upper()] = pair_id
    return out


def save_investing_id_map(
    symbols: list[InvestingSymbol],
    data_dir: Path = DEFAULT_DATA_DIR,
) -> Path:
    data_dir = ensure_data_dir(data_dir)
    path = data_dir / "investing_id_map.json"
    payload_items = [asdict(symbol) for symbol in symbols]
    payload_items.sort(key=lambda item: item["ticker"])
    write_json(
        path,
        {
            "source": f"{INVESTING_VN_BASE}/equities/vietnam",
            "fetchedAt": utc_now(),
            "count": len(payload_items),
            "items": payload_items,
        },
    )
    return path


def save_investing_prices(
    ticker: str,
    bars: list[PriceBar],
    pair_id: int,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> Path:
    data_dir = ensure_data_dir(data_dir)
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker is required")
    target_dir = data_dir / DEFAULT_INVESTING_PRICES_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{ticker}.json"
    write_json(
        path,
        {
            "source": f"{INVESTING_API_BASE}/api/financialdata/historical/{pair_id}",
            "symbol": ticker,
            "pairId": pair_id,
            "fetchedAt": utc_now(),
            "count": len(bars),
            "items": [asdict(bar) for bar in bars],
        },
    )
    return path


def saved_investing_price_file_has_items(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("items"))


def load_investing_prices(
    ticker: str,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> list[PriceBar]:
    ticker = ticker.strip().upper()
    path = data_dir / DEFAULT_INVESTING_PRICES_DIR / f"{ticker}.json"
    if not path.exists():
        return []
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return []
    raw_bars = payload.get("items", [])
    if not isinstance(raw_bars, list):
        return []
    bars: list[PriceBar] = []
    for raw in raw_bars:
        if not isinstance(raw, dict):
            continue
        try:
            bars.append(
                PriceBar(
                    time=str(raw["time"]),
                    open=float(raw["open"]),
                    high=float(raw["high"]),
                    low=float(raw["low"]),
                    close=float(raw["close"]),
                    volume=float(raw.get("volume") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(bars, key=lambda item: item.time)


def sync_investing_prices(
    id_map: dict[str, int],
    start_date: str,
    end_date: str,
    data_dir: Path = DEFAULT_DATA_DIR,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    time_frame: str = TIME_FRAME_DAILY,
    sleep_seconds: float = DEFAULT_PAGE_SLEEP_SECONDS,
    skip_existing: bool = True,
    timeout: float = 30.0,
) -> dict[str, Path]:
    if not _is_iso_date(start_date) or not _is_iso_date(end_date):
        raise ValueError("start_date and end_date must be YYYY-MM-DD")
    if not id_map:
        raise ValueError("id_map is empty; provide at least one (ticker, pair_id) pair")

    ensure_data_dir(data_dir)
    target_dir = data_dir / DEFAULT_INVESTING_PRICES_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    total = len(id_map)
    for index, (ticker, pair_id) in enumerate(sorted(id_map.items()), start=1):
        ticker = ticker.strip().upper()
        path = target_dir / f"{ticker}.json"
        if skip_existing and saved_investing_price_file_has_items(path):
            print(f"[{index}/{total}] skip existing {ticker}")
            paths[ticker] = path
            continue
        print(f"[{index}/{total}] fetching {ticker} (pair_id={pair_id}) {start_date}..{end_date}")
        try:
            bars = fetch_investing_history_chunked(
                pair_id=pair_id,
                start_date=start_date,
                end_date=end_date,
                time_frame=time_frame,
                chunk_days=chunk_days,
                sleep_seconds=sleep_seconds,
                timeout=timeout,
            )
        except RuntimeError as exc:
            print(f"  {ticker} fetch failed: {exc}")
            continue
        paths[ticker] = save_investing_prices(ticker, bars, pair_id, data_dir=data_dir)
    return paths


def merge_history_with_prices(
    primary: list[PriceBar],
    extra: list[PriceBar],
) -> list[PriceBar]:
    by_date: dict[str, PriceBar] = {}
    for bar in primary:
        by_date[bar.time] = bar
    for bar in extra:
        by_date[bar.time] = bar
    return sorted(by_date.values(), key=lambda item: item.time)
