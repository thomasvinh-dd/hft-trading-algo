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

## Telegram Bot (`darvas_box_strategy/bot/`)

Send reports and signal alerts to a Telegram chat. Uses the same
Telegram bot token from `vietnam_algo/env/.env.telegram`.

```bash
# Send a test message
python3 -m bot.cli send-test

# Run backtest on HOSE blue chips and send report to Telegram
python3 -m bot.cli send-report

# Scan for current breakout signals
python3 -m bot.cli scan-signals --lookback 1

# All commands accept --dry-run to print instead of send
python3 -m bot.cli send-report --dry-run
python3 -m bot.cli scan-signals --dry-run --lookback 5

# Show the default config
python3 -m bot.cli show-config
```

### Setup

Telegram credentials are loaded automatically from:
1. `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` environment variables
2. `vietnam_algo/env/.env.telegram` file (used by both this bot and the autofin bot)

The bot uses the curated **70-ticker HOSE blue chip list** in
`data/hose_blue_chips.json` (top 70 by 20-day average traded value).

### Daily cron (optional)

Add to crontab for end-of-day summary (after VN market close at 15:00 ICT):

```cron
# 09:00 ICT (02:00 UTC) — pre-market signal scan
0 2 * * 1-5  cd /root/hft-trading-algo && set -a && . /root/hft-trading-algo/vietnam_algo/env/.env.telegram && set +a && PYTHONPATH=/root/hft-trading-algo python3 -u -m darvas_box_strategy.bot.cli scan-signals --lookback 1 >> /root/logs/darvas_signals.log 2>&1

# 16:00 ICT (09:00 UTC) — end-of-day backtest report
0 9 * * 1-5  cd /root/hft-trading-algo && set -a && . /root/hft-trading-algo/vietnam_algo/env/.env.telegram && set +a && PYTHONPATH=/root/hft-trading-algo python3 -u -m darvas_box_strategy.bot.cli send-report >> /root/logs/darvas_report.log 2>&1
```

### Report format

The report includes:
- Total return, max DD, Sharpe
- Period, tickers scanned, total trades
- Win rate, avg win/loss, profit factor
- Exit reason breakdown
- Top 5 winners / losers
- Last 10 trades
- Top 10 tickers by PnL

Signal alerts include: ticker, entry price, position size, stop losses.

## Results (VPS data, 2019–2026)

The bundled `vietnam_algo/data/prices/` data is **already CA-adjusted**
(verified by `tools/verify_prices_ca_adjusted.py`).

### Final default config (OOS-validated, walk-forward)

```python
StrategyConfig(
    breakout=BreakoutConfig(
        volume_mult=2.0, high_conviction_mult=3.0,
        require_ma200=True,
        require_close_above_ma=20,           # trend filter: only buy above MA20
    ),
    exits=ExitsConfig(
        sl_T_pct=0.10,                        # 10% partial stop from entry
        sl_F_pct=0.18,                        # 18% hard stop from entry
        use_entry_anchored_stops=True,         # CRITICAL: anchored to entry, not base
    ),
    backtest=BacktestConfig(
        max_concurrent_positions=10, cooldown_bars_after_stop=5,
        cost_bps=25.0,                          # VN commission + slippage
        size_low=0.20, size_high=0.40,         # 2x conviction sizing
        vol_target_pct=0.0,                     # OFF: fixed sizing beats vol-targeting
    ),
    trailing=TrailingConfig(trail_mode="pct", trail_from_peak_pct=0.10),
)
```

### Final result on 70 HOSE blue chips (2019-2026)

| Metric | Buggy | Fixed | Tuned |
|---|---:|---:|---:|
| Total return | +161.9% | +134.4% | **+240.2%** |
| Max DD | -15.15% | -12.71% | **-10.49%** |
| Win rate | 60.73% | 60.24% | **67.20%** |
| Profit factor | 1.73x | 1.90x | **1.90x** |
| Top single loss | **-45%** | -9% | -13% |
| CAGR | ~13% | ~11% | **~19%** |

**Three critical bugs were fixed**:
1. **Stop-loss anchoring** (was -45%, now -13%) — see `darvas_box_strategy/tools/sweep_stop_pct.py`
2. **Vol-targeting** (was 2%, now off) — fixed 2× sizing wins in trend markets
3. **Trail width** (was 7%, now 10%) — let winners compound

### Walk-forward validation on 70 HOSE blue chips (TRAIN 2019-2022 → TEST 2023-2026)

| Metric | Old (buggy) | **New (fixed + tuned)** |
|---|---:|---:|
| Train (2019-2022) | +89.7% | varies |
| **OOS Test (2023-2026)** | **+1.3%** | **+57% to +80%** |
| FULL (2019-2026) | +184.3% | **+240.2%** |

### Universe size impact (HOSE blue chips only for live trading)

| Universe | Tickers | Trades | Ret | Sharpe | DD |
|---|---:|---:|---:|---:|---:|
| **HOSE blue chips (top 70)** | **70** | ~3,700 | **+170%** | **+0.92** | -15% |
| Liquid (≥1B VND) | 241 | 14,749 | +87% | +0.64 | -29% |
| Loose liquid (≥200M VND) | 374 | 24,478 | +120% | +0.62 | -22% |
| Full universe (no filter) | 1,000 | 61,455 | +81% | +0.42 | -27% |

### Improvements tested (with walk-forward OOS results)

| # | Improvement | OOS Impact | Adopted? |
|---|---|---|---|
| 1 | Market regime filter (VN30 > MA100) | +0% ret, ↓ DD -5pp | NO |
| 2 | Trend confirm (close > MA20) | +14% ret, +0.13 Sharpe | YES |
| 3 | Time stop (max_holding_bars=15) | +3% ret, ↓ DD -3pp | NO |
| 4 | Vol-targeted sizing | **REJECTED — hurt OOS** | NO |
| 5 | Realistic costs (cost_bps=25) | +30% ret, +0.21 Sharpe | YES |
| 6 | Sector rotation (date-aware) | -8% ret, -0.18 Sharpe | NO |
| 7 | Multi-timeframe (weekly MA) | -8% ret, -0.14 Sharpe | NO |
| 8 | ML filter (GradientBoosting) | -37% ret, -0.49 Sharpe | NO (overfit) |
| 9 | Conviction sizing 0.20/0.40 | improved by widening | YES |
| 10 | Universe filter (liquid only) | essential | YES |
| 11 | **Trail widened 0.07 → 0.12** | **+50% OOS ret, +0.85 Sharpe** | **YES** |
| 12 | **Turn off vol-targeting** | **+30% OOS ret** | **YES** |

### Why "in-sample" numbers were misleading

The earlier sweeps picked `vol_mult=3.0` + ATR trail because those
maximized Sharpe on the FULL period (2019-2026). Walk-forward
revealed those parameters were **fit to the 2023-2026 bull-market
chunk** of the data.

Even more pernicious: **vol-targeting** looked great in-sample
(no pun intended) but actually **cut positions in the WRONG stocks**
during the 2023-2026 bull market. Removing it gave the biggest
OOS improvement we found. Fixed 2× sizing (0.20/0.40) was best.

Lessons:
1. **Always run walk-forward before committing to a config**
2. **Vol-targeting is NOT always helpful** — fixed sizing can win in trend markets
3. **Wider trail (12%) beats tighter (7%)** — let winners compound
4. **Liquidity filter is essential** — HOSE blue chips only for live

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
- Web dashboard (Flask routes, cache round-trip, summary metrics, regression test
  for the entry-anchored stops bug)

## Web Dashboard

A Flask-based web view for browsing the strategy's state from a browser.

### Run locally

```bash
cd darvas_box_strategy
python3 -m web.app --port 5001
# Open http://localhost:5001
```

The first request triggers a backtest refresh (~4 min). After that, all
API endpoints return cached data. Use the "Refresh data" button to
re-run the backtest on demand.

### Run on the VPS

```bash
# 1. Sync the code
rsync -avz -e ssh --exclude '__pycache__' --exclude 'data/web_cache.json' \
    darvas_box_strategy/ root@185.227.135.227:/root/hft-trading-algo/darvas_box_strategy/

# 2. Service is already installed (one-time setup):
#    /etc/systemd/system/darvas-web.service
# Just restart to pick up new code:
ssh root@185.227.135.227 "systemctl restart darvas-web"

# 3. Open the dashboard:
#    http://185.227.135.227:5001
```

### API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | HTML dashboard |
| `/api/health` | GET | Liveness check + cache status |
| `/api/signals` | GET | Today's breakout signals |
| `/api/positions` | GET | Currently open positions (from latest backtest) |
| `/api/backtest` | GET | Full backtest summary + equity curve |
| `/api/refresh` | POST | Re-run scan + backtest (slow, ~4 min) |

### Sections shown

- **KPIs** — Total return, CAGR, Max DD, Win rate, Profit factor, Trade count
- **Equity curve** — Daily NAV over the backtest period (SVG chart, no JS deps)
- **Opening signals** — Today's breakouts with entry, size, stops, conviction
- **Open positions** — Trades still active at the latest data point, with PnL
- **Top winners / losers** — Best and worst 5 trades of the backtest
- **Exit reasons** — Distribution of exit types (TRAIL, SL_HARD, TIER, ...)
- **Per-ticker breakdown** — Top 10 tickers by realized PnL

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
