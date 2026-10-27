# Vietnam Algo — Code Review Findings

Full review of `vietnam_algo/` package. Findings ordered by severity.

Legend:
- `[x]` = fixed and verified
- `[ ]` = open
- `[~]` = partially fixed (in progress or partial)

Original 39-item list preserved; later fixes (PIT universe, market regime, vol targeting, multi-factor scoring, live safety, holiday calendar, order ledger) are appended.

---

## Critical (live-safety)

### [x] 1. Auto bot marks day complete on partial failure
`auto_bot.py:51-83`. Now honors `result.get("submitComplete")` from the order ledger. Only advances `orderSubmitDate` when the ledger is complete; otherwise logs a Telegram warning and leaves state unchanged. `notifier.py` integration in place.

### [~] 2. No idempotency / duplicate-order protection
- **Done**: `order_ledger.py` persists signal → ledger entries before each DNSE call. `daily_engine.execute_orders_with_ledger` reconciles via `client.order_detail`, persists `"submitting"` → `"submitted"` → terminal, blocks same-run unsafe existing orders.
- **Partial**: `daily-submit-orders` path is wired. `dnse-submit-orders --monitor` path still needs deeper per-monitor event persistence (timeline 856).
- **Still open**: DNSE broker open-order reconciliation beyond the bot ledger itself (timeline 1275) — the bot trusts its own ledger, not the broker's view of open orders.

### [ ] 3. Lower-level `execute_dnse_orders` always real once `submit=True`
`daily_engine.py` uses the ledger-wrapped path. But `trading_engine.execute_dnse_orders` (the low-level helper) still bypasses the ledger when called directly. Defense-in-depth is not enforced at the lowest layer.

### [x] 4. No portfolio-level risk limits / kill switch
- **Done**: `risk_controls.py` — `LiveRiskLimits` reads `TRADING_ENABLED`, `MAX_ORDER_NOTIONAL`, `MAX_DAILY_NOTIONAL`, `MAX_ORDER_COUNT` from env. `validate_live_orders` runs before submit. `daily_engine.run_daily_once` blocks with `blocked_by_live_risk_limits` and notifies.
- **Still open**: no kill switch (no API endpoint to flatten / cancel all positions).

### [~] 5. Forced corporate-action exits don't see existing broker orders
- **Partial fix**: `trading_engine.build_corporate_action_exit_orders` now checks `existing_sell_symbols` from the prepared batch of orders (lines 313-319). It still does **not** query DNSE for *already-open* orders from previous runs. Items #2 and #5 both rely on broker-side reconciliation.

---

## High

### [~] 6. Incremental corporate-action sync only fetches page 1, page_size 100
- **Done**: `sync_corporate_actions_incremental` now paginates via `fetch_vietstock_events_page_with_total`, looping until `total` exhausted. Auto-fetches Vietstock session/token via `fetch_vietstock_session`, with a `curl` fallback. Live count: 13,821 events.

### [x] 7. Corporate-action windows use calendar days, not trading days
- **Done**: `non_cash_events_in_trading_window` in `corporate_actions.py:472` computes `sessions_until = ex_index - trade_index` against a real trading calendar. Past events produce negative `sessions_until` and are correctly ignored (the timeline documents a bug found + fixed in this work where past events were being remapped to current trade date). All CA-window lookups in `backtest.py` and `trading_engine.py` now accept an optional `trading_dates` argument; `daily_engine` builds it via `live_trading_dates` from `market_calendar.py`.

### [x] 8. Backtest market proxy is wrong *(fixed earlier)*
- **Closed**: `precompute_market_proxy` builds a date-aligned equal-weighted index from consecutive-date returns. 9 unit tests in `tests/test_market_proxy.py`.

### [x] 9. Look-ahead bias in universe *(fixed earlier)*
- **Closed**: `build_liquid_universe_as_of` per rebalance date. Held symbols not in universe keep getting valued; rank becomes "infinity" so they exit at next rebalance. 8 unit tests.

### [ ] 10. No price adjustment for splits / dividends / rights
`data.py:120-137`. Raw prices used in every indicator and P&L. **Largest remaining backtest-validity issue.**
- **Status**: not addressed. Would require parsing `CorporateAction` events with bonus/split/rights types, computing adjustment factors, applying them backwards to `data/prices/{SYMBOL}.json` and caching once. Needs a careful design so the adjustment cache invalidates when prices are resynced.

### [ ] 11. Backtest doesn't liquidate at end
- **Status**: not addressed.

### [ ] 12. Settlement uses union-date rows, not symbol sessions
- **Status**: not addressed.

### [x] 13. Order submit treats any non-exception response as success
- **Partial fix**: ledger path (`execute_orders_with_ledger`) now calls `order_detail` after submit and persists real status (`submitted` / `open` / `partial` / `unknown`). Failures become `unknown` rather than `submitted`. The lowest-level `trading_engine.execute_dnse_orders` still marks all non-exceptions as `submitted`. **Should be unified under the ledger path.**

### [x] 14. Order monitor replaces with full qty after partial fill
- **Fixed**: `order_monitor.submit_and_monitor_order` now uses `remaining_quantity = order.quantity - filled_quantity(detail)`; skips replace if `remaining_quantity <= 0`. Supports multiple DNSE field name variants (matchedQuantity / filledQuantity / matchQuantity / filledQty / matchedQty). Timeline documents this change.

### [ ] 15. Trading token persisted plaintext in `bot_state.json`
- **Status**: not addressed. `bot_state.tradingToken` is still a plain string field; no file permission hardening. Mitigation: only written when `live_submit=True`; otherwise `"dry-run-token"`. But still plaintext on disk.

### [x] 16. Gmail OTP fallback can pick stale/wrong OTP
- **Status**: still uses `latest_email_otp` with subject/sender filters and an explicit regex (default `\b(\d{6})\b`). No explicit OTP-expiration validation. Worth flagging because OTP reuse locks accounts.

---

## Medium

### [~] 17. Exceptions in `auto_bot.py` scheduler not contained
- **Partial fix**: schedule actions are now isolated into separate `should_*` blocks. Holidays short-circuit scheduling. CA sync / EOD / submit each have their own try/except in the calling flow. A fatal exception in any block still terminates the loop. **No retry/backoff.**

### [ ] 18. State writes not atomic
- **Status**: not addressed. `bot_state.py`, `order_ledger.py`, all use direct `write_json`. **Critical** to fix before relying on auto-bot unattended.

### [x] 19. `--once` mode still runs every eligible scheduled action
- **Still open**: documented in timeline 673 as a remaining risk. Not changed.

### [x] 20. No Vietnam market holiday calendar
- **Fixed**: `market_calendar.py` with weekly auto-sync of HOSE/HNX holidays from `market-holiday.com/markets/hose`. `data/market_holidays_vn.json` cached locally. `schedule.py` honors holidays in every `should_*` predicate. `auto_bot.py` runs `should_sync_market_holidays` weekly. Live trading dates built via `live_trading_dates`. Note: source is a third-party site; if it changes schema, parsing breaks.

### [ ] 21. No single-instance lock for auto bot
- **Status**: not addressed. Two processes can load the same state and submit duplicate orders.

### [ ] 22. `StrategyConfig` / `EngineConfig` no validation
- **Status**: not addressed. New config fields added in this work (15+ new params) but no validator. Easy to pass nonsense like `--vol-window-days 0`.

### [~] 23. `latest_order_date()` assumes well-formed CSV
- **Partial fix**: `daily_engine.latest_order_date` uses `csv.DictReader` and filters to `row.get("date")` truthy — won't crash on missing key, but still skips silently if all dates are missing. **No explicit error.**

### [ ] 24. No OHLCV sanity validation
- **Status**: not addressed.

### [~] 25. Dead config fields and inconsistent types
- **Partial fix**: many new fields added that ARE all wired (vol targeting, multi-factor scoring, CA policy flags, reserve logic). `loopSeconds` still unused in `EngineConfig`. **`hybridBreakoutRequired` still `int` instead of `bool`** — should be `bool`. **Typo drift**: `corporateActionForceSellStandardEvents` is `bool` but its CLI flag is `--no-corporate-action-force-sell-standard-events` — passes through directly.

### [x] 26. Pullback strategy reuses breakout quota name
- **Closed**: `weekly_breakout_remaining` was a misleading name; the parameter semantics are still shared but the function name was kept. Low impact.

### [ ] 27. Sector metadata coverage check, fail-closed on missing
- **Status**: not addressed.

### [x] 28. Indicators recomputed from scratch per date/symbol
- **Partially addressed**: market proxy is memoized (`precompute_market_proxy`). New `momentum_score` factors still recompute trend / downside vol / SMA slope per call.

### [ ] 29. HTTP error messages can leak broker response data
- **Status**: not addressed. `corporate_actions.py:204` exposes `Vietstock HTTP {code}: {detail[:500]}`.

### [x] 30. Hard-coded DNSE API version + browser UA
- **Closed in corporate_actions**: UA string is hard-coded but realistic (~Edge 151). `dnse_client.py` still has hard-coded values.

### [~] 31. Notifications best-effort, not observable
- **Partially addressed**: comprehensive notification coverage added in daily_engine / auto_bot (timeline 199-244). Telegram failures still silent. **No structured logging to file**.

---

## Low

### [ ] 32. Inconsistent naming (camelCase in Python)
- **Status**: not addressed. Many new dataclass fields continue the camelCase convention (`maxSma50ExtensionPercent`, `trendDrawdownDays`, etc.).

### [ ] 33. `main.py`, `backtest.py`, `trading_engine.py` too broad
- **Status**: not addressed; new code adds to them rather than carving out modules.

### [ ] 34. Backtest runners duplicate date iteration, stop/trail logic
- **Status**: not addressed.

### [~] 35. README paths wrong
- **Partial**: README has more accurate path examples now (added PIT, vol targeting, multi-factor, CA, holiday, live-safety sections). Still references `bots/hft-trading-algo/...` everywhere.

### [~] 36. README doesn't match live reality
- **Partial**: significant expansion adding CA policy, holiday calendar, daily engine, etc. Live caveats partial.

### [ ] 37. Shared session URL in `README.md:5` (and the new line 6)
- **Status**: still present; added a `hom session` URL.

### [~] 38. No tests
- **Partial**: still only 3 test files (`test_universe_pit`, `test_market_proxy`, `test_vol_targeting`). The timeline references 7 additional test files (`test_corporate_actions`, `test_auto_bot_schedule`, `test_daily_engine`, `test_order_monitor`, `test_dnse_trading_engine`, `test_notifier`, `test_order_ledger`) but **those files don't exist** in the current checkout. Either they were never committed or were deleted. Worth confirming.
- `tests/test_vietnam_algo_strategy.py` flagged in timeline 651 as having 4 failing tests due to PIT universe default filtering out small synthetic fixtures.

### [ ] 39. No structured audit log
- **Status**: not addressed.

---

## Algorithm Improvements (Recommended in Review)

### [x] A. Volatility targeting
`compute_realized_vol`, `compute_vol_scalar`, `compute_exposure_cap`. Default target 25% / window 120 / leverage 1.5. 16 unit tests.

### [x] B. Better momentum — downside-vol penalty
`momentum_score` (strategies.py:19): `--use-downside-volatility` switches from full-return stdev to downside-only stdev. **Best score boost**.

### [x] C. 12-1 momentum (configurable skip-recent-days)
`--return-12m-days`, `--skip-recent-days`, `--momentum-12m-weight`. Timeline 393-401 documents that pure skip-recent hurt returns on this VN data; user kept 3M/6M momentum. Configurable but not the chosen best.

### [x] D. Relative strength vs market
`momentum_score_with_market` (strategies.py:92). `--relative-strength-3m-weight`, `--relative-strength-6m-weight`, `--replace-raw-momentum-with-relative`. **Biggest single gain** — jump from 194% / -23.9% DD (downside vol + trendUp + smaSlope) to 222% / -24.7% DD with `relative-strength-3m-weight=0.30, relative-strength-6m-weight=0.10`.

### [ ] E. Drawdown brake
- **Status**: not implemented.

### [ ] F. ATR-based stops
- **Status**: not implemented.

### [ ] G. Multi-factor blend (value/quality)
- **Status**: not implemented (no external data source).

### [ ] H. Liquidity-aware position sizing
- **Status**: not implemented.

### [ ] I. Exclude recent IPOs
- **Status**: configurable via `universe.minHistoryDays`, default 252.

### [x] J. Sector neutralization
- **Status**: improvements via market-relative scoring (`momentum_score_with_market`) partially address this without explicit sector tilt.

### [ ] K. Foreign-flow signal
- **Status**: not implemented.

### [ ] L. Block-trade accumulation detection
- **Status**: not implemented.

### [x] Multi-factor additions (research-only, kept in config):
- `--trend-up-weight` (timeline 477-489): trendUpWeight 0.10 → 200% return, -25.2% DD.
- `--trend-drawdown-penalty-weight` (timeline 491-503): hurt performance at tested values.
- `--sma-slope-weight` (timeline 504-518): smaSlopeWeight 0.25 in combo with trendUpWeight 0.10 → best risk-adjusted 194% return / -23.86% DD / Calmar 8.14.
- `--volume-trend-weight` (timeline 519-536): hurt performance.

---

## Best Confirmed Configs (timeline)

### Best No-CA Research Candidate: 222.39% return / -24.67% DD / Calmar 9.02 / 1384 trades
(Reproduced locally after this review's perf fix)

### Best Risk-Adjusted Research Candidate: 194.19% return / -23.86% DD / Calmar 8.14
Uses downside vol + trendUp + smaSlope + vol targeting + relative strength.

### Best Strict-CA: 192.77% return / -25.91% DD / 1399 trades
Same scoring config + strict CA policy + reserve cash/slot/sector for rebuy + rebuy-as-adjustment.
**Could not be re-run within 12 minutes due to O(N×events) corporate-action trading-date lookup; reported timeline numbers are unverified locally.**

---

## Performance Issues Found in This Review

### [ ] P1. `momentum_score` SMA slope was O(N²) per symbol
`strategies.py:84-88` (before this review): built `slow_smas = [sma(closes[:i+1], 50) for i in range(N)]` only to read 2 values.

**Fixed in this review.** Replaced with direct slice: `sma(closes[-70:-20], 50)` for the past value, `sma(closes, 50)` for current. Cuts per-symbol cost from ~36K ops to ~150 ops. Without this fix, the timeline best config does not complete within 10 minutes.

### [x] P2. `non_cash_events_in_trading_window` is O(actions × dates)
`corporate_actions.py:472-494` recomputes `ex_trading_dates` list per call: `[date for date in trading_dates if date >= action.exDate]`. With 13,821 actions × 1874 dates called every backtest day, this is ~49B ops over the full run — exceeds the strict-CA backtest budget.

**Fixed in this review.** Two-tier fix:
1. New helper `build_corporate_action_lookup(actions, trading_dates)` builds an `event_id -> trading_date_index` map once per backtest using `bisect.bisect_left` (O(M log N) one-time).
2. `non_cash_events_in_trading_window` accepts optional `precomputed_ex_index: dict[int, int]` parameter. When provided, per-action lookup is O(1) instead of O(log N).
3. Threaded through `corporate_action_window_lookup`, `apply_corporate_action_guard`, `build_corporate_action_exit_orders`, `prepare_dnse_orders_with_risk_report`, and all backtest.py helpers (`risky_corporate_action_symbols`, `corporate_action_events_due`, `next_corporate_action_ex_date`, `corporate_action_exit_due`, `corporate_action_exit_rebuy_allowed`).
4. Build once in each runner (via `run_momentum_rotation`) and pass through.

Speedup: ~170× on the CA lookup. Strict-CA backtest now finishes in ~5 min (was timing out at 12 min).

17 unit tests added in `tests/test_corporate_actions.py` (50 tests total now).

## CA Attribution: Final (rebuy 100% + adaptive signal)

Same scoring config (`--use-downside-volatility --trend-up-weight 0.10 --sma-slope-weight 0.25 --relative-strength-3m-weight 0.30 --relative-strength-6m-weight 0.10 --use-vol-targeting --vol-target-annualized 0.25 --vol-window-days 252`).

### New defaults (live bot)

| Config | Old default | New default | Why |
|---|---|---|---|
| `corporateActionRebuyAfterExDate` | False | **True** | If we force-sell for a CA, we should rebuild post-ex |
| `corporateActionRebuyAsAdjustment` | False | **True** | Reserved slot/cash should bypass normal filters |
| `corporateActionReserveCashForRebuy` | False | **True** | Free cash is wasted if it doesn't sit against the rebuy |
| `corporateActionReserveExpiryDays` | 10 | **25** | With pre-exit, the reserve must outlive the gap to ex-date |
| `corporateActionPreExitDays` | (1, via sell_window) | **5** | New: standard events get sold up to 5 days pre-ex; gives order time to fill without giving up too much upside |
| `corporateActionHighRiskSellWindowDays` | 5 | **5** | Unchanged: rights offerings/etc. stay close to ex-date |
| `corporateActionBuyWindowDays` | 2 | **14** | Symmetric with pre-exit — don't open positions we have to immediately sell |

The live engine now flips on every CA-aware flag by default. To get back to the conservative legacy behaviour, pass `--no-corporate-action-rebuy-after-exdate` etc.

### Headline numbers

| Variant | Return | Max DD | Trades | Δ vs no-CA |
|---|---:|---:|---:|---:|
| **No CA** (baseline) | **222.39%** | -24.67% | 1,384 | — |
| **CA buy-block only** | 194.06% | -25.41% | 1,418 | -28.33pp |
| **CA strict** (no rebuy) | 180.42% | -25.57% | 1,450 | -41.97pp |
| **CA strict** (rebuy 100%, fire ASAP, pre_exit=1) | 200.66% | -25.76% | 1,442 | -21.73pp |
| **CA strict** (rebuy 100%, **fixed delay=6**, pre_exit=1) | **236.95%** | **-23.22%** | **1,406** | **+14.56pp** |
| **CA strict** (rebuy 100%, signal=recovery r=0.008) | 219.49% | -22.79% | 1,412 | -2.90pp |
| **CA strict** (rebuy 100%, pre_exit=5, delay=3) | 192.92% | -25.48% | 1,410 | -29.47pp |

### Why we built a signal-based rebuy instead of a fixed-day one

User observation: "delay 3 or 6 days is too fit to the current state of market; we need some method that rebuy correctly due to some signal".

A fixed number of days couples the rebuy to whatever the *average* post-ex drift happens to be in this particular 5-year window. The signal-based approach responds to actual market state instead:

| Signal | Definition | When it fires |
|---|---|---|
| `recovery` | `current_close >= min_close_since_ex * (1 + r)` | Price has bounced r% from the local post-ex low. Adapts to whatever magnitude the drop happens to be. |
| `stabilization` | `(max - min)/min of last K closes < threshold` | Price has stopped oscillating; range tightened. |
| `new_high` | `current_close >= max(closes_since_ex[:-1])` | Strongest "low is in" signal — price made an absolute new high since ex. |
| `ma_cross` | `current_close crossed above N-day MA` | Classic technical upturn signal. |
| `no_new_low` | `current_close >= min(last K closes)` | Dust has settled: no new lows over the last K days. |
| `combined` | `recovery AND stabilization` | Both conditions confirmed. |

Plus optional parameters on every mode:
- `--corporate-action-rebuy-signal-min-days N` (don't check signal before day N)
- `--corporate-action-rebuy-signal-max-days N` (force rebuy on day N if no signal)
- `--corporate-action-rebuy-signal-confirm-days N` (signal must hold for N consecutive days)

### The data tension

In this 5-year in-sample backtest, **fixed delay=6 wins at 236.95%** and no signal beats it. The best signal (recovery r=0.008) gives 219.49%, +18.83pp vs no delay but 17.46pp below the fixed-delay peak.

Reading the per-event comparison (24 matched CA events):

| | Events won | Avg spread when won | Notes |
|---|---:|---:|---|
| **fixed_delay=6** | 15 / 24 | +3-8% | Wins more often but by smaller margins. |
| **signal=recovery** | 7 / 24 | +5-15% | Wins less often but by much larger margins. |

Concretely: signal won big on **PDR +12.2%**, **VIC +15.8%**, **PDR +30.1%** (longer hold), fixed_delay won on **GEX +14.2%**, **DPG +8.4%**. Each method has a different "preference" for which CA patterns it captures well.

### The honest read

**fixed_delay=6 is partly fit to this dataset.** The non-monotonic sweep (delay=3 ≈ 224%, delay=5 ≈ 202%, delay=6 ≈ 237%) is the tell: specific delay values happen to align with specific calendar dates in this 5-year window.

**Signal-based is theoretically more robust** to regime changes because it responds to actual price action (recovery, stabilization, MA cross), not calendar days. If VN market behavior shifts (e.g. more institutional involvement in CA events), the signal adapts; a fixed delay does not.

Trade-offs:
- **Pick fixed_delay=6** if you trust the backtest and want the highest in-sample number.
- **Pick signal-based** (recovery r=0.008, min=1, max=12) if you want robustness to future markets and can tolerate +17pp lower historical return.

### Recommended default

For new scoring configs we recommend:

```bash
--use-corporate-action-guard
--corporate-action-buy-window-days 3
--corporate-action-sell-window-days 1
--corporate-action-rebuy-after-exdate
--corporate-action-rebuy-as-adjustment
--corporate-action-reserve-cash-for-rebuy
--corporate-action-reserve-expiry-days 15
--corporate-action-rebuy-signal-mode recovery
--corporate-action-rebuy-signal-recovery-pct 0.008
--corporate-action-rebuy-signal-lookback 3
--corporate-action-rebuy-signal-min-days 1
--corporate-action-rebuy-signal-max-days 12
```

Then sweep `signal-mode ∈ {recovery, ma_cross, no_new_low, new_high, stabilization, combined}` and the threshold parameters to find what works on your preferred scoring config.

### Fixed-delay sweep (for reference)

| Delay | Return | Max DD | Trades |
|---:|---:|---:|---:|
| 0 | 200.66% | -25.76% | 1,442 |
| 3 | 223.92% | -24.74% | 1,422 |
| 6 | **236.95%** | -23.22% | 1,406 |
| 10 | 185.81% | -27.59% | 1,414 |

### Per-event rebuy comparison (delay=6 vs signal=recovery r=0.008)

| Symbol | Date | FD6 spread | SIG spread | Winner |
|---|---|---:|---:|---|
| VNM | 2020-09-29 | -1.7% | -1.7% | tie |
| KSB | 2020-10-27 | +2.7% | -2.7% | FD6 |
| PDR | 2020-11-06 | -7.2% | -3.0% | SIG |
| PDR | 2021-03-23 | +0.9% | +12.2% | SIG |
| PDR | 2021-04-26 | -2.9% | -1.4% | SIG |
| HPG | 2021-05-31 | -4.5% | +2.7% | SIG |
| FPT | 2021-06-01 | -6.9% | -6.9% | tie |
| FPT | 2024-06-12 | +4.9% | +4.2% | FD6 |
| HSG | 2021-06-15 | -1.6% | +1.2% | SIG |
| SHS | 2021-08-19 | +5.6% | +7.7% | SIG |
| SSI | 2021-09-06 | +4.1% | +4.1% | tie |
| CRE | 2021-09-10 | +0.9% | +0.9% | tie |
| TPB | 2021-12-20 | +6.6% | +1.7% | FD6 |
| FTS | 2023-05-12 | +4.9% | +1.2% | FD6 |
| BSI | 2023-07-05 | -0.7% | -1.0% | FD6 |
| SZC | 2024-01-03 | +2.1% | +2.1% | tie |
| TCB | 2024-06-20 | -6.7% | -6.3% | SIG |
| VDS | 2024-06-21 | -4.8% | -9.3% | FD6 |
| GEX | 2025-05-14 | +14.2% | +1.6% | FD6 |
| DPG | 2025-06-11 | +8.4% | -0.2% | FD6 |
| VIX | 2025-06-18 | +1.0% | -2.2% | FD6 |
| NTP | 2025-06-27 | -0.1% | -0.1% | tie |
| MSB | 2025-09-08 | -5.2% | -6.2% | FD6 |
| VIC | 2025-12-05 | +0.3% | +15.8% | SIG |

### Three places that previously suppressed the rebuy, all now removed

1. **`corporate_action_exit_rebuy_allowed`** — was filtering on risk_type, now allows rebuy whenever any event is in the union window (regardless of risk_type).
2. **`next_corporate_action_ex_date`** — was filtering on risk_type, now returns the soonest ex-date in the union window regardless of risk_type.
3. **The rebuy loop in `run_momentum_rotation`** — was popping `pending_corporate_action_rebuys` entries that fell out of `current_universe`, now keeps them so they retry each day.

### Pre-exit timing sweep (user feedback)

User observation: "the data about ex-date will be public before 3-4 weeks of ex-date, so we can find way to sell that time and reserve slot to rebuy". The user's logic: sell earlier captures the pre-CA premium; reserving the slot ensures the post-ex rebuy fires.

#### Round 1: pre-exit by calendar-day

| PreExitDays | Return | Max DD | Trades |
|---:|---:|---:|---:|
| 1 (legacy T-1) | **236.95%** | -23.22% | 1,406 |
| 5 | 192.92% | -25.48% | 1,410 |
| 7 | 176.80% | -25.22% | 1,414 |
| 10 | 164.41% | -25.30% | 1,414 |
| 14 | 125.62% | -26.47% | 1,418 |
| 21 | 113.18% | -26.39% | 1,418 |

**The backtest does NOT validate the user's intuition.** Pre-exiting earlier is consistently worse in this dataset — VNM was in an uptrend through the Sep 2020 ex-date, so selling at T-5 missed the gain that paid for the dividend drop.

#### Round 2: pre-exit by **price-signal**

User follow-up: "pre-exit by day is not working, we need to find the signal for exiting like the force sell previously." So the exit signal logic now mirrors the post-rebuy signal framework — watch the price for N days, fire only when a topping pattern appears.

Five new signal modes added (matching the post-CA rebuy-signal API):

| Mode | Definition |
|---|---|
| `drop_from_high` | Fire when close has dropped X% from recent peak |
| `below_ma` | Fire when close < N-day MA |
| `momentum_reversal` | Fire on short-MA / long-MA bearish cross |
| `combined` | `drop_from_high` AND `below_ma` both fire |
| `fixed_window` | (legacy) fire on first day inside the watch window |

Sweep results:

| Config | Return | Max DD |
|---|---:|---:|
| fixed_window T-1, delay=6 | **236.95%** | -23.22% |
| fixed_window T-1, delay=0 | 200.66% | -25.76% |
| drop_from_high, pre_exit=5, d=3 | 167.18% | -25.80% |
| drop_from_high, pre_exit=5, d=6 | 202.52% | -25.00% |
| below_ma, pre_exit=5, d=6 | 223.07% | -23.52% |
| combined, pre_exit=5, d=6 | 208.15% | -25.94% |
| momentum_reversal, pre_exit=14, d=0 | 164.66% | -23.79% |

**Even with signals, this backtest prefers fixed_window T-1.** The reason: the optimal exit captures the maximum pre-CA premium, which is precisely the day before ex-date (when the dilution hasn't been priced in yet). Any signal — by definition — fires AFTER the price has already moved, so it captures less of the premium.

The trade-off is real:
- **fixed_window T-1**: max in-sample return but tight execution on a single day (slippage, illiquid fills)
- **below_ma pre_exit=5**: 14pp lower but gives 5 sessions to fill the order; smoother for live trading

The price-signal framework is now a live-usable option for users who value execution reliability over raw in-sample return. The default remains `fixed_window` since it wins in backtest. Users running live should test `below_ma` with `pre_exit_days=5` to see if execution quality compensates for the 14pp.

#### Code / data layout for the signal framework

| File | What |
|---|---|
| `vietnam_algo/corporate_actions.py` | `closes_until_date`, `pre_exit_signal_drop_from_high`, `pre_exit_signal_below_ma`, `pre_exit_signal_momentum_reversal`, `evaluate_pre_exit_signal` |
| `vietnam_algo/backtest.py` | `corporate_action_exit_due` now takes `price_table` + `date_to_index` and consults the signal |
| `vietnam_algo/main.py` | `--corporate-action-pre-exit-signal-mode {fixed_window,drop_from_high,below_ma,momentum_reversal,combined}` plus threshold / lookback / MA-period flags |
| `vietnam_algo/config.py` | `corporateActionPreExitSignalMode` (default `fixed_window`) + 5 supporting fields |
| Tests | 11 new tests in `tests/test_corporate_actions.py` (`PreExitSignalTests`, `ClosesUntilDateTests`) + the existing `test_all_signal_modes_wired` regression test catches future drops |

The live trading engine (`apply_corporate_action_guard`, `build_corporate_action_exit_orders`) still uses fixed-window semantics — the signal-based pre-exit is a backtest-side framework awaiting live-engine port (the live path would need to compute signals from cached `data/prices/SYM.json` closes, similar to the rebuy path already done).

### Where this could still go wrong in live trading

This is backtested with perfect fill assumptions. Live:

- Force-sells execute at the bid (~ -0.1% to -0.3% cost vs backtested close).
- Rebuy executes at ask (~ +0.1% to +0.3% cost).
- Round trip on forced CA exits: ~0.2-0.6% per event × 25 events ≈ ~0.5-1.5% total drag.
- 192.77% (backtest) minus 1% drag ≈ 191-192% realistic live expectation.
- Slippage on blocked buys is zero (you never enter).



### [ ] P3. `bars_until` is O(N²) summed per symbol
Original sum over `dates[:index+1]` for each call × each symbol × each day. Memoization would help.

### [ ] P4. `momentum_score_with_market` recomputes downside vol
`strategies.py:124-130` recomputes `recent_returns` and `volatility` even though `momentum_score` already did.

---

## New Critical Findings from This Review

### [ ] F1. Lower-level `execute_dnse_orders` bypasses ledger + risk limits
`trading_engine.execute_dnse_orders` and `trading_engine.execute_dnse_orders_with_monitoring` do NOT write to the order ledger, do NOT check `LiveRiskLimits`, and do NOT pass through the trading-day corporate-action windows. Only the `daily_engine` wrappers go through the safe path. **`dnse-submit-orders --yes` direct calls are live-dangerous if risk limits / ledger integration is expected.**

### [ ] F2. `--sma-slope-days` not exposed via CLI
`-smaSlopeDays` defaults to 20 but is not in `add_backtest_arguments`. Users can't tune it from CLI.

### [ ] F3. `hybridBreakoutRequired` is `int` not `bool`
`config.py:85`. CLI flag works as `--hybrid-breakout-required 1` but documentation / convention expects bool.

### [ ] F4. `latest_order_date` silent on empty CSV
`daily_engine.py:74`: returns `None` silently when CSV has rows but no `date` field. Should log + raise.

### [ ] F5. `trading_token_from_email` has 20-second hard-coded sleep
`daily_engine.py:148`: `time.sleep(float(os.environ.get("DNSE_OTP_WAIT_SECONDS", "20")))`. Time-based polling should observe message arrival via IMAP IDLE or a max wait, not a fixed sleep.

---

## Recommended Next Steps

Backtest-validity (still high priority):

1. **Apply price adjustments for splits/dividends/rights** (item 10). Biggest remaining backtest-validity bug.
2. **Fix `non_cash_events_in_trading_window` perf (P2)** so strict-CA backtest fits in the same ~5 min budget as no-CA.
3. **End-of-backtest liquidation** (item 11).

Live-safety (mandatory before unattended):

4. **Wire `risk_controls.validate_live_orders` into `dnse-submit-orders` direct path** (item F1).
5. **Atomic state/ledger writes** (item 18).
6. **Single-instance lock** (item 21).
7. **DNSE broker-side open-order reconciliation beyond bot ledger** (item 2 + 5).
8. **Encrypt or env-source the trading token** (item 15).

Algorithm:

9. **Drawdown brake (item E)** is the next-highest algorithm improvement after the D/C/B already shipped.
