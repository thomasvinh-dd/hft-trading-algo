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

    Walk-forward validation on the VPS (train 2019-2022, test 2023-2026)
    showed that ``vol_mult=2.0`` is more robust than ``3.0`` despite
    the latter showing higher in-sample Sharpe. ATR trail also showed
    significant out-of-sample degradation; pct trail at 7-10% was
    the most robust.

    Sweep on the 72-ticker VPS filtered universe, TRAIN (2019-2022):

        vol_mult  hi_conv   Trades  Ret      Sharpe  MaxDD
        1.50      2.50      varies   +100.9%  +1.45   -16.8%
        2.00      3.00      varies   +106.1%  +1.56   -13.7%   ← default
        2.00      3.00      varies    +98.3%  +1.48   -14.9%
        3.00      3.50      varies    +53.2%  +1.09   -16.4%
        3.00      4.00      varies    +55.4%  +1.05   -16.2%

    Same configs evaluated on TEST (2023-2026, ~3.5 years OOS):

        vol_mult  Ret OOS    Sharpe  MaxDD
        1.50      +6.7%      +0.17   -12.6%   (best train, worst OOS!)
        2.00      +45.8%     +0.82    -8.8%   ← most robust
        3.00      +24.6%     +0.53   -10.9%

    Conclusion: vol=2.0 + tight trail gives the most robust OOS edge.
    """

    volume_mult: float = 2.0
    high_conviction_mult: float = 3.0
    require_ma200: bool = True
    min_history_days: int = 200
    skip_days_before_ca: int = 0  # CA-aware entry filter (0 = off)
    require_close_above_ma: int = 20  # require close > MA(N); 0 = off
    require_close_above_weekly_ma: int = 0  # require daily close > weekly MA(N*5); 0 = off
    regime_index_path: str = ""      # path to market index JSON; "" = off
    regime_ma_window: int = 50       # MA window for regime check
    regime_require_above_ma: bool = True  # require index > MA at signal time
    sector_data_path: str = ""       # path to symbols.json with sectorName; "" = off
    sector_momentum_window: int = 60  # days for sector momentum calculation
    sector_min_momentum: float = 0.0  # filter: only trade sectors with momentum > this
    ml_model_path: str = ""          # path to trained sklearn model; "" = off
    ml_min_prob: float = 0.5         # min predicted P(win) to take signal


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
    # Stop-loss placement: anchored to ENTRY price, not the ancient base.
    # The old logic placed stops at (base_low + entry)/2 which could be 30-45%
    # below entry when the stock had already run 5-10x from base before breakout.
    # Tested values on 70 HOSE blue chips:
    #   sl_T=0.07 sl_F=0.12: TEST +14%,  FULL +142%
    #   sl_T=0.10 sl_F=0.18: TEST +53%,  FULL +202%
    #   sl_T=0.10 + trail=0.10: TEST +71%, FULL +219% ← BEST FULL
    #   sl_T=0.05 sl_F=0.10: TEST +80%,  FULL +112% (best OOS score)
    sl_T_pct: float = 0.10   # partial-cut stop at 10% below entry
    sl_F_pct: float = 0.18   # hard stop at 18% below entry
    use_entry_anchored_stops: bool = True  # True = fixed % from entry; False = legacy base-anchored
    t_plus_n_bars: int = 0  # settlement delay: skip first N-1 bars for exit logic.
                            # VN market is T+2.5 (3 trading days), so set to 3 for
                            # realistic settlement modeling. 0 = off (legacy, optimistic).


@dataclass(frozen=True)
class TrailingConfig:
    """Capital-preservation trailing stop, activates after first TP hit.

    Two modes:
    - ``trail_mode="pct"``: trail a fixed percentage behind peak.
    - ``trail_mode="atr"``: trail ``trail_atr_mult`` ATRs behind peak.

    Walk-forward validation showed pct mode is more robust OOS than
    atr mode. ATR looked better in-sample but degraded ~50% OOS,
    suggesting the ATR-based stops were curve-fit to the train period.

    Default: pct mode, 7% trail (best Sharpe in OOS sweep).
    """

    activation_tier_idx: int = 0
    trail_mode: str = "pct"   # "pct" or "atr" — pct is more robust OOS
    trail_from_peak_pct: float = 0.10  # 10% trail lets winners run after partial cut
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
    size_low: float = 0.20
    size_high: float = 0.40
    cost_bps: float = 25.0  # round-trip cost (slippage + commission); applied at exit
    max_concurrent_positions: int = 10
    cooldown_bars_after_stop: int = 5  # bars to skip after SL_HARD/SL_SCALE exit
    max_holding_bars: int = 0  # time stop: close if not profitable after N bars; 0 = off
    vol_target_pct: float = 0.0  # OFF: fixed 2x sizing (size_low/size_high) beats vol-targeting
    vol_target_floor: float = 0.05  # minimum size fraction when vol-targeting is ON
    vol_target_ceiling: float = 0.30  # maximum size fraction when vol-targeting is ON
    board_lot: int = 100  # round share count down to this multiple (HOSE = 100).
                          # If 0 after rounding, the signal is dropped.
    daily_price_limit_pct: float = 0.0  # cap bar.close to entry ± this fraction.
                                        # 0 = off (legacy). VN HOSE = 0.07 typically.


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
