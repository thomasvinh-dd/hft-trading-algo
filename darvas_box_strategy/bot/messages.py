"""Telegram message formatters for Darvas Box signals and exit events."""
from __future__ import annotations

from typing import Iterable, List, Optional

from .exits import ExitEvent
from .positions import LivePosition

# Emoji + label for each exit event kind
_EXIT_EMOJI = {
    "TIER": "🎯",
    "TRAIL": "📉",
    "SL_SCALE": "⚠️",
    "SL_HARD": "🛑",
    "TIME_STOP": "⏳",
}

_EXIT_LABEL = {
    "TIER": "Take-profit hit",
    "TRAIL": "Trailing stop hit",
    "SL_SCALE": "Partial stop hit",
    "SL_HARD": "Hard stop hit",
    "TIME_STOP": "Time stop",
}


def _fmt_pct(v: float, sign: bool = True) -> str:
    return f"{v:+.1f}%" if sign else f"{v:.1f}%"


def _fmt_vnd(v: float) -> str:
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1_000_000_000:
        return f"{sign}{a / 1_000_000_000:.2f}B"
    if a >= 1_000_000:
        return f"{sign}{a / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{sign}{a / 1_000:.0f}K"
    return f"{sign}{a:,.0f}"


def format_exit_event(pos: LivePosition, event: ExitEvent) -> str:
    """Format a single exit event as a Telegram message."""
    emoji = _EXIT_EMOJI.get(event.kind, "📌")
    label = _EXIT_LABEL.get(event.kind, event.kind)
    entry_pct = (event.price / pos.entry_price - 1.0) * 100.0
    pnl_pct = (
        event.pnl_delta / (event.shares * pos.entry_price) * 100.0
        if event.shares and pos.entry_price else 0.0
    )

    lines = [
        f"{emoji} *Darvas Box: {label}* — {pos.ticker}",
        "",
        f"*Event*: {event.kind}",
        f"*Shares*: {event.shares:,} @ {event.price:,.0f} VND "
        f"({_fmt_pct(entry_pct)} vs entry {pos.entry_price:,.0f})",
        f"*Remaining*: {event.remaining_after:,} sh "
        f"of {pos.shares:,}",
        f"*Realized P&L*: {_fmt_vnd(event.pnl_delta)} VND ({_fmt_pct(pnl_pct)})",
    ]
    if event.note:
        lines.append(f"*Detail*: {event.note}")
    if event.remaining_after > 0:
        lines.append("")
        lines.append(f"_Position still open — {event.remaining_after:,} sh remain._")
    else:
        lines.append("")
        lines.append(f"_Position closed. Total realized: "
                     f"{_fmt_vnd(pos.cumulative_pnl)} VND_")
    return "\n".join(lines)


def format_exit_events(pos: LivePosition, events: List[ExitEvent]) -> str:
    """Format multiple exit events for one position in a single message."""
    if not events:
        return ""
    if len(events) == 1:
        return format_exit_event(pos, events[0])

    emoji = "📌"
    lines = [
        f"{emoji} *Darvas Box: {len(events)} exit event(s)* — {pos.ticker}",
        "",
        f"_Entry_: {pos.shares:,} sh @ {pos.entry_price:,.0f} VND "
        f"on {pos.entry_date}",
        "",
    ]
    for ev in events:
        e = _EXIT_EMOJI.get(ev.kind, "•")
        lbl = _EXIT_LABEL.get(ev.kind, ev.kind)
        lines.append(
            f"{e} *{lbl}* — {ev.shares:,} sh @ {ev.price:,.0f} "
            f"({_fmt_vnd(ev.pnl_delta)} VND)"
        )
    lines.append("")
    lines.append(f"_Remaining: {events[-1].remaining_after:,} sh of "
                 f"{pos.shares:,}_")
    return "\n".join(lines)


def format_entry_alert(
    ticker: str, entry_price: float, shares: int, size_pct: float,
    sl_T: float, sl_F: float, tiers: Iterable = (),
) -> str:
    """Format a breakout entry alert."""
    notional = shares * entry_price
    lines = [
        f"🚨 *Darvas Box: BREAKOUT* — {ticker}",
        "",
        f"*Buy*: {shares:,} sh @ {entry_price:,.0f} VND "
        f"(~{_fmt_vnd(notional)} VND)",
        f"*Size*: {size_pct * 100:.0f}% of NAV",
        f"*Stops*: sl_T {sl_T:,.0f} (partial) | sl_F {sl_F:,.0f} (hard)",
    ]
    tier_list = list(tiers or ())
    if tier_list:
        tps = ", ".join(
            f"{entry_price * float(m):,.0f} ({float(p) * 100:.0f}%)"
            for m, p in tier_list[:3]
        )
        lines.append(f"*TPs*: {tps}")
    lines.append("")
    lines.append("_T+2.5 settlement — no sell for 2 trading days._")
    return "\n".join(lines)


def format_position_opened(pos: LivePosition) -> str:
    """Format the 'position opened, now tracking' confirmation."""
    notional = pos.shares * pos.entry_price
    lines = [
        f"📥 *Darvas Box: Position opened* — {pos.ticker}",
        "",
        f"*Shares*: {pos.shares:,} @ {pos.entry_price:,.0f} VND "
        f"(~{_fmt_vnd(notional)} VND)",
        f"*Stops*: sl_T {pos.sl_T:,.0f} | sl_F {pos.sl_F:,.0f}",
        "",
        "_Now tracking TIER / TRAIL / SL events for this position._",
    ]
    return "\n".join(lines)


def format_no_exits_summary(checked: int) -> str:
    return (
        f"📭 *Darvas Box: No exit events* — checked {checked} open position(s)"
    )


def format_positions_summary(positions: List[LivePosition]) -> str:
    """Summary of all tracked open positions."""
    if not positions:
        return "📭 *Darvas Box: No open positions tracked*"
    lines = [
        f"📋 *Darvas Box: {len(positions)} open position(s)*",
        "",
    ]
    for p in sorted(positions, key=lambda x: x.entry_date):
        pnl_pct = (p.last_close / p.entry_price - 1.0) * 100.0 if p.entry_price else 0.0
        lines.append(
            f"• *{p.ticker}* — {p.remaining:,}/{p.shares:,} sh "
            f"@ {p.entry_price:,.0f} | last {p.last_close:,.0f} "
            f"({_fmt_pct(pnl_pct)}) | peak {p.peak_close:,.0f}"
        )
    return "\n".join(lines)
