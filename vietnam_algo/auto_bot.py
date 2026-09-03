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
    TradingSchedule, local_date, should_run_eod, should_submit_orders,
    should_sync_corporate_actions, should_send_heartbeat, vietnam_now,
)
from .token_store import ensure_fresh_trading_token


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
) -> None:
    bot_start_time = vietnam_now()
    while True:
        now = vietnam_now()
        today = local_date(now)
        state = load_state(engine_config.dataDir)
        if should_sync_market_holidays(state.marketHolidaySyncDate, today):
            print(f"[{now.isoformat()}] Syncing Vietnam market holidays")
            result = sync_market_holidays(engine_config.dataDir)
            print(result)
            notify("\n".join(["Vietnam Algo: market holidays synced", f"count: {result.get('count')}", f"path: {result.get('path')}"]))
            state.marketHolidaySyncDate = today
            save_state(state, engine_config.dataDir)
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
                    save_state(state, engine_config.dataDir)
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
            save_state(state, engine_config.dataDir)

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
                save_state(state, engine_config.dataDir)
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
            save_state(state, engine_config.dataDir)

        if should_run_eod(now, state.eodDate, schedule, holidays):
            print(f"[{now.isoformat()}] Running EOD data update and order generation")
            notify(f"Vietnam Algo: EOD update started\ntime: {now.isoformat()}")
            update_dataset(engine_config)
            result = generate_live_orders(engine_config, strategy_config)
            print(result)
            if isinstance(result, dict):
                notify(eod_summary_message(result))
                # Pull live portfolio snapshot alongside EOD summary (best-effort)
                try:
                    from .portfolio import build_portfolio_snapshot
                    from .notifier import portfolio_snapshot_message
                    snap = build_portfolio_snapshot(client, account_no)
                    notify(portfolio_snapshot_message(snap))
                except Exception as exc:
                    notify(
                        "\n".join(
                            [
                                "⚠️ Vietnam Algo: portfolio snapshot FAILED at EOD",
                                f"err: {str(exc)[:200]}",
                            ]
                        )
                    )
            state.eodDate = today
            save_state(state, engine_config.dataDir)

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
            save_state(state, engine_config.dataDir)

        if once:
            return
        time.sleep(sleep_seconds)
