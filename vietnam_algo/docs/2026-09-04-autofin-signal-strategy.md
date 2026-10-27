# autofin_signal Strategy — 2026-09-04

A second registered strategy that mirrors autofin.vn signals to DNSE orders. Shares the same DNSE infrastructure (account, prices, proxy fallback, Telegram notifications, state files) as the legacy momentum strategy — each strategy gets its own state file, env file, and order CSV.

## How it works

Each cycle:

1. **Login to autofin.vn** — `POST /gateway/api/auth/sign-in` with `{type:"email", email, password}` → returns `accessToken` + `refreshToken`. Tokens (~7d JWT) cached in `data/live/autofin_token.json`. Auto-refresh on 401.

2. **Fetch active signals** — `GET /gateway/api/gw/v1/signals/signal-static-suggest?pageSize=20&pageIndex=1`. Returns ~20 signals with:
   - `symbol`, `companyName`
   - `lastRecomment` (human-readable e.g. "Khuyến nghị đầu tư")
   - `supply_F`/`supply_T`: recommended buy zone (×1000 of VND, since DNSE stores raw VND)
   - `tP_F`/`tP_T`: take-profit target
   - `sL_F`/`sL_T`: stop-loss
   - `rateForNav`: target portfolio allocation (0..1)
   - `isWaittingBuy` / `isWaittingSell`: explicit trigger flags
   - `statusDeal.isClosed`: false = autofin currently holds this position

3. **Fetch our DNSE portfolio** — calls `build_portfolio_snapshot(client, account)` → positions map + total NAV.

4. **Compute target orders** — for each autofin signal:
   - **`isWaittingBuy=True`** → BUY at supply zone high (1 board lot default)
   - **`isWaittingSell=True`** → SELL current holding at TP zone low
   - **`statusDeal.isClosed=False`** → mirror: target_qty = round(NAV × rateForNav / ref_price / 100) × 100 board lots. delta = target - current. If |delta| ≥ 1 lot → BUY/SELL the delta.

5. **Write orders CSV + JSON** — `data/live/latest_orders.autofin_signal.csv` and `.json`. The existing DNSE submit pipeline (auto_bot → run_daily_once → execute_orders_with_ledger) takes over from there.

## Files

| File | Change |
|---|---|
| `vietnam_algo/autofin_signals.py` | NEW: `AutofinClient` (login + signal fetch with token cache + auto-refresh), `AutofinSignal` dataclass, `parse_signal()` |
| `vietnam_algo/strategy_base.py` | NEW: extracted `StrategyAdapter` ABC here to break circular import between `strategy_registry` and `strategies_impl` |
| `vietnam_algo/strategies_impl/__init__.py` | NEW (empty) |
| `vietnam_algo/strategies_impl/autofin_signal.py` | NEW: `AutofinSignalAdapter` (generates orders from signals, mirrors autofin's NAV allocation) |
| `vietnam_algo/strategy_registry.py` | Register `autofin_signal` in `STRATEGY_REGISTRY` |
| `vietnam_algo/env/.env.autofin-signal` | NEW env template with `AUTOFIN_EMAIL` + `AUTOFIN_PASSWORD` |
| `vietnam_algo/main.py` | `--strategy` flag now accepts any registered strategy (was hardcoded to `['momentum', 'hybrid']`) |
| `vietnam_algo/tests/test_autofin_signals.py` | NEW: 3 tests (basic parse, missing fields, alternate keys) |

## CLI

```bash
$ python3 -m vietnam_algo.main list-strategies
  momentum                   Liquid momentum rotation with corporate-action awareness
    env: vietnam_algo/env/.env.vietnam-algo-best-ca-fixed-delay6
  autofin_signal             Mirror autofin.vn BUY/SELL signals to DNSE orders
    env: vietnam_algo/env/.env.autofin-signal

$ python3 -m vietnam_algo.main auto-trading-bot \
    --account-no 0003937868 --loan-package-id 1775 \
    --sleep-seconds 60 --strategy autofin_signal
```

## Verified

- Login API returns accessToken + refreshToken ✓
- Signal API returns 20 active signals per page ✓
- All current autofin signals are existing open positions (none currently in waiting_buy or waiting_sell) ✓
- Strategy correctly handles NAV=0 → no orders planned (just mirror logic) ✓
- Per-strategy state file: `auto_bot_state.autofin_signal.json` ✓
- Per-strategy order file: `latest_orders.autofin_signal.csv` ✓

## Testing notes

Our DNSE account currently has 0 cash + 0 positions, so `portfolioValue=0`. With NAV=0, `target_qty = 0 × rateForNav / price = 0` — no orders planned. Once the account is funded, the strategy will automatically plan orders each cycle to match autofin's `rateForNav` allocation.

To test with mock NAV, set a small number (e.g. inject a `MOCK_NAV` env var that overrides `snap.totals["portfolioValue"]`) and run dry-run.

## Commits

```
421abdc Add env template .env.autofin-signal with creds
df3fb28 Add autofin_signal strategy: mirror autofin.vn signals to DNSE orders
```

180/180 tests pass.
