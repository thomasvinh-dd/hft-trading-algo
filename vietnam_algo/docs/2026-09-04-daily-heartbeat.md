# Daily Heartbeat Notification — 2026-09-04

## Why

Per user request: "let do the daily heartbeat notify" — we want a Telegram message every day saying the bot is still alive, even on non-trading days when nothing else fires.

## What's new

| File | Change |
|---|---|
| `vietnam_algo/notifier.py` | `heartbeat_message(account_no, live_submit, state, proxy_url, proxy_check, pid, uptime_seconds)` — formats 💓 message with uptime, last-cycle dates, proxy probe |
| `vietnam_algo/schedule.py` | `TradingSchedule.heartbeatTime: time(10, 30)` (default 10:30 ICT); `should_send_heartbeat()` — fires when crossing that time and no heartbeat sent today yet |
| `vietnam_algo/bot_state.py` | `AutoBotState.lastHeartbeatDate: str | None` |
| `vietnam_algo/auto_bot.py` | New heartbeat block at end of main loop: quick proxy probe + notify + persist state. Captures `bot_start_time` at function entry for accurate uptime |
| `vietnam_algo/main.py` | `--heartbeat-time HH:MM` CLI flag for testing |

## Trigger logic

```
now = vietnam_now()
today = local_date(now)
should_send_heartbeat(now, state.lastHeartbeatDate):
   return (today != state.lastHeartbeatDate) and (now.time() >= 10:30)
```

The heartbeat fires **even on non-trading days** — the bot doesn't check `is_trading_day` for this. So you'll always get a "still alive" signal within the first 60s after 10:30 ICT each day.

## Message format

```
💓 Vietnam Algo: DAILY HEARTBEAT
pid: 76138
uptime: 1d 4h 17m
account: 0003937868
mode: dry-run
last-market-holidays: 2026-09-04
last-trading-token: (none yet)
last-submit: 2026-09-04
last-EOD: 2026-09-04
last-CA-sync: 2026-09-04
last-heartbeat: 2026-09-04
proxy: http://180.214.237.169:12345 (ok, 412ms)
```

Failure mode (proxy unreachable):

```
💓 Vietnam Algo: DAILY HEARTBEAT
...
proxy: http://180.214.237.169:12345 (FAIL, err=[Errno 110] Connection timed out)
```

## Proxy connectivity probe

Each heartbeat triggers a quick `urllib.request` GET to `https://openapi.dnse.com.vn/accounts` with a dummy API key. If DNSE returns any HTTP response (401/404/etc.), the proxy is working. Times out after 10s. Failure does NOT prevent the heartbeat from being sent — we just include the failure in the message.

## Verified

- Foreground test with `--heartbeat-time 01:43` fired heartbeat at 01:43 ICT
- State file updated: `"lastHeartbeatDate": "2026-09-04"`
- Both success and failure message formats tested (msg_id 58 + 59 sent successfully)
- 171/171 tests still green

## Bot current state

```
PID 77044 — running persistently (sleep_seconds=60, default heartbeatTime=10:30)
lastHeartbeatDate: 2026-09-04 (already set; next heartbeat tomorrow)
```

## Testing override

```bash
ssh root@185.227.135.227
cd /root/hft-trading-algo
set -a; source vietnam_algo/env/.env.dnse-secrets; source vietnam_algo/env/.env.telegram; set +a
# launch with heartbeat 2 min from now
HB_TIME=$(TZ=Asia/Ho_Chi_Minh date -d "+2 minutes" "+%H:%M")
python3 -u -m vietnam_algo.main auto-trading-bot \
    --account-no 0003937868 --loan-package-id 1775 \
    --sleep-seconds 20 --heartbeat-time $HB_TIME
```

You should see `[YYYY-MM-DD... HH:MM:SS Sending daily heartbeat` in the log within ~30s of the configured time, and a Telegram message lands in your chat.

## Commit

`6bd013c Add daily heartbeat notify (10:30 ICT default, --heartbeat-time override)`
