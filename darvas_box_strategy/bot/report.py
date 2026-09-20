"""Telegram-friendly report formatting for the Darvas Box strategy."""
from __future__ import annotations

from datetime import date
from typing import Iterable

from darvas_box_strategy.darvas.metrics import (
    avg_loss, avg_win, profit_factor, sharpe_ratio,
    total_return, max_drawdown, win_rate,
)
from darvas_box_strategy.darvas.backtest import BacktestResult


def _fmt_pct(v, sign=True):
    if v is None:
        return "n/a"
    s = f"{abs(v) * 100:.2f}%"
    return f"+{s}" if sign and v > 0 else (f"-{s}" if v < 0 else s)


def _fmt_vnd(v):
    if v is None:
        return "n/a"
    if abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    return f"{v:,.0f}"


def format_backtest_report(result: BacktestResult, *,
                             universe_name: str = "HOSE blue chips",
                             period_label: str = "2019-2026") -> str:
    """Build a compact Telegram-friendly report."""
    trades = result.trades
    closed = [t for t in trades if t.exit_reason != "EXHAUST"]
    equity = [v for _, v in result.nav_curve]

    ret = total_return(equity)
    mdd = max_drawdown(equity)
    daily = result.daily_returns
    sharpe = sharpe_ratio(daily)
    winr = win_rate(trades)
    pfactor = profit_factor(trades)
    aw = avg_win(trades)
    al = avg_loss(trades)

    lines = []
    lines.append(f"📊 *Darvas Box Backtest Report*")
    lines.append(f"`{universe_name}` | {period_label}")
    lines.append("")
    lines.append(f"💰 *Return*: {_fmt_pct(ret)}")
    lines.append(f"📉 *Max DD*: {_fmt_pct(mdd)}")
    if sharpe is not None:
        lines.append(f"📈 *Sharpe*: {sharpe:+.2f}")
    lines.append("")
    lines.append(f"*Period*: `{result.start}` → `{result.end}`")
    lines.append(f"*Tickers scanned*: {result.tickers_scanned}")
    lines.append(f"*Trades*: {len(closed)} closed, {result.n_exhausted} open")
    if result.concurrent_position_peak:
        lines.append(f"*Peak concurrent*: {result.concurrent_position_peak}")
    lines.append("")
    lines.append("*Trade stats*")
    lines.append(f"  Win rate:     {_fmt_pct(winr, sign=False)}")
    if aw is not None:
        lines.append(f"  Avg win:      {_fmt_pct(aw, sign=False)}")
    if al is not None:
        lines.append(f"  Avg loss:     {_fmt_pct(al, sign=False)}")
    if pfactor is not None:
        lines.append(f"  Profit factor: {pfactor:.2f}x")
    lines.append("")

    exit_reasons: dict[str, int] = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1
    lines.append("*Exit reasons*")
    for reason, count in sorted(exit_reasons.items(), key=lambda x: -x[1]):
        lines.append(f"  `{reason:<10s}` {count}")
    lines.append("")

    # Top 5 winners
    winners = sorted(trades, key=lambda t: -t.realized_pnl)[:5]
    if winners:
        lines.append("*Top 5 winners*")
        for t in winners:
            lines.append(
                f"  `{t.signal.ticker}` +{t.return_pct * 100:.1f}% "
                f"{_fmt_vnd(t.realized_pnl)}VND"
            )
        lines.append("")

    # Top 5 losers
    losers = sorted(trades, key=lambda t: t.realized_pnl)[:5]
    if losers:
        lines.append("*Top 5 losers*")
        for t in losers:
            lines.append(
                f"  `{t.signal.ticker}` {t.return_pct * 100:+.1f}% "
                f"{_fmt_vnd(t.realized_pnl)}VND"
            )

    return "\n".join(lines)


def format_recent_trades(result: BacktestResult, n: int = 10) -> str:
    """Format the last N closed trades."""
    closed = sorted(
        [t for t in result.trades if t.exit_reason != "EXHAUST"],
        key=lambda t: t.exit_date,
        reverse=True,
    )[:n]

    if not closed:
        return "_(no trades)"

    lines = []
    lines.append(f"*Last {len(closed)} trades*")
    lines.append("```")
    lines.append(f"{'Tkr':<6s} {'Entry':<10s} {'Exit':<10s} {'Held':>5s} {'Reason':<10s} {'Ret':>8s} {'PnL':>12s}")
    lines.append("-" * 65)
    for t in closed:
        lines.append(
            f"{t.signal.ticker:<6s} "
            f"{str(t.signal.entry_date):<10s} "
            f"{str(t.exit_date):<10s} "
            f"{t.bars_held:>5d} "
            f"{t.exit_reason:<10s} "
            f"{t.return_pct * 100:+7.2f}% "
            f"{_fmt_vnd(t.realized_pnl):>10s}V"
        )
    lines.append("```")
    return "\n".join(lines)


def format_ticker_breakdown(result: BacktestResult, top: int = 10) -> str:
    """Format per-ticker PnL breakdown."""
    by_ticker: dict[str, dict] = {}
    for t in result.trades:
        agg = by_ticker.setdefault(t.signal.ticker, {
            "n": 0, "wins": 0, "pnl": 0.0,
        })
        agg["n"] += 1
        if t.is_winner:
            agg["wins"] += 1
        agg["pnl"] += t.realized_pnl

    if not by_ticker:
        return "_(no trades)"

    rows = sorted(by_ticker.items(), key=lambda x: -x[1]["pnl"])

    lines = []
    lines.append(f"*Top {min(top, len(rows))} tickers by PnL*")
    lines.append("```")
    lines.append(f"{'Tkr':<6s} {'N':>3s} {'Wins':>4s} {'PnL (VND)':>14s}")
    lines.append("-" * 35)
    for ticker, agg in rows[:top]:
        lines.append(
            f"{ticker:<6s} {agg['n']:>3d} {agg['wins']:>4d} "
            f"{agg['pnl']:>14,.0f}"
        )
    lines.append("```")

    if len(rows) > top:
        worst = sorted(rows, key=lambda x: x[1]["pnl"])[:5]
        lines.append("")
        lines.append("*Bottom 5 tickers*")
        lines.append("```")
        for ticker, agg in worst:
            lines.append(
                f"{ticker:<6s} {agg['n']:>3d} {agg['wins']:>4d} "
                f"{agg['pnl']:>14,.0f}"
            )
        lines.append("```")

    return "\n".join(lines)


def chunk_message(text: str, max_len: int = 3900) -> Iterable[str]:
    """Split a long message into Telegram-sized chunks."""
    if len(text) <= max_len:
        yield text
        return

    lines = text.split("\n")
    chunk: list[str] = []
    chunk_len = 0

    for line in lines:
        # Telegram message limit is 4096; we use 3900 to leave headroom.
        line_len = len(line) + 1
        if chunk_len + line_len > max_len:
            yield "\n".join(chunk)
            chunk = [line]
            chunk_len = line_len
        else:
            chunk.append(line)
            chunk_len += line_len

    if chunk:
        yield "\n".join(chunk)


__all__ = [
    "format_backtest_report",
    "format_recent_trades",
    "format_ticker_breakdown",
    "chunk_message",
]
