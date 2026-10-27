"""Tight-consolidation (Darvas Box) base detection."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .config import BaseConfig
from .data_loader import PriceBar


@dataclass(frozen=True)
class Base:
    ticker: str
    base_low: float
    base_high: float
    end_idx: int
    days_in_base: int
    volume_compression: float

    @property
    def range_pct(self) -> float:
        if self.base_low <= 0:
            return 0.0
        return (self.base_high - self.base_low) / self.base_low

    @property
    def midpoint(self) -> float:
        return (self.base_low + self.base_high) / 2.0


def _volume_compression(bars, start, end):
    if start <= 0 or end - start + 1 < 1:
        return 1.0
    base_vol = sum(bars[i].volume for i in range(start, end + 1)) / (end - start + 1)
    prior_end = start - 1
    if prior_end < 20:
        return 1.0
    prior_vol = sum(bars[i].volume for i in range(prior_end - 19, prior_end + 1)) / 20
    return base_vol / prior_vol if prior_vol > 0 else 1.0


def _rolling_extremes(values, window):
    n = len(values)
    out_min = [math.inf] * n
    out_max = [-math.inf] * n
    min_dq = []
    max_dq = []
    for i in range(n):
        if min_dq and min_dq[0] <= i - window:
            min_dq.pop(0)
        if max_dq and max_dq[0] <= i - window:
            max_dq.pop(0)
        v = values[i]
        while min_dq and values[min_dq[-1]] >= v:
            min_dq.pop()
        min_dq.append(i)
        while max_dq and values[max_dq[-1]] <= v:
            max_dq.pop()
        max_dq.append(i)
        out_min[i] = values[min_dq[0]]
        out_max[i] = values[max_dq[0]]
    return out_min, out_max


class BaseScanner:
    """Precomputes rolling min/max for every allowed window length."""

    def __init__(self, bars, cfg, ticker=""):
        self.bars = bars
        self.cfg = cfg
        self.ticker = ticker
        lows = [b.low for b in bars]
        highs = [b.high for b in bars]
        vols = [b.volume for b in bars]
        self._min_lo = {}
        self._max_hi = {}
        self._sum_vol = {}
        for length in range(cfg.min_days, cfg.max_days + 1):
            mn, _ = _rolling_extremes(lows, length)
            self._min_lo[length] = mn
            _, mx = _rolling_extremes(highs, length)
            self._max_hi[length] = mx
            sums = [0.0] * len(bars)
            running = 0.0
            for i in range(len(bars)):
                running += vols[i]
                if i >= length:
                    running -= vols[i - length]
                sums[i] = running
            self._sum_vol[length] = sums

    def _volume_compression(self, length, end_idx):
        start = end_idx - length + 1
        if start <= 0:
            return 1.0
        base_vol = self._sum_vol[length][end_idx] / length
        prior_end = start - 1
        if prior_end < 20:
            return 1.0
        prior_vol = sum(self.bars[i].volume for i in range(prior_end - 19, prior_end + 1)) / 20
        return base_vol / prior_vol if prior_vol > 0 else 1.0

    def find_base(self, as_of_idx):
        for end_idx in range(as_of_idx, -1, -1):
            best = None
            for length in range(self.cfg.min_days, self.cfg.max_days + 1):
                start_idx = end_idx - length + 1
                if start_idx < 0:
                    break
                lo = self._min_lo[length][end_idx]
                hi = self._max_hi[length][end_idx]
                if lo <= 0:
                    continue
                rng = (hi - lo) / lo
                if rng > self.cfg.max_range_pct:
                    continue
                comp = self._volume_compression(length, end_idx)
                candidate = Base(
                    ticker=self.ticker,
                    base_low=lo,
                    base_high=hi,
                    end_idx=end_idx,
                    days_in_base=length,
                    volume_compression=comp,
                )
                if best is None or candidate.range_pct < best.range_pct:
                    best = candidate
            if best is not None:
                return best
        return None


def detect_base(bars, cfg, *, as_of_idx=None, ticker=""):
    if not bars:
        return None
    if as_of_idx is None:
        as_of_idx = len(bars) - 1
    scanner = BaseScanner(bars, cfg, ticker=ticker)
    return scanner.find_base(as_of_idx)


__all__ = ["Base", "BaseScanner", "detect_base"]

