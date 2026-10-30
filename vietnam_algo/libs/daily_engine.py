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
from .dnse.client import DnseClient
from .market_calendar import live_trading_dates
from .notifier import daily_generation_message, notify, submit_message, portfolio_snapshot_message
from .order_ledger import (
    LedgerOrder,
    T2_REJECTED_STATUS,
    TOKEN_REJECTED_STATUS,
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
from .dnse.otp import latest_email_otp
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
    # hitting DNSE / Vietstock API twice per EOD cycle.
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
    strategy_name: str = "",
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
    # Mirror the plan into Postgres so dashboards can query the
    # latest orders without parsing JSON files. Best-effort: a
    # Postgres outage must not break the JSON pipeline.
    _maybe_mirror_latest_orders_to_db(
        strategy=strategy_name or json_filename.split(".")[0],
        trade_date=selected_date or "",
        items=selected,
    )
    return csv_path, json_path, selected_date, len(selected)


def _maybe_mirror_latest_orders_to_db(
    *,
    strategy: str,
    trade_date: str,
    items: list[dict],
) -> None:
    import os

    if not strategy or not trade_date:
        return
    if os.environ.get("HFT_MIRROR_TO_DB", "1") == "0":
        return
    try:
        from .db import DbClient, DbError
        from .db_sync import mirror_latest_orders
    except ImportError as exc:  # pragma: no cover
        print(f"[export_orders_for_date] skipping Postgres mirror (no psycopg2): {exc}")
        return
    db = DbClient.from_default()
    result = mirror_latest_orders(
        db, strategy=strategy, trade_date=trade_date, items=items,
    )
    if result is None:
        return
    print(f"[export_orders_for_date] mirrored {result} orders to Postgres")


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

    paths = sync_prices_incremental(
        symbols,
        config.dataDir,
        sleep_seconds=config.priceSleepSeconds,
        incremental_limit=10,
    )
    # Mirror into Postgres if configured (env HFT_MIRROR_TO_DB=1, default on).
    _maybe_mirror_prices_to_db(symbols, config.dataDir, paths)


def _maybe_mirror_prices_to_db(
    symbols: Sequence[str],
    data_dir: Path,
    paths: Sequence[Path],
) -> None:
    """Mirror the freshly-written price JSON files into Postgres.

    Best-effort: a Postgres outage must not break the bot's JSON
    pipeline. ``mirror_*`` helpers swallow ``DbError`` and log.
    """
    import os

    if os.environ.get("HFT_MIRROR_TO_DB", "1") == "0":
        return
    try:
        from .db import DbClient, DbError
        from .db_sync import mirror_prices
        from .data import read_json
    except ImportError as exc:  # pragma: no cover — psycopg2 missing
        print(f"[update_dataset] skipping Postgres mirror (no psycopg2): {exc}")
        return

    db = DbClient.from_default()
    touched = 0
    for symbol, path in zip(symbols, paths):
        try:
            payload = read_json(path)
        except Exception as exc:
            print(f"[update_dataset] mirror read {symbol}: {exc}")
            continue
        bars = payload.get("items") if isinstance(payload, dict) else None
        if not bars:
            continue
        result = mirror_prices(db, symbol, bars)
        if result:
            touched += 1
    print(f"[update_dataset] mirrored {touched}/{len(symbols)} price files to Postgres")


def _run_strategy_backtest(
    config: EngineConfig,
    strategy_config: StrategyConfig,
    strategy_name: str,
) -> tuple[BacktestResult, Path, Path]:
    """Run the strategy's full backtest and persist its artefacts.

    Returns ``(result, result_path, orders_path)`` where:

      * ``result`` — the ``BacktestResult`` returned by
        ``strategy_runner(strategy)``. The caller can read
        ``result.metrics`` for the Telegram summary.
      * ``result_path`` — the saved backtest JSON file under
        ``data/backtests/`` (named via
        ``adapter.backtest_result_filename``).
      * ``orders_path`` — the full backtest orders CSV under
        ``data/backtests/`` (named via
        ``adapter.backtest_orders_filename``). The CSV is what
        ``export_orders_for_date`` filters when building the
        per-date latest-orders file.

    This is the slow path (~3 min for the momentum ~6-year
    backtest). The 09:16 submit cycle calls it; the EOD cycle
    skips it (see ``generate_live_orders(skip_backtest=True)``).
    """
    from vietnam_algo.algos.registry import get_strategy
    save_liquid_universe(build_liquid_universe(config.dataDir), config.dataDir)
    adapter = get_strategy(config.strategy) if strategy_name else None
    result = strategy_runner(config.strategy)(config.dataDir, "liquid_stocks", strategy_config)
    result_path = save_backtest_result(result, config.dataDir)
    orders_path = save_order_list(result, config.dataDir)
    return result, result_path, orders_path


def _build_orders_payload(
    config: EngineConfig,
    strategy_name: str,
    trade_date: str | None,
    orders_path: Path,
    result_path: Path | None,
    result: BacktestResult | None,
    selected_date: str | None,
    count: int,
    csv_path: Path,
    json_path: Path,
) -> dict[str, object]:
    """Assemble the result payload returned by ``generate_live_orders``.

    ``result`` / ``result_path`` may be ``None`` when the caller
    skipped the backtest (EOD reuses the cached artefacts from
    09:16). In that case ``metrics`` is empty and ``"result"`` is
    absent from the payload — both are only consumed in the EOD
    Telegram summary for non-dry-run mode, where the backtest
    numbers wouldn't be displayed anyway.
    """
    payload: dict[str, object] = {
        "orders": str(orders_path),
        "latestOrdersCsv": str(csv_path),
        "latestOrdersJson": str(json_path),
        "tradeDate": selected_date,
        "count": count,
        "strategy": config.strategy,
        "orders_file": str(csv_path),
    }
    if result is not None:
        payload["metrics"] = result.metrics
    if result_path is not None:
        payload["result"] = str(result_path)
    return payload


def generate_live_orders(
    config: EngineConfig,
    strategy_config: StrategyConfig,
    trade_date: str | None = None,
    strategy_name: str = "",
    notify_telegram: bool = False,
    *,
    skip_backtest: bool = False,
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

    ``skip_backtest=True`` reuses the cached orders CSV written by
    an earlier ``generate_live_orders`` call today (typically the
    09:16 submit cycle). The full backtest is skipped — only the
    cheap ``export_orders_for_date`` step runs. This saves the
    ~3-min the full ~6-year momentum backtest takes when the
    auto-trading-bot's EOD cycle runs at 15:15 ICT.

    The skip is best-effort: if the cached orders CSV is missing,
    falls back to running the full backtest (so a freshly-cleaned
    VPS still produces a plan).
    """
    from vietnam_algo.algos.registry import get_strategy
    adapter = get_strategy(config.strategy) if strategy_name else None

    csv_name = (adapter.orders_csv_filename if adapter else "latest_orders.csv")
    json_name = (adapter.orders_json_filename if adapter else "latest_orders.json")
    orders_filename = (
        adapter.backtest_orders_filename if adapter else "backtest_orders.csv"
    )
    cached_orders_path = config.dataDir / "backtests" / orders_filename

    result: BacktestResult | None = None
    result_path: Path | None = None
    orders_path: Path

    if skip_backtest and cached_orders_path.exists():
        # EOD path: reuse today's backtest artefacts. We don't
        # have the BacktestResult in memory (it lives in the saved
        # JSON), so ``result`` and ``result_path`` stay None.
        # The payload assembler omits them from the dict.
        orders_path = cached_orders_path
    else:
        # Submit cycle or no cached artefacts: run the heavy
        # backtest (or fall back to it if skip_backtest=True was
        # requested but the cache is cold).
        result, result_path, orders_path = _run_strategy_backtest(
            config, strategy_config, strategy_name,
        )

    csv_path, json_path, selected_date, count = export_orders_for_date(
        orders_path,
        config.dataDir / config.liveDirName,
        trade_date=trade_date,
        csv_filename=csv_name,
        json_filename=json_name,
        strategy_name=strategy_name,
    )
    result_payload = _build_orders_payload(
        config=config,
        strategy_name=strategy_name,
        trade_date=trade_date,
        orders_path=orders_path,
        result_path=result_path,
        result=result,
        selected_date=selected_date,
        count=count,
        csv_path=csv_path,
        json_path=json_path,
    )
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


def reconcile_ledger_orders(client: DnseClient, account_no: str, ledger: dict[str, LedgerOrder], trading_token: str | None = None) -> tuple[dict[str, LedgerOrder], list[dict[str, object]]]:
    """Refresh the live status of every non-terminal ledger row.

    2026-10-29 (ABB incident): the poll MUST use the order's own
    ``orderCategory``. A STOP order cannot be resolved with the
    default ``orderCategory=NORMAL`` — DNSE answers HTTP 404
    "Order not found" — and this function used to do exactly that.

    The consequence was severe and silent:

      1. Every STOP row was forced back to ``status="unknown"``.
      2. ``unknown`` is an AMBIGUOUS ledger status.
      3. ``unsafe_existing_orders`` blocks the WHOLE submit run when
         any row for that run_id is ambiguous.
      4. So the T+2 retry could not place the missing ABB protective
         stop — the two existing STOP orders (TCB, VOS) blocked it.

    That is exactly what happened: a live ABB position had no stop for
    16 days while the bot reported "T+2 due: 1, resubmitted: 0" every
    cycle with no error.

    The poll now passes the row's own category, mirroring the order
    monitor in ``bots/runner.py`` (which was already fixed for this on
    2026-10-28).
    """
    updates: list[dict[str, object]] = []
    now = utc_now()
    for key, item in list(ledger.items()):
        if not item.dnseOrderId or item.status not in {"submitted", "open", "partial", "unknown", "submitting"}:
            continue
        try:
            detail = client.order_detail(
                account_no, item.dnseOrderId, trading_token=trading_token,
                # A STOP order is only resolvable with orderCategory=STOP.
                order_category=(getattr(item, "orderCategory", "NORMAL") or "NORMAL").upper(),
            )
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
                orderCategory=getattr(item, "orderCategory", "NORMAL") or "NORMAL",
                stopPrice=getattr(item, "stopPrice", 0) or 0,
                strategy=getattr(item, "strategy", "") or "",
            )
            updates.append({"sourceOrderId": item.sourceOrderId, "symbol": item.symbol, "status": status, "dnseOrderId": item.dnseOrderId})
        except Exception as exc:
            # Keep the row's own category on the error path too — a
            # dropped orderCategory would make the NEXT poll fail the
            # same way.
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
    strategy: str = "",
) -> tuple[list[dict[str, object]], list[dict[str, object]], bool]:
    """Submit ``orders`` to DNSE, recording them in the shared ledger.

    ``strategy`` tags each new ledger row with the strategy that
    created it (2026-10-29). The autofin and momentum bots share one
    DNSE account and therefore one ledger file, so without the tag
    each bot's order monitor reports the OTHER bot's orders to its
    own Telegram chat.
    """
    from dataclasses import asdict

    ledger = load_ledger(data_dir)
    ledger, reconciliation = reconcile_ledger_orders(client, account_no, ledger, trading_token=trading_token)
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
            # 2026-10-13: pin the original price at first submit so
            # the order monitor's re-pricing logic has a stable cap
            # to compare against. Without this, a re-priced order
            # would compute its cap against its own (new) price and
            # chase the market forever.
            originalPrice=int(payload["price"]),
            # 2026-10-29: tag the owning strategy so each bot's
            # order monitor only reports its own orders.
            strategy=strategy,
            # 2026-10-29 (ABB incident): persist the DNSE conditional
            # (STOP) fields. Without them the ledger row says
            # ``orderCategory=NORMAL`` / ``stopPrice=0``, and the
            # order monitor then polls the order with the WRONG
            # category — DNSE answers HTTP 404 for a STOP order
            # requested as NORMAL — so a perfectly good protective
            # stop shows up as an error and its row is forced back to
            # the ambiguous "unknown" status, which blocks every
            # subsequent submit run.
            orderCategory=str(payload.get("orderCategory") or "NORMAL"),
            stopPrice=int(payload.get("stopPrice") or 0),
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
                # 2026-10-28: native DNSE conditional (STOP) order
                # fields. A protective stop-loss must be submitted as a
                # conditional order that rests at the broker, NOT as a
                # plain limit sell — a limit sell at the stop level is
                # marketable whenever the market is above it (the
                # 2026-10-28 ORS incident: an LO @ 14,400 against a
                # 15,150 bid filled instantly, liquidating 200
                # shares instead of protecting them).
                #
                # Only passed when the order actually IS a stop; a
                # normal order leaves the defaults in place.
                order_category=str(
                    payload.get("orderCategory") or "NORMAL"
                ).upper(),
                stop_price=payload.get("stopPrice") or None,
                condition_operator=payload.get("conditionOperator") or None,
                duration_type=payload.get("durationType") or None,
                duration_date_time=payload.get("durationDateTime") or None,
            )
            order_id = dnse_order_id(response)
            detail = client.order_detail(
                account_no, order_id, trading_token=trading_token,
                # A STOP order must be resolved with its own category.
                order_category=str(
                    payload.get("orderCategory") or "NORMAL"
                ).upper(),
            ) if order_id else response
            status = normalize_ledger_status(order_status_from_detail(detail)) if order_id else "unknown"
            ledger[key] = LedgerOrder(**{**ledger[key].__dict__, "status": status, "dnseOrderId": order_id, "updatedAt": utc_now(), "lastDetail": detail})
            results.append({"status": "submitted", "order": payload, "response": response, "detail": detail})
        except Exception as exc:
            # 2026-10-13: distinguish a permanent 4xx rejection from a
            # transient 5xx / network error. A 4xx means DNSE will
            # keep rejecting the same payload forever (e.g. a SELL for
            # shares we don't have, a bad symbol, an invalid price).
            # Marking those as "unknown" left them in the unsafe
            # ledger set and blocked all subsequent submits via
            # ``unsafe_existing_orders``. Reclassify as "rejected"
            # (terminal) so they don't gate future cycles.
            err_text = str(exc)
            # 2026-10-25: an INVALID_TRADING_TOKEN rejection is NOT
            # permanent — it means the token we used was expired or
            # rejected by DNSE, and the very SAME payload succeeds
            # with a fresh token. Classifying it as "rejected"
            # (terminal) permanently discards the order: the
            # position is never entered and the reason is invisible
            # afterwards.
            #
            # This is exactly what happened to the KDC BUY on
            # 2026-10-22 (``HTTP 400 INVALID_TRADING_TOKEN``): the
            # order was written to the ledger as "rejected", so the
            # ledger's dedup key
            # (run_id, sourceOrderId, symbol, side) matched on every
            # later cycle and the order was never retried — the
            # entry simply vanished.
            #
            # It must not be "unknown" either: that status is in
            # UNSAFE_LEDGER_STATUSES, and ``unsafe_existing_orders``
            # short-circuits the WHOLE submit run — one bad token
            # would block every other order for that run.
            #
            # Hence the dedicated ``token_rejected`` status: not
            # terminal (so the order can be retried once the token
            # cron refreshes it) and not unsafe (so it doesn't block
            # other orders).
            is_token_rejection = "INVALID_TRADING_TOKEN" in err_text
            # 2026-10-31 (the VOS T+2.5 case): a SELL for shares that
            # have not settled yet is rejected with HTTP 400
            # ``TRADE_QUANTITY_NOT_ENOUGH``. That is NOT permanent —
            # the very same payload succeeds once the T+2.5
            # settlement clears (typically a few hours later on the
            # same day, or the next trading day after a weekend).
            #
            # Classifying it as a plain "rejected" (terminal) made the
            # order permanently un-retryable: the ledger's dedup key
            # matches on any terminal status, so the VOS trim would
            # have been silently lost after one too-early submit.
            #
            # It gets its own status: not terminal (re-attemptable
            # after settlement), not unsafe (it must not block the
            # other orders in the run).
            is_t2_rejection = "TRADE_QUANTITY_NOT_ENOUGH" in err_text
            is_permanent_rejection = (
                "HTTP 400" in err_text
                or "HTTP 401" in err_text
                or "HTTP 403" in err_text
                or "HTTP 404" in err_text
                or "HTTP 422" in err_text
            ) and not is_token_rejection and not is_t2_rejection
            if is_token_rejection:
                terminal_status = TOKEN_REJECTED_STATUS
            elif is_t2_rejection:
                terminal_status = T2_REJECTED_STATUS
            else:
                terminal_status = "rejected" if is_permanent_rejection else "unknown"
            ledger[key] = LedgerOrder(**{
                **ledger[key].__dict__,
                "status": terminal_status,
                "updatedAt": utc_now(),
                "error": err_text,
            })
            results.append({"status": terminal_status, "order": payload, "error": err_text})
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
    results, reconciliation, submit_complete = execute_orders_with_ledger(
        client, account_no, trading_token, orders, engine_config.dataDir, run_id,
        # 2026-10-29: tag the owning strategy so each bot's order
        # monitor only reports its own orders (both bots share one
        # ledger and one DNSE account).
        strategy=engine_config.strategy or "",
    )

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
