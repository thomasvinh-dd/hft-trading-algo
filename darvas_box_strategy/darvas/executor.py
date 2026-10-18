"""Trade executor: walk forward from a Signal and simulate fills."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .config import StrategyConfig
from .data_loader import PriceBar
from .signal_generator import Signal


@dataclass(frozen=True)
class FillEvent:
    date: object
    kind: str
    level: float
    shares: int
    proceeds: float
    fraction_of_pos: float
    cumulative_proceeds: float
    realized_pnl: float = 0.0
    remaining: int = 0  # shares still held after this fill (was 0 by default; see _fills_to_dicts)


@dataclass(frozen=True)
class TradeResult:
    signal: Signal
    fills: tuple
    exit_date: object
    exit_reason: str
    entry_value: float
    exit_value: float
    realized_pnl: float
    return_pct: float
    cost_paid: float
    bars_held: int
    highest_close: float
    last_close: float = 0.0  # latest bar.close during the trade (NOT peak)
    dividend_income: float = 0.0

    @property
    def is_winner(self):
        return self.realized_pnl > 0


def _cap_to_daily_limit(bar, prev_close: float, limit_pct: float) -> float:
    """Cap bar.close to the daily price limit relative to prev_close.

    VN HOSE: ±7% per day. So today's close must be in [prev*(1-L), prev*(1+L)].
    Used by simulate_trade to model the regulatory price limit.
    """
    if prev_close <= 0 or limit_pct <= 0:
        return bar.close
    upper = prev_close * (1.0 + limit_pct)
    lower = prev_close * (1.0 - limit_pct)
    if bar.close > upper:
        return upper
    if bar.close < lower:
        return lower
    return bar.close


def simulate_trade(signal, bars_after, cfg, *, dividend_per_share=None, atr_value=None):
    exits_cfg = cfg.exits
    trail_cfg = cfg.trailing
    cost_rate = cfg.backtest.cost_bps / 10_000.0
    lot = cfg.backtest.board_lot if cfg.backtest.board_lot > 0 else 1
    daily_limit = cfg.backtest.daily_price_limit_pct
    # T+2.5 settlement: skip the first N-1 bars for exit logic.
    # 0 = legacy (immediate exit allowed)
    t_plus_n = max(0, exits_cfg.t_plus_n_bars - 1) if exits_cfg.t_plus_n_bars > 0 else 0

    remaining = signal.shares
    entry_value = signal.shares * signal.entry_price
    cumulative_proceeds = 0.0
    cumulative_pnl = 0.0
    dividend_income = 0.0
    fills = []
    tier_idx = 0
    trailing_active = False
    trail_floor = 0.0
    peak_close = signal.entry_price
    highest_close = signal.entry_price
    last_close = signal.entry_price
    last_bar = None
    prev_close = signal.entry_price  # for daily price limit anchoring

    # ATR-based trail: precompute the trail distance from the ATR.
    # The trail follows `peak - atr_value * trail_atr_mult`. If ATR
    # is None (e.g. not provided), fall back to percentage mode.
    if trail_cfg.trail_mode == "atr" and atr_value is not None:
        atr_trail_distance = atr_value * trail_cfg.trail_atr_mult
    else:
        atr_trail_distance = None

    sl_F = signal.sl_F
    sl_T = signal.sl_T
    sl_mid = (sl_F + sl_T) / 2.0

    bars_held = 0
    for bar in bars_after:
        last_bar = bar
        # Apply the daily price limit relative to the previous close.
        # This caps unrealistic gap moves (e.g. -15% in a day when the
        # regulatory limit is ±7%).
        effective_close = _cap_to_daily_limit(bar, prev_close, daily_limit)
        highest_close = max(highest_close, effective_close)
        peak_close = max(peak_close, effective_close)
        last_close = effective_close
        bars_held += 1

        # T+2.5 settlement: don't allow exits during the lock-up period.
        # Dividends, however, still apply to the position we hold.
        in_settlement = bars_held <= t_plus_n

        if dividend_per_share and bar.date in dividend_per_share and remaining > 0:
            cash_div = dividend_per_share[bar.date]
            income = remaining * cash_div
            cumulative_pnl += income
            # DIVIDEND doesn't change share count — shares=0 in the
            # signed convention (entry=+, sell=-, dividend=0).
            # The "remaining" field shows the position size.
            cumulative_proceeds += income
            dividend_income += income
            # DIVIDEND is informational (no share change). shares=0 in
            # the signed convention.
            fills.append(FillEvent(
                date=bar.date, kind="DIVIDEND", level=cash_div, shares=0,
                proceeds=income, fraction_of_pos=0.0,
                cumulative_proceeds=cumulative_proceeds, realized_pnl=income,
                remaining=remaining,  # dividend doesn't change share count
            ))

        if in_settlement:
            prev_close = effective_close
            continue

        # Time stop: if held too long and not in profit, close at close.
        max_hold = getattr(cfg.backtest, "max_holding_bars", 0) or 0
        if max_hold > 0 and remaining > 0 and bars_held >= max_hold and effective_close < signal.entry_price:
            proceeds = remaining * effective_close
            pnl = (effective_close - signal.entry_price) * remaining
            cost = proceeds * cost_rate * 0.5
            cumulative_proceeds += proceeds - cost
            cumulative_pnl += pnl - cost
            fills.append(FillEvent(
                date=bar.date, kind="TIME_STOP", level=effective_close, shares=-remaining,
                proceeds=proceeds - cost, fraction_of_pos=1.0,
                cumulative_proceeds=cumulative_proceeds, realized_pnl=pnl - cost,
                remaining=0,  # closing everything
            ))
            remaining = 0
            break

        if trailing_active and remaining > 0:
            if atr_trail_distance is not None:
                # ATR-based trail: peak minus N*ATR
                trail_threshold = peak_close - atr_trail_distance
            else:
                trail_threshold = peak_close * (1.0 - trail_cfg.trail_from_peak_pct)
            # Optionally floor at first-tier exit price
            if trail_cfg.trail_floor_lock_profit:
                trail_threshold = max(trail_threshold, trail_floor)
            if effective_close <= trail_threshold:
                proceeds = remaining * effective_close
                pnl = (effective_close - signal.entry_price) * remaining
                cost = proceeds * cost_rate * 0.5
                cumulative_proceeds += proceeds - cost
                cumulative_pnl += pnl - cost
                fills.append(FillEvent(
                    date=bar.date, kind="TRAIL", level=effective_close, shares=-remaining,
                    proceeds=proceeds - cost, fraction_of_pos=1.0,
                    cumulative_proceeds=cumulative_proceeds, realized_pnl=pnl - cost,
                    remaining=0,  # closing everything
                ))
                remaining = 0
                break

        while tier_idx < len(signal.tiers) and remaining > 0:
            mult, pct = signal.tiers[tier_idx]
            tier_price = signal.entry_price * mult
            if bar.high < tier_price:
                break
            # Round the tier cut DOWN to the nearest board lot
            # (HOSE rule: shares traded in 100-share multiples).
            tier_shares = (int(remaining * pct) // lot) * lot
            # If rounding leaves < 1 lot remaining, sell all
            # (don't strand fractional lots).
            if remaining - tier_shares < lot:
                tier_shares = remaining
            if tier_shares <= 0:
                tier_idx += 1
                continue
            proceeds = tier_shares * tier_price
            pnl = (tier_price - signal.entry_price) * tier_shares
            cost = tier_shares * tier_price * cost_rate * 0.5
            cumulative_proceeds += proceeds - cost
            cumulative_pnl += pnl - cost
            remaining -= tier_shares
            fills.append(FillEvent(
                date=bar.date, kind="TIER", level=tier_price, shares=-tier_shares,
                proceeds=proceeds - cost, fraction_of_pos=pct,
                cumulative_proceeds=cumulative_proceeds, realized_pnl=pnl - cost,
                remaining=remaining,
            ))
            if tier_idx == trail_cfg.activation_tier_idx:
                trailing_active = True
                if trail_cfg.trail_floor_lock_profit:
                    trail_floor = max(trail_floor, tier_price)
            tier_idx += 1

        if remaining <= 0:
            break

        if bar.low <= sl_T:
            # Round DOWN to the nearest board lot (HOSE rule).
            cut = (int(remaining * exits_cfg.scale_out_pct_at_sl_T) // lot) * lot
            # If rounding leaves < 1 lot, sell all remaining
            # (otherwise the strategy would be stuck with <100 shares).
            if cut <= 0 or remaining - cut < lot:
                cut = remaining
            proceeds = cut * sl_T
            pnl = (sl_T - signal.entry_price) * cut
            cost = cut * sl_T * cost_rate * 0.5
            cumulative_proceeds += proceeds - cost
            cumulative_pnl += pnl - cost
            remaining -= cut
            fills.append(FillEvent(
                date=bar.date, kind="SL_SCALE", level=sl_T, shares=-cut,
                proceeds=proceeds - cost, fraction_of_pos=exits_cfg.scale_out_pct_at_sl_T,
                cumulative_proceeds=cumulative_proceeds, realized_pnl=pnl - cost,
                remaining=remaining,
            ))
            if remaining <= 0:
                break

        if bar.low <= sl_mid and remaining > 0:
            # Same board-lot rounding for the mid-stop scale-out.
            cut = (int(remaining * exits_cfg.scale_out_pct_at_midpoint) // lot) * lot
            if cut <= 0 or remaining - cut < lot:
                cut = remaining
            proceeds = cut * sl_mid
            pnl = (sl_mid - signal.entry_price) * cut
            cost = cut * sl_mid * cost_rate * 0.5
            cumulative_proceeds += proceeds - cost
            cumulative_pnl += pnl - cost
            remaining -= cut
            fills.append(FillEvent(
                date=bar.date, kind="SL_SCALE", level=sl_mid, shares=-cut,
                proceeds=proceeds - cost, fraction_of_pos=exits_cfg.scale_out_pct_at_midpoint,
                cumulative_proceeds=cumulative_proceeds, realized_pnl=pnl - cost,
                remaining=remaining,
            ))
            if remaining <= 0:
                break

        if bar.low <= sl_F and remaining > 0:
            proceeds = remaining * sl_F
            pnl = (sl_F - signal.entry_price) * remaining
            cost = proceeds * cost_rate * 0.5
            cumulative_proceeds += proceeds - cost
            cumulative_pnl += pnl - cost
            fills.append(FillEvent(
                date=bar.date, kind="SL_HARD", level=sl_F, shares=-remaining,
                proceeds=proceeds - cost, fraction_of_pos=1.0,
                cumulative_proceeds=cumulative_proceeds, realized_pnl=pnl - cost,
                remaining=0,  # closing everything
            ))
            remaining = 0
            break

        prev_close = effective_close

    if remaining > 0 and last_bar is not None:
        # Use the last observed (limit-capped) close for the EXHAUST price
        effective_close = last_close
        proceeds = remaining * effective_close
        pnl = (effective_close - signal.entry_price) * remaining
        cost = proceeds * cost_rate * 0.5
        cumulative_proceeds += proceeds - cost
        cumulative_pnl += pnl - cost
        fills.append(FillEvent(
            date=last_bar.date, kind="EXHAUST", level=effective_close, shares=-remaining,
            proceeds=proceeds - cost, fraction_of_pos=1.0,
            cumulative_proceeds=cumulative_proceeds, realized_pnl=pnl - cost,
            remaining=0,  # closing everything
        ))
        remaining = 0
        exit_reason = "EXHAUST"
    else:
        non_div = [f for f in fills if f.kind != "DIVIDEND"]
        last_kind = non_div[-1].kind if non_div else "OPEN"
        exit_reason = last_kind

    return TradeResult(
        signal=signal,
        fills=tuple(fills),
        exit_date=fills[-1].date if fills else signal.entry_date,
        exit_reason=exit_reason,
        entry_value=entry_value,
        exit_value=cumulative_proceeds,
        realized_pnl=cumulative_pnl,
        return_pct=cumulative_pnl / entry_value if entry_value else 0.0,
        cost_paid=entry_value * cost_rate + sum(f.realized_pnl for f in fills) - cumulative_pnl,
        bars_held=bars_held,
        highest_close=highest_close,
        last_close=last_close,
        dividend_income=dividend_income,
    )


__all__ = ["FillEvent", "TradeResult", "simulate_trade"]
