# Architecture

Last restructured: 2026-09-13.

## Top-level layout

```
hft-trading-algo/
├── vietnam_algo/
│   ├── bots/                # One bot runner; one entry point.
│   ├── algos/               # Strategy plugins (the decision logic).
│   ├── libs/                # Shared libraries (DNSE, data, schedule, …).
│   ├── jobs/                # Cron'd standalone jobs (OTP, common sync).
│   ├── tools/               # Ad-hoc operator/debug scripts.
│   ├── tests/               # pytest suite (mirrors the package layout).
│   ├── data/                # Runtime data (symbols, prices, live state).
│   ├── env/                 # Per-environment secrets (.env.dnse-secrets etc).
│   ├── docs/                # Markdown notes (timelines, post-mortems).
│   └── main.py              # Single CLI dispatcher — keeps the
│                            # ``python -m vietnam_algo.main …`` path.
├── scripts/                 # Production launchers / cron installer.
└── .github/                 # CI (if/when added).
```

## Package responsibilities

### `bots/`
The single bot runner is `vietnam_algo.bots.runner.run_auto_bot`. The
runner takes a `--strategy` flag and dispatches to the matching
`StrategyAdapter` from `algos.registry.STRATEGY_REGISTRY`.  Today we
run two processes side-by-side, one per strategy:

```bash
python -m vietnam_algo.main auto-trading-bot --strategy autofin_signal
python -m vietnam_algo.main auto-trading-bot --strategy momentum
```

Each process owns its own state file (`auto_bot_state.<strategy>.json`)
and writes per-strategy order files (`latest_orders.<strategy>.json`).
The cron'd `get_otp` and `common_sync` jobs centralise the DNSE
OTP exchange and the data refresh so neither bot has to do them on
its own.

### `algos/`
Pluggable decision logic. Each algorithm is a Python module with a
single concrete subclass of `algos.base.StrategyAdapter`:

- `algos.base.StrategyAdapter` (abstract) — defines the contract:
  `name`, `description`, `generate_orders()`, optional
  `format_cycle_summary()`.
- `algos.registry.STRATEGY_REGISTRY` — the lookup table the bot uses.
- `algos.<your_algo>.py` — drop a new file here, register it in
  `registry.py`, and the bot accepts `--strategy <your_algo>`.

Currently registered:

| Name | Class | What it does |
|---|---|---|
| `momentum` | `MomentumRotationAdapter` | Liquid momentum rotation (backtest replay) |
| `autofin_signal` | `AutofinSignalAdapter` | Mirrors autofin.vn signals + 10% SL |

### `libs/`
Shared libraries, bot+strategy agnostic. Most are flat files; the
DNSE bits live under `libs/dnse/`:

- `libs.dnse.client` — REST wrapper (`DnseClient`).
- `libs.dnse.auth`   — trading-token helpers (parse JWT/UUID,
  freshness check, shared file).
- `libs.dnse.otp`    — Gmail IMAP helper for the OTP code.

Other modules are mostly mechanical utilities:

- `libs.schedule`, `libs.market_calendar` — when to run.
- `libs.config` — typed config dataclasses (`StrategyConfig`,
  `EngineConfig`).
- `libs.data` — symbols + price sync (default data dir lives at
  `vietnam_algo/data/`, see the `parent.parent` trick in `data.py`).
- `libs.corporate_actions` — Vietstock CA events.
- `libs.bot_state`, `libs.dry_run_state`, `libs.ca_rebuy_state` —
  per-strategy state files.
- `libs.backtest`, `libs.backtest_autofin`, `libs.backtest_autofin_daily`
  — backtest runners used by `generate_live_orders()`.
- `libs.notifier`, `libs.risk_controls` — Telegram + safety.
- `libs.order_ledger`, `libs.order_monitor`, `libs.trading_engine` —
  DNSE order plumbing.
- `libs.strategy_math` — momentum math (was `strategies.py`); reusable
  across algorithms.

### `jobs/`
Standalone, idempotent scripts intended for cron / systemd timer:

- `jobs.get_otp` — DNSE email OTP → shared trading token.
- `jobs.common_sync` — refresh prices + corporate actions, stamp the
  `data_ready_for_eod` sentinel.
- `jobs._helpers` — internal helper for the CA incremental flow.

CLI surface (added to `vietnam_algo.main`):

```bash
python -m vietnam_algo.main run-get-otp
python -m vietnam_algo.main run-common-sync [--force]
```

### `tools/`
Ad-hoc operator / debug scripts that pre-date the formal test suite
(see `tools/__init__.py` for the full list). Not run by pytest — use
them for one-off investigations. Most hard-code
`/root/hft-trading-algo/vietnam_algo/...` paths.

### `tests/`
pytest suite. 398 tests across 36 files. Tests import via
`vietnam_algo.libs.X` / `vietnam_algo.algos.X` etc. — never via
`from .foo` relative imports inside test files.

## Cron layout (VPS)

```
30 1 * * 1-5  python -m vietnam_algo.main run-get-otp      # 08:30 ICT
15 8 * * 1-5  python -m vietnam_algo.main run-common-sync  # 15:15 ICT
```

The bots are launched as long-lived processes (see
`scripts/launch_autofin.sh` and `scripts/launch_momentum.sh`) and
loop on a 60-sec sleep. They read the `data_ready_for_eod` sentinel
the common-sync job stamps and wait for it before doing EOD work.

Each launcher sources a per-bot Telegram env file
(`vietnam_algo/env/.env.telegram` for momentum,
`vietnam_algo/env/.env.telegram-autofin` for autofin) so the two
strategies post to distinct chats. The shared DNSE creds come
from `vietnam_algo/env/.env.dnse-secrets`. See
`vietnam_algo/tests/test_per_bot_telegram_env.py` for the
regression guards.

## Adding a new algorithm

1. Create `vietnam_algo/algos/<your_algo>.py` with a class that
   subclasses `StrategyAdapter` from `algos.base`.
2. Register it in `algos/registry.py`:
   ```python
   from .your_algo import YourAlgoAdapter
   STRATEGY_REGISTRY["your_algo"] = YourAlgoAdapter()
   ```
3. Run `python -m vietnam_algo.main auto-trading-bot --strategy your_algo`.

That's it. The bot loop, schedule, notifier, dry-run state, OTP, and
data sync all work for the new strategy unchanged.

## Adding a new bot

We currently have one bot (the auto-trading-bot runner). To add a
second bot with a different lifecycle (e.g., one that runs only on
cron, or a different cycle), create a new module under `bots/` and
register it in `main.py`. The reason it's not auto-discovered is that
the auto-trading-bot already handles the common case (24/7 heart-beat
with a 60s sleep), so most strategies want to live inside it.
