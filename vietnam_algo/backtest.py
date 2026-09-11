from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date as date_type
from pathlib import Path

from .config import StrategyConfig, UniverseConfig
from .corporate_actions import (
    CorporateAction,
    build_corporate_action_lookup,
    closes_since_ex_window,
    closes_until_date,
    corporate_action_path,
    corporate_action_risk_type,
    evaluate_post_ca_rebuy_signal,
    evaluate_pre_exit_signal,
    load_corporate_actions,
    non_cash_events_in_trading_window,
    non_cash_events_in_window,
    rebuy_signal_recovery,
)
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
from .universe import (
    build_candidate_price_table,
    build_liquid_universe_as_of,
    load_price_bars,
    load_symbol_metadata,
    load_universe_symbols,
)


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
class ReservedCorporateActionCash:
    amount: float
    exDate: str
    expiryIndex: int
    sector: str


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
    try:
        from .data_investing import load_investing_prices, merge_history_with_prices
    except ImportError:
        load_investing_prices = None
        merge_history_with_prices = None
    for symbol in symbols:
        bars = load_price_bars(symbol, data_dir)
        if load_investing_prices is not None and merge_history_with_prices is not None:
            extra = load_investing_prices(symbol, data_dir)
            if extra:
                bars = merge_history_with_prices(bars, extra)
        if bars:
            table[symbol] = {bar.time: bar for bar in bars}
    return table


def build_corporate_action_table(data_dir: Path, config: StrategyConfig) -> dict[str, dict[str, list[CorporateAction]]]:
    if not config.useCorporateActionGuard:
        return {}
    actions = load_corporate_actions(corporate_action_path(data_dir))
    table: dict[str, dict[str, list[CorporateAction]]] = {}
    for action in actions:
        if not action.exDate or not action.isNonCash:
            continue
        table.setdefault(action.symbol, {}).setdefault(action.exDate, []).append(action)
    return table


def risky_corporate_action_symbols(
    corporate_actions: dict[str, dict[str, list[CorporateAction]]],
    trade_date: str,
    window_days: int,
    high_risk_only: bool = False,
    trading_dates: list[str] | None = None,
    precomputed_ex_index: dict[int, int] | None = None,
) -> set[str]:
    if not corporate_actions:
        return set()
    flattened = [event for events_by_date in corporate_actions.values() for events in events_by_date.values() for event in events]
    if high_risk_only:
        flattened = [event for event in flattened if corporate_action_risk_type(event) == "high"]
    if trading_dates:
        return set(
            non_cash_events_in_trading_window(
                flattened,
                trade_date,
                trading_dates,
                window_days=window_days,
                precomputed_ex_index=precomputed_ex_index,
            ).keys()
        )
    return set(non_cash_events_in_window(flattened, trade_date, window_days=window_days).keys())


def corporate_action_events_due(
    symbol: str,
    date: str,
    corporate_actions: dict[str, dict[str, list[CorporateAction]]],
    window_days: int,
    trading_dates: list[str] | None = None,
    precomputed_ex_index: dict[int, int] | None = None,
) -> list[CorporateAction]:
    events = [event for rows in corporate_actions.get(symbol, {}).values() for event in rows]
    if trading_dates:
        return non_cash_events_in_trading_window(
            events,
            date,
            trading_dates,
            window_days=window_days,
            precomputed_ex_index=precomputed_ex_index,
        ).get(symbol, [])
    return non_cash_events_in_window(events, date, window_days=window_days).get(symbol, [])


def next_corporate_action_ex_date(
    symbol: str,
    date: str,
    corporate_actions: dict[str, dict[str, list[CorporateAction]]],
    config: StrategyConfig,
    trading_dates: list[str] | None = None,
    precomputed_ex_index: dict[int, int] | None = None,
) -> str | None:
    """Return the soonest upcoming ex-date for ``symbol``.

    Uses the union of standard + high-risk windows (configurable via
    ``corporateActionPreExitDays`` and ``corporateActionHighRiskSellWindowDays``)
    so we don't miss ex-dates that triggered exit via either path. Returns
    the soonest ex-date regardless of risk_type — the post-CA rebuy is
    allowed for every forced exit because the adjusted price already
    reflects rights / swap / dilution.
    """
    wider_window = max(config.corporateActionPreExitDays, config.corporateActionHighRiskSellWindowDays)
    events = corporate_action_events_due(
        symbol,
        date,
        corporate_actions,
        wider_window,
        trading_dates,
        precomputed_ex_index=precomputed_ex_index,
    )
    ex_dates = sorted(event.exDate for event in events if event.exDate)
    return ex_dates[0] if ex_dates else None


def corporate_action_exit_due(
    symbol: str,
    date: str,
    corporate_actions: dict[str, dict[str, list[CorporateAction]]],
    config: StrategyConfig,
    trading_dates: list[str] | None = None,
    precomputed_ex_index: dict[int, int] | None = None,
    price_table: dict[str, dict[str, PriceBar]] | None = None,
    date_to_index: dict[str, int] | None = None,
) -> bool:
    """Decide whether the strategy should exit ``symbol`` today because of
    an upcoming corporate action.

    High-risk events (rights, additional issuances, swaps): exit when the
    CA is inside ``corporateActionHighRiskSellWindowDays`` (default 5).
    These are concentrated near the ex-date so a wide window hurts.

    Standard events (cash dividend, stock dividend, stock bonus): the
    behaviour depends on ``corporateActionPreExitSignalMode``:
      - ``fixed_window`` (default): exit on the FIRST day the CA is
        inside ``corporateActionPreExitDays`` (default 5). Legacy
        behaviour.
      - ``drop_from_high``, ``below_ma``, ``momentum_reversal``,
        ``combined``: stay inside the watch window
        ``corporateActionPreExitDays`` and only fire when the configured
        price-action signal triggers. This way the strategy exits when
        the price shows a topping pattern, not just because a calendar
        day passed.

    ``price_table`` is only required for the price-signal modes; pass
    ``None`` for the fixed-window mode.
    """
    high_risk_due = [
        event
        for event in corporate_action_events_due(
            symbol,
            date,
            corporate_actions,
            config.corporateActionHighRiskSellWindowDays,
            trading_dates,
            precomputed_ex_index=precomputed_ex_index,
        )
        if corporate_action_risk_type(event) == "high"
    ]
    if high_risk_due:
        return True
    if not config.corporateActionForceSellStandardEvents:
        return False
    standard_in_window = [
        event
        for event in corporate_action_events_due(
            symbol,
            date,
            corporate_actions,
            config.corporateActionPreExitDays,
            trading_dates,
            precomputed_ex_index=precomputed_ex_index,
        )
        if corporate_action_risk_type(event) == "standard"
    ]
    if not standard_in_window:
        return False
    signal_mode = getattr(config, "corporateActionPreExitSignalMode", "fixed_window")
    if signal_mode == "fixed_window":
        return True
    if price_table is None or date_to_index is None or trading_dates is None:
        return False
    closes_by_symbol = {
        sym: {d: float(bar.close) for d, bar in bars.items()}
        for sym, bars in price_table.items()
    }
    closes_window = closes_until_date(
        symbol, date, trading_dates, closes_by_symbol,
        lookback=max(14, getattr(config, "corporateActionPreExitSignalLookback", 14)),
    )
    triggered, _ = evaluate_pre_exit_signal(closes_window, config)
    return triggered


def corporate_action_exit_rebuy_allowed(
    symbol: str,
    date: str,
    corporate_actions: dict[str, dict[str, list[CorporateAction]]],
    config: StrategyConfig,
    trading_dates: list[str] | None = None,
    precomputed_ex_index: dict[int, int] | None = None,
) -> bool:
    """Decide whether the post-CA rebuy is allowed after a forced exit.

    Bug fix: previously this used only `corporateActionSellWindowDays` (default 1)
    to look for upcoming events. But the exit itself can be triggered by the
    wider `corporateActionHighRiskSellWindowDays` (default 5) when the ex-date
    is more than one trading session away. That meant exits caused by *pure
    standard* events at sessions_until=2..N were considered "no upcoming CA"
    and silently dropped the rebuy.

    Subsequent fix: the previous version also gated on risk_type ("high" events
    were refused rebuy). For adjusted prices, the post-ex price already reflects
    every rights offering / share swap / dilution, so rebuying the same symbol
    at the adjusted price is always fair. We now always allow rebuy when an
    upcoming event is in the union of both windows, regardless of risk_type.
    """
    wider_window = max(config.corporateActionPreExitDays, config.corporateActionHighRiskSellWindowDays)
    events = corporate_action_events_due(
        symbol,
        date,
        corporate_actions,
        wider_window,
        trading_dates,
        precomputed_ex_index=precomputed_ex_index,
    )
    return bool(events)


def average_volume(symbol: str, price_table: dict[str, dict[str, PriceBar]], dates: list[str], index: int, window: int = 20) -> float:
    bars = bars_until(price_table[symbol], dates, index)[-window:]
    if not bars:
        return 0.0
    return sum(bar.volume for bar in bars) / len(bars)


def sorted_trading_dates(price_table: dict[str, dict[str, PriceBar]]) -> list[str]:
    dates: set[str] = set()
    for rows in price_table.values():
        dates.update(rows.keys())
    return sorted(dates)


def resolve_universe_for_date(
    date: str,
    candidate_price_table: dict[str, dict[str, PriceBar]],
    metadata_by_symbol: dict[str, dict[str, object]],
    universe_config: UniverseConfig,
    use_point_in_time_universe: bool,
    cache: dict[str, list[str]],
    fallback_symbols: list[str] | None = None,
) -> list[str]:
    """Return the eligible universe for `date`.

    When `use_point_in_time_universe` is True, compute the universe from data
    available at `date` and cache it. When False, fall back to a static list
    (the previous biased behavior) so legacy results stay reproducible.
    """
    if not use_point_in_time_universe:
        return list(fallback_symbols or list(candidate_price_table.keys()))
    if date in cache:
        return cache[date]
    stocks = build_liquid_universe_as_of(
        candidate_price_table,
        date,
        metadata_by_symbol,
        lookback_days=universe_config.lookbackDays,
        min_avg_value=universe_config.minAvgValue,
        min_avg_volume=universe_config.minAvgVolume,
        min_price=universe_config.minPrice,
        min_history_days=universe_config.minHistoryDays,
        max_symbols=universe_config.maxSymbols,
        max_stale_days=universe_config.maxStaleDays,
    )
    cache[date] = [stock.symbol for stock in stocks]
    return cache[date]


@dataclass
class BacktestContext:
    candidatePriceTable: dict[str, dict[str, PriceBar]]
    metadataBySymbol: dict[str, dict[str, object]]
    priceTable: dict[str, dict[str, PriceBar]]
    symbols: list[str]
    fallbackUniverse: list[str] | None
    universeCache: dict[str, list[str]]


def initialize_backtest_context(
    data_dir: Path,
    universe_name: str,
    config: StrategyConfig,
) -> BacktestContext:
    """Load all data once and prepare helpers for a backtest run."""
    candidate_price_table = build_candidate_price_table(data_dir)
    metadata_by_symbol = load_symbol_metadata(data_dir)
    fallback_universe: list[str] | None = None
    if config.usePointInTimeUniverse:
        initial_symbols = list(candidate_price_table.keys())
        allowed: set[str] | None = None
    else:
        fallback_universe = load_universe_symbols(data_dir, universe_name)
        initial_symbols = fallback_universe
        allowed = set(fallback_universe)
    price_table = {
        symbol: {bar.time: bar for bar in bars}
        for symbol, bars in candidate_price_table.items()
        if allowed is None or symbol in allowed
    }
    return BacktestContext(
        candidatePriceTable=candidate_price_table,
        metadataBySymbol=metadata_by_symbol,
        priceTable=price_table,
        symbols=initial_symbols,
        fallbackUniverse=fallback_universe,
        universeCache={},
    )


def precompute_market_proxy(
    price_table: dict[str, dict[str, PriceBar]],
    dates: list[str],
) -> list[float]:
    """Compute a date-aligned equal-weighted market index.

    For each consecutive pair of dates in `dates`, average the simple return
    of every stock that has a bar on both dates. Compound from 1.0 to produce
    an index level for each date.

    This is the proper replacement for the old broken implementation which
    averaged cumulative-from-inception ratios (mixing stocks of different
    listing ages). It is an equal-weighted proxy because we do not have
    shares-outstanding data for a value-weighted VN-Index equivalent.

    Returns a list of length `len(dates)` of cumulative index levels.
    """
    per_symbol_dates: dict[str, dict[str, PriceBar]] = {
        symbol: rows for symbol, rows in price_table.items() if rows
    }
    levels: list[float] = [1.0]
    for i in range(1, len(dates)):
        prev_date = dates[i - 1]
        cur_date = dates[i]
        returns: list[float] = []
        for rows in per_symbol_dates.values():
            prev_bar = rows.get(prev_date)
            cur_bar = rows.get(cur_date)
            if prev_bar is None or cur_bar is None:
                continue
            if prev_bar.close <= 0 or cur_bar.close <= 0:
                continue
            returns.append(float(cur_bar.close) / float(prev_bar.close) - 1.0)
        if returns:
            avg_return = sum(returns) / len(returns)
            levels.append(levels[-1] * (1.0 + avg_return))
        else:
            levels.append(levels[-1])
    return levels


def market_proxy_closes(
    price_table: dict[str, dict[str, PriceBar]],
    dates: list[str],
    index: int,
    precomputed: list[float] | None = None,
) -> list[float]:
    if precomputed is None:
        precomputed = precompute_market_proxy(price_table, dates)
    return list(precomputed[: index + 1])


def market_regime(
    price_table: dict[str, dict[str, PriceBar]],
    dates: list[str],
    index: int,
    config: StrategyConfig,
    precomputed: list[float] | None = None,
) -> str:
    closes = market_proxy_closes(price_table, dates, index, precomputed=precomputed)
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


def compute_realized_vol(equity_values: list[float], window: int) -> float:
    """Annualized realized portfolio volatility from a series of equity values.

    Returns 0.0 when there is insufficient history to form a meaningful
    rolling window (so callers can treat it as "no scaling").
    """
    if window < 2 or len(equity_values) < window + 1:
        return 0.0
    recent = equity_values[-(window + 1):]
    returns: list[float] = []
    for i in range(1, len(recent)):
        prev, cur = recent[i - 1], recent[i]
        if prev > 0 and cur > 0:
            returns.append(cur / prev - 1.0)
    if len(returns) < 2:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    daily_vol = variance ** 0.5
    return daily_vol * (252 ** 0.5)


def compute_vol_scalar(realized_vol: float, target_vol: float, max_leverage: float) -> float:
    """Vol-targeting scale factor.

    Returns a scalar to multiply position sizes by. Caps leverage at
    `max_leverage`, floors at 0. If inputs are non-positive, returns 1.0
    (no scaling) so the caller falls back to the regime-based exposure cap.
    """
    if target_vol <= 0 or realized_vol <= 0 or max_leverage <= 0:
        return 1.0
    raw = target_vol / realized_vol
    if raw < 0:
        return 0.0
    if raw > max_leverage:
        return max_leverage
    return raw


def compute_exposure_cap(
    current_equity: float,
    regime: str,
    config: StrategyConfig,
    equity_curve: list[EquityPoint],
) -> tuple[float, float]:
    """Compute the exposure cap with optional vol targeting.

    Returns (exposure_cap, vol_scalar). vol_scalar is reported alongside so
    callers can log it.
    """
    base_pct = exposure_percent_for_regime(regime, config) / 100.0
    vol_scalar = 1.0
    if config.useVolTargeting:
        equity_values = [point.equity for point in equity_curve]
        realized_vol = compute_realized_vol(equity_values, config.volWindowDays)
        vol_scalar = compute_vol_scalar(
            realized_vol,
            config.volTargetAnnualized,
            config.volMaxLeverage,
        )
    return current_equity * base_pct * vol_scalar, vol_scalar


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
    bars = price_table.get(symbol, {})
    bar = bars.get(date)
    if bar is not None:
        return float(bar.close)
    # Forward-fill: walk backwards through sorted dates to find the most
    # recent available close. This is critical for DNSE-sourced prices
    # where small-cap tickers have very sparse daily coverage.
    for d, b in sorted(bars.items(), reverse=True):
        if d <= date:
            return float(b.close)
    return None


def open_price(symbol: str, date: str, price_table: dict[str, dict[str, PriceBar]]) -> float | None:
    bars = price_table.get(symbol, {})
    bar = bars.get(date)
    if bar is not None:
        return float(bar.open)
    for d, b in sorted(bars.items(), reverse=True):
        if d <= date:
            return float(b.open)
    return None


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
    ctx = initialize_backtest_context(data_dir, universe_name, config)
    price_table = ctx.priceTable
    corporate_actions = build_corporate_action_table(data_dir, config)
    sector_by_symbol = symbol_sector_map(data_dir)
    dates = sorted_trading_dates(price_table)
    market_proxy = precompute_market_proxy(price_table, dates)
    warmup_days = max(config.return6mDays, config.smaSlowDays) + 1

    # Precompute CA lookup once so per-day trading-window checks are O(1) per action
    if corporate_actions and config.useCorporateActionGuard:
        all_ca = [
            event
            for by_date in corporate_actions.values()
            for events in by_date.values()
            for event in events
        ]
        _, ca_ex_index = build_corporate_action_lookup(all_ca, dates)
    else:
        ca_ex_index = {}

    date_to_index: dict[str, int] = {d: i for i, d in enumerate(dates)}

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    breakout_entries_by_week: dict[str, int] = {}
    pending_corporate_action_rebuys: dict[str, tuple[float, str]] = {}
    reserved_corporate_action_cash: dict[str, ReservedCorporateActionCash] = {}

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue

        current_universe_list = resolve_universe_for_date(
            date,
            ctx.candidatePriceTable,
            ctx.metadataBySymbol,
            config.universe,
            config.usePointInTimeUniverse,
            ctx.universeCache,
            fallback_symbols=ctx.fallbackUniverse,
        )
        current_universe = set(current_universe_list)

        ranked = rank_symbols(price_table, dates, index, config, universe=current_universe, market_levels=market_proxy[: index + 1])
        rank_by_symbol = {symbol: rank for rank, (symbol, _) in enumerate(ranked, start=1)}
        regime = market_regime(price_table, dates, index, config, precomputed=market_proxy)
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
            elif corporate_action_exit_due(
                symbol, date, corporate_actions, config, dates,
                precomputed_ex_index=ca_ex_index,
                price_table=price_table,
                date_to_index=date_to_index,
            ):
                exit_reason = "corporate_action_exit"
            elif price <= position.entryPrice * (1 - config.stopLossPercent / 100):
                exit_reason = "stop_loss"
            elif price <= position.peakPrice * (1 - trailing_stop / 100):
                exit_reason = "trailing_stop"
            elif index % config.rebalanceDays == 0 and rank_by_symbol.get(symbol, 999999) > config.exitRank:
                exit_reason = "rank_exit"
            if exit_reason:
                rebuy_ex_date = None
                if exit_reason == "corporate_action_exit" and corporate_action_exit_rebuy_allowed(symbol, date, corporate_actions, config, dates, precomputed_ex_index=ca_ex_index):
                    rebuy_ex_date = next_corporate_action_ex_date(symbol, date, corporate_actions, config, dates, precomputed_ex_index=ca_ex_index)
                cash, trade = sell_next_open(cash, position, index, dates, price_table, config, exit_reason)
                if trade:
                    trades.append(trade)
                    positions.pop(symbol)
                    if config.corporateActionRebuyAfterExDate and rebuy_ex_date:
                        exit_event = corporate_action_events_due(
                            symbol, date, corporate_actions,
                            max(config.corporateActionSellWindowDays, config.corporateActionHighRiskSellWindowDays),
                            dates, precomputed_ex_index=ca_ex_index,
                        )
                        has_high_risk = any(
                            corporate_action_risk_type(event) == "high"
                            for event in (exit_event or [])
                        )
                        pending_corporate_action_rebuys[symbol] = (
                            trade.value - trade.fee,
                            rebuy_ex_date,
                            has_high_risk,
                        )
                        if config.corporateActionReserveCashForRebuy:
                            expiry_index = min(index + config.corporateActionReserveExpiryDays, len(dates) - 1)
                            reserved_corporate_action_cash[symbol] = ReservedCorporateActionCash(
                                trade.value - trade.fee,
                                rebuy_ex_date,
                                expiry_index,
                                sector_by_symbol.get(symbol, "unknown"),
                            )

        week = week_key(date)
        weekly_breakout_remaining = max(
            config.maxBreakoutEntriesPerWeek - breakout_entries_by_week.get(week, 0),
            0,
        )
        daily_breakouts = (
            set() if regime == "bear" else breakout_symbols(price_table, dates, index, config, universe=current_universe)
        )
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
            standard_blocked_buys = risky_corporate_action_symbols(
                corporate_actions,
                date,
                config.corporateActionBuyWindowDays,
                trading_dates=dates,
                precomputed_ex_index=ca_ex_index,
            )
            high_risk_blocked_buys = risky_corporate_action_symbols(
                corporate_actions,
                date,
                config.corporateActionHighRiskBuyWindowDays,
                high_risk_only=True,
                trading_dates=dates,
                precomputed_ex_index=ca_ex_index,
            )
            blocked_buys = standard_blocked_buys | high_risk_blocked_buys
            exposure_cap, _vol_scalar = compute_exposure_cap(current_equity, regime, config, equity_curve)
            current_positions_value = sum(position_value(position, date, price_table) for position in positions.values())
            available_exposure = max(exposure_cap - current_positions_value, 0.0)
            target_value = exposure_cap / max_positions_today if max_positions_today > 0 else 0
            sector_counts = sector_position_count(positions, sector_by_symbol)
            active_reserves = reserved_corporate_action_cash if config.corporateActionReserveCashForRebuy else {}
            reserved_sector_counts: dict[str, int] = {}
            for reserve in active_reserves.values():
                reserved_sector_counts[reserve.sector] = reserved_sector_counts.get(reserve.sector, 0) + 1
            normal_max_positions_today = max(max_positions_today - len(active_reserves), 0)
            should_try_ca_rebuy = config.corporateActionRebuyAfterExDate and pending_corporate_action_rebuys and (
                config.corporateActionRebuyAsAdjustment or index % config.rebalanceDays == 0
            )
            if should_try_ca_rebuy:
                rank_by_symbol = {symbol: rank for rank, (symbol, _) in enumerate(ranked, start=1)}
                for symbol, pending_entry in list(pending_corporate_action_rebuys.items()):
                    if len(pending_entry) == 3:
                        target_rebuy_value, ex_date, has_high_risk = pending_entry
                    else:  # backward compat for any pickled results from earlier runs
                        target_rebuy_value, ex_date = pending_entry
                        has_high_risk = False
                    reserve = reserved_corporate_action_cash.get(symbol)
                    if reserve and index > reserve.expiryIndex:
                        reserved_corporate_action_cash.pop(symbol, None)
                        pending_corporate_action_rebuys.pop(symbol, None)
                        continue
                    if date <= ex_date or symbol in positions or symbol in blocked_buys or len(positions) >= max_positions_today:
                        continue
                    ex_idx = date_to_index.get(ex_date, index)
                    signal_mode = config.corporateActionRebuySignalMode
                    if signal_mode != "fixed_delay":
                        # Signal-based rebuy. The signal can only fire on day
                        # >= ex_idx + min_days (excludes the ex-date itself if
                        # min_days=0 the day-after ex is still allowed). If
                        # the signal never fires by max_days, force rebuy.
                        min_days = max(0, config.corporateActionRebuySignalMinDays)
                        max_days = max(min_days + 1, config.corporateActionRebuySignalMaxDays)
                        if index < ex_idx + min_days:
                            continue
                        if index >= ex_idx + max_days:
                            pass  # fall through to rebuy
                        else:
                            closes_by_symbol = {
                                sym: {d: float(bar.close) for d, bar in bars.items()}
                                for sym, bars in price_table.items()
                            }
                            closes_window = closes_since_ex_window(
                                symbol, ex_date, dates, closes_by_symbol,
                            )
                            triggered, _reason = evaluate_post_ca_rebuy_signal(
                                closes_window, config,
                            )
                            if not triggered:
                                continue
                    else:
                        # Legacy fixed-delay behaviour.
                        delay_days = config.corporateActionRebuyDelayDays
                        if delay_days > 0:
                            if config.corporateActionRebuyDelayHighRiskOnly and not has_high_risk:
                                pass  # no delay for standard events
                            else:
                                earliest_rebuy_idx = ex_idx + delay_days
                                if index < earliest_rebuy_idx:
                                    continue
                    # Note: symbol is allowed to be outside current_universe.
                    # A forced CA exit always queues a rebuy that bypasses the
                    # liquid-universe filter, because the post-CA adjusted price
                    # is fair regardless of where the symbol sits in the
                    # liquidity ranking. The reserve expiry caps retry windows.
                    if not config.corporateActionRebuyAsAdjustment:
                        max_rebuy_rank = 0
                        if regime == "bull":
                            max_rebuy_rank = config.corporateActionRebuyBullMaxRank
                        elif regime == "neutral":
                            max_rebuy_rank = config.corporateActionRebuyNeutralMaxRank
                        if max_rebuy_rank <= 0 or rank_by_symbol.get(symbol, 999999) > max_rebuy_rank:
                            pending_corporate_action_rebuys.pop(symbol, None)
                            continue
                    price = close_price(symbol, date, price_table)
                    if price is None:
                        continue
                    sector = sector_by_symbol.get(symbol, "unknown")
                    if not reserve and sector_counts.get(sector, 0) >= config.maxSectorPositions:
                        continue
                    bars = bars_until(price_table[symbol], dates, index)
                    if not config.corporateActionRebuyAsAdjustment:
                        closes = [bar.close for bar in bars]
                        if not closes or sma(closes, config.smaFastDays) is None or sma(closes, config.smaSlowDays) is None:
                            continue
                        if closes[-1] <= sma(closes, config.smaFastDays) or closes[-1] <= sma(closes, config.smaSlowDays):
                            continue
                        if average_volume(symbol, price_table, dates, index) < config.corporateActionRebuyMinAvgVolume:
                            continue
                        if too_extended_from_sma50(bars, config):
                            continue
                    reserved_amount = reserve.amount if reserve else target_rebuy_value
                    order_value = min(reserved_amount, available_exposure, cash / (1 + config.feeRate))
                    cash, position, trade = buy_next_open(
                        cash,
                        symbol,
                        index,
                        dates,
                        price_table,
                        order_value,
                        config,
                        "corporate_action_rebuy",
                    )
                    if position and trade:
                        positions[symbol] = position
                        sector_counts[sector] = sector_counts.get(sector, 0) + 1
                        available_exposure = max(available_exposure - trade.value - trade.fee, 0.0)
                        trades.append(trade)
                        pending_corporate_action_rebuys.pop(symbol, None)
                        reserved_corporate_action_cash.pop(symbol, None)
            reserved_total = sum(reserve.amount for reserve in reserved_corporate_action_cash.values()) if config.corporateActionReserveCashForRebuy else 0.0
            strategy_cash_cap = max(cash - reserved_total, 0.0)
            available_exposure = min(available_exposure, strategy_cash_cap)
            for symbol in decision.target_symbols:
                if symbol in positions or len(positions) >= normal_max_positions_today:
                    continue
                if symbol in blocked_buys:
                    continue
                if available_exposure <= 0:
                    break
                price = close_price(symbol, date, price_table)
                if price is None:
                    continue
                sector = sector_by_symbol.get(symbol, "unknown")
                if sector_counts.get(sector, 0) + reserved_sector_counts.get(sector, 0) >= config.maxSectorPositions:
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
        symbols=ctx.symbols,
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
    ctx = initialize_backtest_context(data_dir, universe_name, config)
    price_table = ctx.priceTable
    sector_by_symbol = symbol_sector_map(data_dir)
    dates = sorted_trading_dates(price_table)
    market_proxy = precompute_market_proxy(price_table, dates)
    warmup_days = max(config.breakoutLookbackDays, config.breakoutVolumeDays, config.smaSlowDays, config.marketSmaDays) + 1

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    breakout_entries_by_week: dict[str, int] = {}

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue

        current_universe_list = resolve_universe_for_date(
            date,
            ctx.candidatePriceTable,
            ctx.metadataBySymbol,
            config.universe,
            config.usePointInTimeUniverse,
            ctx.universeCache,
            fallback_symbols=ctx.fallbackUniverse,
        )
        current_universe = set(current_universe_list)

        ranked = rank_symbols(price_table, dates, index, config, universe=current_universe)
        regime = market_regime(price_table, dates, index, config, precomputed=market_proxy)
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
            daily_breakouts = breakout_symbols(price_table, dates, index, config, universe=current_universe)

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
            exposure_cap, _vol_scalar = compute_exposure_cap(current_equity, regime, config, equity_curve)
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
        symbols=ctx.symbols,
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
    ctx = initialize_backtest_context(data_dir, universe_name, config)
    price_table = ctx.priceTable
    sector_by_symbol = symbol_sector_map(data_dir)
    dates = sorted_trading_dates(price_table)
    market_proxy = precompute_market_proxy(price_table, dates)
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

        current_universe_list = resolve_universe_for_date(
            date,
            ctx.candidatePriceTable,
            ctx.metadataBySymbol,
            config.universe,
            config.usePointInTimeUniverse,
            ctx.universeCache,
            fallback_symbols=ctx.fallbackUniverse,
        )
        current_universe = set(current_universe_list)

        regime = market_regime(price_table, dates, index, config, precomputed=market_proxy)
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
        candidates = [] if regime == "bear" or weekly_remaining <= 0 else pullback_candidates(price_table, dates, index, config, universe=current_universe)
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
            exposure_cap, _vol_scalar = compute_exposure_cap(current_equity, regime, config, equity_curve)
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
        symbols=ctx.symbols,
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
    ctx = initialize_backtest_context(data_dir, universe_name, config)
    price_table = ctx.priceTable
    sector_by_symbol = symbol_sector_map(data_dir)
    dates = sorted_trading_dates(price_table)
    market_proxy = precompute_market_proxy(price_table, dates)
    warmup_days = max(config.return6mDays, config.smaSlowDays, config.sectorReturn3mDays, config.marketSmaDays) + 1

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue

        current_universe_list = resolve_universe_for_date(
            date,
            ctx.candidatePriceTable,
            ctx.metadataBySymbol,
            config.universe,
            config.usePointInTimeUniverse,
            ctx.universeCache,
            fallback_symbols=ctx.fallbackUniverse,
        )
        current_universe = set(current_universe_list)

        ranked = rank_symbols(price_table, dates, index, config, universe=current_universe)
        rank_by_symbol = {symbol: rank for rank, (symbol, _) in enumerate(ranked, start=1)}
        regime = market_regime(price_table, dates, index, config, precomputed=market_proxy)
        max_positions_today = allowed_positions_for_regime(regime, config)
        top_sector_names = {
            sector
            for sector, _ in sector_momentum_scores(price_table, dates, index, sector_by_symbol, config, universe=current_universe)[: config.sectorExitRank]
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
            candidates = sector_stock_candidates(price_table, dates, index, sector_by_symbol, config, universe=current_universe)
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
            exposure_cap, _vol_scalar = compute_exposure_cap(current_equity, regime, config, equity_curve)
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
        symbols=ctx.symbols,
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
    ctx = initialize_backtest_context(data_dir, universe_name, config)
    price_table = ctx.priceTable
    dates = sorted_trading_dates(price_table)
    market_proxy = precompute_market_proxy(price_table, dates)
    warmup_days = max(config.marketSmaDays, config.marketBearSmaDays) + 1

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue
        current_universe_list = resolve_universe_for_date(
            date,
            ctx.candidatePriceTable,
            ctx.metadataBySymbol,
            config.universe,
            config.usePointInTimeUniverse,
            ctx.universeCache,
            fallback_symbols=ctx.fallbackUniverse,
        )
        current_universe = set(current_universe_list)
        symbols = list(current_universe)[: config.benchmarkTopSymbols]
        regime = market_regime(price_table, dates, index, config, precomputed=market_proxy)
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
            exposure_cap, _vol_scalar = compute_exposure_cap(current_equity, regime, config, equity_curve)
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
        symbols=ctx.symbols,
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
    ctx = initialize_backtest_context(data_dir, universe_name, config)
    price_table = ctx.priceTable
    sector_by_symbol = symbol_sector_map(data_dir)
    dates = sorted_trading_dates(price_table)
    market_proxy = precompute_market_proxy(price_table, dates)
    warmup_days = max(config.return6mDays, config.smaSlowDays, config.sectorReturn3mDays, config.marketSmaDays) + 1

    cash = config.initialCash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    breakout_entries_by_week: dict[str, int] = {}

    for index, date in enumerate(dates):
        if index < warmup_days:
            continue
        current_universe_list = resolve_universe_for_date(
            date,
            ctx.candidatePriceTable,
            ctx.metadataBySymbol,
            config.universe,
            config.usePointInTimeUniverse,
            ctx.universeCache,
            fallback_symbols=ctx.fallbackUniverse,
        )
        current_universe = set(current_universe_list)
        ranked = rank_symbols(price_table, dates, index, config, universe=current_universe)
        rank_by_symbol = {symbol: rank for rank, (symbol, _) in enumerate(ranked, start=1)}
        regime = market_regime(price_table, dates, index, config, precomputed=market_proxy)
        max_positions_today = allowed_positions_for_regime(regime, config)
        sector_candidates = sector_stock_candidates(price_table, dates, index, sector_by_symbol, config, universe=current_universe)
        current_breakouts = set() if regime == "bear" else breakout_symbols(price_table, dates, index, config, universe=current_universe)
        top_sectors = {
            sector
            for sector, _ in sector_momentum_scores(price_table, dates, index, sector_by_symbol, config, universe=current_universe)[: config.sectorExitRank]
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
            exposure_cap, _vol_scalar = compute_exposure_cap(current_equity, regime, config, equity_curve)
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
        symbols=ctx.symbols,
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


def _initial_cash_from_result(path: Path) -> float | None:
    """Pull the initial cash used by a saved backtest.

    Reads the first row of ``equityCurve`` from the saved JSON. The
    dry-run state uses this to scale its initial NAV so the per-order
    qty from a 1B-VND backtest doesn't get clipped as below_board_lot
    on a 100M-VND dry-run state.
    """
    try:
        import json as _json
        payload = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ec = payload.get("equityCurve") or []
    if not ec:
        return None
    first = ec[0] or {}
    cash = first.get("cash")
    try:
        return float(cash) if cash else None
    except (TypeError, ValueError):
        return None
    return path
