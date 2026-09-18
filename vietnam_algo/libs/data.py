from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .dnse.client import DEFAULT_DAILY_BAR_TYPE, DnseClient, parse_instruments
from .dnse.client import parse_price_bars as _dnse_parse_price_bars


# Runtime data lives at vietnam_algo/data/, NOT inside libs/. The
# module moved from vietnam_algo/data.py to vietnam_algo/libs/data.py
# during the 2026-09 restructure; we hard-code the data dir relative
# to ``vietnam_algo`` (the package root) so existing data files are
# still on the expected path.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_PRICE_LIMIT = 1825

# Marker we stamp into ``symbols.json`` so downstream code can tell
# whether the universe was sourced from DNSE (only source as of
# 2026-09-18 — the previous ``vietnamfinance.vn`` scraper was
# rate-limited and removed). Kept as a constant for symmetry with
# the per-symbol price files.
DATA_SOURCE_DNSE = "dnse-openapi"


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


def _build_dnse_client() -> DnseClient:
    """Build a DnseClient from env vars. Raises if creds are missing.

    We read directly from os.environ so this works in the standalone
    cron job (``common_sync``) without needing a fully-constructed
    EngineConfig — the same DNSE_API_KEY / DNSE_API_SECRET are
    already required by the trading bot, so reuse them.
    """
    api_key = os.environ.get("DNSE_API_KEY", "").strip()
    api_secret = os.environ.get("DNSE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError(
            "DNSE_API_KEY / DNSE_API_SECRET not set — required for "
            "data sync. Export them from vietnam_algo/env/.env.autofin-signal."
        )
    return DnseClient(api_key=api_key, api_secret=api_secret)


def parse_symbols(payload: Any) -> tuple[list[StockSymbol], dict[str, Any]]:
    """Map DNSE ``/market/instruments`` rows onto ``StockSymbol``.

    DNSE exposes {symbol, isin, shortName, name, symbolType, listedDate,
    marketId, securityGroupId, indexName}. We pick what maps cleanly
    onto the legacy StockSymbol shape and leave the rest as None:
      - ticker      ← symbol
      - companyName ← name (fallback: shortName)
      - exchangeCode← marketId ("STO" for HOSE/HNX stocks, "UPX" for
        UPCoM). DNSE does not distinguish HOSE vs HNX in this field;
        a per-symbol ``/price/{sym}/secdef`` call would, but we skip
        it to keep the daily sync at one API call per symbol.
      - sectorName  ← None (DNSE's public API does not expose
        ICB/sector classification).
      - The numeric fields (closePrice / volume / marketCap / …)
        all stay None; they used to come from vietnamfinance's
        /api/stocks payload, which has no DNSE equivalent on
        the public instruments endpoint.
    """
    rows = parse_instruments(payload)
    # DNSE wraps instruments under ``data``; we re-extract marketId
    # from the raw payload since parse_instruments strips it. Fall
    # back to "STO" if the wrapping is missing.
    market_by_symbol: dict[str, str] = {}
    if isinstance(payload, dict):
        raw_items = payload.get("data") or []
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                sym = item.get("symbol")
                mid = item.get("marketId") or item.get("market")
                if sym and mid:
                    market_by_symbol[str(sym).upper()] = str(mid)
    symbols: list[StockSymbol] = []
    for row in rows:
        ticker = str(row.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        name = row.get("name") or row.get("shortName")
        exchange = market_by_symbol.get(ticker, "STO")
        symbols.append(
            StockSymbol(
                ticker=ticker,
                companyName=str(name) if name else None,
                exchangeCode=exchange,
                sectorName=None,
                closePrice=None,
                changePercent=None,
                volume=None,
                value=None,
                marketCap=None,
                peRatio=None,
                tradeDate=None,
            )
        )
    return symbols, {}


def parse_price_bars(payload: Any) -> list[PriceBar]:
    """Map DNSE ``/price/ohlc`` response onto ``PriceBar``.

    DNSE returns parallel arrays ``t/o/h/l/c/v`` (unix seconds +
    floats). The existing ``libs/dnse/client.py:parse_price_bars``
    already converts these to ``{time, open, high, low, close, volume}``
    dicts; we wrap that here so callers get ``PriceBar`` instances
    sorted ascending by time.

    Unit conversion: DNSE returns prices in *thousands of VND*
    (e.g. ``27.5`` for HDB = 27,500 VND). The legacy
    vietnamfinance.vn source returned absolute VND
    (``close: 27500.0``). All downstream code — mark-to-market,
    MV calculations, BUY sizing, autofin signal entry gates —
    assumes absolute VND, so we multiply by 1000 here at the
    boundary. See 2026-09-19 incident where the bot marked QNS
    to 27.5 VND instead of 27,500 VND because the parse path
    passed DNSE's kVND value through unchanged.
    """
    rows = parse_price_bars_dnse(payload)
    bars: list[PriceBar] = []
    for row in rows:
        try:
            bars.append(
                PriceBar(
                    time=str(row["time"]),
                    open=float(row["open"]) * 1000,
                    high=float(row["high"]) * 1000,
                    low=float(row["low"]) * 1000,
                    close=float(row["close"]) * 1000,
                    volume=float(row.get("volume") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(bars, key=lambda item: item.time)


def parse_price_bars_dnse(payload: Any) -> list[dict[str, Any]]:
    """Convert DNSE's ``{t:[..], o:[..], ...}`` shape to list of dicts."""
    return _dnse_parse_price_bars(payload)


def ensure_data_dir(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "prices").mkdir(parents=True, exist_ok=True)
    return data_dir


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_all_symbols(
    limit: int = 100,
    sort: str = "popular",
    order: str = "desc",
    timeout: float = 30.0,
) -> list[StockSymbol]:
    """Fetch the full VN stock universe via DNSE ``/market/instruments``.

    DNSE returns ~3000 instruments including derivatives and bonds;
    ``parse_instruments`` filters to ``marketId in {"STO", "UPX"}``
    (HOSE/HNX + UPCoM stocks — both have ``securityGroupId="ST"``).
    We paginate at the rate-limit-friendly page size of 100
    and dedupe by ticker.

    Note: we feed ``parse_symbols`` the raw API response (one dict
    per page) rather than the already-parsed rows from
    ``parse_instruments`` — we need ``marketId`` on each row so
    the resulting ``StockSymbol.exchangeCode`` preserves the
    HOSE-vs-UPCoM distinction (STO vs UPX). Pre-2026-09-19 this
    was wired backwards and every ticker ended up stamped as
    ``STO`` regardless of its real exchange, which caused the
    QNS/UPCoM price files to never refresh.
    """
    client = _build_dnse_client()
    page_size = min(100, max(1, limit))
    symbols: list[StockSymbol] = []
    seen: set[str] = set()
    page = 1
    pages_fetched = 0
    while pages_fetched < limit:
        response = client.get_instruments(limit=page_size, page=page)
        if isinstance(response, dict):
            raw = response.get("data") or []
            meta_total = int(response.get("total") or 0)
        else:
            raw = response
            meta_total = 0
        if not raw:
            break
        page_symbols, _ = parse_symbols(response)
        for sym in page_symbols:
            if sym.ticker in seen:
                continue
            seen.add(sym.ticker)
            symbols.append(sym)
        page += 1
        pages_fetched += 1
        if len(raw) < page_size:
            break
        if meta_total and len(symbols) >= meta_total:
            break
    return symbols


def fetch_price_bars(
    symbol: str,
    limit: int = DEFAULT_PRICE_LIMIT,
    timeout: float = 30.0,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[PriceBar]:
    """Fetch daily OHLC bars for ``symbol`` via DNSE ``/price/ohlc``.

    When ``from_date`` / ``to_date`` are provided, DNSE returns only
    that window (cheaper — used by the daily incremental sync).
    Otherwise we walk back ``limit`` calendar days from today.
    Resolution is fixed at ``"1D"`` (daily bars); the ``1`` (intraday)
    resolution is rarely useful for backtests and DNSE caps response
    size for it.
    """
    client = _build_dnse_client()
    if to_date is None:
        to_date = datetime.now(tz=UTC).date().isoformat()
    if from_date is None:
        end = datetime.strptime(to_date, "%Y-%m-%d").date()
        lookback_days = max(int(limit), 30)
        from_date = (end - timedelta(days=lookback_days)).isoformat()
    payload = client.get_ohlc(
        bar_type=DEFAULT_DAILY_BAR_TYPE,
        symbol=symbol.strip().upper(),
        resolution="1D",
        from_date=from_date,
        to_date=to_date,
    )
    return parse_price_bars(payload)


def save_symbols(symbols: list[StockSymbol], data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    data_dir = ensure_data_dir(data_dir)
    path = data_dir / "symbols.json"
    write_json(
        path,
        {
            "source": DATA_SOURCE_DNSE,
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
            "source": DATA_SOURCE_DNSE,
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
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Incrementally append new bars to existing price files via DNSE.

    For each symbol:
      - If no existing file, fetch a longer history (``bootstrap_days``)
        so the universe build has enough lookback. Cheap on DNSE.
      - If existing file is present, compute from_date as the last-bar
        date (minus a 7-day overlap to absorb upstream revisions) and
        fetch only the small window. Merge by deduping on ``time``.

    The DNSE API takes explicit ``from_date`` / ``to_date`` parameters
    (the previous ``vietnamfinance.vn`` scraper only returned the most
    recent N bars). That lets us shrink each call to ~7 calendar days
    per existing symbol instead of 10 unaligned bars.

    Returns ``(paths, errors)``:
      - ``paths``: list of paths whose newest bar moved forward
        (or that had no existing file and got one written).
      - ``errors``: list of ``(symbol, error_message)`` tuples for any
        per-symbol fetch that raised. The function never raises on
        per-symbol fetch failures — callers can decide whether to alert
        based on ``len(errors) / total``.
    """
    data_dir = ensure_data_dir(data_dir)
    paths: list[Path] = []
    errors: list[tuple[str, str]] = []
    total = len(symbols)
    new_bars_added = 0
    today = datetime.now(tz=UTC).date()
    bootstrap_days = 90  # used when a symbol has no existing file
    overlap_days = 7  # safety overlap so we catch upstream revisions
    for index, symbol in enumerate(symbols, start=1):
        ticker = symbol.strip().upper()
        path = data_dir / "prices" / f"{ticker}.json"
        existing_rows = load_saved_price_bars(path)
        # Decide the fetch window. DNSE expects YYYY-MM-DD.
        if existing_rows:
            try:
                last_dt = datetime.fromisoformat(
                    str(existing_rows[-1].get("time") or "").replace("Z", "+00:00")
                ).date()
            except ValueError:
                last_dt = today - timedelta(days=bootstrap_days)
            from_date = (last_dt - timedelta(days=overlap_days)).isoformat()
        else:
            from_date = (today - timedelta(days=bootstrap_days)).isoformat()
        to_date = today.isoformat()
        try:
            fresh_bars = fetch_price_bars(
                symbol,
                limit=incremental_limit,
                timeout=timeout,
                from_date=from_date,
                to_date=to_date,
            )
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            print(f"[{index}/{total}] fetch {ticker} FAILED: {exc}")
            errors.append((ticker, err))
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            continue
        # Convert PriceBar -> dict so we can dedupe by 'time' with existing rows.
        fresh_rows = [asdict(b) for b in fresh_bars]
        # existing rows already have the same shape (time/open/high/low/close/volume)
        seen = {str(r.get("time")) for r in existing_rows}
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
        f" {new_bars_added} new bars appended across {total} symbols,"
        f" {len(errors)} fetch errors."
    )
    return paths, errors


def get_market_data_freshness(
    data_dir: Path = DEFAULT_DATA_DIR,
    sample_symbols: tuple[str, ...] = ("HPG", "VHM", "FPT", "VIC", "HDB"),
) -> dict[str, Any]:
    """Return market-data freshness signals for alert/health-check use.

    Output keys:
      - ``sentinel_exists``: bool — does ``data/live/data_ready_for_eod``
        exist?
      - ``sentinel_age_hours``: float | None — age of the sentinel file
        in hours (None if missing). Computed as ``now - mtime``.
      - ``sentinel_mtime``: str | None — ISO8601 UTC mtime (None if missing).
      - ``sample_size``: int — number of sample symbols actually found on
        disk.
      - ``sample_total``: int — total symbols requested.
      - ``median_latest_bar_age_days``: float | None — median of
        ``today - latest_bar.time`` across sample symbols. None if no
        sample symbols had any bars.
      - ``oldest_sample_symbol``: str | None — symbol with the oldest
        latest bar (None if no sample data).
      - ``fresh``: bool — ``True`` iff sentinel_age_hours <= 25 AND
        median_latest_bar_age_days <= 2.0.

    The thresholds match the alerts wired into ``common_sync.run()`` and
    the bot heartbeat block (see ``libs/notifier.py`` and
    ``jobs/common_sync.py``).
    """
    now = datetime.now(UTC)
    result: dict[str, Any] = {
        "sentinel_exists": False,
        "sentinel_age_hours": None,
        "sentinel_mtime": None,
        "sample_size": 0,
        "sample_total": len(sample_symbols),
        "median_latest_bar_age_days": None,
        "oldest_sample_symbol": None,
        "fresh": False,
    }
    sentinel_path = data_dir / "live" / "data_ready_for_eod"
    if sentinel_path.exists():
        mtime = datetime.fromtimestamp(sentinel_path.stat().st_mtime, tz=UTC)
        age_seconds = (now - mtime).total_seconds()
        result["sentinel_exists"] = True
        result["sentinel_age_hours"] = round(age_seconds / 3600.0, 2)
        result["sentinel_mtime"] = mtime.isoformat()
    ages_days: list[tuple[str, float]] = []
    for sym in sample_symbols:
        path = data_dir / "prices" / f"{sym}.json"
        if not path.exists():
            continue
        rows = load_saved_price_bars(path)
        if not rows:
            continue
        last_time_str = str(rows[-1].get("time") or "")
        if not last_time_str:
            continue
        try:
            last_dt = datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
        age_days = (now - last_dt).total_seconds() / 86400.0
        ages_days.append((sym, age_days))
    if ages_days:
        result["sample_size"] = len(ages_days)
        median_age = statistics.median(a for _, a in ages_days)
        result["median_latest_bar_age_days"] = round(median_age, 2)
        result["oldest_sample_symbol"] = max(ages_days, key=lambda x: x[1])[0]
    sentinel_fresh = (
        result["sentinel_age_hours"] is not None and result["sentinel_age_hours"] <= 25.0
    )
    bars_fresh = (
        result["median_latest_bar_age_days"] is not None
        and result["median_latest_bar_age_days"] <= 2.0
    )
    result["fresh"] = bool(sentinel_fresh and bars_fresh)
    return result


def saved_price_file_has_items(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("items"))
