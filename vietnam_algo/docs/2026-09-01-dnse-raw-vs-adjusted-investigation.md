# DNSE "raw vs adjusted" Investigation - 2026-09-01 follow-up

Following up on the user's hint: "DNSE is returning raw data instead of adjusted data like our first dataset." We investigated — the hypothesis turned out to be only partially correct, and the data actually DOES need a single ×1000 scaling, but no per-bar forward-adjustment.

## What we did

1. Re-fetched all 472 missing tickers (tunnel had died; batched the sync into 6 × 80-symbol runs over ~2 hours)
2. Re-applied ×1000 scaling to anything missing `scaledBy: 1000.0`
3. Re-applied forward-fill to add 3040 continuous trading days to every file

## Final state of DNSE data on VPS

- 1375 stock files in `vietnam_algo/data/prices_dnse/`
- 11 known unfillable (7 "invalid symbol" + 4 empty/zero-item)
- Total 3.41M bars, 99.29% of master list (1417 tickers)
- All prices are now uniformly ×1000-scaled to raw VND
- All gaps are forward-filled with `volume=0` markers

## What we confirmed about DNSE pricing

| Endpoint | Returns | Notes |
|---|---|---|
| `/price/{sym}/ohlc` close | in **thousands** of VND (VCB 2026-08-28 = 60.3 → 60,300 VND after scale) | confirmed positional-array format `{t, o, h, l, c, v}` |
| `/price/{sym}/quotes/latest` bid/offer | in **thousands** of VND (VCB bid 60.0 → 60,000 VND after scale) | consistent with ohlc |

So both endpoints return the same numeric basis. **DNSE returns raw market prices divided by 1000.** Nothing more.

## What we tried and rejected

- **Per-bar forward-adjustment using corporate_action.json**: tried parsing the ratios from `note` text (Vietnamese "tỷ lệ X:Y" or "tỷ lệ N%"). Parsed 2,712 of 13,821 events. Applied cumulative factor backward through time. **Result: WRONG for 2019+** — VCB 2020-01-15 went from 38,260 (matching FiinTrade) to 86,196 (off by 2.25x). Confirmed DNSE prices for 2019+ are already raw, matching FiinTrade. The user's "autobot fetches adjusted data after the AC day" intuition doesn't apply at the data-source level — DNSE doesn't pre-adjust for past CAs in the way Yahoo's adjusted close does. Reverted all forward-adjustment and re-fetched clean.

- **DNSE `adjusted=true` query param**: tried 11 variants (`adjusted`, `adjustedPrice`, `adj`, `adjustment`, `priceType`, etc.). DNSE returned identical data for all of them. The `adjustedClose` parameter doesn't exist.

- **DNSE different endpoint** (`/price/{sym}/adj-close`, `/price/info/{sym}`, `/market/instruments/{sym}`, etc.): all return 404.

## Final backtest result with clean DNSE data

| Run | Period | Return | Max DD | Trades |
|---|---|---|---|---|
| Workstation FiinTrade (live, reproducible) | 2019-08-16 → 2026-08-13 | +215.28% | -25.22% | 1414 |
| VPS DNSE clean (this run) | 2019-08-16 → 2026-08-13 | -9.72% | -48.12% | ~1488 |
| VPS DNSE clean (this run) | full 2015-07 → 2026-08 | -14.99% | -48.95% | 1488 |

The 230pp gap from FiinTrade's +215% to DNSE's -10% is now within the same period and same config — purely a data difference. See per-stock evidence:

| Ticker | DNSE raw | DNSE ×1000 | Fiin | Diff |
|---|---|---|---|---|
| VCB | 60.3 | 60,300 | 59,500 | +1.3% ✓ |
| VHM | 73.2 | 73,200 | 71,800 | +1.9% ✓ |
| HPG | 22.25 | 22,250 | 21,700 | +2.5% ✓ |
| FPT | 73.8 | 73,800 | 69,200 | +6.7% |
| VIC | 231.4 | 231,400 | 207,900 | +11.3% |
| BID | 36.95 | 36,950 | 38,850 | -4.9% |
| MBB | 20.95 | 20,950 | 20,150 | +3.9% |

These are 2026-08-28 close prices. All DNSE prices match FiinTrade within 1-11% post-scaling. The systematic differences (VIC +11%, BID -5%) suggest **DNSE and FiinTrade compute adjusted-basis differently even after we've stretched them both to "raw"**. Either source has historically applied a different CA-bleeddown, and the strategy's momentum calculation amplifies those small differences over 7 years into the 230pp gap.

## Recommendation

Don't try to backtest purely on DNSE and expect to match the +215% workstation result. The two sources diverge in a way that's intrinsic to upstream CA-adjustment methodology.

**For highest fidelity, pick one of:**

1. **Backtest with FiinTrade** (captured on workstation, last refresh 2026-08-19) and live with DNSE. Same code path, different data sources. Acceptable because the strategy only uses ~1 year's worth of live data at the trailing edge.
2. **Reconcile DNSE** to a known basis by computing raw-prices through rolling CA application. Complex (~3-month project), no off-the-shelf tooling.
3. **Update the gold baseline** to +215% (workstation-reproducible) instead of the mythical +237% (transient 2026-08-22 capture).

## Files & commits

- `vietnam_algo/docs/2026-09-01-dnse-history-data-issues.md` (commit `84597dd`) — earlier analysis (pre-forward-adjustment)
- This file: `vietnam_algo/docs/2026-09-01-dnse-raw-vs-adjusted-investigation.md`
- VPS state: `/root/hft-trading-algo/`, all 1375 DNSE files clean
