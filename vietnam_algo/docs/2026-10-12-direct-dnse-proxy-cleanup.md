# Market data: direct DNSE access confirmed (2026-10-12)

## What happened

On 2026-10-12, the bot started sending "market data STALE" warnings
because:

1. The price data files (`data/prices/{SYMBOL}.json`) were last
   updated to 2026-08-13, leaving the median latest-bar age at
   ~60 calendar days vs the 2-day threshold.
2. The `data_ready_for_eod` sentinel was missing.
3. The `common_sync` cron (which should keep the data fresh) was
   failing with `HTTP 407 Proxy Authentication Required` on
   `/market/instruments`.

## Root cause

The proxy URL `http://VN18996:jZhQ1b3Y@180.214.237.169:12345` was
still set in `/root/hft-trading-algo/vietnam_algo/env/.env.dnse-secrets`
as `DNSE_PROXY_HTTP_URL`. Per the 2026-09-04 doc
`vietnam_algo/docs/2026-09-04-direct-dnse-access.md`, the VPS IP
`185.227.135.227` was whitelisted on the DNSE OpenAPI server and
**all endpoints work via direct connection** (no proxy needed).
The user also noted in that doc that the $2.75/month proxy
"subscription can be cancelled". The env file was never updated to
clear the proxy URL, so the cron continued trying to route through
the (now defunct) proxy and the sync started failing.

The direct connection has been verified for the historical OHLC
endpoint too:
```
$ DNSE_PROXY_HTTP_URL="" python3 -c "from vietnam_algo.libs.dnse.client import ...; \
  c.get_ohlc(symbol='VCB', from_date='2026-10-09', to_date='2026-10-09', resolution='1')"
{'t': [1791512100, 1791512160, ...], ...}
```

## Fix

Set `DNSE_PROXY_HTTP_URL=` (empty) in
`/root/hft-trading-algo/vietnam_algo/env/.env.dnse-secrets`. The
DnseClient constructor now sees the empty string and does NOT
install a proxy opener (see `vietnam_algo/libs/dnse/client.py:331`):

```python
if os.environ.get("DNSE_PROXY_HTTP_URL"):
    if configure_http_proxy_from_env():
        use_proxy_opener()
```

`configure_http_proxy_from_env()` returns `False` when the URL is
empty, so no proxy is installed and `urllib.request.urlopen` uses
the default direct connection.

## Verification

After the fix, the next `run-common-sync` invocation completed
in ~10 minutes and:

- 1586/1588 symbols updated (2 invalid-symbol rejections, both
  added to the denylist)
- 201 price files refreshed (the ones whose last bar was before
  the new trading day)
- `data_ready_for_eod` sentinel created at the end of the run
- `get_market_data_freshness()` returns `fresh=True` with
  `median_latest_bar_age_days=1` (yesterday's close)

## Cron

The cron line is unchanged:
```
15 1 * * 1-5 cd /root/hft-trading-algo && set -a && \
  . /root/hft-trading-algo/vietnam_algo/env/.env.dnse-secrets && \
  . /root/hft-trading-algo/vietnam_algo/env/.env.telegram && \
  set +a && PYTHONPATH=/root/hft-trading-algo python3 -u -m \
  vietnam_algo.main run-common-sync --force >> /root/logs/common_sync.log 2>&1
```

It will now succeed nightly because the env no longer points to a
defunct proxy. The next 01:15 ICT run (Tuesday 2026-10-13) will
refresh prices + create a fresh sentinel.

## What about the DnseClient `_PROXY_OPENER` cache?

There's a small leftover concern: `_PROXY_OPENER` is a module-level
cache in `vietnam_algo/libs/dnse/client.py`. The first DnseClient
constructed in a process checks `DNSE_PROXY_HTTP_URL` and caches
the proxy opener. If a future process is started with the env set
to a stale URL, the opener is cached for the rest of the process.

For now this is a no-op because the env is empty. But if anyone
ever re-adds a proxy URL to the env, they should restart any
long-running bot process to clear the cache. The fix in
`configure_http_proxy_from_env()` to also bust the cache when
the URL changes is a future cleanup task.

## Related tickets

- 2026-09-04: VPS IP whitelisted on DNSE OpenAPI, direct
  connection works (`vietnam_algo/docs/2026-09-04-direct-dnse-access.md`)
- 2026-10-09: 04:30 ICT bot restart fixed, place_stock_order
  re-implemented (commit `59c70bf`)
- 2026-10-12: this doc
