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
      - exchangeCode← mapped_marketId. DNSE returns ``marketId`` in
        {"STO", "UPX", "STX", "DVX", "HCX"}. STO covers HOSE
        equities; UPX covers UPCoM; STX covers HNX equities (DNSE
        post-merger labelling — verified 2026-10-21 with MST and NTP
        which DNSE returns as STX but are listed on the HNX exchange
        and included in the HNX-30 index). We map STO/STX → HOSE/HNX
        respectively so the universe filter (``DEFAULT_EXCHANGES =
        {"HOSE", "HNX", "UPCOM", "STO", "UPX"}``) accepts the
        resulting ``exchangeCode``. ``DVX`` and ``HCX`` (derivatives,
        bonds, warrants) are filtered upstream by ``parse_instruments``.
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
    # DNSE → StockSymbol exchangeCode mapping. "STX" is DNSE's label
    # for HNX-equities post the HOSE/HNX/HCM merger; map to "HNX".
    # "STO" stays as-is so existing universe filters that key off
    # "STO" keep working (universe DEFAULT_EXCHANGES includes both
    # "STO" and "HNX"). "UPX" stays as-is for UPCoM.
    MARKET_TO_EXCHANGE = {"STO": "STO", "STX": "HNX", "UPX": "UPX"}
    symbols: list[StockSymbol] = []
    for row in rows:
        ticker = str(row.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        name = row.get("name") or row.get("shortName")
        raw_market = market_by_symbol.get(ticker, "STO")
        exchange = MARKET_TO_EXCHANGE.get(raw_market, raw_market)
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


# DNSE rejects a fixed set of tickers (bonds, warrants, delisted names,
# etc.) with ``HTTP 400 {"code":"BAD_REQUEST","message":"invalid symbol"}``
# on every fetch. We maintain a per-data-dir denylist so:
#   * the daily price sync skips them (saves ~50% of the per-cycle
#     API quota when the universe is the broad HOSE/UPCoM list — see
#     the 2026-09-23 incident where 1148/2186 symbols failed);
#   * common_sync computes its success-rate using ``(total - denylist)``
#     as the denominator so the sentinel age reflects how many
#     symbols DNSE ACTUALLY carries, not how many it returns 400 for.
INVALID_SYMBOL_DENYLIST_FILENAME = "dnse_invalid_symbols.json"


def _invalid_symbol_denylist_path(data_dir: Path) -> Path:
    return data_dir / "live" / INVALID_SYMBOL_DENYLIST_FILENAME


def load_invalid_symbol_denylist(data_dir: Path = DEFAULT_DATA_DIR) -> set[str]:
    """Read the set of tickers DNSE has rejected as ``invalid symbol``.

    Returns an empty set if the file is missing or unreadable — callers
    can treat absence as "no denylist yet" and let the first sync run
    populate it.
    """
    path = _invalid_symbol_denylist_path(data_dir)
    if not path.exists():
        return set()
    try:
        payload = read_json(path)
        items = payload.get("symbols", []) if isinstance(payload, dict) else []
        return {str(s).strip().upper() for s in items if s}
    except Exception:
        return set()


def save_invalid_symbol_denylist(
    data_dir: Path,
    symbols: set[str],
) -> Path:
    """Persist the denylist (sorted for readability). Idempotent."""
    path = _invalid_symbol_denylist_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        path,
        {
            "source": "dnse-openapi",
            "fetchedAt": utc_now(),
            "count": len(symbols),
            "symbols": sorted(symbols),
        },
    )
    return path


def is_invalid_symbol_error(err: str) -> bool:
    """True when ``err`` is a DNSE 'invalid symbol' (HTTP 400 BAD_REQUEST).

    Matches the exact pattern the DNSE client raises in
    ``DnseClient.request`` for 400 responses — see common_sync.py
    failure samples like ``HTTP 400 {"...","message":"invalid symbol"}``.
    """
    return "HTTP 400" in err and "invalid symbol" in err


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

    DNSE 'invalid symbol' handling
    ------------------------------

    DNSE returns ``HTTP 400 {"code":"BAD_REQUEST","message":"invalid
    symbol"}`` for tickers it doesn't carry (bonds, warrants, delisted
    names, etc. — the broad HOSE/UPCoM universe has ~1100 of these as
    of 2026-09-23). We maintain a per-data-dir denylist at
    ``data/live/dnse_invalid_symbols.json`` so:

      * Symbols already on the denylist are skipped at the top of the
        loop (no API call, no log line, no error tuple).
      * New 400s are appended to the denylist on the fly and persisted
        at the end of the call, so the next sync run skips them too.

    The callers (common_sync, daily_engine.update_dataset) don't need
    to know about the denylist — it's an implementation detail of the
    sync layer. They can compute success rates using
    ``len(errors) / len(symbols_filtered_to_denylist_excluded)``.
    """
    data_dir = ensure_data_dir(data_dir)
    paths: list[Path] = []
    errors: list[tuple[str, str]] = []
    denylist = load_invalid_symbol_denylist(data_dir)
    denylist_grew = False
    new_bars_added = 0
    today = datetime.now(tz=UTC).date()
    bootstrap_days = 90  # used when a symbol has no existing file
    overlap_days = 7  # safety overlap so we catch upstream revisions
    # Filter out already-denylisted symbols BEFORE the loop so they
    # don't count toward ``total`` (which feeds the per-N progress
    # prints and the post-summary). Callers that want the raw universe
    # size should consult the saved denylist file.
    candidates = [s for s in symbols if s.strip().upper() not in denylist]
    skipped_denylisted = len(symbols) - len(candidates)
    if skipped_denylisted:
        print(
            f"[sync_prices_incremental] skipping {skipped_denylisted} "
            f"symbol(s) on the DNSE invalid-symbol denylist"
        )
    total = len(candidates)
    for index, symbol in enumerate(candidates, start=1):
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
            # 400 'invalid symbol' is a permanent upstream rejection —
            # add to the denylist so future runs skip the API call
            # entirely (saves quota and runtime).
            if ticker not in denylist and is_invalid_symbol_error(err):
                denylist.add(ticker)
                denylist_grew = True
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
    if denylist_grew:
        try:
            save_invalid_symbol_denylist(data_dir, denylist)
            print(
                f"[sync_prices_incremental] denylist now has {len(denylist)}"
                f" symbol(s); saved to {_invalid_symbol_denylist_path(data_dir)}"
            )
        except Exception as exc:
            print(f"[sync_prices_incremental] WARNING: failed to save denylist: {exc}")
    print(
        f"sync_prices_incremental done: {len(paths)} files updated,"
        f" {new_bars_added} new bars appended across {total} symbols,"
        f" {len(errors)} fetch errors,"
        f" {skipped_denylisted} skipped (denylisted)."
    )
    return paths, errors


def get_market_data_freshness(
    data_dir: Path = DEFAULT_DATA_DIR,
    sample_symbols: tuple[str, ...] = ("HPG", "VHM", "FPT", "VIC", "HDB"),
    now: datetime | None = None,
    *,
    include_held_positions: bool = True,
    held_position_strategies: tuple[str, ...] = (
        "momentum", "autofin_signal",
    ),
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
        ``trading_days_between(today, last_bar_date)`` across sample
        symbols. None if no sample symbols had any bars.
      - ``median_latest_bar_age_calendar_days``: float | None — same as
        ``median_latest_bar_age_days`` but in plain calendar days
        (used for the alert text so the operator understands the gap
        including weekends).
      - ``oldest_sample_symbol``: str | None — symbol with the oldest
        latest bar (None if no sample data).
      - ``held_position_count``: int — number of held-position symbols
        added to the sample (0 when ``include_held_positions=False``).
      - ``held_positions``: list[str] — sorted list of held-position
        symbols added to the sample.
      - ``stale_held_positions``: list[dict] — held-position symbols
        whose last bar is older than 2 trading days; each entry has
        ``symbol``, ``last_bar_date``, ``age_trading_days``, and
        ``age_calendar_days``. Empty when all held positions are
        fresh. Always empty when ``include_held_positions=False``.
      - ``fresh``: bool — ``True`` iff sentinel_age_hours <= 25 AND
        median_latest_bar_age_days <= 2.0.

    Bar age is measured in **trading days**, not calendar days.
    Vietnamese markets close on weekends and Vietnamese public
    holidays, so a Friday close on a Monday morning is only 1
    trading day old — not 3. The previous calendar-days count
    flagged Friday data as "stale" every Monday morning and every
    day after a Vietnamese public holiday.

    The thresholds match the alerts wired into ``common_sync.run()`` and
    the bot heartbeat block (see ``libs/notifier.py`` and
    ``jobs/common_sync.py``).

    2026-10-21 incident (MST): the dry-run state held 9,500 MST
    shares but the fixed 5-symbol sample never included MST, so
    ``fresh: True`` was reported even though the position's
    price file was 67+ calendar days stale. With
    ``include_held_positions=True`` (default), the function now
    unions the fixed sample with each strategy's dry-run state's
    ``positions`` keys, surfaces ``stale_held_positions`` as a
    dedicated list, and recomputes the median age over the union
    so a stale held position bumps the alert correctly.

    ``now`` is injectable for tests; defaults to ``datetime.now(UTC)``.
    """
    from .market_calendar import load_market_holidays, trading_dates_between

    if now is None:
        now = datetime.now(UTC)
    result: dict[str, Any] = {
        "sentinel_exists": False,
        "sentinel_age_hours": None,
        "sentinel_mtime": None,
        "sample_size": 0,
        "sample_total": 0,
        "held_position_count": 0,
        "held_positions": [],
        "stale_held_positions": [],
        "median_latest_bar_age_days": None,
        "median_latest_bar_age_calendar_days": None,
        "oldest_sample_symbol": None,
        "fresh": False,
    }
    # 2026-10-21 incident (MST): the dry-run state held 9,500 MST
    # shares but the 5-symbol freshness sample never included MST.
    # Result: ``fresh: True`` was returned even though MST's price
    # file was 67+ calendar days stale, and operators got no
    # Telegram alert. We now union the fixed sample with any symbols
    # held in the dry-run state for ``held_position_strategies`` so
    # a held-but-stale position trips the freshness check. Off by
    # default in tests; on by default everywhere else (the
    # explicit ``include_held_positions=False`` kwarg is mainly
    # for tests that don't want to depend on dry-run state files).
    held_symbols: list[str] = []
    if include_held_positions:
        try:
            from .dry_run_state import _state_path as _dry_state_path
        except ImportError:
            _dry_state_path = None  # type: ignore[assignment]
        if _dry_state_path is not None:
            for strat in held_position_strategies:
                sp = _dry_state_path(data_dir, strat)
                if not sp.exists():
                    continue
                try:
                    payload = json.loads(sp.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                positions = (
                    payload.get("positions", {}) if isinstance(payload, dict) else {}
                )
                if not isinstance(positions, dict):
                    continue
                for sym, p in positions.items():
                    qty = 0
                    if isinstance(p, dict):
                        try:
                            qty = int(p.get("quantity", 0) or 0)
                        except (TypeError, ValueError):
                            qty = 0
                    if qty > 0 and sym:
                        held_symbols.append(str(sym).upper())
    # Build the full sample by unioning the fixed sample with held
    # positions, preserving order, deduped, upper-cased.
    seen: set[str] = set()
    full_sample: list[str] = []
    for sym in list(sample_symbols) + held_symbols:
        su = str(sym).upper()
        if su and su not in seen:
            seen.add(su)
            full_sample.append(su)
    result["sample_total"] = len(full_sample)
    result["held_position_count"] = len(held_symbols)
    result["held_positions"] = sorted(set(held_symbols))
    sentinel_path = data_dir / "live" / "data_ready_for_eod"
    if sentinel_path.exists():
        mtime = datetime.fromtimestamp(sentinel_path.stat().st_mtime, tz=UTC)
        age_seconds = (now - mtime).total_seconds()
        result["sentinel_exists"] = True
        result["sentinel_age_hours"] = round(age_seconds / 3600.0, 2)
        result["sentinel_mtime"] = mtime.isoformat()
    # Convert "now" to ICT date for trading-day math. Prices are
    # timestamped in the VN-market session (ICT), so the last bar's
    # date is already an ICT date string. We compare in ICT.
    from .schedule import VIETNAM_TZ, local_date
    today_ict = local_date(now)
    holidays = load_market_holidays(data_dir)
    ages_trading_days: list[tuple[str, float]] = []
    ages_calendar_days: list[tuple[str, float]] = []
    # Per-symbol staleness record so we can populate
    # ``stale_held_positions`` after the loop. Threshold 2.0
    # trading days matches the existing ``bars_fresh`` gate.
    FRESHNESS_THRESHOLD_TRADING_DAYS = 2.0
    per_symbol_age: dict[str, dict[str, Any]] = {}
    for sym in full_sample:
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
        # Use ICT date for the bar — prices are timestamped in the
        # VN-market session (ICT). If the bar timestamp is UTC, we
        # still want to interpret its calendar date as the date the
        # market saw the close (UTC + 7h ≤ 17:00 ICT → same day).
        last_ict = last_dt.astimezone(VIETNAM_TZ).date()
        last_date_str = last_ict.isoformat()
        # Trading-day age = number of trading days between last bar
        # date (exclusive) and today (exclusive). 0 = today (would
        # mean we have today's bar already, which we never do pre-
        # market-open). 1 = yesterday was a trading day. 2 = two
        # trading days ago. Friday's close on a Monday morning is
        # 1 trading day (Sat/Sun skipped).
        if last_date_str >= today_ict:
            # Bar is from today (or in the future, which can happen
            # with UTC-stamped files and an early-ICT "now"). Treat
            # as fresh.
            trading_age = 0
        else:
            trading_days = trading_dates_between(
                last_date_str, today_ict, holidays
            )
            # trading_dates_between is inclusive on both ends. Subtract
            # the last bar's date (it counts as 1 itself) and the
            # today's date (the bar might still be "today" if we
            # check at 00:01 ICT).
            trading_age = max(0, len(trading_days) - 1)
        calendar_age = (now - last_dt).total_seconds() / 86400.0
        ages_trading_days.append((sym, trading_age))
        ages_calendar_days.append((sym, calendar_age))
        per_symbol_age[sym] = {
            "symbol": sym,
            "last_bar_date": last_date_str,
            "age_trading_days": trading_age,
            "age_calendar_days": round(calendar_age, 2),
        }
    if ages_trading_days:
        result["sample_size"] = len(ages_trading_days)
        median_age = statistics.median(a for _, a in ages_trading_days)
        result["median_latest_bar_age_days"] = round(median_age, 2)
        # Keep the calendar-days median around for the alert text so
        # the operator can see the raw gap ("Friday close on
        # Monday = 3 calendar days, 1 trading day").
        cal_median = statistics.median(a for _, a in ages_calendar_days)
        result["median_latest_bar_age_calendar_days"] = round(cal_median, 2)
        result["oldest_sample_symbol"] = max(ages_trading_days, key=lambda x: x[1])[0]
        # Per-symbol staleness for the HELD-POSITION subset. We only
        # flag held positions so the alert text is actionable
        # ("MST is stale, you can't reprice it" is actionable;
        # "HPG is stale, we didn't include it in today's plan"
        # is not — the plan doesn't depend on HPG's stale data).
        for sym in result["held_positions"]:
            sym_upper = str(sym).upper()
            record = per_symbol_age.get(sym_upper)
            if record is None:
                # No price file on disk for this symbol at all.
                # Treat as stale (infinite age).
                result["stale_held_positions"].append({
                    "symbol": sym_upper,
                    "last_bar_date": None,
                    "age_trading_days": None,
                    "age_calendar_days": None,
                    "reason": "no_price_file",
                })
                continue
            if (
                record["age_trading_days"] is not None
                and record["age_trading_days"] > FRESHNESS_THRESHOLD_TRADING_DAYS
            ):
                result["stale_held_positions"].append(record)
        result["sample_size"] = len(ages_trading_days)
        median_age = statistics.median(a for _, a in ages_trading_days)
        result["median_latest_bar_age_days"] = round(median_age, 2)
        # Keep the calendar-days median around for the alert text so
        # the operator can see the raw gap ("Friday close on
        # Monday = 3 calendar days, 1 trading day").
        cal_median = statistics.median(a for _, a in ages_calendar_days)
        result["median_latest_bar_age_calendar_days"] = round(cal_median, 2)
        result["oldest_sample_symbol"] = max(ages_trading_days, key=lambda x: x[1])[0]
    sentinel_fresh = (
        result["sentinel_age_hours"] is not None and result["sentinel_age_hours"] <= 25.0
    )
    bars_fresh = (
        result["median_latest_bar_age_days"] is not None
        and result["median_latest_bar_age_days"] <= 2.0
    )
    # 2026-10-21 incident (MST): the median-based check above uses the
    # WHOLE sample (fixed + held). When the fixed sample is fresh but
    # a held position is stale, the median can still be <= 2.0 and
    # ``bars_fresh`` would be True — masking the actionable held-
    # position staleness. We now require BOTH: the median freshness
    # AND no stale held positions, so any stale held symbol trips
    # ``fresh: False`` and routes through the Telegram alert.
    stale_held = bool(result["stale_held_positions"])
    result["fresh"] = bool(sentinel_fresh and bars_fresh and not stale_held)
    return result


def saved_price_file_has_items(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("items"))
