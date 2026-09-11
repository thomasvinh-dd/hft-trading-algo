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
from .market_calendar import live_trading_dates
from .notifier import daily_generation_message, notify, submit_message, portfolio_snapshot_message
from .order_ledger import (
    LedgerOrder,
    dnse_order_id,
    ledger_key,
    load_ledger,
    normalize_ledger_status,
    order_status_from_detail,
    save_ledger,
    submission_complete,
    unsafe_existing_orders,
)
from .risk_controls import live_risk_limits_from_env, validate_live_orders
from .otp_reader import latest_email_otp
from .ca_rebuy_state import CaRebuyState
from .corporate_actions import (
    build_corporate_action_lookup,
    corporate_action_path,
    corporate_action_risk_type,
    load_corporate_actions,
)
from .trading_engine import (
    apply_sellable_quantity_cap,
    build_corporate_action_exit_orders,
    execute_dnse_orders,
    extract_sellable_positions,
    prepare_dnse_orders_with_risk_report,
    price_orders_from_orderbook,
)
from .universe import build_liquid_universe, save_liquid_universe


@dataclass(frozen=True)
class EngineConfig:
    dataDir: Path = DEFAULT_DATA_DIR
    strategy: str = "momentum"
    updateSymbols: bool = False
    updatePrices: bool = False
    # When False, the bot skips both price sync (update_dataset) and
    # corporate-action sync, reading from disk instead. Use this for
    # bots that share data with a designated data-sync owner to avoid
    # hitting vietnamfinance.vn / Vietstock API twice per EOD cycle.
    syncData: bool = True
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


def export_orders_for_date(
    orders_csv: Path,
    output_dir: Path,
    trade_date: str | None = None,
    csv_filename: str = "latest_orders.csv",
    json_filename: str = "latest_orders.json",
) -> tuple[Path, Path, str | None, int]:
    with orders_csv.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
        fieldnames = list(rows[0].keys()) if rows else []
    selected_date = trade_date or latest_order_date(orders_csv)
    selected = [row for row in rows if selected_date and row.get("date") == selected_date]
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / csv_filename
    json_path = output_dir / json_filename
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
        print(f"[update_dataset] refreshing symbol universe at {config.dataDir}")
        sync_symbols(config.dataDir)
    if not config.updatePrices:
        # Be loud about this — forgetting --update-prices on the launch
        # command silently leaves price files stale (see git log for the
        # 2026-09-08 incident where prices stayed at 2026-08-28 for days).
        print(
            "[update_dataset] updatePrices=False → skipping price sync."
            " Pass --update-prices to refresh."
        )
        return
    symbols = load_saved_symbols(config.dataDir)
    if config.maxSymbols > 0:
        symbols = symbols[: config.maxSymbols]
    if not symbols:
        print(f"[update_dataset] no symbols found in {config.dataDir}/symbols.json")
        return
    print(
        f"[update_dataset] incremental price sync for {len(symbols)} symbols"
        f" (sleep={config.priceSleepSeconds}s)"
    )
    # Use incremental sync: fetch only the most-recent bars and merge
    # with existing on-disk files. This is dramatically cheaper than
    # re-fetching the full ``priceLimit``-bar history every cycle and
    # keeps the data fresh for EOD mark-to-market and order generation.
    from .data import sync_prices_incremental
    sync_prices_incremental(
        symbols,
        config.dataDir,
        sleep_seconds=config.priceSleepSeconds,
        incremental_limit=10,
    )


def generate_live_orders(
    config: EngineConfig,
    strategy_config: StrategyConfig,
    trade_date: str | None = None,
    strategy_name: str = "",
    notify_telegram: bool = False,
) -> dict[str, object]:
    """Run the strategy's full backtest and write per-strategy order files.

    ``strategy_name`` controls the namespacing of:
      - data/live/latest_orders.{name}.csv  (the live order CSV)
      - data/live/latest_orders.{name}.json
      - data/backtests/{name}_backtest.json

    When empty, falls back to the legacy non-namespaced paths
    (latest_orders.csv) for backward compatibility.

    ``notify_telegram`` defaults to False — callers should use
    ``daily_generation_message()`` or ``format_cycle_summary()`` to
    decide what (if anything) to send to Telegram, rather than always
    sending a generic "orders generated" message on every call.
    """
    from .strategy_registry import get_strategy
    save_liquid_universe(build_liquid_universe(config.dataDir), config.dataDir)
    adapter = get_strategy(config.strategy) if strategy_name else None
    result = strategy_runner(config.strategy)(config.dataDir, "liquid_stocks", strategy_config)
    result_path = save_backtest_result(result, config.dataDir)
    orders_path = save_order_list(result, config.dataDir)

    csv_name = (adapter.orders_csv_filename if adapter else "latest_orders.csv")
    json_name = (adapter.orders_json_filename if adapter else "latest_orders.json")
    csv_path, json_path, selected_date, count = export_orders_for_date(
        orders_path,
        config.dataDir / config.liveDirName,
        trade_date=trade_date,
        csv_filename=csv_name,
        json_filename=json_name,
    )
    result_payload = {
        "result": str(result_path),
        "orders": str(orders_path),
        "latestOrdersCsv": str(csv_path),
        "latestOrdersJson": str(json_path),
        "tradeDate": selected_date,
        "count": count,
        "metrics": result.metrics,
        "strategy": config.strategy,
        "orders_file": str(csv_path),
    }
    if notify_telegram:
        notify(daily_generation_message(result_payload))
    return result_payload


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


def reconcile_ledger_orders(client: DnseClient, account_no: str, ledger: dict[str, LedgerOrder]) -> tuple[dict[str, LedgerOrder], list[dict[str, object]]]:
    updates: list[dict[str, object]] = []
    now = utc_now()
    for key, item in list(ledger.items()):
        if not item.dnseOrderId or item.status not in {"submitted", "open", "partial", "unknown", "submitting"}:
            continue
        try:
            detail = client.order_detail(account_no, item.dnseOrderId)
            status = normalize_ledger_status(order_status_from_detail(detail))
            ledger[key] = LedgerOrder(
                runId=item.runId,
                sourceOrderId=item.sourceOrderId,
                sourceDate=item.sourceDate,
                symbol=item.symbol,
                side=item.side,
                quantity=item.quantity,
                price=item.price,
                reason=item.reason,
                status=status,
                dnseOrderId=item.dnseOrderId,
                createdAt=item.createdAt,
                updatedAt=now,
                lastDetail=detail,
                error=item.error,
            )
            updates.append({"sourceOrderId": item.sourceOrderId, "symbol": item.symbol, "status": status, "dnseOrderId": item.dnseOrderId})
        except Exception as exc:
            ledger[key] = LedgerOrder(**{**item.__dict__, "status": "unknown", "updatedAt": now, "error": str(exc)})
            updates.append({"sourceOrderId": item.sourceOrderId, "symbol": item.symbol, "status": "unknown", "error": str(exc)})
    return ledger, updates


def execute_orders_with_ledger(
    client: DnseClient,
    account_no: str,
    trading_token: str,
    orders: list[object],
    data_dir: Path,
    run_id: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], bool]:
    from dataclasses import asdict

    ledger = load_ledger(data_dir)
    ledger, reconciliation = reconcile_ledger_orders(client, account_no, ledger)
    unsafe = unsafe_existing_orders(ledger, run_id)
    if unsafe:
        save_ledger(ledger, data_dir)
        return [], [*reconciliation, {"action": "blocked_by_unsafe_existing_orders", "count": len(unsafe)}], False
    now = utc_now()
    results: list[dict[str, object]] = []
    for order in orders:
        payload = asdict(order)
        key = ledger_key(run_id, int(payload["sourceOrderId"]), str(payload["symbol"]), str(payload["side"]))
        existing = ledger.get(key)
        if existing and existing.status in {"filled", "cancelled", "rejected", "expired", "closed", "submitted", "open", "partial"}:
            results.append({"status": "skipped_existing_ledger", "order": payload, "ledgerStatus": existing.status, "dnseOrderId": existing.dnseOrderId})
            continue
        ledger[key] = LedgerOrder(
            runId=run_id,
            sourceOrderId=int(payload["sourceOrderId"]),
            sourceDate=str(payload["sourceDate"]),
            symbol=str(payload["symbol"]),
            side=str(payload["side"]),
            quantity=int(payload["quantity"]),
            price=int(payload["price"]),
            reason=str(payload["reason"]),
            status="submitting",
            createdAt=now,
            updatedAt=now,
        )
        save_ledger(ledger, data_dir)
        try:
            response = client.place_stock_order(
                account_no=account_no,
                trading_token=trading_token,
                symbol=str(payload["symbol"]),
                side=str(payload["side"]),
                quantity=int(payload["quantity"]),
                price=float(payload["price"]),
                loan_package_id=int(payload["loanPackageId"]),
                order_type=str(payload["orderType"]),
            )
            order_id = dnse_order_id(response)
            detail = client.order_detail(account_no, order_id) if order_id else response
            status = normalize_ledger_status(order_status_from_detail(detail)) if order_id else "unknown"
            ledger[key] = LedgerOrder(**{**ledger[key].__dict__, "status": status, "dnseOrderId": order_id, "updatedAt": utc_now(), "lastDetail": detail})
            results.append({"status": "submitted", "order": payload, "response": response, "detail": detail})
        except Exception as exc:
            ledger[key] = LedgerOrder(**{**ledger[key].__dict__, "status": "unknown", "updatedAt": utc_now(), "error": str(exc)})
            results.append({"status": "unknown", "order": payload, "error": str(exc)})
        save_ledger(ledger, data_dir)
    complete = submission_complete(ledger, run_id)
    return results, reconciliation, complete


def run_daily_once(
    engine_config: EngineConfig,
    strategy_config: StrategyConfig,
    trade_date: str | None = None,
    submit: bool = False,
    account_no: str = "",
    loan_package_id: int = 0,
    trading_token: str = "",
    client: DnseClient | None = None,
    initial_orderbook_pricing: bool = True,
    max_price_drift_percent: float = 2.0,
) -> dict[str, object]:
    update_dataset(engine_config)
    generated = generate_live_orders(engine_config, strategy_config, trade_date=trade_date)
    # Always fetch portfolio snapshot (works in both dry-run and live mode)
    portfolio_snapshot: dict[str, object] = {}
    if client is not None and account_no:
        try:
            from .portfolio import build_portfolio_snapshot
            portfolio_snapshot = build_portfolio_snapshot(client, account_no)
        except Exception as exc:  # noqa: BLE001 — best-effort
            portfolio_snapshot = {"errors": [f"build_portfolio_snapshot: {exc!r}"]}
    if not submit:
        result = {"status": "generated", **generated}
        if portfolio_snapshot:
            result["portfolio"] = portfolio_snapshot
        return result
    if not account_no or not loan_package_id or not trading_token or client is None:
        raise RuntimeError("account_no, loan_package_id, trading_token, and DNSE client are required for submit")
    actions_path = corporate_action_path(engine_config.dataDir)
    trade_date_value = str(generated.get("tradeDate") or trade_date or latest_order_date(Path(str(generated["latestOrdersCsv"]))) or "")
    trading_dates = live_trading_dates(engine_config.dataDir, trade_date_value, "2030-12-31") if trade_date_value else None
    ca_actions = load_corporate_actions(actions_path) if actions_path.exists() else []
    _, ca_ex_index = build_corporate_action_lookup(ca_actions, trading_dates or [])
    orders, decisions = prepare_dnse_orders_with_risk_report(
        Path(str(generated["latestOrdersCsv"])),
        loan_package_id,
        corporate_actions_path=actions_path if actions_path.exists() else None,
        corporate_action_buy_window_days=strategy_config.corporateActionBuyWindowDays,
        corporate_action_sell_window_days=strategy_config.corporateActionSellWindowDays,
        corporate_action_pre_exit_days=strategy_config.corporateActionPreExitDays,
        corporate_action_high_risk_buy_window_days=strategy_config.corporateActionHighRiskBuyWindowDays,
        corporate_action_high_risk_sell_window_days=strategy_config.corporateActionHighRiskSellWindowDays,
        trading_dates=trading_dates,
        precomputed_ex_index=ca_ex_index,
    )
    if actions_path.exists() and generated.get("tradeDate"):
        exit_orders, exit_decisions = build_corporate_action_exit_orders(
            client,
            account_no,
            loan_package_id,
            str(generated["tradeDate"]),
            ca_actions,
            orders,
            sell_window_days=strategy_config.corporateActionSellWindowDays,
            pre_exit_days=strategy_config.corporateActionPreExitDays,
            high_risk_sell_window_days=strategy_config.corporateActionHighRiskSellWindowDays,
            trading_dates=trading_dates,
            precomputed_ex_index=ca_ex_index,
        )
        orders.extend(exit_orders)
        decisions.extend(exit_decisions)

        # Queue post-CA rebays for every force-exit we just emitted, and emit
        # any rebays from prior cycles whose signal/delay fired today.
        if strategy_config.corporateActionRebuyAfterExDate and exit_orders:
            rebuy_state = CaRebuyState.load(engine_config.dataDir)
            try:
                current_idx = trading_dates.index(trade_date_value) if trading_dates else 0
            except ValueError:
                current_idx = 0
            symbol_ex_dates: dict[str, str] = {}
            symbol_high_risk: dict[str, bool] = {}
            for o in exit_orders:
                # Pick the soonest ex-date for this symbol from the CA events
                # sitting in the union window (standard pre-exit + high-risk).
                risky_for_symbol = corporate_action_window_lookup(
                    ca_actions,
                    trade_date_value,
                    max(
                        strategy_config.corporateActionPreExitDays,
                        strategy_config.corporateActionHighRiskSellWindowDays,
                    ),
                    trading_dates,
                    precomputed_ex_index=ca_ex_index,
                ).get(o.symbol, [])
                if risky_for_symbol:
                    symbol_ex_dates[o.symbol] = min(e.exDate for e in risky_for_symbol if e.exDate)
                    symbol_high_risk[o.symbol] = any(
                        corporate_action_risk_type(e) == "high" for e in risky_for_symbol
                    )
            from .trading_engine import (
                build_post_ca_rebuy_orders,
                corporate_action_window_lookup,
                enqueue_pending_rebuys_from_force_exits,
            )
            enqueue_pending_rebuys_from_force_exits(
                rebuy_state,
                exit_orders,
                trading_dates or [],
                current_idx,
                strategy_config,
                symbol_ex_dates,
                symbol_high_risk,
            )
            rebuy_orders, rebuy_decisions = build_post_ca_rebuy_orders(
                rebuy_state,
                engine_config.dataDir,
                trade_date_value,
                trading_dates or [],
                current_idx,
                strategy_config,
                loan_package_id,
            )
            orders.extend(rebuy_orders)
            decisions.extend(rebuy_decisions)
            rebuy_state.save(engine_config.dataDir)
            result_payload_extras = {
                "pendingRebuysAfter": [p.to_dict() for p in rebuy_state.pendingRebuys],
                "reservedCashAfter": [r.to_dict() for r in rebuy_state.reservedCash],
            }
        else:
            result_payload_extras = {}
    sellable = extract_sellable_positions(client.positions(account_no))
    orders, sellable_decisions = apply_sellable_quantity_cap(orders, sellable)
    pricing_decisions: list[dict[str, object]] = []
    if initial_orderbook_pricing:
        orders, pricing_decisions = price_orders_from_orderbook(client, orders, max_drift_percent=max_price_drift_percent)
    risk_violations = validate_live_orders(orders, live_risk_limits_from_env())
    if risk_violations:
        result_payload = {
            "status": "blocked_by_live_risk_limits",
            **generated,
            "submitResults": [],
            "submitComplete": False,
            "riskViolations": risk_violations,
            "corporateActionDecisions": [decision.__dict__ for decision in decisions],
            "sellableDecisions": sellable_decisions,
            "pricingDecisions": pricing_decisions,
            **result_payload_extras,
        }
        notify(risk_block_message(result_payload))
        return result_payload
    run_id = trade_date_value or "unknown"
    results, reconciliation, submit_complete = execute_orders_with_ledger(client, account_no, trading_token, orders, engine_config.dataDir, run_id)

    # Reconcile any rebuy orders that filled — drop them from the persistent
    # pending queue. Pending rebays whose BUY did NOT fill (rejected, expired,
    # still open) remain queued for the next cycle, with the reserve intact.
    from .trading_engine import mark_pending_rebuys_filled
    state_after_submit = CaRebuyState.load(engine_config.dataDir)
    filled_rebuys: dict[str, int] = {}
    for entry in results:
        order_payload = entry.get("order") if isinstance(entry, dict) else None
        if not order_payload:
            continue
        reason = str(order_payload.get("reason", ""))
        if not reason.startswith("corporate_action_rebuy"):
            continue
        status = str(entry.get("status", ""))
        if status == "submitted":
            sym = str(order_payload.get("symbol", ""))
            if sym:
                filled_rebuys[sym] = filled_rebuys.get(sym, 0) + 1
    dropped_filled = mark_pending_rebuys_filled(state_after_submit, filled_rebuys)
    state_after_submit.save(engine_config.dataDir)
    result_payload_extras = {
        **result_payload_extras,
        "pendingRebuysDroppedAfterFill": dropped_filled,
        "pendingRebuysAfter": [p.to_dict() for p in state_after_submit.pendingRebuys],
        "reservedCashAfter": [r.to_dict() for r in state_after_submit.reservedCash],
    }

    result_payload = {
        "status": "submitted" if submit_complete else "submit_unknown_or_pending",
        **generated,
        "submitResults": results,
        "submitComplete": submit_complete,
        "runId": run_id,
        "reconciliation": reconciliation,
        "riskViolations": [],
        "corporateActionDecisions": [decision.__dict__ for decision in decisions],
        "sellableDecisions": sellable_decisions,
        "pricingDecisions": pricing_decisions,
        "portfolio": portfolio_snapshot,
        **result_payload_extras,
    }
    # Failure detection: surface order rejections / unknown statuses to Telegram.
    order_fail = order_failures_message(
        results,
        trade_date=result_payload.get("tradeDate"),
    )
    if order_fail:
        notify(order_fail)
    # CA forced exits: clearly mark which symbols are exiting today.
    ca_exit = ca_forced_exit_message(
        result_payload.get("corporateActionDecisions", []) or [],
        trade_date=result_payload.get("tradeDate"),
    )
    if ca_exit:
        notify(ca_exit)
    # Portfolio snapshot: cash, holdings, P&L
    if portfolio_snapshot:
        notify(portfolio_snapshot_message(portfolio_snapshot))
    notify(submit_message(result_payload))
    return result_payload
