# DNSE Price Data vs FiinTrade vs Baseline — 2026-09-01

We re-ran the gold backtest config (`vietnam_algo/env/.env.vietnam-algo-best-ca-fixed-delay6`)
three different ways and compared the results. This is the verification report.

## TL;DR

| Run | Period | Return | Max DD | Trades |
|---|---|---|---|---|
| Gold baseline (claimed) | 2019-08-16 → 2026-08-13 | **+236.95 %** | -23.22 % | 1406 |
| FiinTrade on VPS | 2019-08-16 → 2026-08-13 (same period) | +38.49 % | -57.47 % | 1076 |
| DNSE on VPS (with FF) | 2019-08-16 → 2026-08-13 (same period) | -18.88 % | -57.77 % | ~1500 |
| DNSE on VPS (full data) | 2015-07-01 → 2026-08-28 | -26.57 % | -57.77 % | 1520 |

Even on the same hardware with the same config, **FiinTrade gives +38 % and DNSE gives -19 % for the same 2019-08 → 2026-08 window**. Both fall far short of the gold baseline's claimed +236.95 %. The biggest gap is between today and the baseline (the baseline is drifted data, not reproducible).

## What the gold baseline actually was

`vietnam_algo/data/backtests/_ca_delay_v2_6.json` was captured on the workstation on 2026-08-22:
- 1747 days (2019-08-16 → 2026-08-13)
- 645 symbols in the cumulative universe
- Final equity 3.37B from 1B initial
- All other backtest runs since (workstation + VPS) cannot reproduce the +237 % number.

The drift between "what the baseline run saw" and "today's prices/" is the dominant factor. Possibilities (in order of likely impact):

1. **Universe** rebuilt today is 84 stocks (FiinTrade) or 73 (DNSE) — a *current* snapshot, not a historical per-day universe. The baseline's universe was captured at baseline time. Today's point-in-time universe uses the same filter but with prices that may have been re-fetched since.
2. **Corporate-action data** (`corporate_action.json`) was last touched 2026-08-19, and the historical version the baseline saw had different event records.
3. **Price files** (`prices_fiin/`) — even though timestamps show 2026-08-18 20:20 on the VPS, any sub-second corruption or re-fetch with different adjustment basis would shift many bars.

To exactly reproduce the baseline we'd need to freeze: (a) `prices/`, (b) `corporate_action.json`, (c) `universe/liquid_stocks.json`, (d) `market_holidays_vn.json`. None of these are tagged.

## Why DNSE < FiinTrade even with FF fix

DNSE data is sparser than FiinTrade:

```
1407 DNSE files, of which:
  1411 total files in prices_dnse/
  1407 with bars
  4 empty (CNA, EGL, TBW, UTT)
  7 invalid symbol (BCG, BCR, FUCTVGF3, RDP, TAN, VE2, VTS)

~704,357 missing trading-day instances across 1,382 stocks.
The worst offenders (QNW, BTH, DCT, NQB, MCC) have 1,300+ gaps each,
meaning they have bars for less than 25% of expected trading days.
```

We applied two fixes:

1. **Forward-fill in `close_price()`** (`vietnam_algo/backtest.py:598`) — when a bar is missing for a date, walk back to the most recent available close. All 163 tests still green.
2. **Forward-fill on disk** — wrote `forward_fill_dnse.py` that fills each DNSE bar file with carry-forward closes (volume=0) for any expected weekday that was missing. After: 1,406 files now continuous.

Even with both fixes, the DNSE run still trails FiinTrade by 57 pp on the same window. The remaining gaps are:

- **Different adjustment basis.** BID and MBB have DNSE prices that are 7–19 % lower than FiinTrade at the same date (2020-01-15), suggesting DNSE applies more aggressive bonus-issue back-adjustment. This means DNSE's lower prices for *old* bars create unusually large "momentum from low base" signals that the strategy buys into — and then those stocks don't recover because by 2024-2026 the prices align again.
- **Coverage skew.** The market_proxy (`precompute_market_proxy`) averages daily returns across only the stocks that have data on BOTH prev and cur dates. With DNSE gaps, the sample is dominated by liquid large-caps, which biases the proxy upward, so bear-market signals fire too late. (We considered forward-filling the proxy too, but it would dilute legitimate bear moves with stale-flat returns.)
- **`market_bear_exit` whipsaw.** In the 2026 segment the strategy entered and exited positions in 3-7 day cycles (`top_rank → market_bear_exit → top_rank`). That's the bear-regime classifier flipping daily because the average market return is computed over too few stocks.
- **Universe drift.** With DNSE, only 73 stocks pass the liquid-universe filter (vs 84 with FiinTrade, 645 in baseline cumulative). Smaller universe = less diversification on the upside, more concentrated bets.

## What "good enough" looks like with DNSE

The DNSE + FF run does exhibit **the same risks as the live engine** — it trades the same way on data sourced from DNSE, which is what live will see. So the DNSE backtest is the more honest replay of live behavior, even if it's less flattering.

If you want a more representative DNSE backtest, options in priority order:

1. **(quickest win)** Improve DNSE coverage for large-caps. The 18 largest names by `avgValue20` are present, but 90 % of them have hundreds of gaps. A targeted re-fetch with a wider request window (`--max-symbols 100`, `--sleep-seconds 0.1`) plus an OUTC->INTC pass would densify them.
2. **(structural)** Switch the universe filter to require `min_history_days=252` and `min_avg_value=20B VND` only on bars **with** `volume > 0` (i.e. ignore FF bars). Today the FF bars count toward `historyDays`, which lets thinly-traded tickers stay in the universe based on stale prices.
3. **(structural)** Use a *value-weighted* market proxy instead of equal-weighted. With DNSE sparse data, equal-weight is dominated by whichever large-cap happened to trade that day. Equal-weight → value-weight would need shares-outstanding data, which we don't have for VN — but DNSE's `/instruments` could give it.
4. **(cosmetic)** DNSE has `count=0` empty responses for CNA/EGL/TBW/UTT — these might have data on retry with a different `resolution` (try `5` for 5-minute bars and aggregate, or check `/quotes/intraday`). Probably not worth it; these stocks are also delisted on FiinTrade.

## Reproducing this

```bash
# On VPS
ssh root@185.227.135.227
cd /root/hft-trading-algo
set -a && source vietnam_algo/env/.env.dnse-secrets && set +a

# To run with DNSE (default after this commit):
python3 -m vietnam_algo.main --data-dir vietnam_algo/data backtest-momentum

# To compare FiinTrade run:
mv vietnam_algo/data/prices{,_dnse_link}
mv vietnam_algo/data/prices_fiin_run vietnam_algo/data/prices
python3 -m vietnam_algo.main --data-dir vietnam_algo/data backtest-momentum
# (then swap back)

# Forward-fill DNSE if not already done:
python3 /root/forward_fill_dnse.py
```

## Files changed

- `vietnam_algo/backtest.py` — `close_price()` and `open_price()` now forward-fill (commit 5158845)
- `vietnam_algo/data/prices_dnse/*.json` — every file now has `forwardFilled: true` if it had gaps; bars carry `forwardFilled: true` markers and `volume = 0`
- This document: `vietnam_algo/docs/2026-09-01-dnse-vs-fiintrade-backtest.md`
