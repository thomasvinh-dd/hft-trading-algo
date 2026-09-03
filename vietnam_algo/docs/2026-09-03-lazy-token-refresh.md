# Lazy Token Refresh — 2026-09-03

Per user feedback: "we are getting trading token everyday, we only need to get that token when that day we wanna make a trade."

## What was wrong

The auto-bot ran a `should_refresh_token` block at 08:30 ICT every morning:
- Sent email OTP to Gmail
- Polled IMAP for the OTP
- Exchanged with DNSE for a JWT
- Stored in `state.tradingToken` + `state.tokenDate`

This happened even on:
- Days we wouldn't submit (no order plan)
- Holidays
- Days we run dry-run
- Days the market is closed

The token has an 8-hour expiry per DNSE spec, so refreshing every 24h is wasteful. Each refresh is ~10-30s of external infra (Gmail IMAP).

## What changed

1. **`vietnam_algo/token_store.py`** (new) — three primitives:
   - `parse_jwt_expiry(token)`: read `exp` from a JWT (no signature check; we just want the timestamp)
   - `is_token_fresh(token, leeway_seconds=300)`: True iff token is set AND not expiring within 5 min
   - `ensure_fresh_trading_token(client, state, ..., live_submit, notify_fn)`: refreshes only if missing or stale

2. **`vietnam_algo/bot_state.py`** — `AutoBotState` gained `tokenExpiry: str | None` (ISO8601 UTC of JWT `exp`).

3. **`vietnam_algo/auto_bot.py`** — **removed the standalone `should_refresh_token` block**. Token refresh now happens lazily:
   - On dry-run cycle: sets placeholder `dry-run-token` if empty (no DNSE call)
   - On live submit cycle: `ensure_fresh_trading_token()` is called inside the submit block, just before `run_daily_once(submit=True, ...)`. If the cached token is still valid (parse JWT exp), it's reused. Otherwise the email-OTP flow runs and we get a fresh one.

4. **`vietnam_algo/tests/test_token_store.py`** — 8 unit tests covering parse + freshness + edge cases.

## New flow

```
schedule fires at 09:16 (should_submit_orders)
   ↓
auto_bot.run_auto_bot() — submit block
   ↓
ensure_fresh_trading_token(client, state, live_submit)
   ├─ if !live_submit and token empty  → set 'dry-run-token' (no DNSE call)
   ├─ if is_token_fresh(token)         → reuse cached token
   ├─ else                             → trading_token_from_email(client)
   ↓
run_daily_once(submit=True, ..., trading_token=token)
```

## What still works

- 171/171 tests green (163 + 8 new)
- Telegram notify on token refresh (success + failure)
- Token stored in `auto_bot_state.json` (persists across bot restarts within same day)
- Token expiry persisted (`tokenExpiry` field) for crash recovery

## What changes for ops

- **Before**: 1 Gmail IMAP hit per day, every day, even when not trading
- **After**: 0 Gmail IMAP hits on non-trading days; 1 hit on the first trading day after token expires (~8h)

## Manual token refresh

To force a fresh token (e.g., after a long downtime, before a manual live trade):

```bash
ssh root@185.227.135.227
cd /root/hft-trading-algo
set -a; source vietnam_algo/env/.env.dnse-secrets; source vietnam_algo/env/.env.telegram; set +a
PYTHONPATH=. python3 -m vietnam_algo.main dnse-trade-secret
```

Or via Python:

```python
from vietnam_algo.token_store import ensure_fresh_trading_token
from vietnam_algo.bot_state import AutoBotState, save_state
from pathlib import Path
state = AutoBotState()
token, refreshed = ensure_fresh_trading_token(client, state, "0003937868", live_submit=True)
print("got token:", token[:30] + "...", "refreshed:", refreshed)
save_state(state, Path("/root/hft-trading-algo/vietnam_algo/data"))
```

## Files

- `vietnam_algo/token_store.py` (new, 5.4KB)
- `vietnam_algo/bot_state.py` (added `tokenExpiry` field)
- `vietnam_algo/auto_bot.py` (removed standalone token-refresh block)
- `vietnam_algo/tests/test_token_store.py` (new, 8 tests)

## Commit

`<commit-hash> Lazy token refresh: only fetch DNSE JWT when about to submit (not daily)`
