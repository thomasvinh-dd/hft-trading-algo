"""CA handler — kept for opt-in use with raw prices."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable

from .data_loader import PriceBar

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CA_PATH = _PROJECT_ROOT / "vietnam_algo" / "data" / "corporate_action.json"

_CASH_RE = re.compile(r"([\d,]+)\s*đồng\s*/\s*CP", re.IGNORECASE)
_STOCK_RE = re.compile(r"tỷ\s*lệ\s*(\d+)\s*:\s*(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class CAEvent:
    ticker: str
    ex_date: date
    cash_per_share: float = 0.0
    share_multiplier: float = 1.0
    name: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_cash(self):
        return self.cash_per_share > 0

    @property
    def is_stock_dividend(self):
        return self.share_multiplier != 1.0


def parse_event(row):
    ticker = str(row.get("symbol", "")).strip().upper()
    ex_str = row.get("exDate")
    if not ticker or not ex_str:
        return None
    try:
        ex = date.fromisoformat(ex_str)
    except (TypeError, ValueError):
        return None
    note = str(row.get("note", "") or "")
    name = str(row.get("name", "") or "")
    text = f"{note} {name}".lower()

    m_cash = _CASH_RE.search(text)
    if m_cash and ("tiền" in text or "mặt" in text) and "cổ phiếu" not in text:
        amount = float(m_cash.group(1).replace(",", ""))
        return CAEvent(ticker=ticker, ex_date=ex, cash_per_share=amount,
                       share_multiplier=1.0, name=name, raw=row)

    m_stock = _STOCK_RE.search(text)
    if m_stock and "cổ phiếu" in text:
        n = int(m_stock.group(1))
        m = int(m_stock.group(2))
        return CAEvent(ticker=ticker, ex_date=ex, cash_per_share=0.0,
                       share_multiplier=(n + m) / n, name=name, raw=row)

    return None


def load_events(path=DEFAULT_CA_PATH):
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    out = []
    for row in items or []:
        ev = parse_event(row)
        if ev is not None:
            out.append(ev)
    return out


def build_event_index(events):
    idx = {}
    for ev in events:
        idx.setdefault(ev.ticker, []).append(ev)
    for ev_list in idx.values():
        ev_list.sort(key=lambda e: e.ex_date)
    return idx


def _backward_factor(event, post_ex_close):
    if event.is_stock_dividend:
        return event.share_multiplier
    if event.is_cash:
        pre_ex_close = post_ex_close + event.cash_per_share
        if pre_ex_close <= 0:
            return 1.0
        return pre_ex_close / post_ex_close
    return 1.0


def adjust_bars(bars, events):
    if not bars or not events:
        return list(bars), {}

    ev_sorted = sorted(events, key=lambda e: e.ex_date)

    ev_by_date = {}
    for ev in ev_sorted:
        if ev.ex_date not in ev_by_date or ev.is_cash:
            ev_by_date[ev.ex_date] = ev

    dividend_per_share = {
        ev.ex_date: ev.cash_per_share
        for ev in ev_by_date.values()
        if ev.is_cash
    }

    post_close_by_event = {}
    for ev_date in ev_by_date:
        for bar in bars:
            if bar.date >= ev_date:
                post_close_by_event[ev_date] = bar.close
                break

    cumulative = 1.0
    adjusted_back = [None] * len(bars)
    for i in range(len(bars) - 1, -1, -1):
        bar = bars[i]
        if cumulative == 1.0:
            adjusted_back[i] = bar
        else:
            adjusted_back[i] = PriceBar(
                time=bar.time,
                open=bar.open * cumulative,
                high=bar.high * cumulative,
                low=bar.low * cumulative,
                close=bar.close * cumulative,
                volume=bar.volume,
            )
        if bar.date in ev_by_date:
            ev = ev_by_date[bar.date]
            post_close = post_close_by_event.get(bar.date, bar.close)
            factor = _backward_factor(ev, post_close)
            cumulative *= factor

    return adjusted_back, dividend_per_share


__all__ = [
    "CAEvent",
    "parse_event",
    "load_events",
    "build_event_index",
    "adjust_bars",
    "DEFAULT_CA_PATH",
]
