# Vietnamese SOCKS5/HTTP Proxy Integration — 2026-09-03

DNSE confirmed all foreign IPs are blocked; only Vietnamese IPs allowed to call `openapi.dnse.com.vn`. To unblock the Singapore VPS, we route DNSE API calls through a paid Vietnamese datacenter proxy.

## What changed

1. **`vietnam_algo/dnse_client.py`** — added `configure_http_proxy_from_env()` + `use_proxy_opener()` that install a `ProxyHandler` globally on `urllib.request` when env var `DNSE_PROXY_HTTP_URL` is set.
2. **`vietnam_algo/env/.env.dnse-secrets`** — added `DNSE_PROXY_HTTP_URL=http://VN18996:jZhQ1b3Y@180.214.237.169:12345` (1-month subscription, ~$2.75).
3. **VPS `/etc/hosts`** — removed `127.0.0.1 openapi.dnse.com.vn` override (was forcing the SSH tunnel path).
4. **Workstation reverse SSH tunnel** — killed; no longer needed.
5. **Auto-bot on VPS** — restarted in dry-run; all DNSE calls now go through the proxy.

## Why HTTP and not SOCKS5

The proxy accepts both, but we discovered that the proxy's **SOCKS5 endpoint blocks `openapi.dnse.com.vn`** (curl returns `Can't complete SOCKS5 connection to openapi.dnse.com.vn. (2)`), while the **HTTP endpoint forwards it just fine**. So we use the HTTP port (12345) for DNSE only.

## Trust model

- **Cheap**: $2.75/month (cheaper than any Vietnam VPS)
- **Static IP**: doesn't rotate
- **Dedicated**: not shared with spammers (less likely to be DNSE-banned)
- **No logs**: per provider claims — taken on faith
- **TLS inspection**: the proxy operator sees our DNSE credentials in plaintext. Acceptable for paper trading; **NOT recommended for real-money trading without further diligence**.

## Verification

Tested from VPS via proxy (after removing `/etc/hosts` override):

```
$ python3 -m vietnam_algo.main dnse-sync-prices \
    --from-date 2026-08-25 \
    --no-skip-existing \
    --symbols VCB VIC VHM HPG FPT \
    --sleep-seconds 0.5

[1/5] VCB: 4 bars
[2/5] VIC: 4 bars
[3/5] VHM: 4 bars
[4/5] HPG: 4 bars
[5/5] FPT: 4 bars
Saved 5 DNSE price files (20 bars total)
```

Real `/price/VCB/quotes/latest` returns: `bid=58.7 (58,700 VND)  offer=58.8 (58,800 VND)` — confirms proxy forwards DNSE properly.

## How to disable

Set `DNSE_PROXY_HTTP_URL=` (empty) in `.env.dnse-secrets`. The proxy is opt-in; without the env var, code falls back to direct connection (which will fail with DNSE geo-block from VPS).

## Files

- `vietnam_algo/dnse_client.py` — added `configure_http_proxy_from_env`, `use_proxy_opener`, `is_proxy_active` (commit `c022156`)
- `vietnam_algo/env/.env.dnse-secrets` — added `DNSE_PROXY_HTTP_URL` line (gitignored)
- VPS `/etc/hosts` — `127.0.0.1 openapi.dnse.com.vn` removed
- Workstation tunnel process — killed
- Auto-bot — running PID on VPS, all cycles OK

## What to monitor

Watch for proxy errors in the auto-bot log. If the proxy goes down or expires, the next DNSE call will fail. Consider:
- Setting up a secondary proxy as fallback
- Adding a Telegram alert on first DNSE failure
- Subscribing to 3-month or 6-month plan if uptime proves reliable
