"""Human-readable reporting layer."""
from __future__ import annotations

from .backtest import BacktestResult
from .executor import TradeResult
from .metrics import (
    avg_holding_bars,
    avg_loss,
    avg_win,
    expectancy,
    exit_reason_breakdown,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    total_return,
    win_rate,
)


def _fmt_pct(v):
    return "n/a" if v is None else f"{v * 100:+.2f}%"


def _fmt_vnd(v):
    if v is None:
        return "n/a"
    return f"{v:,.0f} VND"


def _fmt_x(v):
    if v is None:
        return "n/a"
    if v == float("inf"):
        return "inf"
    return f"{v:.2f}x"


def summary_report(result):
    trades = result.trades
    closed = [t for t in trades if t.exit_reason != "EXHAUST"]
    equity = [v for _, v in result.nav_curve]
    daily_returns = result.daily_returns

    lines = []
    lines.append("=" * 72)
    lines.append("DARVAS BOX + AUTOFIN TIERED-EXIT STRATEGY - BACKTEST SUMMARY")
    lines.append("=" * 72)
    lines.append(f"Period:              {result.start} -> {result.end}")
    lines.append(f"Tickers scanned:     {result.tickers_scanned}")
    lines.append(f"Bars scanned:        {result.bars_scanned:,}")
    lines.append(
        f"Trades generated:    {result.n_trades}  "
        f"(closed: {len(closed)}, open/EXHAUST: {result.n_exhausted})"
    )
    if result.concurrent_position_peak:
        lines.append(f"Peak concurrent pos: {result.concurrent_position_peak}")
    lines.append("")

    lines.append("--- Trade stats (all trades) ---")
    lines.append(f"  Win rate:          {_fmt_pct(win_rate(trades))}")
    lines.append(f"  Avg win:           {_fmt_pct(avg_win(trades))}")
    lines.append(f"  Avg loss:          {_fmt_pct(avg_loss(trades))}")
    lines.append(f"  Profit factor:     {_fmt_x(profit_factor(trades))}")
    lines.append(f"  Expectancy:        {_fmt_vnd(expectancy(trades))}")
    if avg_holding_bars(trades) is not None:
        lines.append(f"  Avg holding bars:  {avg_holding_bars(trades):.1f}")
    else:
        lines.append("  Avg holding bars:  n/a")
    lines.append("")

    lines.append("--- Equity curve (time-weighted, closed trades only) ---")
    lines.append(f"  Start NAV:         {equity[0]:,.0f}" if equity else "  Start NAV:         n/a")
    lines.append(f"  End NAV:           {equity[-1]:,.0f}" if equity else "  End NAV:           n/a")
    lines.append(f"  Total return:      {_fmt_pct(total_return(equity))}")
    lines.append(f"  Max drawdown:      {_fmt_pct(max_drawdown(equity))}")
    if sharpe_ratio(daily_returns) is not None:
        lines.append(f"  Sharpe (annl.):    {sharpe_ratio(daily_returns):.2f}")
    else:
        lines.append("  Sharpe (annl.):    n/a")
    lines.append("")

    lines.append("--- Exit reasons ---")
    for reason, count in sorted(exit_reason_breakdown(trades).items()):
        lines.append(f"  {reason:<10s} {count}")
    lines.append("")
    return "\n".join(lines)


def ticker_breakdown(result):
    by_ticker = {}
    for t in result.trades:
        agg = by_ticker.setdefault(t.signal.ticker, {"n": 0, "wins": 0, "pnl": 0.0, "fees": 0.0})
        agg["n"] += 1
        if t.is_winner:
            agg["wins"] += 1
        agg["pnl"] += t.realized_pnl
        agg["fees"] += t.cost_paid
    if not by_ticker:
        return "(no trades)"

    rows = []
    for ticker, agg in by_ticker.items():
        rows.append((ticker, int(agg["n"]), int(agg["wins"]), agg["pnl"], agg["fees"]))
    rows.sort(key=lambda r: r[3], reverse=True)

    lines = []
    lines.append(f"{'Ticker':<8s} {'N':>4s} {'Wins':>4s} {'PnL (VND)':>16s} {'Fees (VND)':>14s}")
    lines.append("-" * 50)
    for ticker, n, wins, pnl, fees in rows:
        lines.append(f"{ticker:<8s} {n:>4d} {wins:>4d} {pnl:>16,.0f} {fees:>14,.0f}")
    lines.append("-" * 50)
    total_pnl = sum(r[3] for r in rows)
    total_fees = sum(r[4] for r in rows)
    lines.append(f"{'TOTAL':<8s} {sum(r[1] for r in rows):>4d} {sum(r[2] for r in rows):>4d} {total_pnl:>16,.0f} {total_fees:>14,.0f}")
    return "\n".join(lines)


def recent_trades(result, n=20):
    last = sorted(result.trades, key=lambda t: t.exit_date, reverse=True)[:n]
    if not last:
        return "(no trades)"
    lines = []
    lines.append(f"{'Ticker':<6s} {'Entry':>10s} {'Exit':>10s} {'Held':>5s} {'Reason':<10s} {'Return':>9s} {'PnL (VND)':>14s}")
    lines.append("-" * 70)
    for t in last:
        lines.append(
            f"{t.signal.ticker:<6s} "
            f"{t.signal.entry_date.isoformat():>10s} "
            f"{t.exit_date.isoformat():>10s} "
            f"{t.bars_held:>5d} "
            f"{t.exit_reason:<10s} "
            f"{_fmt_pct(t.return_pct):>9s} "
            f"{t.realized_pnl:>14,.0f}"
        )
    return "\n".join(lines)


def trade_detail(trade):
    s = trade.signal
    lines = []
    lines.append(f"--- {s.ticker} trade detail ---")
    lines.append(f"  Entry:    {s.entry_date} @ {s.entry_price:,.0f}  x{s.shares:,} shares")
    lines.append(f"  Conviction: {s.conviction}  (size {s.size_pct * 100:.0f}% NAV)")
    lines.append(f"  Base:     [{s.base.base_low:,.0f} - {s.base.base_high:,.0f}] "
                 f"({s.base.days_in_base} days, range {s.base.range_pct * 100:.2f}%)")
    lines.append(f"  Stops:    sl_F={s.sl_F:,.0f}  sl_T={s.sl_T:,.0f}  mid={s.sl_midpoint:,.0f}")
    lines.append(f"  Exit:     {trade.exit_date}  reason={trade.exit_reason}")
    lines.append(f"  Return:   {trade.return_pct * 100:+.2f}%  P/L={trade.realized_pnl:,.0f} VND")
    lines.append(f"  Fills ({len(trade.fills)}):")
    for f in trade.fills:
        lines.append(
            f"    {f.date}  {f.kind:<8s} "
            f"{f.shares:>6,} sh @ {f.level:,.0f}  "
            f"({f.fraction_of_pos * 100:.0f}% pos)  "
            f"pnl={f.realized_pnl:+,.0f}"
        )
    return "\n".join(lines)


__all__ = [
    "summary_report",
    "ticker_breakdown",
    "recent_trades",
    "trade_detail",
]
