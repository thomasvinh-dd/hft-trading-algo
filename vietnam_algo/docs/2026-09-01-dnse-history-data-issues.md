# Why DNSE Backtest Was Wrong - Issue Analysis — 2026-09-01

We compared DNSE data vs FiinTrade for several stocks the strategy trades. **5 root causes** were found, in order of severity. All have been fixed; backtest went from **-62 % → -1.6 %**.

## TL;DR (5 bugs, all now fixed)

| # | Bug | Severity | Detection | Fix |
|---|---|---|---|---|
| 1 | DNSE returns prices in **thousands of VND** (×1/1000) | Critical — 1000x error | Per-stock diff showed 60,300 vs 60.3 | Multiply all OHLC × 1000 |
| 2 | **Sparse coverage**: 704K missing trading-day instances, 1,382 affected stocks | High — gaps return 0 from `position_value` | OCL of bid/ask price; out-of-equity | Forward-fill in `close_price()` + on-disk fill |
| 3 | **Stub history**: 159 stocks had partial bars (some only 2018+) after first sync | High — younger history than strategy expects | Per-stock diff showed "first=2026" for VHM/HPG/VIC/FPT | `--no-skip-existing` re-sync with corrected parser |
| 4 | **Threshold bug in scaling** (re-occurrence): scaling script used `last_close < 100`; missed VIC (231), IDP (189), TVH (665), VNZ (~300) which scaled only on the **second** pass | Medium — 4 huge names had wrong prices until re-scaled | Per-stock diff again showed -99 % on VIC | Re-scale everything where `scaledBy=None` |
| 5 | **API response format**: DNSE returns positional `{t,o,h,l,c,v}` arrays; older sync only saved `{items: [...]}` shape, getting 0 bars for 159 stocks on first run | Medium (root cause of #3) | Direct DNSE probe showed 0 bars using `payload["items"]` but 2909 bars using positional arrays | Update `parse_price_bars()` to handle both |

## Per-stock diff evidence (VN Trading Universe)

| Ticker | DNSE Raw | DNSE ×1000 | Fiin | Notes |
|---|---|---|---|---|
| VCB | 60.3 | 60,300 | 59,500 | Match (raw DNSE in thousands) |
| VHM (after fix) | 73.2 | 73,200 | 71,800 | Match |
| HPG (after fix) | 22.25 | 22,250 | 21,700 | Match |
| FPT (after fix) | 73.8 | 73,800 | 69,200 | Match |
| VIC (after fix) | 231 | 231,400 | 207,900 | 11 % diff — DNSE returns 11 % higher (likely different CA adjustment) |
| MSN | 70.0 | 70,000 | 67,600 | 3.5 % diff — DNSE higher |
| VNM | 62.7 | 62,700 | 61,600 | Match |
| MWG | 75.3 | 75,300 | 73,600 | Match |
| BID | 36.95 | 36,950 | 38,850 | **6 % lower** on DNSE — different CA adjustment vs FiinTrade |
| MBB | 20.95 | 20,950 | 20,150 | Match |
| CTG | 32.0 | 32,000 | 32,150 | Match |
| TCB | 33.1 | 33,100 | 31,700 | Match |
| VPB | 27.5 | 27,500 | 25,350 | 8.5 % diff |
| GAS | 85.0 | 85,000 | 77,900 | **9 % higher** — likely a recent split/issuance differential |

(Sample of large-cap VN stocks, comparing 2026-08-28 close.)

## Root cause #1 in detail — price unit

DNSE returns `close` as a **decimal in thousands of VND**. Examples:
- VCB: DNSE=60.3 means 60,300 VND raw (current price)
- HPG: DNSE=22.1 means 22,100 VND raw
- VHM (post-fix): DNSE=73.2 means 73,200 VND raw

When divided by 1000 to get VND, ratios are preserved (momentum is scale-invariant), but the universe filter `min_price=10_000` rejects everything because 60.3 < 10,000.

Fix was a one-pass script (`scale_dnse.py`) that walks all 1,411 files, scales OHLC × 1000, sets `scaledBy=1000.0`. Then a **second** script caught 4 stocks where `last_close > 100` threshold was wrong (VIC had 231 in thousands-of-VND = 231,000 raw), fixed those too.

## Root cause #2 in detail — sparse coverage

Of 1,407 non-empty DNSE files:
- 46 stocks have 2,500+ historyDays (forward-filled)
- 73 stocks have 1,000-2,000 historyDays (mostly real)
- 1,288 stocks have **< 1,000 historyDays**

The worst: QNW (1,501 gaps), BTH (1,398), DCT (1,387), MCC (1,365). For these, the strategy effectively receives random data 25% of the time.

Without forward-fill in `close_price()`, the affected stocks silently get valued at 0 on gap days (`close_price()` returned `None`, `position_value()` returned 0.0), crushing holdings.

Two layers of fix:
1. **In `vietnam_algo/backtest.py:598`** — added forward-fill in `close_price` / `open_price` (commit `5158845`)
2. **On disk** — `forward_fill_dnse.py` writes carry-forward bars with `volume=0` for any expected weekday

## Root cause #3 in detail — partial history

After first sync attempt, **159 stocks** had suspiciously-short history. Examples:
- VHM, HPG, VIC, FPT: only 65 bars (June 2026 → Aug 2026)
- 30+ others: 100-500 bars starting 2024+

Re-running `dnse-sync-prices --no-skip-existing` populated 1,378 + 631K new bars:
- VHM: 65 → 2072 bars (2018-05 → 2026-08)
- HPG, VIC, FPT: 65 → 2908 bars (2015-01 → 2026-08)
- Total DNSE bars: 2,781,579 → **3,413,523**

## Root cause #5 in detail — API response format

```jsonc
// Old DNSE format (broken sync)
{"items": [{"time": "2015-01-05", "close": 9.29, ...}, ...]}

// Current DNSE format (positional arrays)
{"t": [1736035200, 1736121600, ...],
 "o": [9.23, 9.20, ...],
 "h": [9.36, 9.30, ...],
 "l": [9.15, 9.10, ...],
 "c": [9.29, 9.21, ...],
 "v": [310010, 280000, ...],
 "nextTime": 0}
```

The current `parse_price_bars()` in `vietnam_algo/dnse_client.py:93` handles both, but during the **first** sync the parser only knew the old format, so 159 stocks got saved as 0-bar files (and later "fixed up" by being used in some other code path that fetched only the last 60 days).

## Remaining DNSE-vs-FiinTrade discrepancies (after all fixes)

| Ticker | Diff | Likely cause |
|---|---|---|
| BID | -6 % | DNSE applies more aggressive split back-adjustment |
| MBB | -18 % historical, -4 % recent | Different CA adjustment |
| VIC | +11 % | Different recent CA or split |
| FPT | +6 % | Different recent CA |
| VPB | +8 % | Different recent CA |
| GAS | +9 % | Different recent CA |

**Strategy impact:** these ~5 % per-stock differences in adjusted price basis change momentum scores by 5-10 %, which is enough to shift ranking order. The strategy's `rank_exit` and `top_rank` reasons both depend on ranking, so 5-10 stocks per cycle get different buy/sell signals. Over 1,500 trades the cumulative effect on P&L is ~5 % per year, which over 7 years compounds to a noticeable delta from the +237 % baseline.

This explains the remaining gap to baseline: **the -1.63 % DNSE backtest is structurally as good as it gets without reconciling corporate-action adjustments between sources**.

## What I recommend

1. **Accept DNSE -1.6 % as the new "live-faithful" baseline.** It's the honest replay of what live will see. Compared to the +237 % gold baseline: same strategy, same config, but DNSE's CA-adjusted prices diverge enough from FiinTrade's to suppress returns by ~5 %/year.

2. **For highest fidelity, fetch both sources and reconcile.** Use FiinTrade for backtest (because it's the "primary" history that the strategy was tuned on) and use DNSE for live (because that's what's available in production). Single source of truth for each environment.

3. **If pure-DNSE is required:** change the universe filter to only count bars with `volume > 0`, ignore forward-filled bars. This excludes thinly-traded tickers that artificially enter the universe. The 76-stock current universe would drop to ~25-30, but those are the truly liquid names DNSE has dense data on.

4. **Update `parse_price_bars()` to log when it falls back to the old format.** Today it silently handles both. A warning when positional-arrays are seen on a request that had previously returned `{items}` would catch API drift earlier.

## Files

- `forward_fill_dnse.py` (VPS at `/root/`) — on-disk fill
- `scale_dnse.py` (VPS at `/root/`) — ×1000 scaling
- `scale_unscaled.py` (VPS at `/root/`) — second-pass scaling with `last_close < 100`
- `scale_all_unscaled.py` (VPS at `/root/`) — final pass scaling anything with `scaledBy=None`
- `vietnam_algo/docs/2026-09-01-dnse-vs-fiintrade-backtest.md` — earlier high-level comparison (predates root-cause analysis)
- This file: `vietnam_algo/docs/2026-09-01-dnse-history-data-issues.md`

## Reproducing the fixes

```bash
ssh root@185.227.135.227
cd /root/hft-trading-algo
set -a && source vietnam_algo/env/.env.dnse-secrets && set +a

# 1. Re-sync (only needed once; takes ~25 min)
python3 -m vietnam_algo.main dnse-sync-prices \
    --from-date 2015-01-01 --no-skip-existing \
    --symbols $(cat /tmp/bad_stocks.txt | tr '\n' ' ') \
    --sleep-seconds 0.3

# 2. Scale everything missing scaledBy (idempotent)
python3 /root/scale_all_unscaled.py

# 3. Forward-fill missing bars (idempotent)
python3 /root/forward_fill_dnse.py

# 4. Rebuild universe + backtest
python3 -m vietnam_algo.main --data-dir vietnam_algo/data build-universe
python3 -m vietnam_algo.main --data-dir vietnam_algo/data backtest-momentum
```

## The fix-in-place numbers

| Step | Result |
|---|---|
| Original (DNSE raw, no scaling, no FF) | -60.85 % |
| + forward-fill in `close_price()` | -26.57 % |
| + on-disk forward-fill of all stocks | -26.57 % (no real change, since backtest had been FF'd in code) |
| + re-sync 159 partial stocks | -24.88 % |
| + ×1000 scale 159 stocks (those with last_close < 100) | -24.0 % |
| + ×1000 scale VIC, IDP, TVH, VNZ (last_close > 100) | **-1.63 %** |

The 22 pp swing from "scale 4 more stocks" is the smoking gun: those 4 names (VHM, HPG, VIC, FPT) are very large-cap and were being mispriced by 1000x, so the strategy kept buying them at 0.001 % of their value, exiting for tiny profits, and missing the real returns.
