from __future__ import annotations

from dataclasses import dataclass

from .config import StrategyConfig
from .data import PriceBar
from .indicators import rolling_average_volume, rolling_high, sma, returns, volatility


def bars_until(rows: dict[str, PriceBar], dates: list[str], index: int) -> list[PriceBar]:
    bars: list[PriceBar] = []
    for date in dates[: index + 1]:
        bar = rows.get(date)
        if bar:
            bars.append(bar)
    return bars


def momentum_score(closes: list[float], config: StrategyConfig) -> float | None:
    min_days = max(config.return6mDays, config.smaSlowDays) + 1
    if len(closes) < min_days:
        return None
    current = closes[-1]
    close_3m = closes[-config.return3mDays]
    close_6m = closes[-config.return6mDays]
    if current <= 0 or close_3m <= 0 or close_6m <= 0:
        return None
    sma_fast = sma(closes, config.smaFastDays)
    sma_slow = sma(closes, config.smaSlowDays)
    if sma_fast is None or sma_slow is None or current <= sma_fast or current <= sma_slow:
        return None
    recent_returns = returns(closes[-config.volatilityDays - 1 :])
    recent_volatility = volatility(recent_returns)
    return_3m = current / close_3m - 1
    return_6m = current / close_6m - 1
    return (0.6 * return_3m) + (0.4 * return_6m) - (0.3 * recent_volatility)


def volume_breakout(bars: list[PriceBar], config: StrategyConfig) -> bool:
    min_days = max(config.breakoutLookbackDays, config.breakoutVolumeDays, config.smaSlowDays) + 1
    if len(bars) < min_days:
        return False
    current = bars[-1]
    previous = bars[:-1]
    previous_high = rolling_high([bar.close for bar in previous], config.breakoutLookbackDays)
    avg_volume = rolling_average_volume([bar.volume for bar in previous], config.breakoutVolumeDays)
    closes = [bar.close for bar in bars]
    sma_fast = sma(closes, config.smaFastDays)
    sma_slow = sma(closes, config.smaSlowDays)
    return bool(
        previous_high is not None
        and avg_volume is not None
        and sma_fast is not None
        and sma_slow is not None
        and current.close > previous_high
        and current.close > sma_fast
        and current.close > sma_slow
        and avg_volume > 0
        and current.volume >= avg_volume * config.breakoutVolumeMultiplier
    )


def too_extended_from_sma50(bars: list[PriceBar], config: StrategyConfig) -> bool:
    closes = [bar.close for bar in bars]
    sma_fast = sma(closes, config.smaFastDays)
    if sma_fast is None or sma_fast <= 0:
        return True
    return (closes[-1] / sma_fast - 1) * 100 > config.maxSma50ExtensionPercent


def rank_symbols(
    price_table: dict[str, dict[str, PriceBar]],
    dates: list[str],
    index: int,
    config: StrategyConfig,
) -> list[tuple[str, float]]:
    scores: list[tuple[str, float]] = []
    date = dates[index]
    for symbol, rows in price_table.items():
        if date not in rows:
            continue
        bars = bars_until(rows, dates, index)
        score = momentum_score([bar.close for bar in bars], config)
        if score is not None:
            if volume_breakout(bars, config):
                score += config.breakoutScoreBonus
            scores.append((symbol, score))
    return sorted(scores, key=lambda item: item[1], reverse=True)


def breakout_symbols(
    price_table: dict[str, dict[str, PriceBar]],
    dates: list[str],
    index: int,
    config: StrategyConfig,
) -> set[str]:
    ranked = rank_symbols(price_table, dates, index, config)
    rank_by_symbol = {symbol: rank for rank, (symbol, _) in enumerate(ranked, start=1)}
    symbols: set[str] = set()
    date = dates[index]
    for symbol, rows in price_table.items():
        if date not in rows or rank_by_symbol.get(symbol, 999999) > config.breakoutMaxRank:
            continue
        bars = bars_until(rows, dates, index)
        if volume_breakout(bars, config) and not too_extended_from_sma50(bars, config):
            symbols.add(symbol)
    return symbols


@dataclass(frozen=True)
class StrategyDecision:
    target_symbols: list[str]
    entry_reasons: dict[str, str]


class Strategy:
    name: str

    def select_entries(
        self,
        price_table: dict[str, dict[str, PriceBar]],
        dates: list[str],
        index: int,
        ranked: list[tuple[str, float]],
        regime: str,
        max_positions_today: int,
        weekly_breakout_remaining: int,
    ) -> StrategyDecision:
        raise NotImplementedError


@dataclass(frozen=True)
class MomentumRotationStrategy(Strategy):
    name: str = "liquid_momentum_rotation"

    def select_entries(
        self,
        price_table: dict[str, dict[str, PriceBar]],
        dates: list[str],
        index: int,
        ranked: list[tuple[str, float]],
        regime: str,
        max_positions_today: int,
        weekly_breakout_remaining: int,
    ) -> StrategyDecision:
        if not ranked:
            return StrategyDecision([], {})
        target_symbols = [symbol for symbol, _ in ranked[:max_positions_today]]
        return StrategyDecision(target_symbols, {symbol: "top_rank" for symbol in target_symbols})


@dataclass(frozen=True)
class VolumeBreakoutStrategy(Strategy):
    breakout_symbols: set[str]
    name: str = "volume_breakout"

    def select_entries(
        self,
        price_table: dict[str, dict[str, PriceBar]],
        dates: list[str],
        index: int,
        ranked: list[tuple[str, float]],
        regime: str,
        max_positions_today: int,
        weekly_breakout_remaining: int,
    ) -> StrategyDecision:
        if regime == "bear" or weekly_breakout_remaining <= 0:
            return StrategyDecision([], {})
        target_symbols: list[str] = []
        for symbol, _ in ranked:
            if symbol in self.breakout_symbols:
                target_symbols.append(symbol)
            if len(target_symbols) >= weekly_breakout_remaining:
                break
        return StrategyDecision(target_symbols, {symbol: "volume_breakout" for symbol in target_symbols})


@dataclass(frozen=True)
class MomentumWithBreakoutStrategy(Strategy):
    breakout_symbols: set[str]
    name: str = "liquid_momentum_rotation"

    def select_entries(
        self,
        price_table: dict[str, dict[str, PriceBar]],
        dates: list[str],
        index: int,
        ranked: list[tuple[str, float]],
        regime: str,
        max_positions_today: int,
        weekly_breakout_remaining: int,
    ) -> StrategyDecision:
        if not ranked:
            return StrategyDecision([], {})
        target_symbols = [symbol for symbol, _ in ranked[:max_positions_today]]
        reasons = {symbol: "top_rank" for symbol in target_symbols}
        if regime != "bear" and weekly_breakout_remaining > 0:
            breakout_added = 0
            for symbol, _ in ranked:
                if symbol in self.breakout_symbols and symbol not in reasons:
                    target_symbols.append(symbol)
                    reasons[symbol] = "volume_breakout"
                    breakout_added += 1
                if breakout_added >= weekly_breakout_remaining:
                    break
        return StrategyDecision(target_symbols, reasons)
