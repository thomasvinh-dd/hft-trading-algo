from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .data import DEFAULT_DATA_DIR, PriceBar, read_json, utc_now, write_json


DEFAULT_EXCHANGES = {"HOSE", "HNX"}
DEFAULT_EXCLUDED_PREFIXES = ("FUE", "E1", "FU")


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
