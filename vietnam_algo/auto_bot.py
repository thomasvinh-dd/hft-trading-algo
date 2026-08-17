from __future__ import annotations

import time
from pathlib import Path

from .bot_state import AutoBotState, load_state, save_state
from .config import StrategyConfig
from .daily_engine import EngineConfig, generate_live_orders, run_daily_once, trading_token_from_email, update_dataset
from .dnse_client import DnseClient
from .schedule import TradingSchedule, local_date, should_refresh_token, should_run_eod, should_submit_orders, vietnam_now


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

        if should_refresh_token(now, state.tokenDate, schedule):
            print(f"[{now.isoformat()}] Refreshing DNSE trading token")
            if live_submit:
                state.tradingToken = trading_token_from_email(client)
            else:
                state.tradingToken = "dry-run-token"
            state.tokenDate = today
            save_state(state, engine_config.dataDir)

        if should_submit_orders(now, state.orderSubmitDate, schedule):
            print(f"[{now.isoformat()}] Generating and submitting latest orders")
            if not state.tradingToken:
                if live_submit:
                    state.tradingToken = trading_token_from_email(client)
                else:
                    state.tradingToken = "dry-run-token"
                state.tokenDate = today
                save_state(state, engine_config.dataDir)
            result = run_daily_once(
                engine_config,
                strategy_config,
                submit=live_submit,
                account_no=account_no,
                loan_package_id=loan_package_id,
                trading_token=state.tradingToken,
                client=client,
            )
            print(result)
            state.orderSubmitDate = today
            save_state(state, engine_config.dataDir)

        if should_run_eod(now, state.eodDate, schedule):
            print(f"[{now.isoformat()}] Running EOD data update and order generation")
            update_dataset(engine_config)
            result = generate_live_orders(engine_config, strategy_config)
            print(result)
            state.eodDate = today
            save_state(state, engine_config.dataDir)

        if once:
            return
        time.sleep(sleep_seconds)
