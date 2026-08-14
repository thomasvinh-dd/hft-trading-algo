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
