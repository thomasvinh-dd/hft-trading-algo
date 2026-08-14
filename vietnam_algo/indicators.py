from __future__ import annotations

import math


def sma(values: list[float], window: int) -> float | None:
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:]) / window


def returns(values: list[float]) -> list[float]:
    result: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous > 0:
            result.append(current / previous - 1)
    return result


def volatility(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def rolling_high(values: list[float], window: int) -> float | None:
    if window <= 0 or len(values) < window:
        return None
    return max(values[-window:])


def rolling_average_volume(volumes: list[float], window: int) -> float | None:
    return sma(volumes, window)


def rsi(values: list[float], window: int = 14) -> float | None:
    if window <= 0 or len(values) < window + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values[-window - 1 : -1], values[-window:]):
        change = current - previous
        if change >= 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100 - (100 / (1 + relative_strength))


def max_drawdown_percent(equity_values: list[float]) -> float:
    if not equity_values:
        return 0.0
    peak = equity_values[0]
    max_drawdown = 0.0
    for equity in equity_values:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, equity / peak - 1)
    return max_drawdown * 100
