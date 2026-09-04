# Direct DNSE Access (VPS IP Whitelisted) — 2026-09-04

## Status change

DNSE team has added the VPS Singapore IP (`185.227.135.227`) to their whitelist. The bot no longer needs the Vietnamese proxy.

## Verified

| Endpoint | Latency (no proxy) | Latency (with proxy) |
|---|---|---|
| `/accounts` | 235ms | ~400ms |
| `/price/VCB/quotes/latest` | 181ms | ~400ms |
| `/balances` | 185ms | ~400ms |
| `/positions` | 190ms | ~400ms |

Roughly 2x faster without proxy.

## Action taken

`vietnam_algo/env/.env.dnse-secrets` updated:

```diff
-DNSE_PROXY_HTTP_URL=http://VN18996:jZhQ1b3Y@180.214.237.169:12345
+# DNSE_PROXY_HTTP_URL=  # DISABLED - VPS IP is whitelisted, direct connection
```

Bot restarted in background. Will continue with default behavior, just with ~200ms faster DNSE responses.

## Code unchanged

`vietnam_algo/dnse_client.py` still supports `DNSE_PROXY_HTTP_URL` — we just leave it empty. The proxy support stays in code as a fallback for:
- Future VPS moves
- Geo-block re-instatement
- Testing/development from non-whitelisted IPs

## Cost saving

- $2.75/month Vietnamese proxy subscription can be cancelled
- EOD backtest (5-10 min run) speeds up noticeably

## Things to monitor

- DNSE geo-block might re-apply if the whitelist is removed
- VPS IP might change (Contabo IP rotation) → would need to re-whitelist
- Connection latency should stay under 500ms; alert if it goes above

## Files

- `vietnam_algo/env/.env.dnse-secrets` (modified — gitignored, only on VPS)
- Bot process: PID 95932, running in dry-run mode on VPS, polling every 60s

## Don't forget

If you cancel the proxy subscription, remove this entry from proxydancu.com dashboard. Subscription auto-renews on 2026-10-04 at 69,000 VND.
