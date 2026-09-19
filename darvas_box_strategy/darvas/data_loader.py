"""Data access layer for the Darvas Box strategy."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator

from .config import StrategyConfig

DEFAULT_PRICES_DIR = Path("vietnam_algo/data/prices")


@dataclass(frozen=True)
class PriceBar:
    """A single OHLCV row."""

    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def date(self) -> date:
        return date.fromisoformat(self.time)


@dataclass(frozen=True)
class TickerMeta:
    ticker: str
    n_bars: int
    last_close: float
    avg_value_20d: float
    above_ma200: bool


def load_price_bars(path: Path) -> list[PriceBar]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    out: list[PriceBar] = []
    for row in items or []:
        if not isinstance(row, dict):
            continue
        try:
            out.append(
                PriceBar(
                    time=str(row["time"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0.0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda b: b.time)
    return out


def sma(values, window):
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:]) / window


def atr(bars, window=14):
    if len(bars) < window + 1:
        return None
    trs = []
    for i in range(-window, 0):
        prev_close = bars[i - 1].close
        b = bars[i]
        trs.append(max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close)))
    return sum(trs) / window


def avg_value(bars, window=20):
    if not bars:
        return 0.0
    recent = bars[-window:]
    return sum(b.close * b.volume for b in recent) / len(recent)


def derive_meta(ticker, bars):
    last_close = bars[-1].close if bars else 0.0
    ma200 = sma([b.close for b in bars], 200)
    return TickerMeta(
        ticker=ticker,
        n_bars=len(bars),
        last_close=last_close,
        avg_value_20d=avg_value(bars, 20),
        above_ma200=(ma200 is not None and last_close > ma200),
    )


def passes_universe(meta, cfg):
    u = cfg.universe
    if meta.n_bars < cfg.breakout.min_history_days:
        return False
    if meta.last_close < u.min_price or meta.last_close > u.max_price:
        return False
    if meta.avg_value_20d < u.min_avg_value_20d:
        return False
    return True


def iter_universe(prices_dir=DEFAULT_PRICES_DIR, cfg=None):
    cfg = cfg or StrategyConfig.default()
    allow = (
        {t.upper() for t in cfg.universe.ticker_allowlist}
        if cfg.universe.ticker_allowlist
        else None
    )
    for path in sorted(prices_dir.glob("*.json")):
        ticker = path.stem.upper()
        if allow is not None and ticker not in allow:
            continue
        bars = load_price_bars(path)
        if not bars:
            continue
        meta = derive_meta(ticker, bars)
        if not passes_universe(meta, cfg):
            continue
        yield meta, bars


__all__ = [
    "PriceBar",
    "TickerMeta",
    "DEFAULT_PRICES_DIR",
    "load_price_bars",
    "sma",
    "atr",
    "avg_value",
    "derive_meta",
    "passes_universe",
    "iter_universe",
    "list_universe_tickers",
]


def list_universe_tickers(prices_dir=DEFAULT_PRICES_DIR, cfg=None):
    return [meta.ticker for meta, _ in iter_universe(prices_dir, cfg)]
