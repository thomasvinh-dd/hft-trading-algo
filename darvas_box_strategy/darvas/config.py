"""Tunable parameters for the Darvas Box + AutoFin-tiered-exits strategy."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BaseConfig:
    """Detect the most recent tight consolidation rectangle."""

    min_days: int = 5
    max_days: int = 30
    max_range_pct: float = 0.030


@dataclass(frozen=True)
class BreakoutConfig:
    """Trigger a buy when today's close breaks the base top on volume.

    Sweep on the 84-ticker liquid universe, 2019-08 to 2026-08 (portfolio-
    managed, MaxC=10, Cool=5, ATR trail mult=1.5, MTM at close):

        vol_mult  hi_conv   Trades  Ret      Sharpe  MaxDD
        1.50      2.50      8,351   +179.2%  +1.21   -25.1%   (baseline)
        1.80      2.80      5,458   +198.4%  +1.39   -21.0%
        2.00      3.00      4,231   +113.1%  +1.01   -17.9%
        3.00      3.50      1,395   +228.4%  +1.58    -9.5%   ← default
        3.00      4.00      1,395   +201.0%  +1.43    -9.5%
        3.50      4.00        899   +206.9%  +1.41    -9.9%
        5.00      6.00        327    +77.9%  +0.91    -8.5%

    Volume multiplier is the dominant parameter. Higher values filter
    out weak breakouts and dramatically improve Sharpe + drawdown.
    Default 3.0 picks the highest-Sharpe point with sub-10% drawdown.
    """

    volume_mult: float = 3.0
    high_conviction_mult: float = 3.5
    require_ma200: bool = True
    min_history_days: int = 200
    skip_days_before_ca: int = 0  # CA-aware entry filter (0 = off)


@dataclass(frozen=True)
class ExitsConfig:
    """Profit-taking ladder and stop-loss scale-out."""

    tiers: tuple[tuple[float, float], ...] = field(
        default_factory=lambda: (
            (1.063, 0.10),
            (1.125, 0.30),
            (1.210, 0.30),
            (1.330, 0.10),
            (1.460, 0.10),
            (1.610, 0.10),
            (1.760, 0.10),
            (1.950, 0.10),
            (2.150, 0.10),
        )
    )
    scale_out_pct_at_sl_T: float = 0.5
    scale_out_pct_at_midpoint: float = 0.5
    sl_buffer_pct: float = 0.01


@dataclass(frozen=True)
class TrailingConfig:
    """Capital-preservation trailing stop, activates after first TP hit.

    Two modes:
    - ``trail_mode="pct"``: trail a fixed percentage behind peak.
    - ``trail_mode="atr"``: trail ``trail_atr_mult`` ATRs behind peak,
      where ATR is computed from bars at signal time. ATR mode
      adapts to each stock's volatility and consistently outperforms
      fixed-percentage mode in our sweep (Sharpe 1.58 vs 1.42).

    Default: ATR mode, mult=1.5 (best Sharpe in sweep).

    Sweep on 84-ticker universe, vol=3.0, MaxC=10, Cool=10:

        Mode   Mult  Ret      Sharpe  MaxDD
        pct    0.10  +213.0%  +1.42    -9.8%
        pct    0.13  +232.5%  +1.43    -8.5%
        atr    1.50  +228.4%  +1.58    -9.5%   ← default
        atr    4.00  +245.3%  +1.45    -8.5%
    """

    activation_tier_idx: int = 0
    trail_mode: str = "atr"   # "pct" or "atr"
    trail_from_peak_pct: float = 0.10  # used when trail_mode="pct"
    trail_atr_mult: float = 1.5       # used when trail_mode="atr"
    trail_floor_lock_profit: bool = True
    trail_atr_window: int = 14          # ATR window for trail_mode="atr"


@dataclass(frozen=True)
class UniverseConfig:
    """Filter applied to the symbol universe before signal generation."""

    min_avg_value_20d: float = 1.0e9
    min_price: float = 5_000.0
    max_price: float = 200_000.0
    ticker_allowlist: tuple[str, ...] | None = None


@dataclass(frozen=True)
class BacktestConfig:
    """Outer-loop bookkeeping.

    ``max_concurrent_positions`` and ``cooldown_bars_after_stop``
    only take effect when ``run_backtest(..., portfolio=True)``. With
    ``portfolio=False`` (the legacy mode), every signal fires and
    these fields are ignored.

    Sweep on the 84-ticker liquid universe, 2019-08 to 2026-08, MTM
    at bar close, showed:
    - ``max_concurrent_positions=10, cooldown=10``:
      +179.2% return, 1.21 Sharpe, -25.1% max DD (best Sharpe).
    - ``max_concurrent_positions=12, cooldown=30``:
      +188.4% return, 1.17 Sharpe, -28.5% max DD (highest return).
    - ``max_concurrent_positions=1, cooldown=0``:
      -17.4% return (too few positions).
    - ``max_concurrent_positions=unlimited`` (legacy mode):
      +2,841% but at ~1,500× effective leverage (fictitious).
    """

    start_nav: float = 1.0e9
    size_low: float = 0.15
    size_high: float = 0.30
    cost_bps: float = 25.0
    max_concurrent_positions: int = 10
    cooldown_bars_after_stop: int = 5  # bars to skip after SL_HARD/SL_SCALE exit


@dataclass(frozen=True)
class StrategyConfig:
    """Single-bag config passed to every module of the strategy."""

    base: BaseConfig = field(default_factory=BaseConfig)
    breakout: BreakoutConfig = field(default_factory=BreakoutConfig)
    exits: ExitsConfig = field(default_factory=ExitsConfig)
    trailing: TrailingConfig = field(default_factory=TrailingConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    @classmethod
    def default(cls) -> "StrategyConfig":
        return cls()
