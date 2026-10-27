"""Performance metrics."""
from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Sequence

from .executor import TradeResult


def win_rate(trades):
    if not trades:
        return None
    wins = sum(1 for t in trades if t.is_winner)
    return wins / len(trades)


def avg_win(trades):
    wins = [t.return_pct for t in trades if t.is_winner]
    return mean(wins) if wins else None


def avg_loss(trades):
    losses = [t.return_pct for t in trades if not t.is_winner]
    return mean(losses) if losses else None


def profit_factor(trades):
    gross_profit = sum(t.realized_pnl for t in trades if t.realized_pnl > 0)
    gross_loss = sum(t.realized_pnl for t in trades if t.realized_pnl < 0)
    if gross_loss == 0:
        return None if gross_profit == 0 else math.inf
    return gross_profit / abs(gross_loss)


def expectancy(trades):
    if not trades:
        return None
    return sum(t.realized_pnl for t in trades) / len(trades)


def avg_holding_bars(trades):
    if not trades:
        return None
    return mean(t.bars_held for t in trades)


def exit_reason_breakdown(trades):
    counts = {}
    for t in trades:
        counts[t.exit_reason] = counts.get(t.exit_reason, 0) + 1
    return counts


def max_drawdown(equity):
    if len(equity) < 2:
        return None
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            dd = v / peak - 1.0
            if dd < mdd:
                mdd = dd
    return mdd


def sharpe_ratio(returns, *, periods_per_year=252):
    if len(returns) < 2:
        return None
    mu = mean(returns)
    sd = pstdev(returns)
    if sd == 0:
        return None
    return mu / sd * math.sqrt(periods_per_year)


def total_return(equity):
    if not equity or equity[0] == 0:
        return None
    return equity[-1] / equity[0] - 1.0


__all__ = [
    "win_rate", "avg_win", "avg_loss", "profit_factor",
    "expectancy", "avg_holding_bars", "exit_reason_breakdown",
    "max_drawdown", "sharpe_ratio", "total_return",
]
