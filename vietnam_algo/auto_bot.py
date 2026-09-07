from __future__ import annotations

import time
import os
import urllib.request
from pathlib import Path
from datetime import datetime

from .bot_state import AutoBotState, load_state, save_state
from .config import StrategyConfig
from .corporate_actions import sync_corporate_actions_incremental
from .daily_engine import EngineConfig, generate_live_orders, run_daily_once, update_dataset
from .dnse_client import DnseClient
from .market_calendar import load_market_holidays, should_sync_market_holidays, sync_market_holidays
from .notifier import notify, token_refresh_message, eod_summary_message, heartbeat_message
from .schedule import (
    TradingSchedule, local_date, should_poll_strategy, should_run_eod,
    should_run_start_of_day, should_submit_orders,
    should_sync_corporate_actions, should_send_heartbeat, vietnam_now,
)
from .strategy_registry import STRATEGY_REGISTRY, get_strategy
from .token_store import ensure_fresh_trading_token


def _read_strategy_last_poll(data_dir: Path, strategy_name: str) -> str | None:
    """Read ``last_poll_at`` from a strategy's state file (if any).

    Used to throttle mid-day strategy polls. Each strategy can store its
    own last-poll timestamp in its state file (e.g. for autofin_signal
    we keep ``last_poll_at`` in ``autofin_signal_state.json``).
    """
    if not strategy_name:
        return None
    state_files = {
        "autofin_signal": "autofin_signal_state.json",
    }
    fname = state_files.get(strategy_name)
    if not fname:
        return None
    path = data_dir / "live" / fname
    if not path.exists():
        return None
    try:
        import json as _json
        payload = _json.loads(path.read_text())
        return payload.get("last_poll_at")
    except Exception:
        return None


def run_auto_bot(
    engine_config: EngineConfig,
    strategy_config: StrategyConfig,
    client: DnseClient,
    account_no: str,
    loan_package_id: int,
    live_submit: bool = False,
    once: bool = False,
    sleep_seconds: int = 60,
    schedule: TradingSchedule = TradingSchedule(),
    strategy_name: str = "",
) -> None:
    """Main bot loop.

    ``strategy_name`` namespaces per-strategy state files
    (auto_bot_state.{name}.json) so multiple strategies can run
    side-by-side on the same VPS without clobbering each other.
    """
    strategy_adapter = get_strategy(engine_config.strategy) if engine_config.strategy else None
    bot_start_time = vietnam_now()
    while True:
        now = vietnam_now()
        today = local_date(now)
        state = load_state(engine_config.dataDir, strategy_name=strategy_name)
        if should_sync_market_holidays(state.marketHolidaySyncDate, today):
            print(f"[{now.isoformat()}] Syncing Vietnam market holidays")
            result = sync_market_holidays(engine_config.dataDir)
            print(result)
            notify("\n".join(["Vietnam Algo: market holidays synced", f"count: {result.get('count')}", f"path: {result.get('path')}"]))
            state.marketHolidaySyncDate = today
            save_state(state, engine_config.dataDir, strategy_name=strategy_name)
        holidays = load_market_holidays(engine_config.dataDir)

        if should_submit_orders(now, state.orderSubmitDate, schedule, holidays):
            print(f"[{now.isoformat()}] Generating and submitting latest orders")
            notify(f"Vietnam Algo: submit cycle started\ntime: {now.isoformat()}\nliveSubmit: {live_submit}")
            # Lazy refresh: only fetch a token if (a) live mode AND
            # (b) we don't have a fresh one. Dry-run just sets a placeholder.
            def _refresh_and_notify(msg: str) -> None:
                # Called from inside ensure_fresh_trading_token — keep
                # the notify path uniform with the rest of the bot.
                notify(msg)
            try:
                token, was_refreshed = ensure_fresh_trading_token(
                    client=client,
                    state=state,
                    account_no=account_no,
                    live_submit=live_submit,
                    notify_fn=_refresh_and_notify,
                )
                if was_refreshed and live_submit:
                    notify(token_refresh_message(account_no, live_submit, True))
                    save_state(state, engine_config.dataDir, strategy_name=strategy_name)
            except Exception as exc:
                notify(
                    "\n".join(
                        [
                            "🚨 Vietnam Algo: TOKEN REFRESH FAILED",
                            f"account: {account_no}",
                            f"err: {str(exc)[:300]}",
                        ]
                    )
                )
                if once:
                    raise
                continue
            try:
                result = run_daily_once(
                    engine_config,
                    strategy_config,
                    submit=live_submit,
                    account_no=account_no,
                    loan_package_id=loan_package_id,
                    trading_token=token,
                    client=client,
                )
            except Exception as exc:
                notify(
                    "\n".join(
                        [
                            "🚨 Vietnam Algo: SUBMIT CYCLE FAILED",
                            f"account: {account_no}",
                            f"tradeDate: {today}",
                            f"err: {str(exc)[:400]}",
                        ]
                    )
                )
                print(result if False else f"[{now.isoformat()}] submit cycle failed: {exc}")
                if once:
                    raise
                continue
            print(result)
            if result.get("submitComplete") or not live_submit:
                state.orderSubmitDate = today
            else:
                notify(
                    "\n".join(
                        [
                            "⚠️ Vietnam Algo: submit not marked complete",
                            f"date: {today}",
                            f"status: {result.get('status')}",
                            "reason: order ledger has unknown/open/pending orders",
                        ]
                    )
                )
            save_state(state, engine_config.dataDir, strategy_name=strategy_name)

        if should_sync_corporate_actions(now, state.corporateActionDate, schedule, holidays):
            print(f"[{now.isoformat()}] Syncing Vietstock corporate actions")
            notify(f"Vietnam Algo: syncing corporate actions\ntime: {now.isoformat()}")
            token = os.environ.get("VIETSTOCK_REQUEST_TOKEN", "")
            cookie = os.environ.get("VIETSTOCK_COOKIE", "")
            try:
                result = sync_corporate_actions_incremental(
                    engine_config.dataDir,
                    request_verification_token=token,
                cookie=cookie,
                to_date=today,
                )
            except Exception as exc:
                notify(
                    "\n".join(
                        [
                            "⚠️ Vietnam Algo: corporate-action sync FAILED",
                            f"err: {str(exc)[:300]}",
                            "continuing without updated CA data",
                        ]
                    )
                )
                print(f"[{now.isoformat()}] CA sync failed: {exc}")
                state.corporateActionDate = today  # don't retry on same day
                save_state(state, engine_config.dataDir, strategy_name=strategy_name)
                continue
            print(result)
            notify(
                "\n".join(
                    [
                        "Vietnam Algo: corporate actions synced",
                        f"fromDate: {result.get('fromDate')}",
                        f"toDate: {result.get('toDate')}",
                        f"fetched: {result.get('fetched')}",
                        f"added: {result.get('added')}",
                        f"total: {result.get('total')}",
                    ]
                )
            )
            state.corporateActionDate = today
            save_state(state, engine_config.dataDir, strategy_name=strategy_name)

        if should_run_start_of_day(
            now,
            state.startOfDayDate,
            schedule=schedule,
            holidays=holidays,
        ):
            print(f"[{now.isoformat()}] Sending start-of-day briefing")
            try:
                from .portfolio import build_portfolio_snapshot
                from .notifier import start_of_day_message
                portfolio_snap = build_portfolio_snapshot(client, account_no)
                # read latest_orders JSON for the per-strategy file
                from .strategy_base import StrategyAdapter
                adapter = STRATEGY_REGISTRY.get(engine_config.strategy) if engine_config.strategy else None
                json_name = adapter.orders_json_filename if adapter else "latest_orders.json"
                json_path = (
                    engine_config.dataDir / engine_config.liveDirName / json_name
                )
                msg = start_of_day_message(
                    portfolio=portfolio_snap,
                    today=today,
                    orders_json_path=str(json_path),
                    strategy_name=engine_config.strategy or "?",
                    account=account_no,
                )
                notify(msg)
            except Exception as exc:
                notify(
                    "\n".join(
                        [
                            "⚠️ Vietnam Algo: start-of-day briefing FAILED",
                            f"err: {str(exc)[:300]}",
                        ]
                    )
                )
                print(f"[{now.isoformat()}] start-of-day failed: {exc}")
            state.startOfDayDate = today
            save_state(state, engine_config.dataDir, strategy_name=strategy_name)

        if should_poll_strategy(
            now,
            _read_strategy_last_poll(engine_config.dataDir, strategy_name),
            schedule=schedule,
            holidays=holidays,
        ):
            # Mid-day polls only run for strategies that need real-time
            # detection of mid-day signal changes (e.g. autofin partial
            # closes). Other strategies (momentum, hybrid, etc.) are
            # designed to run at EOD only — running them mid-day just
            # regenerates the same orders and spams Telegram.
            if engine_config.strategy != "autofin_signal":
                print(
                    f"[{now.isoformat()}] Skipping mid-day poll for "
                    f"strategy={engine_config.strategy} (EOD-only strategy)"
                )
            else:
                print(f"[{now.isoformat()}] Mid-day autofin poll (partial-close detection)")
                try:
                    # Call the autofin adapter directly so it detects new
                    # partial closes and sends per-order Telegram
                    # notifications. Do NOT call generate_live_orders
                    # (that runs the momentum backtest).
                    if strategy_adapter is not None:
                        poll_result = strategy_adapter.generate_orders(
                            engine_config, strategy_config,
                            trade_date=today,
                        )
                    else:
                        poll_result = generate_live_orders(
                            engine_config, strategy_config,
                            strategy_name=strategy_name,
                        )
                    # The strategy itself sends per-order Telegram
                    # notifications for any newly-detected action (partial
                    # close, new entry, full close) — see
                    # AutofinSignalAdapter._notify_new_orders.
                    print(f"[{now.isoformat()}] poll result: {poll_result.get('count', 0)} orders planned")
                except Exception as exc:
                    notify(
                        "\n".join(
                            [
                                "⚠️ Vietnam Algo: mid-day poll FAILED",
                                f"strategy: {engine_config.strategy}",
                                f"err: {str(exc)[:300]}",
                            ]
                        )
                    )
                    print(f"[{now.isoformat()}] mid-day poll failed: {exc}")

        if should_run_eod(now, state.eodDate, schedule, holidays):
            print(f"[{now.isoformat()}] Running EOD data update and order generation")
            notify(f"Vietnam Algo: EOD update started\ntime: {now.isoformat()}\nstrategy: {engine_config.strategy}")
            update_dataset(engine_config)
            result = generate_live_orders(
                engine_config, strategy_config, strategy_name=strategy_name,
                notify_telegram=False,
            )
            print(result)
            # Fetch live portfolio snapshot once so the EOD summary can
            # show cash, NAV, and per-position P&L inline.
            portfolio_snap: dict | None = None
            try:
                from .portfolio import build_portfolio_snapshot
                portfolio_snap = build_portfolio_snapshot(client, account_no)
            except Exception as exc:
                notify(
                    "\n".join(
                        [
                            "⚠️ Vietnam Algo: portfolio snapshot FAILED at EOD",
                            f"err: {str(exc)[:200]}",
                        ]
                    )
                )
                print(f"[{now.isoformat()}] portfolio snapshot failed: {exc}")
            if isinstance(result, dict):
                # Use strategy-specific formatter if available, else default.
                # Both now accept the live portfolio snapshot.
                if strategy_adapter is not None:
                    notify(strategy_adapter.format_cycle_summary(result, portfolio=portfolio_snap))
                else:
                    notify(eod_summary_message(result, portfolio=portfolio_snap))
            state.eodDate = today
            save_state(state, engine_config.dataDir, strategy_name=strategy_name)

        if should_send_heartbeat(now, state.lastHeartbeatDate, schedule, holidays):
            print(f"[{now.isoformat()}] Sending daily heartbeat")
            # Quick proxy connectivity check (best-effort, non-fatal on error)
            proxy_check: dict[str, object] | None = None
            proxy_url = os.environ.get("DNSE_PROXY_HTTP_URL")
            if proxy_url:
                try:
                    import time as _t
                    t0 = _t.time()
                    req = urllib.request.Request(
                        "https://openapi.dnse.com.vn/accounts",
                        headers={
                            "X-API-Key": "heartbeat-probe",
                            "version": "2026-07-23",
                        },
                    )
                    urllib.request.urlopen(req, timeout=10).read()
                    proxy_check = {"ok": True, "latency_ms": (_t.time() - t0) * 1000}
                except Exception as exc:
                    proxy_check = {"ok": False, "error": str(exc)[:120]}
            uptime_s = (now - bot_start_time).total_seconds() if bot_start_time else None
            pid = os.getpid()
            msg = heartbeat_message(
                account_no=account_no,
                live_submit=live_submit,
                state=state.__dict__,
                proxy_url=proxy_url,
                proxy_check=proxy_check,
                pid=pid,
                uptime_seconds=uptime_s,
            )
            notify(msg)
            state.lastHeartbeatDate = today
            save_state(state, engine_config.dataDir, strategy_name=strategy_name)

        if once:
            return
        time.sleep(sleep_seconds)
