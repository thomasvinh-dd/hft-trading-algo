from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .backtest import (
    BacktestResult,
    run_hybrid_momentum_breakout_sector,
    run_momentum_rotation,
    save_backtest_result,
    save_order_list,
)
from .config import StrategyConfig
from .data import DEFAULT_DATA_DIR, load_saved_symbols, sync_prices, sync_symbols, utc_now, write_json
from .dnse_client import DnseClient
from .otp_reader import latest_email_otp
from .trading_engine import execute_dnse_orders, prepare_dnse_orders
from .universe import build_liquid_universe, save_liquid_universe


@dataclass(frozen=True)
class EngineConfig:
    dataDir: Path = DEFAULT_DATA_DIR
    strategy: str = "momentum"
    updateSymbols: bool = False
    updatePrices: bool = False
    priceLimit: int = 1825
    priceSleepSeconds: float = 1.0
    maxSymbols: int = 0
    loopSeconds: int = 3600
    liveDirName: str = "live"


def strategy_runner(name: str) -> Callable[[Path, str, StrategyConfig | None], BacktestResult]:
    if name == "hybrid":
        return run_hybrid_momentum_breakout_sector
    if name == "momentum":
        return run_momentum_rotation
    raise ValueError(f"Unsupported live strategy: {name}")


def latest_order_date(orders_csv: Path) -> str | None:
    with orders_csv.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        return None
    return max(str(row["date"]) for row in rows if row.get("date"))


def export_orders_for_date(orders_csv: Path, output_dir: Path, trade_date: str | None = None) -> tuple[Path, Path, str | None, int]:
    with orders_csv.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
        fieldnames = list(rows[0].keys()) if rows else []
    selected_date = trade_date or latest_order_date(orders_csv)
    selected = [row for row in rows if selected_date and row.get("date") == selected_date]
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "latest_orders.csv"
    json_path = output_dir / "latest_orders.json"
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(selected)
    write_json(
        json_path,
        {
            "generatedAt": utc_now(),
            "source": str(orders_csv),
            "tradeDate": selected_date,
            "count": len(selected),
            "items": selected,
        },
    )
    return csv_path, json_path, selected_date, len(selected)


def update_dataset(config: EngineConfig) -> None:
    if config.updateSymbols:
        sync_symbols(config.dataDir)
    if config.updatePrices:
        symbols = load_saved_symbols(config.dataDir)
        if config.maxSymbols > 0:
            symbols = symbols[: config.maxSymbols]
        sync_prices(
            symbols,
            config.dataDir,
            price_limit=config.priceLimit,
            sleep_seconds=config.priceSleepSeconds,
            skip_existing=False,
        )


def generate_live_orders(config: EngineConfig, strategy_config: StrategyConfig, trade_date: str | None = None) -> dict[str, object]:
    save_liquid_universe(build_liquid_universe(config.dataDir), config.dataDir)
    result = strategy_runner(config.strategy)(config.dataDir, "liquid_stocks", strategy_config)
    result_path = save_backtest_result(result, config.dataDir)
    orders_path = save_order_list(result, config.dataDir)
    csv_path, json_path, selected_date, count = export_orders_for_date(
        orders_path,
        config.dataDir / config.liveDirName,
        trade_date=trade_date,
    )
    return {
        "result": str(result_path),
        "orders": str(orders_path),
        "latestOrdersCsv": str(csv_path),
        "latestOrdersJson": str(json_path),
        "tradeDate": selected_date,
        "count": count,
        "metrics": result.metrics,
    }


def trading_token_from_email(client: DnseClient, otp_type: str = "email_otp") -> str:
    username = os.environ.get("GMAIL_ADDRESS", "")
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not username or not app_password:
        raise RuntimeError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD to read DNSE email OTP")
    client.send_email_otp()
    time.sleep(float(os.environ.get("DNSE_OTP_WAIT_SECONDS", "20")))
    otp = latest_email_otp(
        username=username,
        app_password=app_password,
        mailbox=os.environ.get("GMAIL_MAILBOX", "INBOX"),
        sender_filter=os.environ.get("DNSE_OTP_SENDER", ""),
        subject_filter=os.environ.get("DNSE_OTP_SUBJECT", ""),
        otp_regex=os.environ.get("DNSE_OTP_REGEX", r"\b(\d{6})\b"),
    )
    return client.trading_token(otp_type, otp)


def run_daily_once(
    engine_config: EngineConfig,
    strategy_config: StrategyConfig,
    trade_date: str | None = None,
    submit: bool = False,
    account_no: str = "",
    loan_package_id: int = 0,
    trading_token: str = "",
    client: DnseClient | None = None,
) -> dict[str, object]:
    update_dataset(engine_config)
    generated = generate_live_orders(engine_config, strategy_config, trade_date=trade_date)
    if not submit:
        return {"status": "generated", **generated}
    if not account_no or not loan_package_id or not trading_token or client is None:
        raise RuntimeError("account_no, loan_package_id, trading_token, and DNSE client are required for submit")
    orders = prepare_dnse_orders(Path(str(generated["latestOrdersCsv"])), loan_package_id)
    results = execute_dnse_orders(client, account_no, trading_token, orders, dry_run=False)
    return {"status": "submitted", **generated, "submitResults": results}
