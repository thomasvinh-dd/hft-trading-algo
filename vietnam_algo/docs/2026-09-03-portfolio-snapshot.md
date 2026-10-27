# Live Trading Cycle — Portfolio Snapshot — 2026-09-03

Per request: "for live trading we need to get portfolio data all from DNSE like stock value, money available, etc., not like our dry run." We added a live portfolio snapshot that runs in both submit and dry-run paths.

## What's new

1. **`vietnam_algo/portfolio.py`** (new) — `build_portfolio_snapshot(client, account_no)` fetches:
   - `/accounts` → account name, custodyCode, investorId
   - `/accounts/{acct}/balances` → cash, availableCash, withdrawableCash, secureAmount, orderSecured, totalDebt
   - `/accounts/{acct}/positions` → quantity + avgPrice per held symbol
   - `/price/{sym}/quotes/latest` → live mid-price for each held symbol
   - Computes marketValue, costBasis, unrealizedPnl, unrealizedPnlPct per position
   - Computes portfolioValue (cash + stockValue), totalUnrealizedPnl, totalUnrealizedPnlPct
   - Lists any endpoint errors in `errors[]`

2. **`vietnam_algo/notifier.py`** — `portfolio_snapshot_message(snap, top_n=8)` formats:
   - Account info
   - Cash section (total/available/withdrawable/secure/orderSecured/debt)
   - Positions section (top 8 by market value with avg/mkt/PnL/PnL%)
   - Totals section (stockValue / portfolioValue / unrealizedPnL / PnL%)
   - Endpoint errors if any

3. **`vietnam_algo/daily_engine.py`** — `run_daily_once()` now:
   - Calls `build_portfolio_snapshot()` BEFORE the submit/dry-run branch
   - In **dry-run** mode: attaches `result_payload['portfolio']` so callers can see state
   - In **live/submit** mode: same, plus fires a Telegram `portfolio_snapshot_message` after the submit completes
   - Failure-tolerant: snapshots errors (e.g. proxy down) don't kill the cycle

4. **`vietnam_algo/auto_bot.py`** — EOD flow now also fetches a portfolio snapshot and notifies it alongside the EOD summary.

## Why this is the "right" version of live trading

Previously, the live cycle:
- Generated orders locally from the strategy backtest
- Pulled DNSE positions ONLY to cap order quantities to sellable
- Notified just submitted counts and total trade count

That was missing:
- ❌ How much cash the account has available
- ❌ What stocks are currently held (and at what cost basis)
- ❌ What's the live market value of holdings
- ❌ Whether we're sitting on unrealized gains/losses
- ❌ What % of portfolio is in stocks vs cash

The new portfolio snapshot answers all of these on every cycle, so Telegram updates are now actionable: "you have 423M cash, 1.3B in stocks, +50M unrealized PnL (+5.8%)" — instead of just "submitted 2 orders."

## Live vs dry-run

- **Live mode (`submit=True`)**: snapshot fetched AND sent via Telegram as part of the submit cycle
- **Dry-run mode**: snapshot still fetched (so you can see what the live state looks like without submitting). The auto-bot currently runs dry-run, so we see the snapshot in the next EOD cycle.

## Verified

Sent msg_id=49 to Telegram from VPS via Vietnamese proxy. Format works:

```
💼 Vietnam Algo: PORTFOLIO SNAPSHOT
account: 0003937868 (Nguyễn Danh Vinh)
fetchedAt: 2026-09-03T17:21:36Z

💰 CASH
  totalCash:        0 VND
  availableCash:    0 VND
  ...

📈 POSITIONS (0)
  (no positions)

💵 TOTALS
  stockValue:        0 VND
  portfolioValue:    0 VND  (cash + stocks)
  unrealizedPnL:     0 VND  (+0.00%)
```

(Account currently has 0 cash and 0 positions — fresh account, no funds deposited.)

## Tests

163/163 still green. The snapshot is best-effort (catches all exceptions per-endpoint) so it never breaks the cycle.

## Commit

`2b37413 Add portfolio snapshot (cash + positions + live P&L) to live cycle and EOD notify`
