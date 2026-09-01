from __future__ import annotations

import time
import os
from pathlib import Path

from .bot_state import AutoBotState, load_state, save_state
from .config import StrategyConfig
from .corporate_actions import sync_corporate_actions_incremental
from .daily_engine import EngineConfig, generate_live_orders, run_daily_once, trading_token_from_email, update_dataset
from .dnse_client import DnseClient
from .market_calendar import load_market_holidays, should_sync_market_holidays, sync_market_holidays
from .notifier import notify, token_refresh_message, eod_summary_message
from .schedule import TradingSchedule, local_date, should_refresh_token, should_run_eod, should_submit_orders, should_sync_corporate_actions, vietnam_now


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

        if should_refresh_token(now, state.tokenDate, schedule, holidays):
            print(f"[{now.isoformat()}] Refreshing DNSE trading token")
            refresh_ok = False
            if live_submit:
                try:
                    state.tradingToken = trading_token_from_email(client)
                    refresh_ok = True
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
                    raise
            else:
                state.tradingToken = "dry-run-token"
                refresh_ok = True
            notify(token_refresh_message(account_no, live_submit, refresh_ok))
            state.tokenDate = today
            save_state(state, engine_config.dataDir)

        if should_submit_orders(now, state.orderSubmitDate, schedule, holidays):
            print(f"[{now.isoformat()}] Generating and submitting latest orders")
            notify(f"Vietnam Algo: submit cycle started\ntime: {now.isoformat()}\nliveSubmit: {live_submit}")
            if not state.tradingToken:
                if live_submit:
                    state.tradingToken = trading_token_from_email(client)
                else:
                    state.tradingToken = "dry-run-token"
                state.tokenDate = today
                save_state(state, engine_config.dataDir)
            try:
                result = run_daily_once(
                    engine_config,
                    strategy_config,
                    submit=live_submit,
                    account_no=account_no,
                    loan_package_id=loan_package_id,
                    trading_token=state.tradingToken,
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
            result = sync_corporate_actions_incremental(
                engine_config.dataDir,
                request_verification_token=token,
                cookie=cookie,
                to_date=today,
            )
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
            state.eodDate = today
            save_state(state, engine_config.dataDir)

        if once:
            return
        time.sleep(sleep_seconds)
