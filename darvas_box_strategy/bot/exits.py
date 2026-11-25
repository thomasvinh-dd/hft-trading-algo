"""Exit-event evaluator for the Darvas Box Telegram bot.

Given a tracked :class:`LivePosition` and price bars, decide which exit
events (if any) fire. The rules mirror
``darvas/executor.py::simulate_trade`` so the Telegram notifications
match what the backtest/dashboard would show.

Check order (same as the executor): trail > tier > sl_T > sl_mid > sl_F.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..darvas.executor import _scale_out_cut
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
    """Evaluate exit events for a position against the given bars.

    ``bars`` must be the price bars AFTER the entry date, oldest first.
    Every bar is evaluated in sequence (each bar can fire multiple
    events, exactly like the backtest executor). The position's running
    state (peak close, tier_idx, trailing_active, bars_held, P&L) is
    updated as we go, so the trail and tier ladders stay accurate.

    Returns the list of NEW exit events that should be notified —
    events whose dedupe key is not in ``already_notified``.
    """
    exits_cfg = cfg.exits
    trail_cfg = cfg.trailing
    cost_rate = cfg.backtest.cost_bps / 10_000.0
    lot = cfg.backtest.board_lot if cfg.backtest.board_lot > 0 else 1
    # Minimum residue a partial exit may leave behind (in board lots).
    # 0 = disable the residual-liquidation rule.
    min_residual_lots = getattr(exits_cfg, "min_residual_lots", 0) or 0
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
    entry_price = pos.entry_price

    def _sync() -> None:
        _sync_state(pos, remaining, tier_idx, trailing_active, trail_floor,
                    peak_close, last_close, bars_held, cumulative_pnl)

    def _sell(kind: str, shares: int, price: float, *,
              tier_index: Optional[int] = None, note: str = "") -> Optional[ExitEvent]:
        nonlocal remaining, cumulative_pnl
        shares = min(shares, remaining)
        if shares <= 0:
            return None
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

    for bar in bars:
        if remaining <= 0:
            break

        # Advance the bar counter and the running state.
        bars_held += 1
        if bar.close > peak_close:
            peak_close = bar.close
        last_close = bar.close

        # T+2.5 lock-up: no exits during the first N-1 bars after entry.
        if bars_held <= t_plus_n:
            continue

        # --- 1) Trailing stop (only active after the first TP hit) ---
        if trailing_active and remaining > 0:
            if trail_cfg.trail_mode == "atr" and getattr(pos, "atr_value", None):
                threshold = peak_close - pos.atr_value * trail_cfg.trail_atr_mult
            else:
                threshold = peak_close * (1.0 - trail_cfg.trail_from_peak_pct)
            if trail_cfg.trail_floor_lock_profit:
                threshold = max(threshold, trail_floor)
            if bar.close <= threshold:
                # The trail is a stop order: fill at the trail price, not at
                # whatever the close happened to be. Filling at the close let
                # the exit slip by up to -20pp against the intended stop.
                # The stop legs (sl_T / sl_F) already fill at their stop
                # price, so the trail does the same.
                #
                # If the bar's low is below the trail price the market gapped
                # through the stop, so the fill is the low — we cannot sell
                # above the market. Otherwise the stop was touched intraday
                # and the fill is the trail price itself.
                trail_fill = bar.low if bar.low < threshold else threshold
                ev = _sell("TRAIL", remaining, trail_fill,
                           note=f"trail from peak {peak_close:,.0f}")
                if ev and ev.dedupe_key not in already:
                    events.append(ev)
                break

        # --- 2) Take-profit ladder ---
        while tier_idx < len(pos.tiers) and remaining > 0:
            mult, pct = pos.tiers[tier_idx]
            tier_price = entry_price * mult
            if bar.high < tier_price:
                break
            # Board-lot rounding + residual-liquidation (shared helper).
            tier_shares = _scale_out_cut(remaining, pct, lot, min_residual_lots)
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
            break

        # --- 3) Partial-cut stop (sl_T) ---
        if bar.low <= pos.sl_T:
            cut = _scale_out_cut(
                remaining, exits_cfg.scale_out_pct_at_sl_T, lot, min_residual_lots,
            )
            ev = _sell("SL_SCALE", cut, pos.sl_T,
                       note=f"partial stop at {pos.sl_T:,.0f} "
                            f"({(pos.sl_T / entry_price - 1) * 100:.1f}%)")
            if ev and ev.dedupe_key not in already:
                events.append(ev)
            if remaining <= 0:
                break

        # --- 4) Midpoint stop ---
        sl_mid = pos.sl_midpoint
        if sl_mid and bar.low <= sl_mid and remaining > 0:
            cut = _scale_out_cut(
                remaining, exits_cfg.scale_out_pct_at_midpoint, lot, min_residual_lots,
            )
            ev = _sell("SL_SCALE", cut, sl_mid,
                       note=f"mid stop at {sl_mid:,.0f} "
                            f"({(sl_mid / entry_price - 1) * 100:.1f}%)")
            if ev and ev.dedupe_key not in already:
                events.append(ev)
            if remaining <= 0:
                break

        # --- 5) Hard stop ---
        if bar.low <= pos.sl_F and remaining > 0:
            ev = _sell("SL_HARD", remaining, pos.sl_F,
                       note=f"hard stop at {pos.sl_F:,.0f} "
                            f"({(pos.sl_F / entry_price - 1) * 100:.1f}%)")
            if ev and ev.dedupe_key not in already:
                events.append(ev)
            break

    _sync()
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
