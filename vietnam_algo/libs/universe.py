from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date as date_type
from pathlib import Path

from .data import DEFAULT_DATA_DIR, PriceBar, read_json, utc_now, write_json


DEFAULT_EXCHANGES = {"HOSE", "HNX", "STO"}
DEFAULT_EXCLUDED_PREFIXES = ("FUE", "E1", "FU")
DEFAULT_MAX_STALE_DAYS = 30


@dataclass(frozen=True)
class LiquidStock:
    symbol: str
    companyName: str | None
    exchangeCode: str | None
    sectorName: str | None
    lastClose: float
    avgVolume20: float
    avgValue20: float
    historyDays: int


def load_symbol_metadata(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, dict[str, object]]:
    payload = read_json(data_dir / "symbols.json")
    return {str(item["ticker"]).upper(): item for item in payload.get("items", []) if item.get("ticker")}


def load_price_bars(symbol: str, data_dir: Path = DEFAULT_DATA_DIR) -> list[PriceBar]:
    payload = read_json(data_dir / "prices" / f"{symbol.upper()}.json")
    bars: list[PriceBar] = []
    for item in payload.get("items", []):
        try:
            bars.append(
                PriceBar(
                    time=str(item["time"]),
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    volume=float(item.get("volume") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(bars, key=lambda bar: bar.time)


def load_symbol_closes(symbol: str, data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, float]:
    """Return {date_str: close_float} for ``symbol``.

    Convenience wrapper over :func:`load_price_bars` for callers (like the
    live rebuy-signal evaluator) that only need closes, not full OHLCV bars.
    Returns an empty dict if the symbol has no saved price data.
    """
    try:
        bars = load_price_bars(symbol, data_dir)
    except (FileNotFoundError, OSError):
        return {}
    return {bar.time: float(bar.close) for bar in bars}


def build_symbol_sector_map(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, str]:
    """Map symbol -> sector for every symbol in the cached metadata.

    Live-side equivalent of :func:`backtest.symbol_sector_map`; kept here so
    the trading engine can read sector caps without pulling in the entire
    backtest module.
    """
    try:
        metadata = load_symbol_metadata(data_dir)
    except (FileNotFoundError, OSError):
        return {}
    return {symbol: str(item.get("sectorName") or "unknown") for symbol, item in metadata.items()}


def avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def is_excluded_symbol(symbol: str, excluded_prefixes: tuple[str, ...] = DEFAULT_EXCLUDED_PREFIXES) -> bool:
    upper_symbol = symbol.upper()
    return any(upper_symbol.startswith(prefix) for prefix in excluded_prefixes)


def build_liquid_universe(
    data_dir: Path = DEFAULT_DATA_DIR,
    lookback_days: int = 20,
    min_avg_value: float = 20_000_000_000,
    min_avg_volume: float = 300_000,
    min_price: float = 10_000,
    min_history_days: int = 252,
    max_symbols: int = 100,
    exchanges: set[str] | None = None,
) -> list[LiquidStock]:
    metadata = load_symbol_metadata(data_dir)
    allowed_exchanges = {exchange.upper() for exchange in (exchanges or DEFAULT_EXCHANGES)}
    candidates: list[LiquidStock] = []

    for symbol, item in metadata.items():
        exchange = str(item.get("exchangeCode") or "").upper()
        if exchange not in allowed_exchanges or is_excluded_symbol(symbol):
            continue
        try:
            bars = load_price_bars(symbol, data_dir)
        except FileNotFoundError:
            continue
        if len(bars) < min_history_days:
            continue
        recent = bars[-lookback_days:]
        last_close = float(bars[-1].close)
        avg_volume = avg([float(bar.volume) for bar in recent])
        avg_value = avg([float(bar.close) * float(bar.volume) for bar in recent])
        if last_close < min_price or avg_volume < min_avg_volume or avg_value < min_avg_value:
            continue
        candidates.append(
            LiquidStock(
                symbol=symbol,
                companyName=str(item.get("companyName")) if item.get("companyName") is not None else None,
                exchangeCode=str(item.get("exchangeCode")) if item.get("exchangeCode") is not None else None,
                sectorName=str(item.get("sectorName")) if item.get("sectorName") is not None else None,
                lastClose=last_close,
                avgVolume20=avg_volume,
                avgValue20=avg_value,
                historyDays=len(bars),
            )
        )

    candidates.sort(key=lambda stock: stock.avgValue20, reverse=True)
    return candidates[:max_symbols] if max_symbols > 0 else candidates


def build_candidate_price_table(
    data_dir: Path = DEFAULT_DATA_DIR,
    exchanges: set[str] | None = None,
    excluded_prefixes: tuple[str, ...] = DEFAULT_EXCLUDED_PREFIXES,
) -> dict[str, list[PriceBar]]:
    """Load price bars for every symbol that could be a candidate (right exchange, not excluded).

    This is meant to be called once per backtest so the point-in-time universe
    can be computed repeatedly without re-reading price files from disk.

    Returns symbol -> sorted bar list. The list format is faster for repeated
    point-in-time slicing than dict-of-dates.
    """
    metadata = load_symbol_metadata(data_dir)
    allowed_exchanges = {exchange.upper() for exchange in (exchanges or DEFAULT_EXCHANGES)}
    table: dict[str, list[PriceBar]] = {}
    for symbol, item in metadata.items():
        exchange = str(item.get("exchangeCode") or "").upper()
        if exchange not in allowed_exchanges:
            continue
        if is_excluded_symbol(symbol, excluded_prefixes):
            continue
        try:
            bars = load_price_bars(symbol, data_dir)
        except FileNotFoundError:
            continue
        if bars:
            table[symbol] = bars
    return table


def bars_until_list(bars: list[PriceBar], as_of_date: str) -> list[PriceBar]:
    """Return bars whose time is <= `as_of_date`. Assumes `bars` is sorted by time."""
    result: list[PriceBar] = []
    for bar in bars:
        if bar.time > as_of_date:
            break
        result.append(bar)
    return result


def build_liquid_universe_as_of(
    candidate_price_table: dict[str, list[PriceBar]],
    as_of_date: str,
    metadata_by_symbol: dict[str, dict[str, object]] | None = None,
    lookback_days: int = 20,
    min_avg_value: float = 20_000_000_000,
    min_avg_volume: float = 300_000,
    min_price: float = 10_000,
    min_history_days: int = 252,
    max_symbols: int = 100,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
) -> list[LiquidStock]:
    """Build the liquid universe using only information available up to `as_of_date`.

    Avoids look-ahead bias by:
      - using only bars with time <= as_of_date
      - excluding symbols whose most recent bar is older than max_stale_days
        (they are effectively delisted/suspended from the strategy's perspective)
    """
    candidates: list[LiquidStock] = []
    as_of = date_type.fromisoformat(as_of_date)

    for symbol, bars in candidate_price_table.items():
        hist = bars_until_list(bars, as_of_date)
        if len(hist) < min_history_days:
            continue
        last_bar = hist[-1]
        try:
            last_close_dt = date_type.fromisoformat(last_bar.time)
        except ValueError:
            continue
        if (as_of - last_close_dt).days > max_stale_days:
            continue
        recent = hist[-lookback_days:]
        last_close = float(last_bar.close)
        avg_volume = avg([float(bar.volume) for bar in recent])
        avg_value = avg([float(bar.close) * float(bar.volume) for bar in recent])
        if last_close < min_price or avg_volume < min_avg_volume or avg_value < min_avg_value:
            continue
        meta = (metadata_by_symbol or {}).get(symbol, {})
        candidates.append(
            LiquidStock(
                symbol=symbol,
                companyName=str(meta.get("companyName")) if meta.get("companyName") is not None else None,
                exchangeCode=str(meta.get("exchangeCode")) if meta.get("exchangeCode") is not None else None,
                sectorName=str(meta.get("sectorName")) if meta.get("sectorName") is not None else None,
                lastClose=last_close,
                avgVolume20=avg_volume,
                avgValue20=avg_value,
                historyDays=len(hist),
            )
        )

    candidates.sort(key=lambda stock: stock.avgValue20, reverse=True)
    return candidates[:max_symbols] if max_symbols > 0 else candidates


def save_liquid_universe(universe: list[LiquidStock], data_dir: Path = DEFAULT_DATA_DIR, name: str = "liquid_stocks") -> Path:
    path = data_dir / "universe" / f"{name}.json"
    write_json(
        path,
        {
            "generatedAt": utc_now(),
            "count": len(universe),
            "items": [asdict(stock) for stock in universe],
        },
    )
    return path


def load_universe_symbols(data_dir: Path = DEFAULT_DATA_DIR, name: str = "liquid_stocks") -> list[str]:
    payload = read_json(data_dir / "universe" / f"{name}.json")
    return [str(item["symbol"]).upper() for item in payload.get("items", []) if item.get("symbol")]
