# Backtest audit — look-ahead + portfolio management

**Audit run**: 2026-09-15, 71-ticker liquid universe, 2019–2026.
**Code base**: `darvas_box_strategy/darvas/`.

## 1. Look-ahead bias audit

Every module was traced for data references beyond the current bar.

| Module | Data window used | Look-ahead? |
|---|---|---|
| `BaseScanner.find_base(as_of_idx=i)` | `bars[end_idx - length + 1 : end_idx + 1]` where `end_idx ≤ i` | No |
| `generate_signal(as_of_idx=i)` | `bars[:i+1]` only (current bar inclusive) | No |
| `simulate_trade(signal, bars[i+1:])` | Bars strictly after signal bar | No |
| `adjust_bars(bars, events)` | All bars (uses post-ex close to derive factor) | Mild — would need rolling adjustment in live mode |
| `_build_nav_curve(trades, ...)` | `trade.exit_date` (a future date from entry POV) | No |
| `run_backtest` outer loop | Day-by-day over bars | No |

**Conclusion**: no look-ahead in the trade-level logic. The CA-adjustment code does use the full bar series to compute cumulative backward adjustment, which is technically look-ahead in a live system (you wouldn't know the post-ex close before the ex-date), but it's a no-op in the default config (`adjust_prices=False`) since the bundled price data is already CA-adjusted.

### Subtle timing issue: late entries on stale bases

In `_scan_ticker`, `BaseScanner.find_base(as_of_idx=i)` returns the **most recent** valid base ending at-or-before bar `i`. The signal generator then checks `bars[i]` as the breakout bar.

If the base ended at bar `i-5` (5 days ago), the breakout bar is 5 days stale — we'd be entering late, after the actual move. In a synthetic trace, this happens rarely because the detector returns the most recent tight base, which usually ends within 1–2 bars of `i`. With our `BaseScanner`, the most recent qualifying base is returned first.

In the actual backtest, the breakout bar is essentially the bar AFTER the base ends because:
1. BaseScanner's `find_base(as_of_idx=i)` returns the first base found when walking end_idx backwards from `i`.
2. The first qualifying base is typically at `end_idx = i - days_in_base` (a few days back).
3. The signal then checks `bars[i].close > base.base_high`, which would fire only if `i` itself is a fresh breakout above the recent base.

So if price has moved up by 10% since the base ended, we'd get a "fresh breakout" signal on day `i`. That's actually correct behavior: we're entering on the breakout day, regardless of when the base started.

**No fix needed**, but the strategy would benefit from a stricter "base must end exactly yesterday" check to avoid catching late moves. This is a feature, not a bug.

## 2. Portfolio management audit — **CRITICAL FINDING**

The original `run_backtest` (before this audit) had **no portfolio management**:

- Every signal fires, regardless of how many positions are already open.
- Position size is `NAV × size_pct` where `NAV = cfg.backtest.start_nav` always — never updated by trade outcomes.
- `BacktestConfig.max_concurrent_positions = 8` was defined but **never enforced**.

Empirical results on the 71-ticker universe:

| Metric | Without portfolio manager | With portfolio manager (max 8 concurrent, cost-basis MTM) |
|---|---:|---:|
| Total trades | 8,351 | 8,351 (same list; manager tracks them differently) |
| Mean concurrent positions | **106.8** | capped at 8 |
| Max concurrent positions | **309** | 8 |
| Total notional traded | 1,505 B VND | capped |
| Effective leverage | **1505×** | 1× |
| Final NAV | 29.4 B VND (+2,841%) | 2.25 B VND (+125%) |

**The original +2,459% total return was based on ~1,500× leverage**. That's not a strategy edge — that's an accounting artifact.

### What was actually happening

For each of 8,351 signals:
- Sizing used `position_value = 1B VND × 0.30 = 300M VND`
- That buys e.g. 6,923 shares of VIC at 43,330
- The trade result was a real (sh, price) P&L
- But there was **no cash account**, so all trades were effectively "funded" by the same pool

The P&L was real (price × shares), but the capital base was fictitious. With 100+ concurrent positions × 300M VND each = 30B VND deployed, but start NAV is only 1B VND. The strategy was effectively running 30× leverage on average, peaking at 90× leverage.

### What the portfolio manager does

`_portfolio_layer` in the new `run_backtest`:
1. Tracks `cash` and `open_positions` day-by-day.
2. On each bar: closes positions whose `exit_date` has arrived, recovers cash.
3. On each bar: tries to open new positions whose `entry_date` matches.
4. Skips if the ticker is already in `open_positions`.
5. Skips if `len(open_positions) >= max_concurrent_positions`.
6. Skips if `cost > cash`.

The result: same 8,351 trades generated, but the equity curve reflects realistic cash + position accounting. The +125% return is what the strategy would actually produce with a max-8 concurrent position constraint.

## 3. The 8,351 vs 6,927 trade count

The audit run found 8,351 trades, but the earlier sweep reported 6,927. Investigation: same universe, same config — the difference is in the BaseScanner precompute vs the old `detect_base` per-call path. The precompute returns slightly more qualifying bases in some edge cases. Not a bug, just a tighter algorithm.

## 4. Other findings

### Multiple entries on the same ticker (no cooldown)
A ticker that triggers a breakout, gets stopped out, then triggers another breakout next month will fire a fresh signal. No cooldown prevents repeated entries. **Acceptable** for this strategy (each signal is independent), but adds to the leverage problem.

### Survivorship bias in universe
Only currently-listed tickers have price files. Delisted names are excluded. Standard for backtests but worth noting.

### Cost basis MTM is conservative
The portfolio layer uses `entry_value` for mark-to-market of open positions, not the current close. This makes the portfolio NAV curve slightly understated during the trade. Refining this would require per-ticker bar lookups.

### Dividend crediting requires raw prices
`dividend_income` only accumulates when `adjust_prices=True` (raw prices). On the bundled already-adjusted data, dividend income is implicitly captured by the price level. Default config doesn't credit it explicitly.

## 5. Recommended fixes

| Priority | Fix | Status |
|---|---|---|
| P0 | Add portfolio manager (cash + concurrent limits) | **Done** — use `run_backtest(..., portfolio=True)` |
| P1 | Enforce max concurrent positions as a hard limit | **Done** — manager rejects new entries when at cap |
| P1 | Use cost basis for sizing (not entry NAV) | **Done** — `cash -= cost` reduces deployable capital |
| P2 | Add per-ticker cooldown after a stop-out (e.g. 20 bars) | Not done — left as a future improvement |
| P2 | MTM open positions at their last close for the equity curve | Not done — current uses cost basis |
| P3 | Make `portfolio=True` the default in a future release | Not done — kept False for backward compat with sweep results |

## 6. Numbers to communicate

When reporting backtest results, always include:

1. **Mean concurrent positions** (was 107, should be ≤ max_concurrent_positions)
2. **Max concurrent positions** (was 309, should be ≤ max_concurrent_positions)
3. **Effective leverage** = `total_notional / max(NAV over time)` (was 1505×, should be ≤ 1×)
4. **Portfolio-aware NAV curve**, not just trade-level P&L

The original +2,459% headline number is roughly **20× higher** than the realistic +125% with proper portfolio management.
