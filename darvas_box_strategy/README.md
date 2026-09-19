# Darvas Box + AutoFin-Tiered-Exits Strategy

A standalone backtest of the strategy reverse-engineered from
AutoFin's `signal-static-suggest` endpoint.

The strategy is a **Darvas Box breakout** (tight-consolidation
breakout with volume confirmation) extended with three AutoFin-specific
features:

1. **Fixed profit-tier ladder** — close partial positions at fixed
   percentage multipliers from entry (verified across 8 closed ASP
   deals to land on the exact same multipliers AutoFin uses).
2. **Scale-out stop loss** — instead of a binary full-close on stop,
   bleed out 50% at `sl_T`, 50% of remaining at the midpoint of the
   stop band, and close the rest at the hard stop `sl_F`.
3. **Trailing capital preservation** — once the first profit tier
   fires, the running peak is trailed by a fixed percentage; if close
   drops that much below peak (or below the first-tier exit price if
   `trail_floor_lock_profit` is on), the position is closed.

## Layout

```
darvas_box_strategy/
├── README.md
├── docs/
│   ├── AUDIT.md                       # look-ahead + portfolio audit
│   └── CA_HANDLING_COMPARISON.md     # side-by-side vs existing project
├── run.py
├── tools/
│   └── verify_prices_ca_adjusted.py   # confirms data is already adjusted
├── darvas/
│   ├── config.py
│   ├── data_loader.py
│   ├── ca_handler.py
│   ├── base_detector.py               # BaseScanner (precomputes rolling min/max)
│   ├── signal_generator.py
│   ├── executor.py
│   ├── backtest.py                    # includes optional portfolio manager
│   ├── metrics.py
│   └── report.py
└── tests/
    ├── test_strategy.py
    └── test_ca_handler.py
```

## Quick start

```bash
# Run on the 84-ticker liquid universe WITH portfolio management (default)
python3 darvas_box_strategy/run.py

# Tune concurrent position limit
python3 darvas_box_strategy/run.py --max-concurrent-positions 15

# Legacy mode: every signal fires (unbounded leverage, for benchmarking only)
python3 darvas_box_strategy/run.py --quiet   # portfolio=False default... wait, we made it default

# Single ticker detail
python3 darvas_box_strategy/run.py --ticker HDB --detail
```

## Results (84-ticker liquid universe, 2019–2026)

The bundled `vietnam_algo/data/prices/` data is **already CA-adjusted**
(verified by `tools/verify_prices_ca_adjusted.py`).

### Improvement progression

Each row adds one improvement to the previous. All use the same
84-ticker universe, MTM at close, `portfolio=True`.

| Stage | Ret | Sharpe | MaxDD |
|---|---:|---:|---:|
| Initial (loose filter, no portfolio manager) | +2,841.6%* | n/a | n/a |
| + Portfolio manager (MaxC=10, Cool=10) | +179.2% | +1.21 | -25.1% |
| + Tighter entries (vol_mult=3.0) | +213.0% | +1.42 | -9.8% |
| **+ ATR-based trailing stop (mult=1.5)** | **+228.4%** | **+1.58** | **-9.5%** |
| + Cool=5 instead of 10 | +228.0% | +1.59 | -9.5% |

\* Inflated by ~1,500× effective leverage. See `docs/AUDIT.md`.

### Final default config (the recommended one)

```
vol_mult=3.0, high_conviction_mult=3.5
max_concurrent_positions=10, cooldown_bars_after_stop=5
trail_mode="atr", trail_atr_mult=1.5, trail_floor_lock_profit=True
```

Result: **+228.4% return, 1.58 Sharpe, -9.5% max DD** over 7 years.

### Sweep grid

#### Entry filter tightness (vol_mult)

| vol_mult | hi_conv | Trades | Final NAV | Ret | Sharpe | MaxDD |
|---:|---:|---:|---:|---:|---:|---:|
| 1.50 | 2.50 | 8,351 | 2,792M | +179.2% | +1.21 | -25.1% |
| 1.80 | 2.80 | 5,458 | 2,984M | +198.4% | +1.39 | -21.0% |
| 2.00 | 3.00 | 4,231 | 2,131M | +113.1% | +1.01 | -17.9% |
| **3.00** | **3.50** | **1,395** | **3,284M** | **+228.4%** | **+1.58** | **-9.5%** ← default |
| 3.50 | 4.00 | 899 | 3,069M | +206.9% | +1.41 | -9.9% |
| 5.00 | 6.00 | 327 | 1,779M | +77.9% | +0.91 | -8.5% |

#### Trailing stop mode

| Mode | Mult | Final NAV | Ret | Sharpe | MaxDD |
|---|---|---:|---:|---:|---:|
| pct | 0.10 | 3,130M | +213.0% | +1.42 | -9.8% |
| pct | 0.13 | 3,325M | +232.5% | +1.43 | -8.5% |
| **atr** | **1.50** | **3,284M** | **+228.4%** | **+1.58** | **-9.5%** ← default |
| atr | 4.00 | 3,453M | +245.3% | +1.45 | -8.5% |

## Tests

```bash
cd darvas_box_strategy
python3 -m unittest discover -v tests
```

21 tests covering:
- Base detection (tight consolidation, range rejection, most-recent-base selection)
- Signal generation (volume breakout, high/low conviction, no-volume no-signal, CA-aware entry filter)
- Trade execution (tier ladder, hard stop, trailing capital preservation, ATR trail)
- CA handler (cash dividend adjustment, stock dividend adjustment, compound events, OHLC consistency)

## VPS testing (2026-09)

The VPS at `root@185.227.135.227` (Singapore, IP-whitelisted by DNSE)
has a more recent price snapshot than the local workstation
(2026-09-19 vs 2026-08-13). On the VPS, we swept the **full universe**
(387 tickers with 200+ days history, 84 with the 1B VND liquidity
filter) against the same strategy.

### Local vs VPS comparison

| Universe | Source | Best vol | Best ATR mult | Best MaxC | Ret | Sharpe | MaxDD |
|---|---|---:|---:|---:|---:|---:|---:|
| Local liquid (84) | workstation | **3.0** | **1.5** | 10 | **+228.4%** | **+1.58** | -9.5% |
| VPS filtered (72) | VPS | 2.0 | 1.0 | **15** | +214.1% | +1.32 | -19.9% |
| VPS full (387) | VPS | 3.0 | 2.0 | 15 | +138.9% | +0.60 | -16.8% |

**Key insight**: the optimal config differs between local and VPS
data. The VPS data is more recent and includes price action from the
last month. The general pattern is consistent (vol_mult > 1.5, ATR
trail > pct trail), but the exact optimum shifts.

### VPS run commands

```bash
# Sync the strategy to the VPS
rsync -avz -e ssh darvas_box_strategy/ root@185.227.135.227:/root/hft-trading-algo/darvas_box_strategy/

# Run a sweep on the VPS
ssh root@185.227.135.227 "cd /root/hft-trading-algo && python3 darvas_box_strategy/tools/vps_sweep.py"

# Single-ticker detail on the VPS
ssh root@185.227.135.227 "cd /root/hft-trading-algo && python3 darvas_box_strategy/run.py --ticker VIX --detail"
```

### Why local config differs from VPS

The VPS sweep is on **more recent** price data. Specifically:
- Local data was fetched 2026-08-13
- VPS data was fetched 2026-09-19

Recent price action (the last month) might favor slightly different
parameters. If the strategy is going to run live, the VPS-tuned
config is more relevant. For backtest/research on historical data,
the local-tuned config is fine.

The local config is the default because it's the original
calibration. To switch to VPS tuning, set in your config:

```python
cfg = StrategyConfig(
    breakout=BreakoutConfig(volume_mult=2.0, high_conviction_mult=3.0),
    backtest=BacktestConfig(max_concurrent_positions=15, cooldown_bars_after_stop=5),
    trailing=TrailingConfig(trail_mode="atr", trail_atr_mult=1.0),
)
```

Saved sweep outputs are in `results/`:
- `vps_sweep.out` — vol_mult sweep on filtered universe
- `vps_sweep2.out` — ATR mult / cooldown / MaxC sweep
- `vps_final.out` — final best config + full-universe comparison
- `full_run.out`, `full_filtered.out` — initial full-universe runs
