"""Clean Signal type: no portfolio concerns.

A Signal is the pure output of the strategy's signal generator. It
describes an opportunity (entry, stops, take-profit levels) but does
NOT decide how much to allocate, how many shares, or what to do with
it. Those decisions live in the Portfolio layer.

Why this split matters:
- The same Signal can be paper-traded and live-traded by separate
  portfolios (different NAV, different risk rules).
- The same Portfolio can take signals from multiple strategies.
- The signal logic is testable in isolation — no NAV mocks needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional


@dataclass(frozen=True)
class Signal:
    """Pure signal: a candidate buy opportunity with exit plan.

    The portfolio decides WHETHER to act on this signal and HOW MUCH
    to allocate. The signal just says "here's an entry with these
    stops and take-profit levels."

    Fields:
        ticker: Stock symbol (e.g. "MSN")
        signal_date: The breakout bar's date — when the buy trigger fired.
        entry_date: The actual fill day (next bar after signal in backtest;
                    next trading day for live trading).
        entry_price: The breakout bar's close (assumed fill price).
        base: The Base that triggered the signal (for provenance).
        sl_F: Hard-stop level (entry-anchored).
        sl_T: Partial-cut stop level.
        tiers: Tuple of (mult, pct) pairs — TP ladder.
        conviction: "high" or "low" — based on breakout volume.
        atr_value: ATR at signal time, for the portfolio's optional use.
        bars_remaining: Days left in the bar history after entry.
    """
    ticker: str
    signal_date: object
    entry_date: object
    entry_price: float
    base: object  # darvas.base_detector.Base
    sl_F: float
    sl_T: float
    tiers: tuple
    conviction: str
    atr_value: float | None = None
    bars_remaining: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def sl_midpoint(self) -> float:
        return (self.sl_F + self.sl_T) / 2.0

    @property
    def risk_per_share(self) -> float:
        return self.entry_price - self.sl_F

    def is_high_conviction(self) -> bool:
        return self.conviction == "high"


@dataclass
class SignalHistory:
    """A running list of signals, with filtering helpers.

    Manages the signal stream across one or more tickers. The
    portfolio (or any consumer) can query it by ticker, by date
    range, or as a flat list.
    """
    signals: List[Signal] = field(default_factory=list)

    def add(self, signal: Signal) -> None:
        self.signals.append(signal)

    def extend(self, signals: Iterable[Signal]) -> None:
        self.signals.extend(signals)

    def all(self) -> List[Signal]:
        return list(self.signals)

    def by_ticker(self, ticker: str) -> List[Signal]:
        return [s for s in self.signals if s.ticker == ticker]

    def since(self, since_date) -> List[Signal]:
        return [s for s in self.signals if s.signal_date >= since_date]

    def until(self, until_date) -> List[Signal]:
        return [s for s in self.signals if s.signal_date <= until_date]

    def between(self, start_date, end_date) -> List[Signal]:
        return [s for s in self.signals
                if start_date <= s.signal_date <= end_date]

    def most_recent(self, n: int = 10) -> List[Signal]:
        """Return the N most recent signals (newest first)."""
        return sorted(
            self.signals,
            key=lambda s: s.signal_date,
            reverse=True,
        )[:n]

    def clear(self) -> None:
        self.signals.clear()

    def __len__(self) -> int:
        return len(self.signals)

    def __iter__(self):
        return iter(self.signals)


__all__ = ["Signal", "SignalHistory"]
