"""One-off helper: trigger the EOD on a specific bot, bypassing
``is_trading_day`` so we can run it on weekends for testing.

This is a temporary utility. It mirrors the EOD path in
``run_auto_bot`` (bots/runner.py:2858-3192) but skips the
``should_run_eod`` gate.

Usage (run on VPS):
  python3 -m vietnam_algo.tools.force_eod autofin_signal
  python3 -m vietnam_algo.tools.force_eod momentum

After running, the state.eodDate is updated to today and a
Telegram EOD summary is sent.
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# Bootstrap
PROJECT_ROOT = Path("/root/hft-trading-algo")
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DNSE_ACCOUNT_NO", "0003937868")
os.environ.setdefault("DNSE_PROXY_HTTP_URL", "")
os.environ.setdefault("PYTHONPATH", str(PROJECT_ROOT))

# Set up env (DNSE secrets only; the Telegram env is loaded PER
# STRATEGY below so the EOD message routes to the right chat).
import os as _os
_os.environ.setdefault("DNSE_PROXY_HTTP_URL", "")
_dnse_env = PROJECT_ROOT / "vietnam_algo/env/.env.dnse-secrets"
try:
    if _dnse_env.exists():
        for line in _dnse_env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            _os.environ.setdefault(k.strip(), v.strip())
except OSError:
    # Permission denied during tests, etc — skip.
    pass


def _load_telegram_env_for_strategy(strategy_name: str) -> str:
    """Override TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in os.environ
    with the values from the per-strategy env file. Mirrors the
    override in scripts/launch_*.sh.

    Returns the path of the env file that was loaded (for logging).
    """
    if strategy_name == "autofin_signal":
        env_path = PROJECT_ROOT / "vietnam_algo/env/.env.telegram-autofin"
    else:
        # momentum + any other strategy
        env_path = PROJECT_ROOT / "vietnam_algo/env/.env.telegram"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()
    return str(env_path)


def force_eod(strategy_name: str) -> int:
    """Mirror the EOD path in run_auto_bot but skip is_trading_day."""
    from dataclasses import replace as _dc_replace

    from vietnam_algo.algos.registry import STRATEGY_REGISTRY
    from vietnam_algo.bots.runner import (
        _submit_autofin_deals_to_dnse,
        _build_portfolio_snapshot,
        _apply_dry_run_fills,
        notify,
        vietnam_now,
        local_date,
        load_state,
        save_state,
        update_dataset,
    )
    from vietnam_algo.libs.daily_engine import EngineConfig, StrategyConfig
    from vietnam_algo.libs.dnse.client import DnseClient
    from vietnam_algo.libs.dnse.auth import load_shared_trading_token
    from vietnam_algo.libs.schedule import TradingSchedule, is_trading_day, local_time

    data_dir = PROJECT_ROOT / "vietnam_algo/data"
    live_dir_name = "live"

    # 1. Build config + client
    cfg = EngineConfig(
        dataDir=data_dir,
        strategy=strategy_name,
        liveDirName=live_dir_name,
    )
    strat_cfg = StrategyConfig(initialCash=100_000_000)

    # 2. Override Telegram env for the strategy under test, so the
    # EOD message goes to the CORRECT channel. Each bot has its own
    # chat: .env.telegram-autofin owns the "Auto Fin Bot" channel,
    # .env.telegram owns the momentum bot's channel. Without this
    # override, the autofin EOD would land in the momentum chat and
    # confuse the operator (the launch_autofin.sh script does the
    # same override via `set -a; source .env.telegram-autofin`).
    _telegram_env_path = _load_telegram_env_for_strategy(strategy_name)
    print(
        f"[force_eod] telegram env: {Path(_telegram_env_path).name} → "
        f"chat_id={os.environ.get('TELEGRAM_CHAT_ID', '?')[:14]}…"
    )

    client = DnseClient(
        api_key=os.environ["DNSE_API_KEY"],
        api_secret=os.environ["DNSE_API_SECRET"],
    )

    # 2. Load token
    token_data = load_shared_trading_token(data_dir)
    token = str(token_data["token"]) if token_data and token_data.get("token") else None

    # 3. Resolve adapter
    if strategy_name not in STRATEGY_REGISTRY:
        print(f"ERROR: unknown strategy {strategy_name!r}")
        return 2
    strategy_adapter = STRATEGY_REGISTRY[strategy_name]
    live_submit = (strategy_name == "autofin_signal")
    now = vietnam_now()
    today = local_date(now)
    state = load_state(data_dir, strategy_name=strategy_name)
    schedule = TradingSchedule()
    holidays = set()

    print(f"force-eod: strategy={strategy_name} live_submit={live_submit} date={today}")
    print(f"  state: eodDate={state.eodDate!r} startOfDayDate={state.startOfDayDate!r}")
    print(f"  is_trading_day={is_trading_day(now, holidays)} (BYPASS for force-eod)")

    # 4. Dry-run initial cash override (mirror the runner logic)
    if not live_submit:
        try:
            from vietnam_algo.libs.dry_run_state import init_dry_run_state
            _dry = init_dry_run_state(data_dir, strategy_name=strategy_name)
            if _dry.initial_nav > 0 and _dry.initial_nav < float(strat_cfg.initialCash) * 0.99:
                strat_cfg = _dc_replace(strat_cfg, initialCash=float(_dry.initial_nav))
                print(f"[eod] dry-run: backtest initialCash overridden to {strat_cfg.initialCash:,.0f}")
        except Exception as exc:
            print(f"[eod] dry-run initialCash override failed: {exc}")

    # 5. Run the strategy's decision pipeline (skip_backtest=True for EOD)
    result = strategy_adapter.generate_orders(
        cfg, strat_cfg, trade_date=today, skip_backtest=True, live_submit=live_submit,
    )
    print(f"strategy result: {result.get('count', 0)} order(s) planned")

    # 6. Live submit (autofin only)
    submit_result = None
    if live_submit and result.get("deals_actioned") and token:
        try:
            submit_result = _submit_autofin_deals_to_dnse(
                result.get("deals_actioned") or [],
                account_no=os.environ["DNSE_ACCOUNT_NO"],
                loan_package_id=1775,
                trading_token=token,
                client=client,
                data_dir=data_dir,
                trade_date=today,
            )
            print(f"submit: {len(submit_result.get('submitted', []))} submitted "
                  f"(of {submit_result.get('total', 0)} planned)")
        except Exception as exc:
            print(f"submit FAILED: {type(exc).__name__}: {exc}")
            submit_result = {"error": str(exc)[:300]}

    # 7. Dry-run apply (momentum)
    if not live_submit:
        try:
            from vietnam_algo.algos.base import StrategyAdapter
            adapter = STRATEGY_REGISTRY.get(strategy_name)
            json_name = adapter.orders_json_filename if adapter else "latest_orders.json"
            json_path = data_dir / live_dir_name / json_name
            if json_path.exists():
                _apply_dry_run_fills(
                    json_path, cfg, os.environ["DNSE_ACCOUNT_NO"],
                    strat_cfg, strategy_name=strategy_name,
                    qty_scale=1.0,
                )
                print(f"dry-run apply: {json_path.name} → state updated")
        except Exception as exc:
            print(f"dry-run fill replay failed: {type(exc).__name__}: {exc}")

    # 8. Build portfolio snapshot
    portfolio_snap = None
    try:
        portfolio_snap = _build_portfolio_snapshot(
            client, os.environ["DNSE_ACCOUNT_NO"], cfg, live_submit,
            strategy_name=strategy_name,
        )
    except Exception as exc:
        notify(f"⚠️ Vietnam Algo: portfolio snapshot FAILED at EOD (force-eod)\nerr: {str(exc)[:200]}")
        print(f"portfolio snapshot failed: {exc}")

    # 9. Format + send EOD summary
    if isinstance(result, dict):
        if hasattr(strategy_adapter, "format_cycle_summary"):
            eod_msg = strategy_adapter.format_cycle_summary(result, portfolio=portfolio_snap)
        else:
            from vietnam_algo.libs.notifier import eod_summary_message
            eod_msg = eod_summary_message(result, portfolio=portfolio_snap)
        notify(eod_msg)
        print(f"EOD summary sent to Telegram ({len(eod_msg)} chars)")

    # 10. Update state
    state.eodDate = today
    save_state(state, data_dir, strategy_name=strategy_name)
    print(f"state.eodDate updated to {today}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("strategy", help="Strategy name (e.g. autofin_signal, momentum)")
    args = parser.parse_args()
    return force_eod(args.strategy)


if __name__ == "__main__":
    raise SystemExit(main())
