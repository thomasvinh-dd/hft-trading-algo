"""Helper utilities: sector rotation, multi-timeframe MA, ML filter."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from .data_loader import PriceBar


def build_sector_map(symbols_path: str) -> Dict[str, str]:
    """Build ticker → sector mapping from a symbols.json file."""
    if not symbols_path:
        return {}
    p = Path(symbols_path)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
        items = d.get("items", [])
        out = {}
        for it in items:
            ticker = (it.get("ticker") or it.get("symbol") or "").upper()
            sector = it.get("sectorName")
            if ticker and sector:
                out[ticker] = sector
        return out
    except (json.JSONDecodeError, OSError):
        return {}


def compute_sector_momentum_as_of(bars_by_ticker: Dict[str, list],
                                   sector_map: Dict[str, str],
                                   as_of_date,
                                   window: int = 60) -> Dict[str, float]:
    """Compute sector momentum as of a specific date.

    Returns dict: ticker → sector momentum (return over `window` days ending at as_of_date).
    Each ticker gets the average return of all tickers in its sector.
    """
    by_sector: Dict[str, list] = {}
    for ticker, bars in bars_by_ticker.items():
        sector = sector_map.get(ticker.upper())
        if not sector:
            continue
        # Get bars up to and including as_of_date
        relevant = [b for b in bars if b.date <= as_of_date]
        if len(relevant) < window + 1:
            continue
        ret = relevant[-1].close / relevant[-window - 1].close - 1.0
        by_sector.setdefault(sector, []).append(ret)

    sector_mom = {s: sum(v) / len(v) for s, v in by_sector.items() if v}

    out: Dict[str, float] = {}
    for ticker in bars_by_ticker:
        sector = sector_map.get(ticker.upper())
        if sector in sector_mom:
            out[ticker.upper()] = sector_mom[sector]
    return out


def build_sector_momentum_by_date(bars_by_ticker: Dict[str, list],
                                    sector_map: Dict[str, str],
                                    window: int = 60,
                                    step: int = 5) -> Dict:
    """Build a date-indexed sector momentum lookup.

    Returns dict: as_of_date → {ticker: sector_momentum}.
    Computed every `step` days (default 5) to save time.
    """
    # Find all unique dates
    all_dates = set()
    for bars in bars_by_ticker.values():
        for b in bars:
            all_dates.add(b.date)
    all_dates = sorted(all_dates)

    out = {}
    # Only compute every `step` days
    for i, d in enumerate(all_dates):
        if i % step != 0:
            continue
        out[d] = compute_sector_momentum_as_of(bars_by_ticker, sector_map, d, window)
    return out


def lookup_sector_momentum(momentum_by_date: Dict, as_of_date, ticker: str):
    """Look up sector momentum as of (or just before) as_of_date for given ticker."""
    if not momentum_by_date:
        return None
    # Find latest date <= as_of_date
    dates = sorted(d for d in momentum_by_date if d <= as_of_date)
    if not dates:
        return None
    latest = dates[-1]
    return momentum_by_date[latest].get(ticker.upper())


# Backward-compatible alias: computes momentum using last bar in each ticker
def compute_sector_momentum(bars_by_ticker: Dict[str, list],
                             sector_map: Dict[str, str],
                             window: int = 60) -> Dict[str, float]:
    """Compute sector momentum using the most recent bar of each ticker."""
    # Find the latest common date
    latest_date = None
    for bars in bars_by_ticker.values():
        if bars and (latest_date is None or bars[-1].date > latest_date):
            latest_date = bars[-1].date
    if latest_date is None:
        return {}
    return compute_sector_momentum_as_of(bars_by_ticker, sector_map, latest_date, window)


def compute_weekly_ma(bars: list, weekly_window: int = 20) -> float | None:
    """Approximate weekly MA from daily bars: average close over last N*5 daily bars."""
    daily_window = weekly_window * 5
    if len(bars) < daily_window:
        return None
    closes = [b.close for b in bars[-daily_window:]]
    return sum(closes) / daily_window


__all__ = ["build_sector_map", "compute_sector_momentum", "compute_sector_momentum_as_of",
           "build_sector_momentum_by_date", "lookup_sector_momentum", "compute_weekly_ma"]
