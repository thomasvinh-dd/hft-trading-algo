from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


VIETNAM_FINANCE_URL = "https://data.vietnamfinance.vn"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_PRICE_LIMIT = 1825


@dataclass(frozen=True)
class StockSymbol:
    ticker: str
    companyName: str | None
    exchangeCode: str | None
    sectorName: str | None
    closePrice: float | None
    changePercent: float | None
    volume: float | None
    value: float | None
    marketCap: float | None
    peRatio: float | None
    tradeDate: str | None


@dataclass(frozen=True)
class PriceBar:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_json(url: str, timeout: float = 30.0, max_retries: int = 3) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://data.vietnamfinance.vn/",
            "User-Agent": "Mozilla/5.0 market-bot-vietnam-algo/0.1",
        },
        method="GET",
    )
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < max_retries:
                retry_after = retry_after_seconds(detail, default=30.0)
                print(f"Rate limited; sleeping {retry_after:.0f}s before retrying {url}")
                time.sleep(retry_after)
                continue
            raise RuntimeError(f"{url} -> HTTP {exc.code} {detail}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            if attempt < max_retries:
                sleep_seconds = 2.0 * (attempt + 1)
                print(f"Request failed: {reason}; sleeping {sleep_seconds:.0f}s before retrying {url}")
                time.sleep(sleep_seconds)
                continue
            raise RuntimeError(f"{url} -> {reason}") from exc
    raise RuntimeError(f"{url} -> request failed")


def retry_after_seconds(detail: str, default: float) -> float:
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return default
    try:
        return max(float(payload.get("retryAfterSeconds") or default), 1.0)
    except (TypeError, ValueError):
        return default


def parse_symbols(payload: Any) -> tuple[list[StockSymbol], dict[str, Any]]:
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    symbols: list[StockSymbol] = []
    for row in rows:
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        symbols.append(
            StockSymbol(
                ticker=ticker,
                companyName=row.get("companyName"),
                exchangeCode=row.get("exchangeCode"),
                sectorName=row.get("sectorName"),
                closePrice=number_or_none(row.get("closePrice")),
                changePercent=number_or_none(row.get("changePercent")),
                volume=number_or_none(row.get("volume")),
                value=number_or_none(row.get("value")),
                marketCap=number_or_none(row.get("marketCap")),
                peRatio=number_or_none(row.get("peRatio")),
                tradeDate=row.get("tradeDate"),
            )
        )
    return symbols, dict(meta) if isinstance(meta, dict) else {}


def parse_price_bars(payload: Any) -> list[PriceBar]:
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    bars: list[PriceBar] = []
    for row in rows:
        try:
            bars.append(
                PriceBar(
                    time=str(row["time"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(bars, key=lambda item: item.time)


def number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ensure_data_dir(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "prices").mkdir(parents=True, exist_ok=True)
    return data_dir


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_symbols_page(page: int, limit: int, sort: str, order: str, timeout: float) -> tuple[list[StockSymbol], dict[str, Any]]:
    params = urllib.parse.urlencode({"page": page, "limit": limit, "sort": sort, "order": order})
    payload = fetch_json(f"{VIETNAM_FINANCE_URL}/api/stocks?{params}", timeout=timeout)
    return parse_symbols(payload)


def fetch_all_symbols(limit: int = 50, sort: str = "popular", order: str = "desc", timeout: float = 30.0) -> list[StockSymbol]:
    symbols: list[StockSymbol] = []
    seen: set[str] = set()
    page = 1
    total_pages = 1
    while page <= total_pages:
        page_symbols, meta = fetch_symbols_page(page, limit, sort, order, timeout)
        for symbol in page_symbols:
            if symbol.ticker not in seen:
                symbols.append(symbol)
                seen.add(symbol.ticker)
        total_pages = int(meta.get("totalPages") or total_pages)
        if not page_symbols:
            break
        page += 1
    return symbols


def fetch_price_bars(symbol: str, limit: int = DEFAULT_PRICE_LIMIT, timeout: float = 30.0) -> list[PriceBar]:
    clean_symbol = urllib.parse.quote(symbol.strip().upper())
    params = urllib.parse.urlencode({"limit": limit})
    payload = fetch_json(f"{VIETNAM_FINANCE_URL}/api/stock/{clean_symbol}/prices?{params}", timeout=timeout)
    return parse_price_bars(payload)


def save_symbols(symbols: list[StockSymbol], data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    data_dir = ensure_data_dir(data_dir)
    path = data_dir / "symbols.json"
    write_json(
        path,
        {
            "source": f"{VIETNAM_FINANCE_URL}/api/stocks",
            "fetchedAt": utc_now(),
            "count": len(symbols),
            "items": [asdict(symbol) for symbol in symbols],
        },
    )
    return path


def save_price_bars(symbol: str, bars: list[PriceBar], data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    data_dir = ensure_data_dir(data_dir)
    ticker = symbol.strip().upper()
    path = data_dir / "prices" / f"{ticker}.json"
    write_json(
        path,
        {
            "source": f"{VIETNAM_FINANCE_URL}/api/stock/{ticker}/prices",
            "symbol": ticker,
            "fetchedAt": utc_now(),
            "count": len(bars),
            "items": [asdict(bar) for bar in bars],
        },
    )
    return path


def load_saved_symbols(data_dir: Path = DEFAULT_DATA_DIR) -> list[str]:
    path = data_dir / "symbols.json"
    payload = read_json(path)
    return [str(item["ticker"]).upper() for item in payload.get("items", []) if item.get("ticker")]


def sync_symbols(data_dir: Path = DEFAULT_DATA_DIR, limit: int = 50, timeout: float = 30.0) -> Path:
    symbols = fetch_all_symbols(limit=limit, timeout=timeout)
    return save_symbols(symbols, data_dir)


def sync_prices(
    symbols: list[str],
    data_dir: Path = DEFAULT_DATA_DIR,
    price_limit: int = DEFAULT_PRICE_LIMIT,
    timeout: float = 30.0,
    sleep_seconds: float = 0.2,
    skip_existing: bool = True,
) -> list[Path]:
    data_dir = ensure_data_dir(data_dir)
    paths: list[Path] = []
    total = len(symbols)
    for index, symbol in enumerate(symbols, start=1):
        ticker = symbol.strip().upper()
        path = data_dir / "prices" / f"{ticker}.json"
        if skip_existing and saved_price_file_has_items(path):
            print(f"[{index}/{total}] skip existing {ticker}")
            paths.append(path)
            continue
        print(f"[{index}/{total}] fetching {ticker}")
        bars = fetch_price_bars(symbol, limit=price_limit, timeout=timeout)
        paths.append(save_price_bars(symbol, bars, data_dir))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return paths


def load_saved_price_bars(path: Path) -> list[dict[str, Any]]:
    """Read existing price JSON file and return its raw bar dicts."""
    if not path.exists():
        return []
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    return [it for it in (items or []) if isinstance(it, dict)]


def sync_prices_incremental(
    symbols: list[str],
    data_dir: Path = DEFAULT_DATA_DIR,
    timeout: float = 30.0,
    sleep_seconds: float = 0.2,
    incremental_limit: int = 10,
) -> list[Path]:
    """Incrementally append new bars to existing price files.

    For each symbol:
      - If no existing file, fetch full ``incremental_limit`` bars (cheap
        fallback — full history will be built up over the next refreshes).
      - If existing file is present, fetch only ``incremental_limit``
        most-recent bars from the upstream API, merge with existing bars
        by deduping on ``time``, and persist the merged result.

    Returns the list of paths that were written (newest bar timestamp
    moved forward).
    """
    data_dir = ensure_data_dir(data_dir)
    paths: list[Path] = []
    total = len(symbols)
    new_bars_added = 0
    for index, symbol in enumerate(symbols, start=1):
        ticker = symbol.strip().upper()
        path = data_dir / "prices" / f"{ticker}.json"
        existing_rows = load_saved_price_bars(path)
        try:
            fresh_bars = fetch_price_bars(symbol, limit=incremental_limit, timeout=timeout)
        except Exception as exc:
            print(f"[{index}/{total}] fetch {ticker} FAILED: {exc}")
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            continue
        # Convert PriceBar -> dict so we can dedupe by 'time' with existing rows.
        fresh_rows = [asdict(b) for b in fresh_bars]
        # existing rows already have the same shape (time/open/high/low/close/volume)
        seen = {str(r.get("time")) for r in existing_rows}
        before = len(existing_rows)
        merged = list(existing_rows)
        for row in fresh_rows:
            t = str(row.get("time"))
            if t and t not in seen:
                seen.add(t)
                merged.append(row)
                new_bars_added += 1
        # Sort by time ascending (defensive)
        merged.sort(key=lambda r: str(r.get("time") or ""))
        if not merged:
            continue
        # Always write if we added new bars OR if the file wasn't there.
        if new_bars_added > 0 or not existing_rows:
            save_price_bars(
                symbol,
                [
                    PriceBar(
                        time=str(r.get("time") or ""),
                        open=float(r.get("open") or 0),
                        high=float(r.get("high") or 0),
                        low=float(r.get("low") or 0),
                        close=float(r.get("close") or 0),
                        volume=float(r.get("volume") or 0),
                    )
                    for r in merged
                ],
                data_dir,
            )
            paths.append(path)
        if index % 50 == 0 or index == total:
            print(
                f"[{index}/{total}] updated {len(paths)}/{index}"
                f" (+{new_bars_added} new bars so far)"
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    print(
        f"sync_prices_incremental done: {len(paths)} files updated,"
        f" {new_bars_added} new bars appended across {total} symbols."
    )
    return paths


def saved_price_file_has_items(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("items"))
