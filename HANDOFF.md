# Vietnam Algo Handoff — 2026-09-01

This is a brief a new agent reads on the VPS to continue the work
from the workstation session. It assumes **no memory of the prior
conversation**; everything below is the minimum state needed to be
productive immediately.

## 1. Identity of the project

Vietnam stock market algorithmic trading bot. Daily momentum
strategy with corporate-action awareness (rebuy after bonus/dividend
events). Reads price data from `data.vietnamfinance.vn` (FiinTrade) and
DNSE OpenAPI for the live side.

- Repo path on VPS: `/root/hft-trading-algo/`
- Workspace absolute path that opencode associates with this project
  on the workstation: `/home/vst/Documents/Ai-trade/hft-trading-algo/`
- Python 3.12, single-package layout under `vietnam_algo/`
- The project has its own test suite: `python3 -m pytest vietnam_algo/tests/`
  (163 tests, all green)

## 2. The VPS — what is set up and what is broken

```bash
ssh root@185.227.135.227   # password is <REDACTED - see the secret store>
```

- Ubuntu 24.04, 193GB disk, Python 3.12, `pip3` installed.
- Project repo synced at `/root/hft-trading-algo/`.
- SSH key auth: local-machine ed25519 key is on the VPS at
  `/root/.ssh/authorized_keys`. Passwordless ssh from the workstation.
- **DNSE openapi.dnse.com.vn is geo-blocked from the VPS** at L4.
  Workaround: a reverse SSH tunnel from the workstation to the VPS
  that forwards VPS port 443 → workstation → openapi.dnse.com.vn.
  See §6.
- All other Vietnamese sources reachable from the VPS
  (vietstock, market_holidays, etc.).

## 3. The repo — main files and what they do

```
vietnam_algo/
├── main.py                 # CLI dispatcher (sync-prices, backtest-momentum, dnse-*, etc.)
├── backtest.py             # Backtest engine (momentum/breakout/pullback/sector/hybrid)
├── strategies.py           # Signal generation
├── corporate_actions.py    # CA guard logic + post-CA rebuy signals
├── data_investing.py       # Investing.com fetcher (used for pre-2018 backfill)
├── data.py / dnse_client.py  # FiinTrade + DNSE OpenAPI + openapi.dnse.com.vn client
├── otp_reader.py           # IMAP-based email_otp reader (Gmail App Password)
├── order_monitor.py        # BBO pricing (DNSE /quotes/latest with nested bid/offer arrays)
├── trading_engine.py       # Submit, monitor, replace DNSE orders
├── daily_engine.py         # One run cycle: sync data → compute orders → submit
├── auto_bot.py             # The scheduled bot loop (Vietnam market hours schedule)
├── env/.env.vietnam-algo-best-ca-fixed-delay6   # GOLD config (236.95% in-sample)
├── env/.env.vietnam-algo-best-ca-recovery-r0.008   # Recovery r=0.008 variant (215.28%)
├── env/.env.dnse-secrets   # DNSE_API_KEY + DNSE_API_SECRET + Gmail creds
├── data/
│   ├── symbols.json                # 1417 VN tickers (master list)
│   ├── prices/                     # FiinTrade daily bars (1417 tickers, 2019-2026)
│   ├── prices_investing/           # Investing.com backfill (5y window)
│   ├── prices_dnse/                # 1411/1417 daily bars 2015-2026 from DNSE
│   ├── instruments_dnse.json       # DNSE instrument master
│   ├── corporate_action.json       # Vietstock events
│   ├── market_holidays_vn.json     # VN market holidays
│   ├── universe/liquid_stocks.json # Liquid universe
│   ├── backtests/                  # Backtest results + baselines (_ca_*.json)
│   └── live/
│       ├── latest_orders.csv / .json   # Latest order plan (preview)
│       ├── auto_bot_state.json         # Auto-bot state (last run timestamps)
│       └── dnse_trading_token.json     # Live JWT (8h TTL, sensitive!)
├── docs/2026-08-23-timeline.md    # Day log: what was built, what drifted, what's canonical
```

## 4. Source-of-truth configs (env files)

`vietnam_algo/env/.env.vietnam-algo-best-ca-fixed-delay6` is the **gold**
reproduction. Locked-in result: 236.95% / -23.22% DD / 1406 trades.
The `.env` is local-only (gitignored) — it must be present at the path
above before any backtest or run.

The exact command to reproduce is in the env file as
`VIETNAM_ALGO_BACKTEST_COMMAND`. The `Re-buy` defaults at HEAD are
**reverted to False**; the env command re-asserts them explicitly:

```bash
--corporate-action-rebuy-after-exdate
--corporate-action-rebuy-as-adjustment
--corporate-action-reserve-cash-for-rebuy
--corporate-action-reserve-expiry-days 25
--corporate-action-rebuy-bull-max-rank 10 --corporate-action-rebuy-neutral-max-rank 5
--corporate-action-rebuy-min-avg-volume 300000
--corporate-action-pre-exit-days 1   # the silent killer — default 5 costs ~17pp
--corporate-action-rebuy-delay-days 6
```

**Critical: `corporate-action-pre-exit-days 1` is mandatory.** Default at
HEAD is 5 (wider window = exits 5 days before ex-date, which loses ~17pp).

The same defaults are encoded in `vietnam_algo/env/.env.vietnam-algo-best-ca-recovery-r0.008` for the signal-based variant (215.28% / -25.22% DD / 1414 trades).

## 5. Daily pipeline on the VPS

```bash
ssh root@185.227.135.227
cd /root/hft-trading-algo
set -a && source vietnam_algo/env/.env.dnse-secrets && set +a

# (1) refresh trading token (sends email OTP, polls IMAP, exchanges) ~30s
python -m vietnam_algo.main --data-dir vietnam_algo/data dnse-trade-secret

# (2) refresh prices for all 1417 tickers (~12 min, skip-existing aware)
python -m vietnam_algo.main --data-dir vietnam_algo/data dnse-sync-prices \
    --from-date 2015-01-01

# (3) live orderbook pricing inspection (BBO + 3-level depth, both boards)
python -m vietnam_algo.main --data-dir vietnam_algo/data dnse-quote --symbol VCB

# (4) margin config for the account
python -m vietnam_algo.main --data-dir vietnam_algo/data dnse-loan-packages \
    --account-no 0003937868 --symbol VCB
#   1775 = "GD Tiền mặt" (Cash, 100% init)
#   1769 = "RocketX"  (50% init, 0.04% fee)

# (5) generate today's order plan (no live submit)
python -m vietnam_algo.main --data-dir vietnam_algo/data daily-generate-orders

# (6) submit today's plan (LIVE; --yes required)
python -m vietnam_algo.main --data-dir vietnam_algo/data daily-submit-orders \
    --account-no 0003937868 --loan-package-id 1775 --yes

# (7) run the auto-bot in a loop (Vietnam market hours, default dry-run)
python -m vietnam_algo.main --data-dir vietnam_algo/data auto-trading-bot \
    --account-no 0003937868 --loan-package-id 1775
#   add --live-submit to actually trade
```

## 6. Reverse SSH tunnel (the DNSE lifeline)

`openapi.dnse.com.vn:443` is hard-blocked from the VPS IP at L4. The
workaround routes VPS port 443 through the workstation via SSH.

On the workstation (where the user actually works):

```bash
setsid bash -c 'exec ssh -N \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    -R 443:openapi.dnse.com.vn:443 \
    root@185.227.135.227' </dev/null >/var/tmp/dnse-tunnel.log 2>&1 &
disown
```

systemd user-mode fails on this workstation (`setgroups` blocked), so
the cron watchdog is used instead. Add this entry to the workstation's
crontab:

```bash
* * * * * pgrep -f "ssh -N.*185.227.135.227" >/dev/null || \
  setsid bash -c "exec /usr/bin/ssh -N -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    -R 443:openapi.dnse.com.vn:443 root@185.227.135.227" \
  </dev/null >/var/tmp/dnse-tunnel.log 2>&1 & disown
```

On the VPS, the route `openapi.dnse.com.vn` is forced to localhost via
`/etc/hosts`:

```
127.0.0.1 openapi.dnse.com.vn
```

(only on the VPS; do not put this on the workstation).

The default base URL `https://openapi.dnse.com.vn` works on the VPS without
changing the client. `https://openapi.dnse.com.vn:8443` would be needed if
the tunnel used a non-standard port.

## 7. DNSE API specifics (handed to a fresh agent)

- **Base URL**: `https://openapi.dnse.com.vn`
- **Version header**: `version: 2026-07-23`
- **Auth**: HMAC-SHA256 over `(request-target): {method} {path}\ndate: {RFC2822}\nnonce: {hex}`. **Query string is NOT in the signing string.** Headers: `Date`, `x-api-key`, `version`, `X-Signature`. Header format:
  `Signature keyId="<key>",algorithm="hmac-sha256",headers="(request-target) date",signature="<base64url>",nonce="<hex>"`
- **Path query key** for `/price/ohlc` is `type=STOCK` (not `barType`)
- **Resolution** `1D` = daily, `1` = 1-minute intraday (default in
  `fetch_dnse_history_chunked` is `1D` daily)
- **Response shape** for `/price/{sym}/quotes/latest`:
  `{"quotes": [{"bid": [{"price", "quantity"}], "offer": [...], "boardId", ...}]}`
  (positional arrays, NOT flat `bidPrice1`/`askPrice1`).
- **Full daily history** for a single symbol fits in ONE call
  (~2900 bars 2015-2026); use `chunk_days=180` to be safe.

## 8. Status of work items (from `docs/2026-08-23-timeline.md`)

| Item | Status | Note |
|---|---|---|
| Gold config (delay=6, pre-exit=1) | 236.95% reproducible | env file |
| Recovery r=0.008 | 215.28% reproducible | env file; 4pp drift from 219.49% baseline noted |
| Pre-exit signal framework | done, 4 modes | defaults to fixed_window |
| Post-CA rebuy signal framework | done, 7 modes | default mode = fixed_delay |
| Investing.com price backfill | done, 21 tests | Cloudflare-blocks-this-server; run from workstation |
| DNSE live trading | end-to-end verified | via reverse SSH tunnel |
| Reverse SSH tunnel setup | done | cron watchdog on workstation |
| Auto-bot dry-run on VPS | works | 1384 trades / 222.39% (matches local) |
| DNSE daily price sync | 1407/1417 = 99.29% | 11 tickers DNSE doesn't carry — see below |

**DNSE price coverage (snapshot 2026-08-31 17:43):**
- Master list: 1417 tickers (from `data/symbols.json`, FiinTrade)
- DNSE prices_dnse files: 1411, of which **1407 have bars**, 4 are empty
- Newest bar in 872 tickers: **2026-08-28** (others lag by a few days — typical
  for thinly-traded UPCoM names)
- Total bars: 2,781,579
- Full manifest at `vietnam_algo/data/prices_dnse/SYNC_REPORT.json`
- **Unfillable from DNSE (11 tickers, all delisted or not in DNSE's book):**
  - `HTTP 400 invalid symbol`: BCG, BCR, FUCTVGF3, RDP, TAN, VE2, VTS
  - `HTTP 200 with items=[]`: CNA, EGL, TBW, UTT
- 1 extra in prices_dnse not in master: **VND** (DNSE carries it, FiinTrade master doesn't)
- To re-run: `python -m vietnam_algo.main dnse-sync-prices --from-date 2015-01-01` (skips existing by default; ~20s)
- To force-refresh everything: add `--no-skip-existing` (takes ~30h for 1417 tickers at 0.3s sleep)

## 9. Open items the fresh agent should keep working on

- **Tighten security on the VPS**: `/root/hft-trading-algo/dnse.txt` and
  `/root/hft-trading-algo/vietnam_algo/data/live/dnse_trading_token.json`
  are still present in plaintext. Recommend wipe + rotate key.
- **Run the DNSE final sync from the workstation**, then
  `rsync -az vietnam_algo/data/prices_dnse/ root@185.227.135.227:/root/hft-trading-algo/vietnam_algo/data/prices_dnse/`
  — only 7 tickers remain missing on the VPS.
- **Pre-exit-days default is 5** in `vietnam_algo/config.py` but should
  be 1 to match the gold config. Worth a PR to flip the default.
- **Pre-exit signal mode default** is `fixed_window` (legacy T-1). The
  signal modes (drop_from_high, below_ma, etc.) under-perform fixed_window
  in this backtest.
- **VNDS orderbook data** is L1+BBO+2 levels; L2 depth requires the DNSE
  WebSocket feed (WSS). Not built yet.
- **No unit test for `quote_match_price` DNSE shape** beyond the
  basic happy path. Worth a `test_dnse_quote_match.py` (already exists
  with 5 tests in `vietnam_algo/tests/test_dnse_quote_match.py`).

## 10. Quick smoke tests to confirm everything is wired

```bash
# 1. Tests still green
cd /root/hft-trading-algo
python3 -m pytest vietnam_algo/tests/ -q
#   expected: 163 passed

# 2. DNSE live through tunnel
ssh root@185.227.135.227 curl -sS -m 5 -w "  HTTP %{http_code} time=%{time_total}s\n" \
    -H "X-API-Key: test" -H "version: 2026-07-23" https://openapi.dnse.com.vn/accounts | head
#   expected: HTTP 401 (real API, just bad key) or 404

# 3. CA-aware backtest
set -a && source vietnam_algo/env/.env.vietnam-algo-best-ca-fixed-delay6 && set +a
python3 -m vietnam_algo.main backtest-momentum \
    --max-positions 10 --bear-max-positions 2 --max-sector-positions 3 \
    --exit-rank 30 --max-breakout-entries-per-week 1 --breakout-max-rank 15 \
    --neutral-exposure-percent 60 --bear-exposure-percent 15 \
    --trailing-stop-percent 9 --neutral-trailing-stop-percent 6 --bear-trailing-stop-percent 4 \
    --use-vol-targeting --vol-target-annualized 0.25 --vol-window-days 252 --vol-max-leverage 1.5 \
    --use-downside-volatility --volatility-penalty-weight 0.3 \
    --trend-up-weight 0.10 --sma-slope-weight 0.25 \
    --relative-strength-3m-weight 0.30 --relative-strength-6m-weight 0.10 \
    --use-corporate-action-guard \
    --corporate-action-window-days 3 --corporate-action-buy-window-days 3 --corporate-action-sell-window-days 1 \
    --corporate-action-pre-exit-days 1 \
    --corporate-action-high-risk-buy-window-days 5 --corporate-action-high-risk-sell-window-days 3 \
    --corporate-action-rebuy-after-exdate --corporate-action-rebuy-as-adjustment \
    --corporate-action-reserve-cash-for-rebuy --corporate-action-reserve-expiry-days 25 \
    --corporate-action-rebuy-delay-days 6 \
    --corporate-action-rebuy-bull-max-rank 10 --corporate-action-rebuy-neutral-max-rank 5 \
    --corporate-action-rebuy-min-avg-volume 300000 \
    --corporate-action-rebuy-signal-mode fixed_delay
#   expected: totalReturnPercent ≈ 222.39 (current FiinTrade snapshot)
#   the saved baseline _ca_delay_v2_6.json reports 236.95% (older snapshot)

# 4. Test that the DNSE Python client works through the tunnel
python3 -c "
import sys; sys.path.insert(0, '/root/hft-trading-algo')
from vietnam_algo.dnse_client import DnseClient
with open('/root/hft-trading-algo/dnse.txt') as f:
    lines = [l.strip() for l in f if l.strip()]
api_key = next(l.split(':', 1)[1].strip() for l in lines if l.startswith('dnse api key'))
api_secret = next(l.split(':', 1)[1].strip() for l in lines if l.startswith('api secret'))
c = DnseClient(api_key, api_secret)
q = c.get_latest_quote('VCB')
print(f'  VCB bid: {q[\"quotes\"][0][\"bid\"][0]}')"
#   expected: VCB bid: {'price': 60.0, 'quantity': 22750}
```

## 11. Who to ask for what

- The **user** (vinhdongdo123@gmail.com) owns the VPS, the API keys,
  and the Gmail app password for the IMAP OTP reader.
- The **tunnel must be running on the user's workstation** for
  anything DNSE to work on the VPS. If the user reboots their
  laptop, the cron on the workstation will restart the tunnel.
