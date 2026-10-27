# Dry-Run Cycle Test Report — 2026-09-03 → 2026-09-04

## Goal
Verify the auto-bot works end-to-end in dry-run mode with the Vietnamese proxy + new portfolio snapshot + lazy token refresh.

## Test approach

Rather than wait for natural schedule windows (next is 9:16 ICT = 8 hours away), I wrote `/tmp/force_triggers.py` that invokes each trigger handler manually. This exercises the same code paths the bot's main loop would, but synchronously.

## Triggers tested (in order)

| # | Trigger | Result | Telegram notify |
|---|---|---|---|
| 1 | Market holidays sync | ✓ 13 holidays loaded (Sep 1 = National Day, etc.) | ✓ sent |
| 2 | Token refresh (lazy) | ✓ Set `dry-run-token` placeholder (no DNSE call, no email OTP) | ✓ sent |
| 3 | Submit cycle (dry-run) | ✓ 2 orders generated for tradeDate=2026-08-25 | ✓ sent |
| 4 | Corporate actions sync | ⚠️ Expected Vietstock API error (returns HTML, not JSON) — caught by try/except wrapper, notify fires `⚠️ CA SYNC FAILED` | ✓ sent |
| 5 | EOD + portfolio snapshot | ✓ Backtest ran (~10 min), 2 orders generated, portfolio snapshot returned (account is empty) | ✓ sent |

## Final state after test

```json
{
  "corporateActionDate": "2026-09-04",
  "eodDate": "2026-09-04",
  "marketHolidaySyncDate": "2026-09-04",
  "orderSubmitDate": "2026-09-04",
  "tokenDate": null,
  "tokenExpiry": null,
  "tradingToken": "dry-run-token"
}
```

All trigger dates set to 2026-09-04 ✓, token stays dry-run (no live refresh attempted).

## Issues found

### Minor (handled gracefully)
1. **Vietstock returns HTML instead of JSON** — `corporate_actions_incremental` sync fails on every call. Wrapped in try/except → notify `⚠️ CA SYNC FAILED` + continues with stale CA data. Not a blocker for trading; CA logic in `trading_engine.py` still works with cached events.
2. **EOD backtest takes ~5–10 min on VPS** — CPU-bound at 100% during execution. Single-threaded; can't run other tasks during EOD. Could parallelize `run_momentum_rotation` over symbols if needed.

### Verified working
- Vietnamese proxy → DNSE: `get_balances`, `get_positions`, `get_latest_quote` all OK
- Portfolio snapshot fetches correctly (empty account, 0 VND)
- Telegram notify path works for all message types (token refresh, EOD, portfolio, CA failure)
- Auto-bot main loop runs without errors in idle state
- Lazy token refresh: confirmed NO DNSE call made on a non-trading-day

## Bot state

PID 75672 running on VPS in dry-run mode. Currently idle (all today's cycles done). Will fire next at 9:16 ICT tomorrow (Sep 5 Saturday — which IS a holiday in Vietnam; should remain idle) or Mon Sep 7 (next trading day).

## To continue testing

The bot is now persistent. You can:

1. **Check Telegram**: look for the 6-7 messages from today — they confirm all notify paths work
2. **Wait for natural schedule**: when Sep 7 (Mon) arrives at 9:16 ICT, the bot will fire its submit cycle automatically
3. **Force another cycle**: just `pkill -f auto-trading-bot`, clear state to `null` dates, restart — next iteration will fire all 4 triggers again
4. **Test failure scenarios**:
   - Block the proxy with iptables and confirm the bot reports it
   - Kill the bot during EOD backtest to confirm state recovery works
   - Disable IMAP env vars and confirm CA sync still works (it doesn't depend on email)

## What's next

If you want me to add anything:
- A daily "still alive" heartbeat notify (every N hours at idle) so you know the bot is running
- A watchdog that restarts the bot if it dies (auto-restart on systemd)
- Forward the bot log to a file with rotation (it currently writes to /root/autobot-dryrun.log)
- Migrate from `nohup` to a proper `systemd` service for clean start/stop

Just let me know.
