# Multi-Strategy Refactor — 2026-09-04

## Goal

Per user request: "let me create another bot that just different in logic but will use the same dnse base and price, pls rearrange our structure to do that." We refactored to support multiple strategy implementations sharing one DNSE infrastructure.

## New architecture

```
vietnam_algo/strategy_registry.py   # NEW: StrategyAdapter ABC + STRATEGY_REGISTRY
```

Each strategy is a Python class:

```python
class MyNewStrategy(StrategyAdapter):
    name = "my_new"               # CLI flag value + filename prefix
    description = "..."           # shown in list-strategies
    env_filename = ".env.my_new" # optional override env file

    def generate_orders(self, config, strategy_config, trade_date=None) -> dict:
        # ... runs backtest, returns same shape as the legacy generate_live_orders
        ...
    
    def format_cycle_summary(self, result) -> str:  # optional Telegram override
        ...
```

Register it:
```python
STRATEGY_REGISTRY["my_new"] = MyNewStrategy()
```

That's it. To run:
```bash
python3 -m vietnam_algo.main auto-trading-bot \
    --account-no 0003937868 --loan-package-id 1775 \
    --sleep-seconds 60 --strategy my_new
```

## Per-strategy isolation

Multiple strategies run side-by-side without clobbering each other:

| Concern | Naming |
|---|---|
| State file | `data/live/auto_bot_state.{strategy_name}.json` |
| Latest orders CSV | `data/live/latest_orders.{strategy_name}.csv` |
| Latest orders JSON | `data/live/latest_orders.{strategy_name}.json` |
| Env file override | `vietnam_algo/env/.env.{env_filename}` |
| DNSE creds + Telegram | Shared from `.env.dnse-secrets` / `.env.telegram` |

So you can run `--strategy momentum` and `--strategy my_new` simultaneously on the same VPS. Each maintains its own lastCycleDates, token cache, heartbeat state, and order CSVs.

## CLI

```bash
$ python3 -m vietnam_algo.main list-strategies
  momentum                   Liquid momentum rotation with corporate-action awareness
    env: vietnam_algo/env/.env.vietnam-algo-best-ca-fixed-delay6
```

A second strategy, once added to the registry, will appear here automatically.

`--strategy NAME` flag on `auto-trading-bot` picks which one to run. Default is `momentum` (backward-compatible).

## Files added/changed

| File | Change |
|---|---|
| `vietnam_algo/strategy_registry.py` | NEW: `StrategyAdapter`, `MomentumRotationAdapter`, `STRATEGY_REGISTRY`, `get_strategy()`, `list_strategies()` |
| `vietnam_algo/bot_state.py` | `state_path(strategy_name=...)` returns namespaced file; `load_state/save_state` accept strategy_name |
| `vietnam_algo/daily_engine.py` | `generate_live_orders(strategy_name=...)` routes order outputs through strategy adapter's filenames; `export_orders_for_date` accepts csv_filename/json_filename |
| `vietnam_algo/auto_bot.py` | `run_auto_bot(strategy_name=...)`; state file namespaced; EOD notify uses adapter's `format_cycle_summary` |
| `vietnam_algo/main.py` | `auto-trading-bot --strategy NAME`; new `list-strategies` subcommand; `_with_strategy` helper for frozen-dataclass replace |
| `vietnam_algo/tests/test_strategy_registry.py` | NEW: 6 tests (registry presence, get_strategy, unknown rejection, list_strategies, filenames, legacy fallback) |

## Tests

177/177 green (171 prior + 6 new).

## Verified on VPS

- `list-strategies` shows the momentum strategy
- Bot launched with `--strategy momentum` writes to `auto_bot_state.momentum.json` (separate from the legacy `auto_bot_state.json`)
- Heartbeat, market-holiday, EOD, submit cycles all fire correctly per-strategy
- 6 unit tests pass

## Backward compatibility

- Default `--strategy momentum` keeps old behavior intact
- `auto_bot_state.json` legacy file still readable (for any old bot that's still running); new bot just doesn't touch it
- All existing `auto-trading-bot` runs without `--strategy` work the same as before (well, they use the namespaced state now — but that's what you want for isolation)

## How to add a new strategy

1. Create `vietnam_algo/strategies_impl/my_strategy.py`:
```python
from vietnam_algo.strategy_registry import StrategyAdapter
from vietnam_algo.daily_engine import EngineConfig, generate_live_orders
from vietnam_algo.config import StrategyConfig
from typing import Any

class MyStrategy(StrategyAdapter):
    name = "my_strategy"
    description = "My custom logic — pure alpha"
    env_filename = ".env.my-strategy"

    def generate_orders(self, config, strategy_config, trade_date=None):
        # Option A: reuse existing backtest machinery via generate_live_orders
        # (you'll get the standard momentum run; not what you want)
        #
        # Option B: write your own logic
        orders = self._compute_orders(config, strategy_config)
        result = {
            "status": "generated",
            "tradeDate": trade_date or "...",
            "count": len(orders),
            "latestOrdersCsv": "/path/to/csv",
            "latestOrdersJson": "/path/to/json",
            "metrics": {},
        }
        return result

    def _compute_orders(self, config, strategy_config):
        # your strategy logic here
        ...

    def format_cycle_summary(self, result):
        return "MY CUSTOM SUMMARY\n..."  # or omit for default
```

2. Register it in `vietnam_algo/strategy_registry.py`:
```python
from .strategies_impl.my_strategy import MyStrategy
STRATEGY_REGISTRY["my_strategy"] = MyStrategy()
```

3. Create env file `vietnam_algo/env/.env.my-strategy`:
```
# Your strategy parameters
```

4. Run:
```bash
python3 -m vietnam_algo.main auto-trading-bot \
    --account-no ... --loan-package-id ... --strategy my_strategy
```

Or query:
```bash
python3 -m vietnam_algo.main list-strategies
```

## Commit

`<commit-hash> Refactor: strategy registry for pluggable multi-strategy bots`
