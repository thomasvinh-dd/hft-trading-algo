from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date as date_type
from pathlib import Path

from .config import StrategyConfig
from .data import DEFAULT_DATA_DIR, PriceBar, utc_now, write_json
from .indicators import max_drawdown_percent, sma
from .strategies import (
    MomentumWithBreakoutStrategy,
    HybridMomentumSectorBreakoutStrategy,
    MarketRegimeBenchmarkStrategy,
    PullbackMeanReversionStrategy,
    SectorMomentumStrategy,
    VolumeBreakoutStrategy,
    bars_until,
    breakout_symbols,
    pullback_candidates,
    pullback_exit_signal,
    rank_symbols,
    sector_momentum_scores,
    sector_stock_candidates,
    too_extended_from_sma50,
)
from .universe import load_price_bars, load_symbol_metadata, load_universe_symbols


@dataclass
class Position:
    symbol: str
    quantity: float
    entryPrice: float
    entryDate: str
    entryIndex: int
    peakPrice: float


@dataclass(frozen=True)
class Trade:
    date: str
    symbol: str
    side: str
    price: float
    quantity: float
    value: float
    fee: float
    reason: str


@dataclass(frozen=True)
class EquityPoint:
    date: str
    equity: float
    cash: float
    positionsValue: float


@dataclass(frozen=True)
class BacktestResult:
    strategy: str
    generatedAt: str
    config: dict[str, float | int]
    symbols: list[str]
    metrics: dict[str, float | int]
    equityCurve: list[dict[str, float | str]]
    trades: list[dict[str, float | str]]


def symbol_sector_map(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, str]:
    metadata = load_symbol_metadata(data_dir)
    return {symbol: str(item.get("sectorName") or "unknown") for symbol, item in metadata.items()}


def build_price_table(symbols: list[str], data_dir: Path) -> dict[str, dict[str, PriceBar]]:
    table: dict[str, dict[str, PriceBar]] = {}
    for symbol in symbols:
        bars = load_price_bars(symbol, data_dir)
        if bars:
            table[symbol] = {bar.time: bar for bar in bars}
    return table


def sorted_trading_dates(price_table: dict[str, dict[str, PriceBar]]) -> list[str]:
    dates: set[str] = set()
    for rows in price_table.values():
        dates.update(rows.keys())
    return sorted(dates)


def market_proxy_closes(price_table: dict[str, dict[str, PriceBar]], dates: list[str], index: int) -> list[float]:
    proxy: list[float] = []
    for date in dates[: index + 1]:
        returns: list[float] = []
        for rows in price_table.values():
            bar = rows.get(date)
            first_bar = next(iter(rows.values()), None)
            if bar and first_bar and first_bar.close > 0:
                returns.append(bar.close / first_bar.close)
        if returns:
            proxy.append(sum(returns) / len(returns))
    return proxy


def market_regime(price_table: dict[str, dict[str, PriceBar]], dates: list[str], index: int, config: StrategyConfig) -> str:
    closes = market_proxy_closes(price_table, dates, index)
    if len(closes) < config.marketSmaDays:
        return "neutral"
    current = closes[-1]
    sma_fast = sma(closes, config.marketSmaDays)
    sma_bear = sma(closes, config.marketBearSmaDays) if len(closes) >= config.marketBearSmaDays else None
    if sma_bear is not None and current < sma_bear:
        return "bear"
    if sma_fast is not None and current < sma_fast:
        return "neutral"
    return "bull"


def allowed_positions_for_regime(regime: str, config: StrategyConfig) -> int:
    if regime == "bear":
        return max(0, min(config.bearMaxPositions, config.maxPositions))
    if regime == "neutral":
        return max(1, min((config.maxPositions + 1) // 2, config.maxPositions))
    return config.maxPositions


def exposure_percent_for_regime(regime: str, config: StrategyConfig) -> float:
    if regime == "bear":
        return max(config.bearExposurePercent, 0.0)
    if regime == "neutral":
        return max(config.neutralExposurePercent, 0.0)
    return max(config.bullExposurePercent, 0.0)


def trailing_stop_percent_for_regime(regime: str, config: StrategyConfig) -> float:
    if regime == "bear":
        return config.bearTrailingStopPercent
    if regime == "neutral":
        return config.neutralTrailingStopPercent
    return config.trailingStopPercent


def sector_position_count(positions: dict[str, Position], sector_by_symbol: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for symbol in positions:
        sector = sector_by_symbol.get(symbol, "unknown")
        counts[sector] = counts.get(sector, 0) + 1
    return counts


def regime_exit_reason(regime: str, config: StrategyConfig) -> str:
    if config.marketExitRegime <= 0:
        return ""
    if config.marketExitRegime == 1 and regime == "bear":
        return "market_bear_exit"
    if config.marketExitRegime >= 2 and regime in {"neutral", "bear"}:
        return "market_weak_exit"
    return ""


def week_key(value: str) -> str:
    parsed = date_type.fromisoformat(value)
    year, week, _ = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def close_price(symbol: str, date: str, price_table: dict[str, dict[str, PriceBar]]) -> float | None:
    bar = price_table.get(symbol, {}).get(date)
    return float(bar.close) if bar else None


def open_price(symbol: str, date: str, price_table: dict[str, dict[str, PriceBar]]) -> float | None:
    bar = price_table.get(symbol, {}).get(date)
    return float(bar.open) if bar else None


def next_execution_date(dates: list[str], index: int) -> tuple[int, str] | None:
    if index + 1 >= len(dates):
        return None
    return index + 1, dates[index + 1]


def position_value(position: Position, date: str, price_table: dict[str, dict[str, PriceBar]]) -> float:
    price = close_price(position.symbol, date, price_table)
    if price is None:
        return 0.0
    return position.quantity * price


def portfolio_value(
    cash: float,
    positions: dict[str, Position],
    date: str,
    price_table: dict[str, dict[str, PriceBar]],
) -> tuple[float, float]:
    positions_value = sum(position_value(position, date, price_table) for position in positions.values())
    return cash + positions_value, positions_value


def sell_position(
    cash: float,
    position: Position,
    date: str,
    price: float,
    config: StrategyConfig,
    reason: str,
) -> tuple[float, Trade]:
    fill_price = price * (1 - config.slippageRate)
    value = position.quantity * fill_price
    fee = value * config.feeRate
    trade = Trade(date, position.symbol, "SELL", fill_price, position.quantity, value, fee, reason)
    return cash + value - fee, trade


def buy_position(
    cash: float,
    symbol: str,
    date: str,
    index: int,
    price: float,
    target_value: float,
    config: StrategyConfig,
    reason: str,
) -> tuple[float, Position | None, Trade | None]:
    fill_price = price * (1 + config.slippageRate)
    fee = target_value * config.feeRate
    total_cost = target_value + fee
    if fill_price <= 0 or total_cost > cash:
        target_value = cash / (1 + config.feeRate)
        total_cost = target_value + (target_value * config.feeRate)
    if target_value <= 0 or total_cost > cash or config.boardLotSize <= 0:
        return cash, None, None
    raw_quantity = target_value / fill_price
    quantity = int(raw_quantity // config.boardLotSize) * config.boardLotSize
    if quantity <= 0:
        return cash, None, None
    value = quantity * fill_price
    fee = value * config.feeRate
    if value + fee > cash:
        return cash, None, None
    position = Position(symbol, quantity, fill_price, date, index, fill_price)
    trade = Trade(date, symbol, "BUY", fill_price, quantity, value, fee, reason)
    return cash - value - fee, position, trade


def position_sellable(position: Position, current_index: int, config: StrategyConfig) -> bool:
    return current_index - position.entryIndex >= config.sellSettlementDays


def sell_next_open(
    cash: float,
    position: Position,
    signal_index: int,
    dates: list[str],
    price_table: dict[str, dict[str, PriceBar]],
    config: StrategyConfig,
    reason: str,
) -> tuple[float, Trade | None]:
    execution = next_execution_date(dates, signal_index)
    if not execution:
        return cash, None
    _, execution_date = execution
    execution_price = open_price(position.symbol, execution_date, price_table)
    if execution_price is None:
        return cash, None
    return sell_position(cash, position, execution_date, execution_price, config, reason)


def buy_next_open(
    cash: float,
    symbol: str,
    signal_index: int,
    dates: list[str],
    price_table: dict[str, dict[str, PriceBar]],
    target_value: float,
    config: StrategyConfig,
    reason: str,
) -> tuple[float, Position | None, Trade | None]:
    execution = next_execution_date(dates, signal_index)
    if not execution:
        return cash, None, None
    execution_index, execution_date = execution
    execution_price = open_price(symbol, execution_date, price_table)
    if execution_price is None:
        return cash, None, None
    return buy_position(cash, symbol, execution_date, execution_index, execution_price, target_value, config, reason)


def close_below_sma_exit(symbol: str, price_table: dict[str, dict[str, PriceBar]], dates: list[str], index: int, config: StrategyConfig) -> bool:
    bars = bars_until(price_table[symbol], dates, index)
    closes = [bar.close for bar in bars]
    exit_sma = sma(closes, config.breakoutExitSmaDays)
    return bool(exit_sma is not None and closes[-1] < exit_sma)


def run_momentum_rotation(
    data_dir: Path = DEFAULT_DATA_DIR,
    universe_name: str = "liquid_stocks",
    config: StrategyConfig | None = None,
) -> BacktestResult:
    config = config or StrategyConfig()
    symbols = load_universe_symbols(data_dir, universe_name)
    price_table = build_price_table(symbols, data_dir)
    sector_by_symbol = symbol_sector_map(data_dir)
    dates = sorted_trading_dates(price_table)
    warmup_days = max(config.return6mDays, config.smaSlowDays) + 1

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    breakout_entries_by_week: dict[str, int] = {}

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue

        ranked = rank_symbols(price_table, dates, index, config)
        rank_by_symbol = {symbol: rank for rank, (symbol, _) in enumerate(ranked, start=1)}
        regime = market_regime(price_table, dates, index, config)
        max_positions_today = allowed_positions_for_regime(regime, config)

        for symbol in list(positions):
            position = positions[symbol]
            price = close_price(symbol, date, price_table)
            if price is None:
                continue
            position.peakPrice = max(position.peakPrice, price)
            if not position_sellable(position, index, config):
                continue
            exit_reason = ""
            regime_reason = regime_exit_reason(regime, config)
            trailing_stop = trailing_stop_percent_for_regime(regime, config)
            if regime_reason:
                exit_reason = regime_reason
            elif price <= position.entryPrice * (1 - config.stopLossPercent / 100):
                exit_reason = "stop_loss"
            elif price <= position.peakPrice * (1 - trailing_stop / 100):
                exit_reason = "trailing_stop"
            elif index % config.rebalanceDays == 0 and rank_by_symbol.get(symbol, 999999) > config.exitRank:
                exit_reason = "rank_exit"
            if exit_reason:
                cash, trade = sell_next_open(cash, position, index, dates, price_table, config, exit_reason)
                if trade:
                    trades.append(trade)
                    positions.pop(symbol)

        week = week_key(date)
        weekly_breakout_remaining = max(
            config.maxBreakoutEntriesPerWeek - breakout_entries_by_week.get(week, 0),
            0,
        )
        daily_breakouts = set() if regime == "bear" else breakout_symbols(price_table, dates, index, config)
        if weekly_breakout_remaining <= 0:
            daily_breakouts = set()
        elif config.maxBreakoutEntriesPerWeek > 0:
            daily_breakouts = set(
                symbol for symbol, _ in ranked if symbol in daily_breakouts
            )
        should_try_entries = bool(daily_breakouts) or (index % config.rebalanceDays == 0 and ranked)
        if should_try_entries and ranked:
            if index % config.rebalanceDays == 0:
                strategy = MomentumWithBreakoutStrategy(daily_breakouts)
            else:
                strategy = VolumeBreakoutStrategy(daily_breakouts)
            decision = strategy.select_entries(
                price_table,
                dates,
                index,
                ranked,
                regime,
                max_positions_today,
                weekly_breakout_remaining,
            )
            current_equity, _ = portfolio_value(cash, positions, date, price_table)
            exposure_cap = current_equity * (exposure_percent_for_regime(regime, config) / 100)
            current_positions_value = sum(position_value(position, date, price_table) for position in positions.values())
            available_exposure = max(exposure_cap - current_positions_value, 0.0)
            target_value = exposure_cap / max_positions_today if max_positions_today > 0 else 0
            sector_counts = sector_position_count(positions, sector_by_symbol)
            for symbol in decision.target_symbols:
                if symbol in positions or len(positions) >= max_positions_today:
                    continue
                if available_exposure <= 0:
                    break
                price = close_price(symbol, date, price_table)
                if price is None:
                    continue
                sector = sector_by_symbol.get(symbol, "unknown")
                if sector_counts.get(sector, 0) >= config.maxSectorPositions:
                    continue
                bars = bars_until(price_table[symbol], dates, index)
                if too_extended_from_sma50(bars, config):
                    continue
                reason = decision.entry_reasons[symbol]
                order_value = min(target_value, available_exposure)
                cash, position, trade = buy_next_open(cash, symbol, index, dates, price_table, order_value, config, reason)
                if position and trade:
                    positions[symbol] = position
                    sector_counts[sector] = sector_counts.get(sector, 0) + 1
                    available_exposure = max(available_exposure - trade.value - trade.fee, 0.0)
                    if reason == "volume_breakout":
                        breakout_entries_by_week[week] = breakout_entries_by_week.get(week, 0) + 1
                    trades.append(trade)

        equity, positions_value = portfolio_value(cash, positions, date, price_table)
        equity_curve.append(EquityPoint(date, equity, cash, positions_value))

    metrics = calculate_metrics(equity_curve, trades, config.initialCash)
    return BacktestResult(
        strategy="liquid_momentum_rotation",
        generatedAt=utc_now(),
        config=asdict(config),
        symbols=symbols,
        metrics=metrics,
        equityCurve=[asdict(point) for point in equity_curve],
        trades=[asdict(trade) for trade in trades],
    )


def run_volume_breakout(
    data_dir: Path = DEFAULT_DATA_DIR,
    universe_name: str = "liquid_stocks",
    config: StrategyConfig | None = None,
) -> BacktestResult:
    config = config or StrategyConfig()
    symbols = load_universe_symbols(data_dir, universe_name)
    price_table = build_price_table(symbols, data_dir)
    sector_by_symbol = symbol_sector_map(data_dir)
    dates = sorted_trading_dates(price_table)
    warmup_days = max(config.breakoutLookbackDays, config.breakoutVolumeDays, config.smaSlowDays, config.marketSmaDays) + 1

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    breakout_entries_by_week: dict[str, int] = {}

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue

        ranked = rank_symbols(price_table, dates, index, config)
        regime = market_regime(price_table, dates, index, config)
        max_positions_today = allowed_positions_for_regime(regime, config)

        for symbol in list(positions):
            position = positions[symbol]
            price = close_price(symbol, date, price_table)
            if price is None:
                continue
            position.peakPrice = max(position.peakPrice, price)
            if not position_sellable(position, index, config):
                continue
            exit_reason = ""
            regime_reason = regime_exit_reason(regime, config)
            trailing_stop = trailing_stop_percent_for_regime(regime, config)
            if regime_reason:
                exit_reason = regime_reason
            elif price <= position.entryPrice * (1 - config.stopLossPercent / 100):
                exit_reason = "stop_loss"
            elif price <= position.peakPrice * (1 - trailing_stop / 100):
                exit_reason = "trailing_stop"
            elif close_below_sma_exit(symbol, price_table, dates, index, config):
                exit_reason = "close_below_sma_exit"
            if exit_reason:
                cash, trade = sell_next_open(cash, position, index, dates, price_table, config, exit_reason)
                if trade:
                    trades.append(trade)
                    positions.pop(symbol)

        week = week_key(date)
        weekly_breakout_remaining = max(config.maxBreakoutEntriesPerWeek - breakout_entries_by_week.get(week, 0), 0)
        if regime == "bear" or weekly_breakout_remaining <= 0:
            daily_breakouts: set[str] = set()
        else:
            daily_breakouts = breakout_symbols(price_table, dates, index, config)

        if daily_breakouts and ranked:
            strategy = VolumeBreakoutStrategy(daily_breakouts)
            decision = strategy.select_entries(
                price_table,
                dates,
                index,
                ranked,
                regime,
                max_positions_today,
                weekly_breakout_remaining,
            )
            current_equity, _ = portfolio_value(cash, positions, date, price_table)
            exposure_cap = current_equity * (exposure_percent_for_regime(regime, config) / 100)
            current_positions_value = sum(position_value(position, date, price_table) for position in positions.values())
            available_exposure = max(exposure_cap - current_positions_value, 0.0)
            target_value = exposure_cap / max_positions_today if max_positions_today > 0 else 0
            sector_counts = sector_position_count(positions, sector_by_symbol)

            for symbol in decision.target_symbols:
                if symbol in positions or len(positions) >= max_positions_today:
                    continue
                if available_exposure <= 0:
                    break
                price = close_price(symbol, date, price_table)
                if price is None:
                    continue
                sector = sector_by_symbol.get(symbol, "unknown")
                if sector_counts.get(sector, 0) >= config.maxSectorPositions:
                    continue
                bars = bars_until(price_table[symbol], dates, index)
                if too_extended_from_sma50(bars, config):
                    continue
                order_value = min(target_value, available_exposure)
                cash, position, trade = buy_next_open(
                    cash, symbol, index, dates, price_table, order_value, config, decision.entry_reasons[symbol]
                )
                if position and trade:
                    positions[symbol] = position
                    sector_counts[sector] = sector_counts.get(sector, 0) + 1
                    available_exposure = max(available_exposure - trade.value - trade.fee, 0.0)
                    breakout_entries_by_week[week] = breakout_entries_by_week.get(week, 0) + 1
                    trades.append(trade)

        equity, positions_value = portfolio_value(cash, positions, date, price_table)
        equity_curve.append(EquityPoint(date, equity, cash, positions_value))

    metrics = calculate_metrics(equity_curve, trades, config.initialCash)
    return BacktestResult(
        strategy="volume_breakout",
        generatedAt=utc_now(),
        config=asdict(config),
        symbols=symbols,
        metrics=metrics,
        equityCurve=[asdict(point) for point in equity_curve],
        trades=[asdict(trade) for trade in trades],
    )


def run_pullback_mean_reversion(
    data_dir: Path = DEFAULT_DATA_DIR,
    universe_name: str = "liquid_stocks",
    config: StrategyConfig | None = None,
) -> BacktestResult:
    config = config or StrategyConfig()
    symbols = load_universe_symbols(data_dir, universe_name)
    price_table = build_price_table(symbols, data_dir)
    sector_by_symbol = symbol_sector_map(data_dir)
    dates = sorted_trading_dates(price_table)
    warmup_days = max(
        config.smaSlowDays,
        config.pullbackLookbackDays,
        config.pullbackVolumeDays,
        config.rsiWindow,
        config.marketSmaDays,
    ) + 1

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    entries_by_week: dict[str, int] = {}

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue

        regime = market_regime(price_table, dates, index, config)
        max_positions_today = allowed_positions_for_regime(regime, config)

        for symbol in list(positions):
            position = positions[symbol]
            price = close_price(symbol, date, price_table)
            if price is None:
                continue
            position.peakPrice = max(position.peakPrice, price)
            if not position_sellable(position, index, config):
                continue
            holding_days = index - position.entryIndex
            exit_reason = ""
            regime_reason = regime_exit_reason(regime, config)
            trailing_stop = trailing_stop_percent_for_regime(regime, config)
            if regime_reason:
                exit_reason = regime_reason
            elif price <= position.entryPrice * (1 - config.stopLossPercent / 100):
                exit_reason = "stop_loss"
            elif price <= position.peakPrice * (1 - trailing_stop / 100):
                exit_reason = "trailing_stop"
            elif pullback_exit_signal(bars_until(price_table[symbol], dates, index), config):
                exit_reason = "pullback_exit"
            elif holding_days >= config.pullbackMaxHoldingDays:
                exit_reason = "max_holding_days"
            if exit_reason:
                cash, trade = sell_next_open(cash, position, index, dates, price_table, config, exit_reason)
                if trade:
                    trades.append(trade)
                    positions.pop(symbol)

        week = week_key(date)
        weekly_remaining = max(config.maxPullbackEntriesPerWeek - entries_by_week.get(week, 0), 0)
        candidates = [] if regime == "bear" or weekly_remaining <= 0 else pullback_candidates(price_table, dates, index, config)
        if candidates:
            strategy = PullbackMeanReversionStrategy(candidates)
            decision = strategy.select_entries(
                price_table,
                dates,
                index,
                candidates,
                regime,
                max_positions_today,
                weekly_remaining,
            )
            current_equity, _ = portfolio_value(cash, positions, date, price_table)
            exposure_cap = current_equity * (exposure_percent_for_regime(regime, config) / 100)
            current_positions_value = sum(position_value(position, date, price_table) for position in positions.values())
            available_exposure = max(exposure_cap - current_positions_value, 0.0)
            target_value = exposure_cap / max_positions_today if max_positions_today > 0 else 0
            sector_counts = sector_position_count(positions, sector_by_symbol)

            for symbol in decision.target_symbols:
                if symbol in positions or len(positions) >= max_positions_today:
                    continue
                if available_exposure <= 0:
                    break
                price = close_price(symbol, date, price_table)
                if price is None:
                    continue
                sector = sector_by_symbol.get(symbol, "unknown")
                if sector_counts.get(sector, 0) >= config.maxSectorPositions:
                    continue
                order_value = min(target_value, available_exposure)
                cash, position, trade = buy_next_open(
                    cash, symbol, index, dates, price_table, order_value, config, decision.entry_reasons[symbol]
                )
                if position and trade:
                    positions[symbol] = position
                    sector_counts[sector] = sector_counts.get(sector, 0) + 1
                    available_exposure = max(available_exposure - trade.value - trade.fee, 0.0)
                    entries_by_week[week] = entries_by_week.get(week, 0) + 1
                    trades.append(trade)

        equity, positions_value = portfolio_value(cash, positions, date, price_table)
        equity_curve.append(EquityPoint(date, equity, cash, positions_value))

    metrics = calculate_metrics(equity_curve, trades, config.initialCash)
    return BacktestResult(
        strategy="pullback_mean_reversion",
        generatedAt=utc_now(),
        config=asdict(config),
        symbols=symbols,
        metrics=metrics,
        equityCurve=[asdict(point) for point in equity_curve],
        trades=[asdict(trade) for trade in trades],
    )


def run_sector_momentum(
    data_dir: Path = DEFAULT_DATA_DIR,
    universe_name: str = "liquid_stocks",
    config: StrategyConfig | None = None,
) -> BacktestResult:
    config = config or StrategyConfig()
    symbols = load_universe_symbols(data_dir, universe_name)
    price_table = build_price_table(symbols, data_dir)
    sector_by_symbol = symbol_sector_map(data_dir)
    dates = sorted_trading_dates(price_table)
    warmup_days = max(config.return6mDays, config.smaSlowDays, config.sectorReturn3mDays, config.marketSmaDays) + 1

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue

        ranked = rank_symbols(price_table, dates, index, config)
        rank_by_symbol = {symbol: rank for rank, (symbol, _) in enumerate(ranked, start=1)}
        regime = market_regime(price_table, dates, index, config)
        max_positions_today = allowed_positions_for_regime(regime, config)
        top_sector_names = {
            sector
            for sector, _ in sector_momentum_scores(price_table, dates, index, sector_by_symbol, config)[: config.sectorExitRank]
        }

        for symbol in list(positions):
            position = positions[symbol]
            price = close_price(symbol, date, price_table)
            if price is None:
                continue
            position.peakPrice = max(position.peakPrice, price)
            if not position_sellable(position, index, config):
                continue
            exit_reason = ""
            regime_reason = regime_exit_reason(regime, config)
            trailing_stop = trailing_stop_percent_for_regime(regime, config)
            sector = sector_by_symbol.get(symbol, "unknown")
            if regime_reason:
                exit_reason = regime_reason
            elif price <= position.entryPrice * (1 - config.stopLossPercent / 100):
                exit_reason = "stop_loss"
            elif price <= position.peakPrice * (1 - trailing_stop / 100):
                exit_reason = "trailing_stop"
            elif index % config.rebalanceDays == 0 and sector not in top_sector_names:
                exit_reason = "sector_exit"
            elif index % config.rebalanceDays == 0 and rank_by_symbol.get(symbol, 999999) > config.exitRank:
                exit_reason = "rank_exit"
            if exit_reason:
                cash, trade = sell_next_open(cash, position, index, dates, price_table, config, exit_reason)
                if trade:
                    trades.append(trade)
                    positions.pop(symbol)

        if index % config.rebalanceDays == 0 and regime != "bear":
            candidates = sector_stock_candidates(price_table, dates, index, sector_by_symbol, config)
            strategy = SectorMomentumStrategy(candidates)
            decision = strategy.select_entries(
                price_table,
                dates,
                index,
                candidates,
                regime,
                max_positions_today,
                config.maxPositions,
            )
            current_equity, _ = portfolio_value(cash, positions, date, price_table)
            exposure_cap = current_equity * (exposure_percent_for_regime(regime, config) / 100)
            current_positions_value = sum(position_value(position, date, price_table) for position in positions.values())
            available_exposure = max(exposure_cap - current_positions_value, 0.0)
            target_value = exposure_cap / max_positions_today if max_positions_today > 0 else 0
            sector_counts = sector_position_count(positions, sector_by_symbol)

            for symbol in decision.target_symbols:
                if symbol in positions or len(positions) >= max_positions_today:
                    continue
                if available_exposure <= 0:
                    break
                price = close_price(symbol, date, price_table)
                if price is None:
                    continue
                sector = sector_by_symbol.get(symbol, "unknown")
                if sector_counts.get(sector, 0) >= config.maxSectorPositions:
                    continue
                bars = bars_until(price_table[symbol], dates, index)
                if too_extended_from_sma50(bars, config):
                    continue
                order_value = min(target_value, available_exposure)
                cash, position, trade = buy_next_open(
                    cash, symbol, index, dates, price_table, order_value, config, decision.entry_reasons[symbol]
                )
                if position and trade:
                    positions[symbol] = position
                    sector_counts[sector] = sector_counts.get(sector, 0) + 1
                    available_exposure = max(available_exposure - trade.value - trade.fee, 0.0)
                    trades.append(trade)

        equity, positions_value = portfolio_value(cash, positions, date, price_table)
        equity_curve.append(EquityPoint(date, equity, cash, positions_value))

    metrics = calculate_metrics(equity_curve, trades, config.initialCash)
    return BacktestResult(
        strategy="sector_momentum_rotation",
        generatedAt=utc_now(),
        config=asdict(config),
        symbols=symbols,
        metrics=metrics,
        equityCurve=[asdict(point) for point in equity_curve],
        trades=[asdict(trade) for trade in trades],
    )


def run_market_regime_benchmark(
    data_dir: Path = DEFAULT_DATA_DIR,
    universe_name: str = "liquid_stocks",
    config: StrategyConfig | None = None,
) -> BacktestResult:
    config = config or StrategyConfig()
    symbols = load_universe_symbols(data_dir, universe_name)[: config.benchmarkTopSymbols]
    price_table = build_price_table(symbols, data_dir)
    dates = sorted_trading_dates(price_table)
    warmup_days = max(config.marketSmaDays, config.marketBearSmaDays) + 1

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue
        regime = market_regime(price_table, dates, index, config)
        max_positions_today = allowed_positions_for_regime(regime, config)

        for symbol in list(positions):
            position = positions[symbol]
            price = close_price(symbol, date, price_table)
            if price is None or not position_sellable(position, index, config):
                continue
            exit_reason = regime_exit_reason(regime, config)
            if exit_reason or symbol not in symbols[:max_positions_today]:
                cash, trade = sell_next_open(
                    cash, position, index, dates, price_table, config, exit_reason or "benchmark_rebalance"
                )
                if trade:
                    trades.append(trade)
                    positions.pop(symbol)

        if index % config.rebalanceDays == 0 and regime != "bear":
            strategy = MarketRegimeBenchmarkStrategy(symbols)
            decision = strategy.select_entries(price_table, dates, index, [], regime, max_positions_today, 0)
            current_equity, _ = portfolio_value(cash, positions, date, price_table)
            exposure_cap = current_equity * (exposure_percent_for_regime(regime, config) / 100)
            current_positions_value = sum(position_value(position, date, price_table) for position in positions.values())
            available_exposure = max(exposure_cap - current_positions_value, 0.0)
            target_value = exposure_cap / max_positions_today if max_positions_today > 0 else 0
            for symbol in decision.target_symbols:
                if symbol in positions or len(positions) >= max_positions_today or available_exposure <= 0:
                    continue
                price = close_price(symbol, date, price_table)
                if price is None:
                    continue
                order_value = min(target_value, available_exposure)
                cash, position, trade = buy_next_open(
                    cash, symbol, index, dates, price_table, order_value, config, decision.entry_reasons[symbol]
                )
                if position and trade:
                    positions[symbol] = position
                    available_exposure = max(available_exposure - trade.value - trade.fee, 0.0)
                    trades.append(trade)

        equity, positions_value = portfolio_value(cash, positions, date, price_table)
        equity_curve.append(EquityPoint(date, equity, cash, positions_value))

    metrics = calculate_metrics(equity_curve, trades, config.initialCash)
    return BacktestResult(
        strategy="market_regime_benchmark",
        generatedAt=utc_now(),
        config=asdict(config),
        symbols=symbols,
        metrics=metrics,
        equityCurve=[asdict(point) for point in equity_curve],
        trades=[asdict(trade) for trade in trades],
    )


def run_hybrid_momentum_breakout_sector(
    data_dir: Path = DEFAULT_DATA_DIR,
    universe_name: str = "liquid_stocks",
    config: StrategyConfig | None = None,
) -> BacktestResult:
    config = config or StrategyConfig()
    symbols = load_universe_symbols(data_dir, universe_name)
    price_table = build_price_table(symbols, data_dir)
    sector_by_symbol = symbol_sector_map(data_dir)
    dates = sorted_trading_dates(price_table)
    warmup_days = max(config.return6mDays, config.smaSlowDays, config.sectorReturn3mDays, config.marketSmaDays) + 1

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    breakout_entries_by_week: dict[str, int] = {}

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue
        ranked = rank_symbols(price_table, dates, index, config)
        rank_by_symbol = {symbol: rank for rank, (symbol, _) in enumerate(ranked, start=1)}
        regime = market_regime(price_table, dates, index, config)
        max_positions_today = allowed_positions_for_regime(regime, config)
        sector_candidates = sector_stock_candidates(price_table, dates, index, sector_by_symbol, config)
        current_breakouts = set() if regime == "bear" else breakout_symbols(price_table, dates, index, config)
        top_sectors = {
            sector
            for sector, _ in sector_momentum_scores(price_table, dates, index, sector_by_symbol, config)[: config.sectorExitRank]
        }

        for symbol in list(positions):
            position = positions[symbol]
            price = close_price(symbol, date, price_table)
            if price is None:
                continue
            position.peakPrice = max(position.peakPrice, price)
            if not position_sellable(position, index, config):
                continue
            sector = sector_by_symbol.get(symbol, "unknown")
            exit_reason = ""
            regime_reason = regime_exit_reason(regime, config)
            trailing_stop = trailing_stop_percent_for_regime(regime, config)
            if regime_reason:
                exit_reason = regime_reason
            elif price <= position.entryPrice * (1 - config.stopLossPercent / 100):
                exit_reason = "stop_loss"
            elif price <= position.peakPrice * (1 - trailing_stop / 100):
                exit_reason = "trailing_stop"
            elif index % config.rebalanceDays == 0 and sector not in top_sectors:
                exit_reason = "sector_exit"
            elif index % config.rebalanceDays == 0 and rank_by_symbol.get(symbol, 999999) > config.exitRank:
                exit_reason = "rank_exit"
            if exit_reason:
                cash, trade = sell_next_open(cash, position, index, dates, price_table, config, exit_reason)
                if trade:
                    trades.append(trade)
                    positions.pop(symbol)

        week = week_key(date)
        weekly_breakout_remaining = max(config.maxBreakoutEntriesPerWeek - breakout_entries_by_week.get(week, 0), 0)
        if config.hybridBreakoutRequired:
            hybrid_candidates = [(symbol, score) for symbol, score in sector_candidates if symbol in current_breakouts]
        else:
            hybrid_candidates = sector_candidates
        strategy = HybridMomentumSectorBreakoutStrategy(hybrid_candidates, current_breakouts)
        should_rebalance = index % config.rebalanceDays == 0
        should_enter_breakout = bool(current_breakouts) and weekly_breakout_remaining > 0
        if regime != "bear" and (should_rebalance or should_enter_breakout):
            decision = strategy.select_entries(
                price_table,
                dates,
                index,
                ranked,
                regime,
                max_positions_today,
                weekly_breakout_remaining,
            )
            current_equity, _ = portfolio_value(cash, positions, date, price_table)
            exposure_cap = current_equity * (exposure_percent_for_regime(regime, config) / 100)
            current_positions_value = sum(position_value(position, date, price_table) for position in positions.values())
            available_exposure = max(exposure_cap - current_positions_value, 0.0)
            target_value = exposure_cap / max_positions_today if max_positions_today > 0 else 0
            sector_counts = sector_position_count(positions, sector_by_symbol)
            for symbol in decision.target_symbols:
                if symbol in positions or len(positions) >= max_positions_today or available_exposure <= 0:
                    continue
                price = close_price(symbol, date, price_table)
                if price is None:
                    continue
                sector = sector_by_symbol.get(symbol, "unknown")
                if sector_counts.get(sector, 0) >= config.maxSectorPositions:
                    continue
                bars = bars_until(price_table[symbol], dates, index)
                if too_extended_from_sma50(bars, config):
                    continue
                order_value = min(target_value, available_exposure)
                cash, position, trade = buy_next_open(
                    cash, symbol, index, dates, price_table, order_value, config, decision.entry_reasons[symbol]
                )
                if position and trade:
                    positions[symbol] = position
                    sector_counts[sector] = sector_counts.get(sector, 0) + 1
                    available_exposure = max(available_exposure - trade.value - trade.fee, 0.0)
                    if symbol in current_breakouts:
                        breakout_entries_by_week[week] = breakout_entries_by_week.get(week, 0) + 1
                    trades.append(trade)

        equity, positions_value = portfolio_value(cash, positions, date, price_table)
        equity_curve.append(EquityPoint(date, equity, cash, positions_value))

    metrics = calculate_metrics(equity_curve, trades, config.initialCash)
    return BacktestResult(
        strategy="hybrid_momentum_breakout_sector",
        generatedAt=utc_now(),
        config=asdict(config),
        symbols=symbols,
        metrics=metrics,
        equityCurve=[asdict(point) for point in equity_curve],
        trades=[asdict(trade) for trade in trades],
    )


def calculate_metrics(equity_curve: list[EquityPoint], trades: list[Trade], initial_cash: float) -> dict[str, float | int]:
    if not equity_curve:
        return {
            "initialCash": initial_cash,
            "finalEquity": initial_cash,
            "totalReturnPercent": 0.0,
            "maxDrawdownPercent": 0.0,
            "tradeCount": len(trades),
            "buyCount": 0,
            "sellCount": 0,
        }
    final_equity = equity_curve[-1].equity
    return {
        "initialCash": initial_cash,
        "finalEquity": final_equity,
        "totalReturnPercent": (final_equity / initial_cash - 1) * 100 if initial_cash else 0.0,
        "maxDrawdownPercent": max_drawdown_percent([point.equity for point in equity_curve]),
        "tradeCount": len(trades),
        "buyCount": sum(1 for trade in trades if trade.side == "BUY"),
        "sellCount": sum(1 for trade in trades if trade.side == "SELL"),
    }


def save_backtest_result(result: BacktestResult, data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    path = data_dir / "backtests" / f"{result.strategy}.json"
    write_json(path, asdict(result))
    return path


def save_order_list(result: BacktestResult, data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    path = data_dir / "backtests" / f"{result.strategy}_orders.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["orderId", "date", "symbol", "side", "price", "quantity", "value", "fee", "reason"]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for order_id, trade in enumerate(result.trades, start=1):
            writer.writerow({"orderId": order_id, **trade})
    return path
