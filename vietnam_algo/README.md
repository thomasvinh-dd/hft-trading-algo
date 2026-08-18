# Vietnam Algo Research

Standalone research/data tools for building a Vietnam stock trading algorithm. This folder is separate from live signal publishing so we can collect local data and backtest safely first.

## Data Sources

Symbol universe:

```text
https://data.vietnamfinance.vn/api/stocks?page=1&limit=50&sort=popular&order=desc
```

Historical daily prices, example:

```text
https://data.vietnamfinance.vn/api/stock/VCB/prices?limit=1825
```

Price rows are stored with:

```text
time, open, high, low, close, volume
```

## Commands

Fetch all stock symbols into `bots/hft-trading-algo/vietnam_algo/data/symbols.json`:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main sync-symbols
```

Fetch 5 years of daily prices for selected symbols:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main sync-prices --symbols VCB FPT HPG
```

Fetch symbols and prices together:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main sync-all
```

For a quick test without downloading every symbol:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main sync-all --max-symbols 5
```

Stored files:

```text
bots/hft-trading-algo/vietnam_algo/data/symbols.json
bots/hft-trading-algo/vietnam_algo/data/prices/{SYMBOL}.json
```

## Liquid Universe

Build a VN30/midcap-style liquid universe from downloaded prices:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main build-universe
```

Default filters:

```text
HoSE + HNX only
20-day average traded value >= 20B VND
20-day average volume >= 300k shares
last close >= 10,000 VND
at least 252 trading days
top 100 by 20-day traded value
```

Output:

```text
bots/hft-trading-algo/vietnam_algo/data/universe/liquid_stocks.json
```

## Momentum Rotation Backtest

The algo folder now has shared modules for comparing strategies consistently:

```text
indicators.py    Common SMA, RSI, returns, volatility, rolling high, average volume, drawdown
config.py        Shared StrategyConfig for all strategy/backtest variants
strategies.py    Strategy logic: ranking, breakout detection, entry target selection
backtest.py      Common order simulation, Vietnam market mechanics, risk controls, metrics, CSV output
```

Current best baseline without corporate-action guard:

```text
finalEquity: 7,775,373,862 VND from 1,000,000,000 VND
totalReturnPercent: 677.54%
maxDrawdownPercent: -28.55%
tradeCount: 1370
```

Current live-safe corporate-action configuration:

```text
finalEquity: 6,791,966,304 VND from 1,000,000,000 VND
totalReturnPercent: 579.20%
maxDrawdownPercent: -29.19%
tradeCount: 1418
corporateActionBuyWindowDays: 3
corporateActionSellWindowDays: 1
corporateActionRebuyAfterExDate: true
```

Run the first baseline strategy:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main backtest-momentum
```

Strategy rules:

```text
Universe: liquid_stocks
Rank: 0.6 * 3-month return + 0.4 * 6-month return - 0.3 * 20-day volatility
Entry: top-ranked stocks with close > SMA50 and close > SMA100
Breakout entry: 55-day close breakout with volume >= 1.5x 20-day average volume, allowed up to rank 20
Portfolio: 5 positions, equal-weight target
Market regime: bull uses 5 positions, neutral uses half exposure, bear uses max 2 positions and disables breakout entries
Market exits: default exits sellable positions when market proxy is below SMA200
Dynamic trailing stops: bull 10%, neutral 7%, bear 5%
Exposure cap: bull 100%, neutral 50%, bear 20%
Sector cap: max 2 open positions from the same sector
Breakout throttle: max 2 breakout entries per calendar week
Overextension filter: skip new buys when price is more than 25% above SMA50
Rebalance: every 5 trading days
Exit: rank below top 20, stop loss -8%, or trailing stop -10%
Board lot: BUY quantity rounded down to packs of 100 shares
Settlement: newly bought shares cannot be sold until 3 trading days later, approximating Vietnam T+2.5
Cost model: 0.15% fee + 0.10% slippage per side
```

Tune Vietnam market mechanics:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main backtest-momentum --board-lot-size 100 --sell-settlement-days 3
```

Tune risk protections:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main backtest-momentum \
  --bear-max-positions 2 \
  --max-sector-positions 2 \
  --max-breakout-entries-per-week 2 \
  --max-sma50-extension-percent 25 \
  --market-exit-regime 1 \
  --neutral-exposure-percent 50 \
  --bear-exposure-percent 20
```

`--market-exit-regime` values:

```text
0 = no market-regime forced exit
1 = exit sellable positions in bear regime only
2 = exit sellable positions in neutral or bear regime
```

Output:

```text
bots/hft-trading-algo/vietnam_algo/data/backtests/liquid_momentum_rotation.json
bots/hft-trading-algo/vietnam_algo/data/backtests/liquid_momentum_rotation_orders.csv
```

The CSV order list is for manual review and contains:

```text
orderId, date, symbol, side, price, quantity, value, fee, reason
```

Reproduce the current best baseline without corporate-action guard:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main backtest-momentum \
  --max-positions 10 \
  --bear-max-positions 2 \
  --max-sector-positions 3 \
  --exit-rank 30 \
  --max-breakout-entries-per-week 1 \
  --breakout-max-rank 15 \
  --neutral-exposure-percent 60 \
  --bear-exposure-percent 15 \
  --trailing-stop-percent 9 \
  --neutral-trailing-stop-percent 6 \
  --bear-trailing-stop-percent 4
```

Reproduce the current live-safe corporate-action version:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main backtest-momentum \
  --max-positions 10 \
  --bear-max-positions 2 \
  --max-sector-positions 3 \
  --exit-rank 30 \
  --max-breakout-entries-per-week 1 \
  --breakout-max-rank 15 \
  --neutral-exposure-percent 60 \
  --bear-exposure-percent 15 \
  --trailing-stop-percent 9 \
  --neutral-trailing-stop-percent 6 \
  --bear-trailing-stop-percent 4 \
  --use-corporate-action-guard \
  --corporate-action-buy-window-days 3 \
  --corporate-action-sell-window-days 1 \
  --corporate-action-rebuy-after-exdate
```

Saved local env presets:

```text
bots/hft-trading-algo/vietnam_algo/env/.env.vietnam-algo-best
bots/hft-trading-algo/vietnam_algo/env/.env.vietnam-algo-best-corporate-action
bots/hft-trading-algo/vietnam_algo/env/.env.vietnam-algo-aggressive
```

## Corporate Actions

Corporate actions are stored in:

```text
bots/hft-trading-algo/vietnam_algo/data/corporate_action.json
```

The file is built from Vietstock:

```text
https://finance.vietstock.vn/data/eventstypedata
```

Vietstock request token/session handling:

```text
Default behavior:
  fetch lich-su-kien.htm
  extract __RequestVerificationToken from the hidden form
  keep the same CookieJar session
  post to /data/eventstypedata with that session

Manual override, usually not needed:
  VIETSTOCK_REQUEST_TOKEN
  VIETSTOCK_COOKIE
```

Fetch one page of events:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main sync-corporate-actions \
  --from-date 2012-07-13 \
  --to-date 2026-08-13 \
  --page 1 \
  --page-size 100
```

Incrementally fetch new events and merge them into the existing file:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main sync-corporate-actions-incremental
```

Incremental sync behavior:

```text
read existing corporate_action.json
use the latest existing exDate as fDate
use today as tDate by default
fetch Vietstock page 1 with pageSize 100
merge by EventID
update existing rows if Vietstock changed them
append only new EventID values
write corporate_action.json back to disk
```

Current downloaded coverage after incremental sync:

```text
count: 13,821 events
min exDate: 2015-06-08
max exDate: 2026-08-18
cash-only events: 10,546
non-cash events: 3,264
```

Corporate-action live policy:

```text
Cash dividend:
  allow normal trading

Standard non-cash events, such as stock dividend / stock bonus:
  block new BUY 3 trading days before ex-date because Vietnam settlement is T+2.5
  force/allow SELL 1 trading day before ex-date
  allow re-buy after ex-date if the stock is still selected by the strategy

High-risk non-cash events, such as rights offering / issuance / convertible / share swap:
  block new BUY 5 trading days before ex-date
  force/allow SELL 3 trading days before ex-date
  do not force re-buy after ex-date
```

Reason for this policy:

```text
Vietnam settlement is T+2.5, approximated as 3 trading days.
Buying too close to a non-cash ex-date means we may not be able to sell before the event.
Bonus/new shares may not be immediately tradable, and Vietstock does not reliably provide the tradable date.
```

## Volume Breakout Backtest

Run standalone breakout without weekly momentum rotation entries:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main backtest-breakout
```

Strategy rules:

```text
Universe: liquid_stocks
Entry: close > previous 55-day high
Volume confirmation: current volume >= 1.5x previous 20-day average volume
Trend filter: close > SMA50 and close > SMA100
Rank quality filter: breakout candidate must be within configured breakoutMaxRank
Exit: market-regime exit, stop loss, trailing stop, or close below SMA20
Risk controls: same board lot, settlement, sector cap, exposure cap, and overextension filters as momentum strategy
```

Output:

```text
bots/hft-trading-algo/vietnam_algo/data/backtests/volume_breakout.json
bots/hft-trading-algo/vietnam_algo/data/backtests/volume_breakout_orders.csv
```

## Sector Momentum Backtest

Run sector rotation across liquid Vietnam stocks:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main backtest-sector
```

Strategy rules:

```text
Rank sectors by blended 1-month and 3-month return
Select top configured sectors
Within those sectors, buy top-ranked liquid stocks
Exit when sector falls out of the configured sector-exit rank, stock rank weakens, market weakens, stop loss, or trailing stop
```

Output:

```text
bots/hft-trading-algo/vietnam_algo/data/backtests/sector_momentum_rotation.json
bots/hft-trading-algo/vietnam_algo/data/backtests/sector_momentum_rotation_orders.csv
```

## DNSE Trading Engine

The trading engine converts backtest/algo order CSV rows into DNSE stock orders. It uses DNSE OpenAPI signing with API key/secret and requires a DNSE trading token for real order submission.

Safety defaults:

```text
dnse-preview-orders only prints converted orders
dnse-submit-orders is dry-run by default
real submission requires --yes
quantity is rounded down to 100-share board lots
side BUY maps to DNSE NB
side SELL maps to DNSE NS
default orderType is LO
```

Set credentials locally, never commit them:

```bash
export DNSE_API_KEY=your_api_key
export DNSE_API_SECRET=your_api_secret
```

List accounts:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-accounts
```

Request email OTP if your account uses Email OTP:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-send-email-otp
```

Create trading token from OTP or Smart OTP:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-trading-token --otp-type smart_otp --passcode 123456
export DNSE_TRADING_TOKEN=returned_trading_token
```

Preview converted orders for one execution date:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-preview-orders \
  --orders-file bots/hft-trading-algo/vietnam_algo/data/backtests/liquid_momentum_rotation_orders.csv \
  --loan-package-id 5757 \
  --trade-date 2026-01-02 \
  --limit 5
```

Dry-run submit format, still does not place real orders without `--yes`:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-submit-orders \
  --account-no YOUR_ACCOUNT_NO \
  --loan-package-id 5757 \
  --trade-date 2026-01-02 \
  --limit 1
```

Real submission requires explicit `--yes`:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-submit-orders \
  --account-no YOUR_ACCOUNT_NO \
  --loan-package-id 5757 \
  --trade-date 2026-01-02 \
  --limit 1 \
  --yes
```

For order-book-aware execution, add `--initial-orderbook-pricing` and `--monitor`. The engine fetches DNSE live quote/orderbook before submit, prices initial orders at best ask/bid, polls order detail, fetches latest bid/ask, and replaces the order toward the current best matching price when needed:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-submit-orders \
  --account-no YOUR_ACCOUNT_NO \
  --loan-package-id 5757 \
  --trade-date 2026-01-02 \
  --limit 1 \
  --initial-orderbook-pricing \
  --max-price-drift-percent 2 \
  --monitor \
  --max-checks 3 \
  --check-interval-seconds 5 \
  --cancel-if-unfilled \
  --yes
```

Initial pricing and repricing rule:

```text
BUY  -> use latest best ask
SELL -> use latest best bid
```

Price drift protection:

```text
BUY signal price: 50,000
best ask: 50,700
allowed if --max-price-drift-percent 2 because +1.4%

BUY signal price: 50,000
best ask: 51,500
kept/skipped from orderbook repricing because +3.0% > 2%

SELL signal price: 50,000
best bid: 49,400
allowed because -1.2%

SELL signal price: 50,000
best bid: 48,800
kept/skipped from orderbook repricing because -2.4% > 2%
```

The monitor stops when the order status is filled/cancelled/rejected/closed or after `--max-checks`. If `--cancel-if-unfilled` is set, an open order is cancelled after the final check.

If the monitor reaches `--max-checks` and the order is still open, it records:

```text
monitor_exhausted_unfilled
```

If `--cancel-if-unfilled` is enabled, it then cancels the order and records:

```text
cancelled_unfilled
```

Telegram notifications include a warning for these cases:

```text
WARNING: unfilled orders after reprice limit
NB VCB qty=100 price=50,000 | status=UNFILLED_AFTER_REPRICE_LIMIT
```

Or if cancelled:

```text
WARNING: unfilled orders after reprice limit
NB VCB qty=100 price=50,000 | status=CANCELLED_UNFILLED
```

Live sell protection:

```text
Before real submit, the engine reads DNSE positions.
SELL quantity is capped to DNSE sellableQuantity / availableQuantity.
Quantity is rounded down to 100-share board lot.
SELL is skipped if no sellable quantity is available.
```

Before real submission, check buying/selling power:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-ppse \
  --account-no YOUR_ACCOUNT_NO \
  --symbol VCB \
  --price 59500 \
  --loan-package-id 5757
```

## Daily Engine

The daily engine is the operational path for live trading. It can update the local dataset, rebuild the universe, run the selected strategy, and export only the latest execution-date orders.

Generate latest live orders without submitting:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main daily-generate-orders \
  --strategy momentum \
  --max-positions 10 \
  --bear-max-positions 2 \
  --max-sector-positions 3 \
  --exit-rank 30 \
  --max-breakout-entries-per-week 1 \
  --breakout-max-rank 15 \
  --neutral-exposure-percent 60 \
  --bear-exposure-percent 15 \
  --trailing-stop-percent 9 \
  --neutral-trailing-stop-percent 6 \
  --bear-trailing-stop-percent 4
```

This writes:

```text
bots/hft-trading-algo/vietnam_algo/data/live/latest_orders.csv
bots/hft-trading-algo/vietnam_algo/data/live/latest_orders.json
```

To refresh all price data before generating orders, add:

```bash
--update-prices
```

Because refreshing every symbol can take a long time and may hit rate limits, you can test with:

```bash
--update-prices --max-symbols 10
```

Email OTP automation for DNSE trading token uses Gmail IMAP with an app password:

```bash
export GMAIL_ADDRESS=your_email@gmail.com
export GMAIL_APP_PASSWORD=your_gmail_app_password
export DNSE_OTP_SENDER=dnse
export DNSE_OTP_SUBJECT=OTP
```

Real daily submit requires both `--yes` and either `DNSE_TRADING_TOKEN` or `--auto-email-otp`:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main daily-submit-orders \
  --account-no YOUR_ACCOUNT_NO \
  --loan-package-id 5757 \
  --strategy momentum \
  --max-positions 10 \
  --bear-max-positions 2 \
  --max-sector-positions 3 \
  --exit-rank 30 \
  --max-breakout-entries-per-week 1 \
  --breakout-max-rank 15 \
  --neutral-exposure-percent 60 \
  --bear-exposure-percent 15 \
  --trailing-stop-percent 9 \
  --neutral-trailing-stop-percent 6 \
  --bear-trailing-stop-percent 4 \
  --use-corporate-action-guard \
  --corporate-action-buy-window-days 3 \
  --corporate-action-sell-window-days 1 \
  --corporate-action-rebuy-after-exdate \
  --auto-email-otp \
  --yes
```

`daily-submit-orders` applies these live protections automatically:

```text
corporate-action guard from corporate_action.json
DNSE sellable quantity cap for SELL orders
initial order-book pricing from DNSE latest quote
max price drift protection, default 2%
```

Recommended production flow:

```text
1. Run daily-generate-orders after data is updated.
2. Inspect latest_orders.csv.
3. Check PPSE for large orders.
4. Run daily-submit-orders with --yes only after confirming the order list.
```

## Telegram Notifications

The bot can report important events to a Telegram chat:

```text
order generation summary
submit cycle start
submitted order summary
final order status after monitor checks
matched quantity and average price if DNSE returns them
reprice count per order
warning when an order remains unfilled after the reprice limit
warning when an unfilled order is cancelled
corporate-action sync start/result
EOD update start
DNSE token refresh start
strategy metrics such as return, final equity, and max drawdown
corporate-action, sellable quantity, and pricing decision counts
```

Example Telegram submit status line:

```text
NB VCB qty=100 price=50,000 | status=FILLED | matchedQty=100 | avgPrice=50,100 | repriceCount=1
```

Example unfilled warning:

```text
WARNING: unfilled orders after reprice limit
NS FPT qty=100 price=100,000 | status=CANCELLED_UNFILLED
```

Create a Telegram bot with BotFather, then set:

```bash
export TELEGRAM_BOT_TOKEN=your_telegram_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
```

Disable notifications if needed:

```bash
export TELEGRAM_NOTIFICATIONS=0
```

Send a test message:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main telegram-test \
  --message "Vietnam Algo test notification"
```

If the bot is posting to a group, add the bot to the group and use the group chat ID. Telegram group IDs are often negative numbers.

## Real Environment Test Runbook

Use this sequence before the first real DNSE test.

Set environment variables:

```bash
export DNSE_API_KEY=your_api_key
export DNSE_API_SECRET=your_api_secret
export DNSE_ACCOUNT_NO=your_account_no
export DNSE_LOAN_PACKAGE_ID=5757

export GMAIL_ADDRESS=your_email@gmail.com
export GMAIL_APP_PASSWORD=your_gmail_app_password
export DNSE_OTP_SENDER=dnse
export DNSE_OTP_SUBJECT=OTP
export TELEGRAM_BOT_TOKEN=your_telegram_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
```

Check DNSE connectivity:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-accounts
```

Check positions:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-positions \
  --account-no "$DNSE_ACCOUNT_NO"
```

Generate or refresh orders after EOD data is ready:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main daily-generate-orders \
  --strategy momentum \
  --max-positions 10 \
  --bear-max-positions 2 \
  --max-sector-positions 3 \
  --exit-rank 30 \
  --max-breakout-entries-per-week 1 \
  --breakout-max-rank 15 \
  --neutral-exposure-percent 60 \
  --bear-exposure-percent 15 \
  --trailing-stop-percent 9 \
  --neutral-trailing-stop-percent 6 \
  --bear-trailing-stop-percent 4 \
  --use-corporate-action-guard \
  --corporate-action-buy-window-days 3 \
  --corporate-action-sell-window-days 1 \
  --corporate-action-rebuy-after-exdate
```

Inspect generated files:

```text
bots/hft-trading-algo/vietnam_algo/data/live/latest_orders.csv
bots/hft-trading-algo/vietnam_algo/data/live/latest_orders.json
```

Preview DNSE orders with corporate-action decisions:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-preview-orders \
  --orders-file bots/hft-trading-algo/vietnam_algo/data/live/latest_orders.csv \
  --loan-package-id "$DNSE_LOAN_PACKAGE_ID" \
  --use-corporate-action-guard \
  --corporate-action-buy-window-days 3 \
  --corporate-action-sell-window-days 1
```

Dry-run submit before real money:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-submit-orders \
  --orders-file bots/hft-trading-algo/vietnam_algo/data/live/latest_orders.csv \
  --account-no "$DNSE_ACCOUNT_NO" \
  --loan-package-id "$DNSE_LOAN_PACKAGE_ID" \
  --trading-token dry_run_token \
  --initial-orderbook-pricing \
  --max-price-drift-percent 2 \
  --monitor \
  --max-checks 3 \
  --check-interval-seconds 5 \
  --cancel-if-unfilled
```

Real manual submit at start of the continuous session, after ATO:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-submit-orders \
  --orders-file bots/hft-trading-algo/vietnam_algo/data/live/latest_orders.csv \
  --account-no "$DNSE_ACCOUNT_NO" \
  --loan-package-id "$DNSE_LOAN_PACKAGE_ID" \
  --trading-token "$DNSE_TRADING_TOKEN" \
  --initial-orderbook-pricing \
  --max-price-drift-percent 2 \
  --monitor \
  --max-checks 3 \
  --check-interval-seconds 5 \
  --cancel-if-unfilled \
  --yes
```

Recommended timing for manual real test:

```text
08:30 VN: create/refresh DNSE trading token
09:00-09:15 VN: do not submit during ATO
09:16 VN: submit during continuous session
11:30-13:00 VN: do not submit during lunch break
14:45-15:00 VN: do not submit during ATC
15:15 VN: update EOD data and generate tomorrow orders
```

First real-money test recommendations:

```text
use --limit 1 for the first live submit
keep --max-price-drift-percent 2 or lower
keep --max-checks 3
keep --cancel-if-unfilled
verify DNSE positions immediately after submit
verify latest_orders.csv before every real submit
```

## All-Day Auto Trading Bot

The auto bot runs continuously and follows Vietnam market timing:

```text
08:30 VN time: refresh DNSE trading token through Email OTP if needed
09:16 VN time: submit generated orders during continuous morning session, avoiding ATO
11:30-13:00: no submission during noon break
13:00-14:45: continuous afternoon session
14:45-15:00: no submission during ATC
15:10 VN time: sync new Vietstock corporate actions into corporate_action.json
15:15 VN time: update dataset and generate next live order file
```

Dry-run mode, recommended first:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main auto-trading-bot \
  --account-no YOUR_ACCOUNT_NO \
  --loan-package-id 5757 \
  --strategy momentum \
  --update-prices \
  --sleep-seconds 60
```

Real mode requires `--live-submit` and Gmail app password environment variables:

```bash
export DNSE_API_KEY=your_api_key
export DNSE_API_SECRET=your_api_secret
export GMAIL_ADDRESS=your_email@gmail.com
export GMAIL_APP_PASSWORD=your_gmail_app_password
export DNSE_OTP_SENDER=dnse
export DNSE_OTP_SUBJECT=OTP
export TELEGRAM_BOT_TOKEN=your_telegram_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
```

Optional Vietstock overrides, normally not needed because the bot auto-fetches a fresh token/session:

```bash
export VIETSTOCK_REQUEST_TOKEN=your_vietstock_request_token
export VIETSTOCK_COOKIE=your_vietstock_browser_cookie
```

Run the auto bot:

```bash

PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main auto-trading-bot \
  --account-no YOUR_ACCOUNT_NO \
  --loan-package-id 5757 \
  --strategy momentum \
  --update-prices \
  --live-submit
```

State is persisted at:

```text
bots/hft-trading-algo/vietnam_algo/data/live/auto_bot_state.json
```

Use `--once` to test one scheduler cycle and exit:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main auto-trading-bot \
  --account-no YOUR_ACCOUNT_NO \
  --loan-package-id 5757 \
  --once
```
