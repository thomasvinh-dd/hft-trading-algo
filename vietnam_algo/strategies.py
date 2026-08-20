from __future__ import annotations

from dataclasses import dataclass

from .config import StrategyConfig
from .data import PriceBar
from .indicators import max_drawdown_percent, rolling_average_volume, rolling_high, rsi, sma, returns, volatility


def bars_until(rows: dict[str, PriceBar], dates: list[str], index: int) -> list[PriceBar]:
    bars: list[PriceBar] = []
    for date in dates[: index + 1]:
        bar = rows.get(date)
        if bar:
            bars.append(bar)
    return bars


def momentum_score(closes: list[float], config: StrategyConfig) -> float | None:
    lookbacks = []
    if config.momentum3mWeight:
        lookbacks.append(config.return3mDays + config.skipRecentDays + 1)
    if config.momentum6mWeight:
        lookbacks.append(config.return6mDays + config.skipRecentDays + 1)
    if config.momentum12mWeight:
        lookbacks.append(config.return12mDays + config.skipRecentDays + 1)
    min_days = max([config.smaSlowDays + 1, config.volatilityDays + config.skipRecentDays + 1, *lookbacks])
    if len(closes) < min_days:
        return None
    current = closes[-1]
    momentum_end_index = -1 - config.skipRecentDays if config.skipRecentDays > 0 else -1
    momentum_end = closes[momentum_end_index]
    if current <= 0 or momentum_end <= 0:
        return None
    sma_fast = sma(closes, config.smaFastDays)
    sma_slow = sma(closes, config.smaSlowDays)
    if sma_fast is None or sma_slow is None or current <= sma_fast or current <= sma_slow:
        return None
    recent_returns = returns(closes[-config.volatilityDays - 1 :])
    if config.useDownsideVolatility:
        downside_returns = [value for value in recent_returns if value < 0]
        recent_volatility = volatility(downside_returns) if len(downside_returns) >= 2 else 0.0
    else:
        recent_volatility = volatility(recent_returns)
    score = 0.0
    total_weight = config.momentum3mWeight + config.momentum6mWeight + config.momentum12mWeight
    if total_weight <= 0:
        return None
    if config.momentum3mWeight:
        start_index = -config.return3mDays if config.skipRecentDays == 0 else momentum_end_index - config.return3mDays
        start = closes[start_index]
        if start <= 0:
            return None
        score += config.momentum3mWeight * (momentum_end / start - 1)
    if config.momentum6mWeight:
        start_index = -config.return6mDays if config.skipRecentDays == 0 else momentum_end_index - config.return6mDays
        start = closes[start_index]
        if start <= 0:
            return None
        score += config.momentum6mWeight * (momentum_end / start - 1)
    if config.momentum12mWeight:
        start_index = -config.return12mDays if config.skipRecentDays == 0 else momentum_end_index - config.return12mDays
        start = closes[start_index]
        if start <= 0:
            return None
        score += config.momentum12mWeight * (momentum_end / start - 1)
    score = (score / total_weight) - (config.volatilityPenaltyWeight * recent_volatility)
    if config.trendUpWeight:
        trend_returns = returns(closes[-config.trendUpDays - 1 :])
        if trend_returns:
            up_ratio = sum(1 for value in trend_returns if value > 0) / len(trend_returns)
            score += config.trendUpWeight * up_ratio
    if config.trendDrawdownPenaltyWeight:
        drawdown = abs(max_drawdown_percent(closes[-config.trendDrawdownDays :])) / 100
        score -= config.trendDrawdownPenaltyWeight * drawdown
    if config.smaSlopeWeight:
        slow_smas = [sma(closes[: i + 1], config.smaFastDays) for i in range(len(closes))]
        slow_smas = [value for value in slow_smas if value is not None]
        if len(slow_smas) > config.smaSlopeDays and slow_smas[-config.smaSlopeDays] > 0:
            score += config.smaSlopeWeight * (slow_smas[-1] / slow_smas[-config.smaSlopeDays] - 1)
    return score


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
    universe: set[str] | None = None,
) -> list[tuple[str, float]]:
    scores: list[tuple[str, float]] = []
    date = dates[index]
    for symbol, rows in price_table.items():
        if universe is not None and symbol not in universe:
            continue
        if date not in rows:
            continue
        bars = bars_until(rows, dates, index)
        score = momentum_score([bar.close for bar in bars], config)
        if score is not None:
            if config.volumeTrendWeight:
                volumes = [bar.volume for bar in bars]
                recent = rolling_average_volume(volumes, max(2, config.volumeTrendDays // 2))
                previous = rolling_average_volume(volumes[: -max(2, config.volumeTrendDays // 2)], max(2, config.volumeTrendDays // 2))
                if recent is not None and previous is not None and previous > 0:
                    score += config.volumeTrendWeight * (recent / previous - 1)
            if volume_breakout(bars, config):
                score += config.breakoutScoreBonus
            scores.append((symbol, score))
    return sorted(scores, key=lambda item: item[1], reverse=True)


def breakout_symbols(
    price_table: dict[str, dict[str, PriceBar]],
    dates: list[str],
    index: int,
    config: StrategyConfig,
    universe: set[str] | None = None,
) -> set[str]:
    ranked = rank_symbols(price_table, dates, index, config, universe=universe)
    rank_by_symbol = {symbol: rank for rank, (symbol, _) in enumerate(ranked, start=1)}
    symbols: set[str] = set()
    date = dates[index]
    for symbol, rows in price_table.items():
        if universe is not None and symbol not in universe:
            continue
        if date not in rows or rank_by_symbol.get(symbol, 999999) > config.breakoutMaxRank:
            continue
        bars = bars_until(rows, dates, index)
        if volume_breakout(bars, config) and not too_extended_from_sma50(bars, config):
            symbols.add(symbol)
    return symbols


def pullback_score(bars: list[PriceBar], config: StrategyConfig) -> float | None:
    min_days = max(config.smaSlowDays, config.pullbackLookbackDays, config.pullbackVolumeDays, config.rsiWindow) + 1
    if len(bars) < min_days:
        return None
    current = bars[-1]
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    slow_sma = sma(closes, config.smaSlowDays)
    current_rsi = rsi(closes, config.rsiWindow)
    recent_high = rolling_high(closes[:-1], config.pullbackLookbackDays)
    avg_volume = rolling_average_volume(volumes[:-1], config.pullbackVolumeDays)
    if slow_sma is None or current_rsi is None or recent_high is None or avg_volume is None:
        return None
    if current.close <= slow_sma or current_rsi >= config.pullbackRsiEntry or avg_volume <= 0:
        return None
    pullback_percent = (recent_high / current.close - 1) * 100 if current.close > 0 else 0
    if pullback_percent < config.pullbackMinPercent or pullback_percent > config.pullbackMaxPercent:
        return None
    if current.volume > avg_volume * config.pullbackPanicVolumeMultiplier:
        return None
    return pullback_percent + ((config.pullbackRsiEntry - current_rsi) / 10)


def pullback_candidates(
    price_table: dict[str, dict[str, PriceBar]],
    dates: list[str],
    index: int,
    config: StrategyConfig,
    universe: set[str] | None = None,
) -> list[tuple[str, float]]:
    date = dates[index]
    candidates: list[tuple[str, float]] = []
    for symbol, rows in price_table.items():
        if universe is not None and symbol not in universe:
            continue
        if date not in rows:
            continue
        score = pullback_score(bars_until(rows, dates, index), config)
        if score is not None:
            candidates.append((symbol, score))
    return sorted(candidates, key=lambda item: item[1], reverse=True)


def pullback_exit_signal(bars: list[PriceBar], config: StrategyConfig) -> bool:
    min_days = max(config.pullbackExitSmaDays, config.rsiWindow) + 1
    if len(bars) < min_days:
        return False
    closes = [bar.close for bar in bars]
    current_rsi = rsi(closes, config.rsiWindow)
    exit_sma = sma(closes, config.pullbackExitSmaDays)
    return bool(
        current_rsi is not None
        and exit_sma is not None
        and (current_rsi >= config.pullbackRsiExit or closes[-1] >= exit_sma)
    )


def sector_momentum_scores(
    price_table: dict[str, dict[str, PriceBar]],
    dates: list[str],
    index: int,
    sector_by_symbol: dict[str, str],
    config: StrategyConfig,
    universe: set[str] | None = None,
) -> list[tuple[str, float]]:
    grouped_scores: dict[str, list[float]] = {}
    date = dates[index]
    for symbol, rows in price_table.items():
        if universe is not None and symbol not in universe:
            continue
        if date not in rows:
            continue
        bars = bars_until(rows, dates, index)
        closes = [bar.close for bar in bars]
        min_days = max(config.sectorReturn1mDays, config.sectorReturn3mDays) + 1
        if len(closes) < min_days or closes[-config.sectorReturn1mDays] <= 0 or closes[-config.sectorReturn3mDays] <= 0:
            continue
        return_1m = closes[-1] / closes[-config.sectorReturn1mDays] - 1
        return_3m = closes[-1] / closes[-config.sectorReturn3mDays] - 1
        sector = sector_by_symbol.get(symbol, "unknown")
        grouped_scores.setdefault(sector, []).append((0.4 * return_1m) + (0.6 * return_3m))
    sector_scores = [
        (sector, sum(scores) / len(scores))
        for sector, scores in grouped_scores.items()
        if scores
    ]
    return sorted(sector_scores, key=lambda item: item[1], reverse=True)


def sector_stock_candidates(
    price_table: dict[str, dict[str, PriceBar]],
    dates: list[str],
    index: int,
    sector_by_symbol: dict[str, str],
    config: StrategyConfig,
    universe: set[str] | None = None,
) -> list[tuple[str, float]]:
    top_sectors = {
        sector for sector, _ in sector_momentum_scores(price_table, dates, index, sector_by_symbol, config, universe=universe)[: config.topSectors]
    }
    if not top_sectors:
        return []
    ranked = rank_symbols(price_table, dates, index, config, universe=universe)
    per_sector_counts: dict[str, int] = {}
    candidates: list[tuple[str, float]] = []
    for symbol, score in ranked:
        sector = sector_by_symbol.get(symbol, "unknown")
        if sector not in top_sectors:
            continue
        if per_sector_counts.get(sector, 0) >= config.topStocksPerSector:
            continue
        candidates.append((symbol, score))
        per_sector_counts[sector] = per_sector_counts.get(sector, 0) + 1
    return candidates


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


@dataclass(frozen=True)
class PullbackMeanReversionStrategy(Strategy):
    candidates: list[tuple[str, float]]
    name: str = "pullback_mean_reversion"

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
        target_symbols = [symbol for symbol, _ in self.candidates[:weekly_breakout_remaining]]
        return StrategyDecision(target_symbols, {symbol: "pullback_mean_reversion" for symbol in target_symbols})


@dataclass(frozen=True)
class SectorMomentumStrategy(Strategy):
    candidates: list[tuple[str, float]]
    name: str = "sector_momentum_rotation"

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
        if regime == "bear":
            return StrategyDecision([], {})
        target_symbols = [symbol for symbol, _ in self.candidates[:max_positions_today]]
        return StrategyDecision(target_symbols, {symbol: "sector_momentum" for symbol in target_symbols})


@dataclass(frozen=True)
class MarketRegimeBenchmarkStrategy(Strategy):
    symbols: list[str]
    name: str = "market_regime_benchmark"

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
        if regime == "bear":
            return StrategyDecision([], {})
        target_symbols = self.symbols[:max_positions_today]
        return StrategyDecision(target_symbols, {symbol: "market_regime_benchmark" for symbol in target_symbols})


@dataclass(frozen=True)
class HybridMomentumSectorBreakoutStrategy(Strategy):
    candidates: list[tuple[str, float]]
    breakout_symbols: set[str]
    name: str = "hybrid_momentum_breakout_sector"

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
        if regime == "bear":
            return StrategyDecision([], {})
        target_symbols = [symbol for symbol, _ in self.candidates[:max_positions_today]]
        reasons = {
            symbol: ("hybrid_breakout_sector" if symbol in self.breakout_symbols else "hybrid_momentum_sector")
            for symbol in target_symbols
        }
        return StrategyDecision(target_symbols, reasons)
