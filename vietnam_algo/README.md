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

Current safer-best momentum baseline for comparison:

```text
totalReturnPercent: about 875.40%
maxDrawdownPercent: about -24.28%
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

For order-book-aware execution, add `--monitor`. The engine will submit the order, poll DNSE order detail, fetch latest bid/ask, and replace the order toward the current best matching price when needed:

```bash
PYTHONPATH=bots/hft-trading-algo python3 -m vietnam_algo.main dnse-submit-orders \
  --account-no YOUR_ACCOUNT_NO \
  --loan-package-id 5757 \
  --trade-date 2026-01-02 \
  --limit 1 \
  --monitor \
  --max-checks 3 \
  --check-interval-seconds 5 \
  --yes
```

Repricing rule:

```text
BUY  -> use latest best ask when current order price does not match the order book
SELL -> use latest best bid when current order price does not match the order book
```

The monitor stops when the order status is filled/cancelled/rejected/closed or after `--max-checks`.

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
  --auto-email-otp \
  --yes
```

Recommended production flow:

```text
1. Run daily-generate-orders after data is updated.
2. Inspect latest_orders.csv.
3. Check PPSE for large orders.
4. Run daily-submit-orders with --yes only after confirming the order list.
```

## All-Day Auto Trading Bot

The auto bot runs continuously and follows Vietnam market timing:

```text
08:30 VN time: refresh DNSE trading token through Email OTP if needed
09:16 VN time: submit generated orders during continuous morning session, avoiding ATO
11:30-13:00: no submission during noon break
13:00-14:45: continuous afternoon session
14:45-15:00: no submission during ATC
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
