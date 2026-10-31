"""Exit-event evaluator for the Darvas Box Telegram bot.

Given a tracked :class:`LivePosition` and the latest price bars, decide
which exit event (if any) fires today. The rules mirror
``darvas/executor.py::simulate_trade`` so the Telegram notifications
match what the backtest/dashboard would show.

Check order (same as the executor): trail > tier > sl_T > sl_mid > sl_F.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .positions import LivePosition

log = logging.getLogger("darvas.bot.exits")


@dataclass
class ExitEvent:
    """One exit event to notify about."""

    kind: str                 # TIER | TRAIL | SL_SCALE | SL_HARD | TIME_STOP
    shares: int               # shares to sell (positive number)
    price: float              # fill price
    remaining_after: int      # shares left after this fill
    pnl_delta: float          # realized P&L from this fill (VND)
    cumulative_pnl: float     # running realized P&L (VND)
    tier_index: Optional[int] = None   # which tier (for TIER)
    note: str = ""

    @property
    def dedupe_key(self) -> str:
        """Stable key so the same event isn't notified twice."""
        return f"{self.kind}:{self.tier_index if self.tier_index is not None else ''}"


def _round_down_lot(value: float, lot: int) -> int:
    """Round a share count DOWN to the nearest board lot (HOSE = 100)."""
    if lot <= 1:
        return int(value)
    return (int(value) // lot) * lot


def evaluate_exits(
    pos: LivePosition,
    bars: Sequence,
    cfg,
    *,
    already_notified=None,
) -> List[ExitEvent]:
    """Evaluate exit events for a position against the latest bars.

    ``bars`` must be the price bars AFTER the entry date, oldest first.
    Only the LAST bar is evaluated for new events (earlier bars are
    assumed to have been handled by previous runs) — but the position's
    running state (peak, tier_idx, bars_held) is updated across all bars
    so the trail/tier state stays accurate.

    Returns the list of NEW exit events that should be notified.
    """
    exits_cfg = cfg.exits
    trail_cfg = cfg.trailing
    cost_rate = cfg.backtest.cost_bps / 10_000.0
    lot = cfg.backtest.board_lot if cfg.backtest.board_lot > 0 else 1
    # T+2.5 settlement: the first N-1 bars are locked for exits.
    t_plus_n = (
        max(0, exits_cfg.t_plus_n_bars - 1)
        if exits_cfg.t_plus_n_bars > 0 else 0
    )

    already = already_notified if already_notified is not None else set()

    events: List[ExitEvent] = []
    remaining = pos.remaining
    tier_idx = pos.tier_idx
    trailing_active = pos.trailing_active
    trail_floor = pos.trail_floor
    peak_close = pos.peak_close or pos.entry_price
    last_close = pos.last_close or pos.entry_price
    cumulative_pnl = pos.cumulative_pnl
    bars_held = pos.bars_held

    if not bars:
        return events

    # Update the running state (peak / last close) for every bar we see,
    # and advance the bar counter by the number of new bars.
    for b in bars:
        if b.close > peak_close:
            peak_close = b.close
        last_close = b.close
    bars_held += len(bars)

    # Only evaluate the newest bar for new events; earlier bars are
    # historical (already handled by previous runs).
    bar = bars[-1]

    # T+2.5 lock-up: no exits during the first N-1 bars after entry.
    # We evaluate the LAST bar, whose 1-based index is `bars_held`
    # (the cumulative count of bars seen since the entry).
    evaluated_bar_index = bars_held
    if evaluated_bar_index <= t_plus_n:
        _sync_state(pos, remaining, tier_idx, trailing_active, trail_floor,
                    peak_close, last_close, bars_held, cumulative_pnl)
        return events

    entry_price = pos.entry_price

    def _sell(kind: str, shares: int, price: float, *,
              tier_index: Optional[int] = None, note: str = "") -> ExitEvent:
        nonlocal remaining, cumulative_pnl
        shares = min(shares, remaining)
        if shares <= 0:
            return None  # type: ignore[return-value]
        proceeds = shares * price
        cost = proceeds * cost_rate * 0.5
        pnl = (price - entry_price) * shares
        cumulative_pnl += pnl - cost
        remaining -= shares
        return ExitEvent(
            kind=kind, shares=shares, price=price, remaining_after=remaining,
            pnl_delta=pnl - cost, cumulative_pnl=cumulative_pnl,
            tier_index=tier_index, note=note,
        )

    # --- 1) Trailing stop (only active after the first TP hit) ---
    if trailing_active and remaining > 0:
        if trail_cfg.trail_mode == "atr" and getattr(pos, "atr_value", None):
            threshold = peak_close - pos.atr_value * trail_cfg.trail_atr_mult
        else:
            threshold = peak_close * (1.0 - trail_cfg.trail_from_peak_pct)
        if trail_cfg.trail_floor_lock_profit:
            threshold = max(threshold, trail_floor)
        if bar.close <= threshold:
            ev = _sell("TRAIL", remaining, bar.close,
                       note=f"trail from peak {peak_close:,.0f}")
            if ev and ev.dedupe_key not in already:
                events.append(ev)
            _sync_state(pos, remaining, tier_idx, trailing_active, trail_floor,
                        peak_close, last_close, bars_held, cumulative_pnl)
            return events

    # --- 2) Take-profit ladder ---
    while tier_idx < len(pos.tiers) and remaining > 0:
        mult, pct = pos.tiers[tier_idx]
        tier_price = entry_price * mult
        if bar.high < tier_price:
            break
        # Board-lot rounding: cut DOWN to the nearest lot.
        tier_shares = _round_down_lot(remaining * pct, lot)
        # If the cut would strand < 1 lot, sell everything instead.
        if tier_shares <= 0 or remaining - tier_shares < lot:
            tier_shares = remaining
        if tier_shares <= 0:
            tier_idx += 1
            continue
        ev = _sell("TIER", tier_shares, tier_price, tier_index=tier_idx,
                   note=f"TP{tier_idx + 1} at {tier_price:,.0f} "
                        f"(+{(mult - 1) * 100:.1f}%)")
        if ev and ev.dedupe_key not in already:
            events.append(ev)
        if tier_idx == trail_cfg.activation_tier_idx:
            trailing_active = True
            if trail_cfg.trail_floor_lock_profit:
                trail_floor = max(trail_floor, tier_price)
        tier_idx += 1
        if remaining <= 0:
            break

    if remaining <= 0:
        _sync_state(pos, remaining, tier_idx, trailing_active, trail_floor,
                    peak_close, last_close, bars_held, cumulative_pnl)
        return events

    # --- 3) Partial-cut stop (sl_T) ---
    if bar.low <= pos.sl_T:
        cut = _round_down_lot(remaining * exits_cfg.scale_out_pct_at_sl_T, lot)
        if cut <= 0 or remaining - cut < lot:
            cut = remaining
        ev = _sell("SL_SCALE", cut, pos.sl_T,
                   note=f"partial stop at {pos.sl_T:,.0f} "
                        f"({(pos.sl_T / entry_price - 1) * 100:.1f}%)")
        if ev and ev.dedupe_key not in already:
            events.append(ev)
        if remaining <= 0:
            _sync_state(pos, remaining, tier_idx, trailing_active, trail_floor,
                        peak_close, last_close, bars_held, cumulative_pnl)
            return events

    # --- 4) Midpoint stop ---
    sl_mid = pos.sl_midpoint
    if sl_mid and bar.low <= sl_mid and remaining > 0:
        cut = _round_down_lot(
            remaining * exits_cfg.scale_out_pct_at_midpoint, lot)
        if cut <= 0 or remaining - cut < lot:
            cut = remaining
        ev = _sell("SL_SCALE", cut, sl_mid,
                   note=f"mid stop at {sl_mid:,.0f} "
                        f"({(sl_mid / entry_price - 1) * 100:.1f}%)")
        if ev and ev.dedupe_key not in already:
            events.append(ev)
        if remaining <= 0:
            _sync_state(pos, remaining, tier_idx, trailing_active, trail_floor,
                        peak_close, last_close, bars_held, cumulative_pnl)
            return events

    # --- 5) Hard stop ---
    if bar.low <= pos.sl_F and remaining > 0:
        ev = _sell("SL_HARD", remaining, pos.sl_F,
                   note=f"hard stop at {pos.sl_F:,.0f} "
                        f"({(pos.sl_F / entry_price - 1) * 100:.1f}%)")
        if ev and ev.dedupe_key not in already:
            events.append(ev)

    _sync_state(pos, remaining, tier_idx, trailing_active, trail_floor,
                peak_close, last_close, bars_held, cumulative_pnl)
    return events


def _sync_state(pos: LivePosition, remaining: int, tier_idx: int,
                trailing_active: bool, trail_floor: float, peak_close: float,
                last_close: float, bars_held: int, cumulative_pnl: float) -> None:
    """Write the running state back onto the position."""
    pos.remaining = remaining
    pos.tier_idx = tier_idx
    pos.trailing_active = trailing_active
    pos.trail_floor = trail_floor
    pos.peak_close = peak_close
    pos.last_close = last_close
    pos.bars_held = bars_held
    pos.cumulative_pnl = cumulative_pnl
    pos.realized_pnl = cumulative_pnl
