"""Signal generation: breakout trigger.

Two APIs:
  - ``generate_signal(bars, base, cfg, ...)`` — returns a PURE Signal
    (no NAV, no sizing). Use this in new code with the Portfolio.
  - ``generate_signal(bars, base, nav, cfg, ...)`` — DEPRECATED wrapper
    that returns the legacy Signal with shares/size_pct baked in.
    Used by the existing backtest/executor path.

The new clean Signal lives in darvas.signals; the legacy one (with
shares) is defined here for backward compat.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Sequence

from .base_detector import Base
from .config import StrategyConfig
from .data_loader import PriceBar, atr as _atr
from .filters import compute_weekly_ma, lookup_sector_momentum
from .signals import Signal as _PureSignal, SignalHistory as _SignalHistory


@dataclass(frozen=True)
class Signal(_PureSignal):
    """LEGACY Signal with sizing fields. Subclass of the pure Signal.

    The fields below (size_pct, shares, max_volume) were historically
    computed in the signal generator. They live here for backward
    compat with the existing backtest/executor path, but the new
    Portfolio architecture puts sizing in the Portfolio layer instead.
    """
    size_pct: float = 0.0
    shares: int = 0
    max_volume: int = 0

    @property
    def notional(self) -> float:
        return self.shares * self.entry_price


# Re-export SignalHistory under the same name
SignalHistory = _SignalHistory


__all__ = ["Signal", "SignalHistory", "generate_signal"]


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


def _market_volatility_pct(market_bars, target_date, window):
    """Realized daily volatility of the index at ``target_date``, in percent.

    Returns None when there is not enough history.
    """
    if not market_bars:
        return None
    relevant = [b for b in market_bars if b.date <= target_date]
    if len(relevant) < window + 1:
        return None
    rets = []
    for i in range(len(relevant) - window, len(relevant)):
        prev, cur = relevant[i - 1].close, relevant[i].close
        if prev > 0:
            rets.append(cur / prev - 1)
    if len(rets) < window - 1:
        return None
    return statistics.pstdev(rets) * 100


def _market_volatility_ok(breakout_bar, cfg, market_bars):
    """Require the market to be volatile enough (and not too volatile).

    Off unless ``min_market_volatility_pct`` (or the max) is set.
    """
    min_pct = getattr(cfg.breakout, "min_market_volatility_pct", 0.0) or 0.0
    max_pct = getattr(cfg.breakout, "max_market_volatility_pct", 0.0) or 0.0
    if min_pct <= 0 and max_pct <= 0:
        return True
    window = getattr(cfg.breakout, "market_volatility_window", 20) or 20
    vol = _market_volatility_pct(market_bars, breakout_bar.date, window)
    if vol is None:
        return True  # not enough data — allow trading
    if min_pct > 0 and vol < min_pct:
        return False
    if max_pct > 0 and vol > max_pct:
        return False
    return True


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


def _sector_ok(breakout_bar, ticker, cfg, sector_momentum_by_date):
    """Check sector momentum filter using as-of-date lookup."""
    if not cfg.breakout.sector_data_path:
        return True
    if not sector_momentum_by_date:
        return True
    mom = lookup_sector_momentum(sector_momentum_by_date, breakout_bar.date, ticker)
    if mom is None:
        return True  # no sector data, allow
    return mom >= cfg.breakout.sector_min_momentum


def _weekly_ma_ok(breakout_bar, bars, breakout_idx, cfg):
    """Check weekly MA filter."""
    if cfg.breakout.require_close_above_weekly_ma <= 0:
        return True
    weekly_ma = compute_weekly_ma(bars[: breakout_idx + 1],
                                   cfg.breakout.require_close_above_weekly_ma)
    if weekly_ma is None:
        return True
    return breakout_bar.close > weekly_ma


def _extract_features(bars, idx, base, signal):
    """Build a feature vector for the ML model."""
    if idx < 60:
        return None
    breakout_bar = bars[idx]
    closes = [b.close for b in bars[: idx + 1]]
    # 1. momentum_20 (20-day return)
    mom_20 = closes[-1] / closes[-21] - 1.0 if len(closes) >= 21 else 0.0
    # 2. momentum_60 (60-day return)
    mom_60 = closes[-1] / closes[-61] - 1.0 if len(closes) >= 61 else 0.0
    # 3. ATR_pct (volatility)
    atr_v = _atr(bars[: idx + 1], window=14) or 0.0
    atr_pct = atr_v / closes[-1] if closes[-1] > 0 else 0.0
    # 4. volume ratio (today's volume vs avg)
    avg_vol = _avg_volume(bars, idx - 1) or 1.0
    vol_ratio = breakout_bar.volume / avg_vol
    # 5. range_pct of base
    rng_pct = base.range_pct if base else 0.0
    # 6. distance to MA20
    if len(closes) >= 20:
        ma20 = sum(closes[-20:]) / 20
        dist_ma20 = closes[-1] / ma20 - 1.0
    else:
        dist_ma20 = 0.0
    # 7. days in base
    days_in_base = base.days_in_base if base else 0
    # 8. close > MA50
    above_ma50 = 0.0
    if len(closes) >= 50:
        ma50 = sum(closes[-50:]) / 50
        above_ma50 = 1.0 if closes[-1] > ma50 else 0.0
    # 9. close > MA200
    above_ma200 = 0.0
    if len(closes) >= 200:
        ma200 = sum(closes[-200:]) / 200
        above_ma200 = 1.0 if closes[-1] > ma200 else 0.0
    return [mom_20, mom_60, atr_pct, vol_ratio, rng_pct,
            dist_ma20, days_in_base, above_ma50, above_ma200]


def _vol_targeted_size_pct(atr_value, entry_price, cfg, conviction):
    """Volatility-targeted position size as a fraction of nav."""
    target_vol = cfg.backtest.vol_target_pct
    if target_vol <= 0 or atr_value is None or entry_price <= 0:
        # fall back to default sizing
        return cfg.backtest.size_high if conviction == "high" else cfg.backtest.size_low
    atr_pct = atr_value / entry_price
    if atr_pct <= 0:
        return cfg.backtest.size_low
    size = target_vol / atr_pct
    # apply ceiling and floor
    size = max(cfg.backtest.vol_target_floor, min(cfg.backtest.vol_target_ceiling, size))
    return size


def generate_signal(bars, base, *args, as_of_idx=None, upcoming_ca=None,
                    market_bars=None, volatility_bars=None,
                    sector_momentum_by_date=None,
                    ml_model=None, nav=None, cfg=None):
    """Generate a Signal.

    Two calling conventions are supported (for backward compat):
      - generate_signal(bars, base, nav, cfg, ...) — legacy: 4 positional
        args (or kw nav=..., cfg=...), nav required, sizing is computed
        in-line, returned Signal has size_pct/shares.
      - generate_signal(bars, base, cfg, ...) — new: 3 positional args
        (or kw cfg=...), cfg only, pure Signal with no sizing. The
        Portfolio sizes the position later.

    New code should use the no-nav form and let the Portfolio size.
    """
    # Backward compat: allow kw-only nav= and cfg= alongside positional
    if len(args) == 0 and (nav is not None or cfg is not None):
        args = (nav, cfg) if nav is not None else (cfg,)
    elif len(args) == 1 and cfg is not None:
        # Positional cfg + kw nav — invalid
        raise TypeError(
            "Cannot pass both positional cfg and keyword nav=..."
        )

    # Parse positional args to detect calling convention
    if len(args) == 1:
        # New API: (bars, base, cfg) — 3 positional args total
        cfg = args[0]
        nav = None
    elif len(args) == 2:
        # Legacy API: (bars, base, nav, cfg) — 4 positional args total
        nav, cfg = args
    else:
        raise TypeError(
            f"generate_signal() takes 3 or 4 positional arguments "
            f"but {2 + len(args)} were given. "
            f"Use generate_signal(bars, base, cfg) for pure Signal, "
            f"or generate_signal(bars, base, nav, cfg) for legacy."
        )

    # Run the pure signal generation
    sig = _generate_pure_signal(
        bars, base, cfg, as_of_idx=as_of_idx,
        upcoming_ca=upcoming_ca, market_bars=market_bars,
        volatility_bars=volatility_bars,
        sector_momentum_by_date=sector_momentum_by_date,
        ml_model=ml_model,
    )
    if sig is None:
        return None

    if nav is None:
        # New API: return pure signal
        return sig

    # Legacy API: add sizing
    return _with_legacy_sizing(sig, nav, cfg)


def _generate_pure_signal(bars, base, cfg, *, as_of_idx, upcoming_ca,
                           market_bars, volatility_bars,
                           sector_momentum_by_date, ml_model):
    """Pure signal: no sizing, just the opportunity with exit levels."""
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

    # Multi-timeframe: require close > weekly MA(N)
    if not _weekly_ma_ok(breakout_bar, bars, breakout_idx, cfg):
        return None

    # Market regime filter
    if not _regime_ok(breakout_bar, cfg, market_bars):
        return None

    # Market volatility filter. Off by default; see BreakoutConfig for
    # why volatility is the one market feature that helps.
    if not _market_volatility_ok(breakout_bar, cfg, volatility_bars):
        return None

    # Sector rotation filter
    if not _sector_ok(breakout_bar, base.ticker, cfg, sector_momentum_by_date):
        return None

    # ML filter
    if ml_model is not None:
        features = _extract_features(bars, breakout_idx, base, breakout_bar)
        if features is not None:
            try:
                prob = ml_model.predict_proba([features])[0][1]
                if prob < cfg.breakout.ml_min_prob:
                    return None
            except Exception:
                pass

    if cfg.breakout.skip_days_before_ca > 0 and upcoming_ca:
        for ev in upcoming_ca:
            days_to_ex = (ev.ex_date - breakout_bar.date).days
            if 0 <= days_to_ex <= cfg.breakout.skip_days_before_ca:
                return None

    conviction = _conviction(breakout_bar.volume, avg_vol, cfg.breakout)

    # ATR at signal time (for ATR-based trailing stop and vol-targeting)
    atr_value = _atr(bars[: breakout_idx + 1], window=cfg.trailing.trail_atr_window)

    # Entry price and date.
    #
    # The signal fires on the breakout bar, but the position is entered on
    # the NEXT bar. The fill price is that bar's OPEN — the price we would
    # actually have paid when the order was placed at the previous close.
    #
    # The old code used the breakout bar's CLOSE as the entry price while
    # dating the entry to the next bar. That is wrong in two ways:
    #   - it ignores the overnight gap, so entries are recorded at a price
    #     that was no longer available (measured across 3,719 signals:
    #     next-bar close vs signal-bar close has p75 +1.85%, p90 +4.75%,
    #     max +40.93%);
    #   - the stop levels and tier targets are computed from that
    #     optimistic price, so the whole trade is measured against a fill
    #     that never happened.
    #
    # Using the next bar's open makes the sizing, the stops and the P&L
    # all consistent with a real order.
    if breakout_idx + 1 < len(bars):
        entry_bar = bars[breakout_idx + 1]
        entry_date = entry_bar.date
        entry_price = entry_bar.open
    else:
        # No bar after the signal — the position cannot be opened. The
        # caller skips signals with no room left to trade.
        entry_date = breakout_bar.date
        entry_price = breakout_bar.close

    sl_buffer = 1.0 - cfg.exits.sl_buffer_pct
    if cfg.exits.use_entry_anchored_stops:
        # Anchored to the fill price, not the signal-bar close.
        sl_F = entry_price * (1.0 - cfg.exits.sl_F_pct)
        sl_T = entry_price * (1.0 - cfg.exits.sl_T_pct)
    else:
        sl_F = base.base_low * sl_buffer
        sl_T = (base.base_low + entry_price) / 2.0

    return Signal(
        ticker=base.ticker,
        signal_date=breakout_bar.date,
        entry_date=entry_date,
        entry_price=entry_price,
        base=base,
        sl_F=round(sl_F, 4),
        sl_T=round(sl_T, 4),
        tiers=cfg.exits.tiers,
        conviction=conviction,
        atr_value=atr_value,
        bars_remaining=len(bars) - breakout_idx - 1,
        extra={"avg_vol": avg_vol},
    )


def _with_legacy_sizing(sig, nav, cfg):
    """Decorate a pure Signal with legacy sizing fields (size_pct, shares, max_volume).

    Used only by the legacy generate_signal(bars, base, nav, cfg, ...) call
    sites. New code should use Portfolio.size_position() instead.

    Returns None if the position is too small for one board lot —
    matches the legacy behavior where generate_signal returned None
    for un-tradable signals.
    """
    from .portfolio import size_position
    shares, size_pct = size_position(sig, nav, cfg)
    if shares <= 0:
        return None
    avg_vol = sig.extra.get("avg_vol", 0) if sig.extra else 0
    max_volume = int(avg_vol * 0.05) if avg_vol else 0
    return Signal(
        ticker=sig.ticker, signal_date=sig.signal_date,
        entry_date=sig.entry_date, entry_price=sig.entry_price,
        base=sig.base, sl_F=sig.sl_F, sl_T=sig.sl_T,
        tiers=sig.tiers, conviction=sig.conviction,
        atr_value=sig.atr_value, bars_remaining=sig.bars_remaining,
        extra=sig.extra,
        size_pct=size_pct, shares=shares, max_volume=max_volume,
    )


__all__ = ["Signal", "SignalHistory", "generate_signal"]
