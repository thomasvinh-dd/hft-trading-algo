"""Signal generation: breakout trigger + position sizing."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .base_detector import Base
from .config import StrategyConfig
from .data_loader import PriceBar, atr as _atr


@dataclass(frozen=True)
class Signal:
    ticker: str
    entry_date: object
    entry_price: float
    base: Base
    sl_F: float
    sl_T: float
    tiers: tuple
    conviction: str
    size_pct: float
    shares: int
    max_volume: int
    bars_remaining: int
    extra: dict = field(default_factory=dict)
    atr_value: float | None = None

    @property
    def sl_midpoint(self):
        return (self.sl_F + self.sl_T) / 2.0

    @property
    def risk_per_share(self):
        return self.entry_price - self.sl_F

    @property
    def notional(self):
        return self.shares * self.entry_price


def _avg_volume(bars, idx, window=20):
    lo = max(0, idx - window + 1)
    relevant = bars[lo:idx + 1]
    if not relevant:
        return 0.0
    return sum(b.volume for b in relevant) / len(relevant)


def _ma(bars, idx, window):
    """Simple moving average of close up to and including idx."""
    if idx + 1 < window:
        return None
    closes = [b.close for b in bars[idx - window + 1: idx + 1]]
    return sum(closes) / window


def _conviction(volume, avg_vol, cfg):
    if avg_vol <= 0:
        return "low"
    if volume >= cfg.high_conviction_mult * avg_vol:
        return "high"
    return "low"


def _regime_ok(breakout_bar, cfg, market_bars):
    """Check market regime: is the index above its MA at signal time?

    market_bars: list of PriceBar for the index, sorted by date.
    Returns True if regime filter is disabled or regime allows trading.
    """
    if not cfg.breakout.regime_index_path or not market_bars:
        return True
    window = cfg.breakout.regime_ma_window
    target_date = breakout_bar.date
    # find bars up to and including target_date
    relevant = [b for b in market_bars if b.date <= target_date]
    if len(relevant) < window:
        return True  # not enough data, allow trading
    closes = [b.close for b in relevant[-window:]]
    ma = sum(closes) / window
    above = relevant[-1].close > ma
    return above if cfg.breakout.regime_require_above_ma else not above


def generate_signal(bars, base, nav, cfg, *, as_of_idx=None, upcoming_ca=None,
                    market_bars=None):
    breakout_idx = base.end_idx + 1
    if as_of_idx is None:
        as_of_idx = breakout_idx
    if breakout_idx >= len(bars):
        return None
    if breakout_idx != as_of_idx:
        breakout_idx = as_of_idx
    breakout_bar = bars[breakout_idx]
    if breakout_bar.close <= base.base_high:
        return None

    avg_vol = _avg_volume(bars, breakout_idx - 1)
    if avg_vol <= 0:
        return None
    if breakout_bar.volume < cfg.breakout.volume_mult * avg_vol:
        return None

    if cfg.breakout.require_ma200:
        closes = [b.close for b in bars[: breakout_idx + 1]]
        ma200 = sum(closes[-200:]) / min(len(closes), 200)
        if breakout_bar.close <= ma200:
            return None

    # Trend confirmation: require close > MA(N) at signal time
    if cfg.breakout.require_close_above_ma > 0:
        ma_n = _ma(bars, breakout_idx, cfg.breakout.require_close_above_ma)
        if ma_n is None or breakout_bar.close <= ma_n:
            return None

    # Market regime filter
    if not _regime_ok(breakout_bar, cfg, market_bars):
        return None

    if cfg.breakout.skip_days_before_ca > 0 and upcoming_ca:
        for ev in upcoming_ca:
            days_to_ex = (ev.ex_date - breakout_bar.date).days
            if 0 <= days_to_ex <= cfg.breakout.skip_days_before_ca:
                return None

    conviction = _conviction(breakout_bar.volume, avg_vol, cfg.breakout)
    size_pct = cfg.backtest.size_high if conviction == "high" else cfg.backtest.size_low
    position_value = nav * size_pct
    shares = int(position_value // breakout_bar.close)
    if shares <= 0:
        return None

    max_volume = int(avg_vol * 0.05)
    if shares > max_volume > 0:
        shares = max_volume

    sl_buffer = 1.0 - cfg.exits.sl_buffer_pct
    sl_F = base.base_low * sl_buffer
    sl_T = (base.base_low + breakout_bar.close) / 2.0

    # ATR at signal time (for ATR-based trailing stop)
    atr_value = _atr(bars[: breakout_idx + 1], window=cfg.trailing.trail_atr_window)

    return Signal(
        ticker=base.ticker,
        entry_date=breakout_bar.date,
        entry_price=breakout_bar.close,
        base=base,
        sl_F=round(sl_F, 4),
        sl_T=round(sl_T, 4),
        tiers=cfg.exits.tiers,
        conviction=conviction,
        size_pct=size_pct,
        shares=shares,
        max_volume=max_volume,
        bars_remaining=len(bars) - breakout_idx - 1,
        atr_value=atr_value,
    )


__all__ = ["Signal", "generate_signal"]
