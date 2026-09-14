from __future__ import annotations

import time
import os
import urllib.request
import csv as _csv
import json as _json
from pathlib import Path
from datetime import datetime

from vietnam_algo.algos.registry import STRATEGY_REGISTRY, get_strategy
from vietnam_algo.libs.bot_state import AutoBotState, load_state, save_state
from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.corporate_actions import sync_corporate_actions_incremental
from vietnam_algo.libs.daily_engine import (
    EngineConfig,
    generate_live_orders,
    run_daily_once,
    update_dataset,
)
from vietnam_algo.libs.dnse.client import DnseClient
from vietnam_algo.libs.dnse.auth import (
    SHARED_TOKEN_PATH,
    ensure_fresh_trading_token,
    load_shared_trading_token,
)
from vietnam_algo.libs.market_calendar import (
    load_market_holidays,
    should_sync_market_holidays,
    sync_market_holidays,
)
from vietnam_algo.libs.notifier import (
    notify,
    token_refresh_message,
    eod_summary_message,
    heartbeat_message,
)
from vietnam_algo.libs.schedule import (
    TradingSchedule,
    local_date,
    should_poll_strategy,
    should_run_eod,
    should_run_start_of_day,
    should_submit_orders,
    should_sync_corporate_actions,
    should_send_heartbeat,
    vietnam_now,
)


def _build_db_for_bot() -> Any:
    """Open a single DbClient for the bot's lifetime.

    The pool is created lazily on first write; we never connect at
    startup so a Postgres outage doesn't block the bot from
    starting. ``None`` is returned when the bot has been told to
    skip mirroring via ``HFT_MIRROR_TO_DB=0``.
    """
    import os

    if os.environ.get("HFT_MIRROR_TO_DB", "1") == "0":
        return None
    try:
        from vietnam_algo.libs.db import DbClient
    except ImportError:
        return None
    return DbClient.from_default()


def _emit_cycle(
    db: Any,
    strategy: str,
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
    message: str | None = None,
    local_date: str | None = None,
    occurred_at: Any | None = None,
) -> None:
    """Write a ``cycle_events`` row in Postgres (best-effort)."""
    if db is None:
        return
    try:
        from vietnam_algo.libs.db_sync import emit_cycle_event
        emit_cycle_event(
            db,
            strategy=strategy,
            event_type=event_type,
            payload=payload,
            message=message,
            local_date=local_date,
            occurred_at=occurred_at,
        )
    except Exception as exc:  # pragma: no cover — defensive
        print(f"[cycle_events] emit '{event_type}' failed: {exc}")


def _mirror_daily_report(
    db: Any,
    *,
    strategy: str,
    source: str,
    snapshot: dict[str, Any],
    raw_message: str | None,
    local_date: str,
) -> None:
    """Write a ``daily_reports`` row in Postgres (best-effort)."""
    if db is None:
        return
    try:
        from vietnam_algo.libs.db_sync import mirror_daily_report
        payload = dict(snapshot or {})
        payload.setdefault("report_date", local_date)
        payload["raw_message"] = raw_message
        mirror_daily_report(
            db, strategy=strategy, source=source,
            report=payload, local_date=local_date,
        )
    except Exception as exc:  # pragma: no cover
        print(f"[daily_reports] write '{source}' failed: {exc}")


def _hydrate_shared_token_into_state(state, data_dir) -> bool:
    """Copy the latest DNSE token from the shared file into the
    bot's in-memory state if it's fresh enough.

    Returns True when the token was hydrated (and ``state.tradingToken``
    is now usable), False otherwise (caller falls back to lazy refresh).
    """
    shared = load_shared_trading_token(data_dir)
    if not shared:
        return False
    from vietnam_algo.libs.dnse.auth import is_token_fresh
    if not is_token_fresh(
        shared["token"],
        issued_at=shared.get("issued_at"),
    ):
        return False
    state.tradingToken = shared["token"]
    if shared.get("expires_at"):
        state.tokenExpiry = shared["expires_at"]
    if hasattr(state, "tokenDate") and shared.get("date"):
        state.tokenDate = shared["date"]
    return True


def _apply_dry_run_fills(
    result: dict,
    engine_config: "EngineConfig",
    account_no: str,
    strategy_config: "StrategyConfig | None" = None,
    strategy_name: str = "",
) -> None:
    """Apply today's ``latest_orders.json`` orders to the dry-run state.

    In dry-run mode the bot doesn't actually submit orders, but it still
    generates a daily plan. We replay the plan as simulated fills against
    the local ``DryRunState`` so subsequent cycles see realistic cash,
    positions, and realized P&L — without ever calling DNSE.

    Two layers of dedup protect against double-applies:

    1. Per-trade-date marker (``_applied:{trade_date}``) so a bot restart
       mid-cycle doesn't re-apply today's plan.
    2. Per-order dedup (date+symbol+side+qty+price) inside
       ``apply_orders_to_state`` so individual orders from the backtest's
       repeated trade history don't accumulate position qty across
       multiple EOD cycles.

    For backtest-driven strategies (momentum), the per-order dedup also
    lets the state self-heal: if the state file was wiped or the bot
    crashed mid-replay, the next EOD cycle replay-walks the FULL backtest
    orders CSV (1442 trades for momentum) and re-applies any trades that
    haven't been seen yet. The autofin adapter is signal-driven (no
    historical replay), so it only consumes the items in the latest
    orders file.

    Cash-constrained BUYs are clipped to a board lot (default 100 —
       Vietnamese market) instead of partial fractional shares.

    The dry-run state is per-strategy (autofin_signal vs momentum) so
    that running two bots on the same VPS doesn't have them clobber
    each other's simulated cash and positions.
    """
    from vietnam_algo.libs.dry_run_state import (
        apply_orders_to_state,
        init_dry_run_state,
        save_dry_run_state,
    )
    state = init_dry_run_state(engine_config.dataDir, strategy_name=strategy_name)
    # Migration: if there's an unsuffixed dry_run_state.json from an
    # older build (before per-strategy isolation) AND the per-strategy
    # state file is fresh, copy the unsuffixed state into the
    # per-strategy file so the bot doesn't lose its portfolio on the
    # first cycle after the upgrade.
    if not state.positions and not state.closed_trades and strategy_name:
        legacy_path = engine_config.dataDir / "live" / "dry_run_state.json"
        if legacy_path.exists():
            try:
                legacy_payload = _json.loads(legacy_path.read_text(encoding="utf-8"))
                if legacy_payload.get("positions") or legacy_payload.get("closed_trades"):
                    print(
                        f"[dry-run] migrating legacy dry_run_state.json → "
                        f"dry_run_state.{strategy_name}.json"
                    )
                    state.initial_nav = float(legacy_payload.get("initial_nav") or state.initial_nav)
                    state.cash = float(legacy_payload.get("cash") or state.cash)
                    state.positions = {
                        sym: DryRunPosition(
                            symbol=p.get("symbol", sym),
                            quantity=int(p.get("quantity", 0)),
                            avg_price=float(p.get("avg_price", 0.0)),
                            last_price=float(p.get("last_price", 0.0)),
                            cost_basis=float(p.get("cost_basis", 0.0)),
                        )
                        for sym, p in (legacy_payload.get("positions") or {}).items()
                        if isinstance(p, dict)
                    }
                    state.closed_trades = list(legacy_payload.get("closed_trades") or [])
                    state.realized_pnl = float(legacy_payload.get("realized_pnl") or 0.0)
                    save_dry_run_state(
                        state, engine_config.dataDir, strategy_name=strategy_name,
                    )
            except Exception as exc:
                print(f"[dry-run] legacy-state migration failed: {exc}")
    # The momentum adapter exposes the full backtest orders CSV in
    # ``result["orders"]``. Walking it ensures the state reflects ALL
    # history, not just today's filtered slice. Autofin leaves
    # ``result["orders"]`` pointing at the latest-orders.csv which is
    # already the right file for a signal-driven strategy.
    #
    # The dry-run respects the user-configured DRY_RUN_INITIAL_NAV /
    # MOCK_NAV env var. To match the backtest's % return profile at a
    # smaller cash budget, we SCALE each order's quantity by
    # ``state.initial_nav / backtest_initial_cash``. Without this,
    # applying 1B-VND-scale trades to a 100M-VND dry-run would clip
    # most BUYs as insufficient_cash / below_board_lot, producing
    # nonsensical realized losses (-80% in a single day, etc.).
    #
    # Scaling quantities preserves the trade direction (BUY/SELL) and
    # prices, so the simulated P&L tracks the backtest's % return at
    # the dry-run's smaller scale.
    full_orders_csv = result.get("orders")
    payload: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    if full_orders_csv and Path(str(full_orders_csv)).exists():
        try:
            with open(full_orders_csv, "r", encoding="utf-8", newline="") as f:
                items = list(_csv.DictReader(f))
        except Exception as exc:
            print(f"[dry-run] failed to read full orders csv {full_orders_csv}: {exc}")
            items = []
    if not items:
        # Fallback: use the per-date filtered JSON if the full CSV is
        # missing or unreadable.
        json_path = result.get("latestOrdersJson")
        if not json_path:
            return
        try:
            payload = _json.loads(Path(json_path).read_text(encoding="utf-8"))
            items = list(payload.get("items") or [])
        except Exception:
            return
    if not isinstance(items, list) or not items:
        return
    # Scale order quantities to match the dry-run's initial NAV.
    # Pull the backtest's initial cash from the saved backtest result
    # (if available) so we know the source scale.
    qty_scale: float = 1.0
    try:
        result_path = result.get("result") if isinstance(result, dict) else None
        if result_path:
            from vietnam_algo.libs.backtest import _initial_cash_from_result
            backtest_initial_cash = _initial_cash_from_result(Path(str(result_path)))
            if (
                backtest_initial_cash
                and backtest_initial_cash > 0
                and state.initial_nav > 0
                and backtest_initial_cash > state.initial_nav * 1.5
            ):
                qty_scale = float(state.initial_nav) / float(backtest_initial_cash)
                print(
                    f"[dry-run] scaling order quantities by {qty_scale:.4f} "
                    f"(dry-run NAV={state.initial_nav:,.0f} vs "
                    f"backtest initial={backtest_initial_cash:,.0f})"
                )
    except Exception as exc:
        print(f"[dry-run] qty-scale detection failed: {exc}")
    if qty_scale < 1.0:
        for o in items:
            raw_qty = o.get("quantity")
            try:
                q = int(float(raw_qty)) if raw_qty not in (None, "") else 0
            except (TypeError, ValueError):
                q = 0
            if q <= 0:
                continue
            o["quantity"] = max(1, int(q * qty_scale))
    # SL/TP brackets are CONDITIONAL orders — they should NOT fill at
    # submission time. Treating them as immediate fills caused the bot
    # to instantly close every new entry at a loss (autofin emits
    # BUY + SL SELL as a pair). Only the entry kind (BUY/NS) is
    # applied at plan time; bracket arms are tracked separately by
    # the live order ledger.
    #
    # Filter rules:
    #   * If the order has ``kind`` set to one of the bracket kinds,
    #     skip.
    #   * Otherwise (some adapters don't populate ``kind``), check
    #     the ``reason`` prefix: ``autofin_sl:``, ``autofin_user_sl:``,
    #     ``autofin_tp:`` are bracket arms. The momentum adapter writes
    #     ``kind="entry"|"sl"|"tp"|...`` so the first check handles it.
    bracket_reasons = ("autofin_sl:", "autofin_user_sl:", "autofin_tp:")
    bracket_kinds = ("sl", "tp", "tp_bracket")
    filtered: list[dict[str, Any]] = []
    skipped_brackets = 0
    for o in items:
        kind = str(o.get("kind") or "").lower()
        reason = str(o.get("reason") or "")
        if kind in bracket_kinds:
            skipped_brackets += 1
            continue
        if any(reason.startswith(p) for p in bracket_reasons):
            skipped_brackets += 1
            continue
        filtered.append(o)
    items = filtered
    if skipped_brackets:
        print(
            f"[dry-run] skipped {skipped_brackets} bracket arm(s) "
            f"(SL/TP — conditional, not filled at plan time)"
        )
    if not items:
        return
    trade_date = (
        payload.get("tradeDate")
        or result.get("tradeDate")
        or ""
    )
    marker_key = f"_applied:{trade_date}"
    # Dedup: skip if we've already applied orders for this tradeDate.
    if any(
        isinstance(t, dict) and t.get("note") == marker_key
        for t in state.closed_trades[-100:]
    ):
        return
    # Use the same board lot and fee the strategy uses for real orders
    # so dry-run cash accounting matches what live mode would do.
    if strategy_config is not None:
        board_lot = int(getattr(strategy_config, "boardLotSize", 0) or 100) or 100
        fee_rate = float(getattr(strategy_config, "feeRate", 0) or 0)
    else:
        from vietnam_algo.libs.config import StrategyConfig
        default_cfg = StrategyConfig()
        board_lot = int(getattr(default_cfg, "boardLotSize", 100) or 100)
        fee_rate = float(getattr(default_cfg, "feeRate", 0.0) or 0)
    fees_bps = fee_rate * 10_000
    fills = apply_orders_to_state(
        state, items, board_lot=board_lot, fees_bps=fees_bps,
    )
    applied = [f for f in fills if f.get("status") == "applied"]
    clipped = [f for f in fills if f.get("status") == "clipped"]
    skipped = [f for f in fills if f.get("status") == "skipped"]
    if applied or clipped or skipped:
        # Stamp each clipped entry with the requested_qty it came from
        # so the notifier can match it back to the original order row
        # in latest_orders.json (which still has the full requested qty).
        clipped_for_log = []
        for f in clipped:
            entry = dict(f)
            if "requested_qty" not in entry and "qty" in entry:
                entry["requested_qty"] = entry["qty"]
            clipped_for_log.append(entry)
        state.closed_trades.append({
            "note": marker_key,
            "applied_count": len(applied),
            "clipped_count": len(clipped),
            "skipped_count": len(skipped),
            "skipped": skipped,
            "clipped": clipped_for_log,
            "fill_count": len(fills),
        })
    save_dry_run_state(state, engine_config.dataDir, strategy_name=strategy_name)


def _build_portfolio_snapshot(
    client: "DnseClient | None",
    account_no: str,
    engine_config: "EngineConfig",
    live_submit: bool,
    strategy_name: str = "",
) -> dict:
    """Build a portfolio snapshot.

    In live mode (``live_submit=True``) this queries DNSE for the real
    account state. In dry-run mode (``live_submit=False``) we DO NOT
    touch DNSE — instead we read the locally-simulated ``DryRunState``
    that the bot has been updating each cycle. The dry-run state has
    the same shape as ``build_portfolio_snapshot``'s output so the
    EOD/start-of-day formatters work transparently for both modes.

    The dry-run state is per-strategy (``dry_run_state.<strategy>.json``)
    so autofin and momentum don't clobber each other's simulated
    cash and positions when both bots are running on the same VPS.

    The dry-run state is initialized from ``DRY_RUN_INITIAL_NAV`` (or
    the legacy ``MOCK_NAV``) env var, defaulting to 100,000,000 VND.
    """
    if not live_submit:
        from vietnam_algo.libs.dry_run_state import (
            build_dry_run_snapshot,
            init_dry_run_state,
            mark_to_market,
        )
        state = init_dry_run_state(engine_config.dataDir, strategy_name=strategy_name)
        # Mark-to-market any open positions against the most recent close
        # we have locally — this gives a meaningful "live" P&L in the
        # summary even though we're not actually connected to a feed.
        try:
            from vietnam_algo.libs.universe import load_price_bars
            prices: dict[str, float] = {}
            for sym in list(state.positions.keys()):
                bars = load_price_bars(sym, engine_config.dataDir)
                if bars:
                    last_close = bars[-1].close if hasattr(bars[-1], "close") else 0
                    if last_close > 0:
                        prices[sym] = float(last_close)
            mark_to_market(state, prices)
        except Exception:
            pass
        return build_dry_run_snapshot(state, account_no=account_no)

    # Live mode — query DNSE.
    from vietnam_algo.libs.portfolio import build_portfolio_snapshot
    return build_portfolio_snapshot(client, account_no)


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

    Side effects
    ------------

    Every cycle emits a ``cycle_events`` row to Postgres (best-effort)
    so dashboards / audit queries can answer "what did the bot do
    today?". EOD and start-of-day also write a structured
    ``daily_reports`` row.
    """
    strategy_adapter = get_strategy(engine_config.strategy) if engine_config.strategy else None
    bot_start_time = vietnam_now()
    _db = _build_db_for_bot()
    _emit_cycle(_db, strategy_name or engine_config.strategy or "?",
                "bot_started",
                payload={"live_submit": live_submit,
                         "strategy": engine_config.strategy,
                         "loan_package_id": loan_package_id},
                local_date=local_date(bot_start_time),
                occurred_at=bot_start_time)
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
            # The legacy 09:16 submit cycle is only for strategies that
            # go through run_daily_once → run_momentum_rotation /
            # run_hybrid_*. Strategies like autofin_signal generate orders
            # continuously via mid-day polls + EOD; their CSV is the
            # source of truth and they have no DNSE submit path here.
            # Skip the submit cycle entirely to avoid spamming
            # "Unsupported live strategy: autofin_signal" errors.
            from vietnam_algo.libs.daily_engine import strategy_runner
            try:
                strategy_runner(engine_config.strategy)
            except ValueError:
                print(
                    f"[{now.isoformat()}] Skipping 09:16 submit cycle for "
                    f"strategy={engine_config.strategy} (mid-day/EOD strategy)"
                )
                state.orderSubmitDate = today
                save_state(state, engine_config.dataDir, strategy_name=strategy_name)
            else:
                print(f"[{now.isoformat()}] Generating and submitting latest orders")
                notify(f"Vietnam Algo: submit cycle started\ntime: {now.isoformat()}\nliveSubmit: {live_submit}")
                # Prefer the shared token file written by
                # ``vietnam_algo.jobs.get_otp`` (cron @ 08:30 ICT).
                # The hybrid lazy-refresh below only fires when the
                # shared file is missing or stale, which is the
                # documented degraded mode for a single-bot deployment
                # that hasn't adopted the cron job yet.
                if not live_submit:
                    _hydrate_shared_token_into_state(
                        state, engine_config.dataDir,
                    )
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
                        trade_date=today,
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
                    print(f"[{now.isoformat()}] submit cycle failed: {exc}")
                    if once:
                        raise
                    continue
                print(result)
                _emit_cycle(
                    _db, strategy_name or engine_config.strategy or "?",
                    "submit",
                    payload={"date": today, "result_status": result.get("status")},
                    local_date=today,
                )
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

        # Skip CA sync (and price sync in the EOD path below) when this
        # bot doesn't own data sync. Lets one bot do the heavy API
        # fetching while the other just reads from disk.
        if engine_config.syncData and should_sync_corporate_actions(now, state.corporateActionDate, schedule, holidays):
            print(f"[{now.isoformat()}] Syncing Vietstock corporate actions")
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
                # CA sync failed — downgrade from "FAILED" alert to a
                # low-noise log+optional warning. The bot uses CA data
                # only for forced exits on CA events; in dry-run mode
                # the impact is zero, and even in live mode the existing
                # CA cache (last successful sync) is still usable. Per
                # user feedback (2026-09-09): the per-attempt Telegram
                # alert was noisy; consolidate to ONE warning per day.
                err_short = str(exc)[:200]
                print(f"[{now.isoformat()}] CA sync failed: {err_short}")
                # First-time failure today → Telegram warning.
                if getattr(state, "caAlertSentDate", None) != today:
                    notify(
                        "\n".join(
                            [
                                "⚠️ Vietnam Algo: corporate-action sync FAILED (will retry tomorrow)",
                                f"err: {err_short[:200]}",
                                "continuing without updated CA data; using cached file",
                            ]
                        )
                    )
                    state.caAlertSentDate = today
                state.corporateActionDate = today  # don't retry on same day
                save_state(state, engine_config.dataDir, strategy_name=strategy_name)
                continue
            print(result)
            state.corporateActionDate = today
            state.caAlertSentDate = ""
            save_state(state, engine_config.dataDir, strategy_name=strategy_name)
            # Only emit the success message if we actually fetched new
            # events — avoids noisy "synced 0 events" messages.
            if result.get("fetched", 0) > 0:
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

        if should_run_start_of_day(
            now,
            state.startOfDayDate,
            schedule=schedule,
            holidays=holidays,
        ):
            # Same data-ready gate as EOD: don't show a briefing that
            # uses yesterday's prices just because the data-sync owner
            # hasn't run yet today. We re-use the data_ready_for_eod
            # sentinel that's stamped after the owner's sync.
            if not engine_config.syncData:
                try:
                    _ready = engine_config.dataDir / engine_config.liveDirName / "data_ready_for_eod"
                    if not _ready.exists() or (
                        _ready.exists()
                        and (
                            __import__("datetime").datetime
                            .now(__import__("datetime").timezone.utc)
                            .timestamp()
                            - _ready.stat().st_mtime
                        )
                        / 60
                        > 60 * 18
                    ):
                        print(
                            f"[{now.isoformat()}] waiting for data-sync owner "
                            f"to refresh prices before start-of-day briefing"
                        )
                        continue
                except Exception as exc:
                    print(f"[{now.isoformat()}] data-ready probe failed: {exc}")
            print(f"[{now.isoformat()}] Sending start-of-day briefing")
            try:
                from vietnam_algo.libs.notifier import start_of_day_message
                # For backtest-driven strategies (momentum), the dry-run
                # apply (rescale + walk full backtest CSV) only runs in
                # _apply_dry_run_fills via the EOD path. If the start-
                # of-day briefing fires before the EOD apply, the
                # snapshot would show a fresh state (cash=100M, no
                # positions) even though the EOD will rescale to 1B
                # and apply all 1442 trades. Run the same apply here
                # so the briefing reflects the post-rescale state.
                if not live_submit:
                    try:
                        from vietnam_algo.algos.base import StrategyAdapter
                        _adapter = STRATEGY_REGISTRY.get(engine_config.strategy) if engine_config.strategy else None
                        _json_name = (_adapter.orders_json_filename if _adapter else "latest_orders.json")
                        _json_path = (
                            engine_config.dataDir / engine_config.liveDirName / _json_name
                        )
                        if _json_path.exists():
                            _payload = _json.loads(_json_path.read_text(encoding="utf-8"))
                            _items = list(_payload.get("items") or [])
                            if _items:
                                _fake_result = {
                                    "orders": str(
                                        engine_config.dataDir
                                        / "backtests"
                                        / "liquid_momentum_rotation_orders.csv"
                                    ),
                                    "latestOrdersJson": str(_json_path),
                                    "latestOrdersCsv": str(_json_path).replace(".json", ".csv"),
                                    "result": str(
                                        engine_config.dataDir
                                        / "backtests"
                                        / "liquid_momentum_rotation.json"
                                    ),
                                    "tradeDate": today,
                                    "count": len(_items),
                                }
                                _apply_dry_run_fills(
                                    _fake_result, engine_config, account_no,
                                    strategy_config, strategy_name=strategy_name,
                                )
                    except Exception as _exc:
                        print(f"[{now.isoformat()}] start-of-day dry-run apply failed: {_exc}")
                portfolio_snap = _build_portfolio_snapshot(
                    client, account_no, engine_config, live_submit,
                    strategy_name=strategy_name,
                )
                # read latest_orders JSON for the per-strategy file
                from vietnam_algo.algos.base import StrategyAdapter
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
                _emit_cycle(
                    _db, strategy_name or engine_config.strategy or "?",
                    "start_of_day",
                    payload={"date": today, "items": len(_items)},
                    message=msg[:500],
                    local_date=today,
                )
                if isinstance(portfolio_snap, dict):
                    _mirror_daily_report(
                        _db,
                        strategy=strategy_name or engine_config.strategy or "?",
                        source="start_of_day",
                        snapshot=portfolio_snap,
                        raw_message=msg,
                        local_date=today,
                    )
            except Exception as exc:
                _emit_cycle(
                    _db, strategy_name or engine_config.strategy or "?",
                    "start_of_day_error",
                    payload={"date": today, "err": str(exc)[:200]},
                    local_date=today,
                )
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
            # When this bot doesn't own data sync, wait for the data-
            # sync owner (autofin) to finish refreshing prices + CA
            # before we proceed. We poll for a sentinel file
            # ``data_ready_for_eod`` written by the owner; if it's
            # absent or stale, skip this cycle and try again next.
            data_ready = True
            if not engine_config.syncData:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    def _utc_now() -> _dt:
                        return _dt.now(tz=_tz.utc)
                    _ready = engine_config.dataDir / engine_config.liveDirName / "data_ready_for_eod"
                    if not _ready.exists():
                        print(
                            f"[{now.isoformat()}] waiting for data-sync owner to "
                            f"refresh prices/CA before EOD..."
                        )
                        data_ready = False
                    else:
                        age_min = (_utc_now().timestamp() - _ready.stat().st_mtime) / 60
                        if age_min > 60:
                            print(
                                f"[{now.isoformat()}] data_ready_for_eod is "
                                f"{age_min:.0f} min old — owner may be stuck"
                            )
                except Exception as exc:
                    print(f"[{now.isoformat()}] data-ready probe failed: {exc}")
                if not data_ready:
                    continue  # try again next cycle
            print(f"[{now.isoformat()}] Running EOD data update and order generation")
            notify(f"Vietnam Algo: EOD update started\ntime: {now.isoformat()}\nstrategy: {engine_config.strategy}")
            # Price sync (data sync owner only) — skip when --no-sync-data.
            # The EOD order-generation / portfolio snapshot steps still
            # run regardless; they just read prices from disk.
            if engine_config.syncData:
                update_dataset(engine_config)
                # Stamp the data-ready sentinel so peer bots (e.g.
                # momentum reading from disk) can resume their EOD.
                try:
                    from datetime import datetime as _dt2, timezone as _tz2
                    _stamp = engine_config.dataDir / engine_config.liveDirName / "data_ready_for_eod"
                    _stamp.parent.mkdir(parents=True, exist_ok=True)
                    _stamp.write_text(_dt2.now(tz=_tz2).isoformat())
                except Exception:
                    pass
            # Use the strategy adapter's generate_orders() when available.
            # This is the right path for strategies like autofin_signal
            # which don't go through run_daily_once → run_momentum_rotation.
            # Fall back to the legacy generate_live_orders() for momentum /
            # hybrid strategies.
            #
            # For backtest-style strategies (momentum), the EOD should
            # always pick the latest trades in the backtest CSV (or
            # ``today`` if those happen to align). Forcing
            # ``trade_date=today`` produces an empty plan when the
            # backtest hasn't been refreshed to the calendar's current
            # trading day — see the 2026-09-11 incident where the
            # latest_orders file ended up empty and the briefing then
            # read a stale Sept 7 fallback. Letting adapters decide
            # (most pass None → falls back to the latest CSV row) is
            # safer for dry-run/replay bots.
            if strategy_adapter is not None:
                result = strategy_adapter.generate_orders(
                    engine_config, strategy_config,
                )
            else:
                result = generate_live_orders(
                    engine_config, strategy_config, strategy_name=strategy_name,
                    notify_telegram=False,
                )
            print(result)
            # In dry-run mode, REPLAY today's planned orders against the
            # simulated state BEFORE building the snapshot, so the EOD
            # summary shows cash + positions post-fill (matching what the
            # bot would have entered if it were live).
            if not live_submit and isinstance(result, dict):
                try:
                    _apply_dry_run_fills(
                        result, engine_config, account_no, strategy_config,
                        strategy_name=strategy_name,
                    )
                except Exception as exc:
                    print(f"[{now.isoformat()}] dry-run fill replay failed: {exc}")
            # Fetch live portfolio snapshot once so the EOD summary can
            # show cash, NAV, and per-position P&L inline.
            # In dry-run mode, use the local simulated state instead of DNSE.
            portfolio_snap: dict | None = None
            try:
                portfolio_snap = _build_portfolio_snapshot(
                    client, account_no, engine_config, live_submit,
                    strategy_name=strategy_name,
                )
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
                    eod_msg = strategy_adapter.format_cycle_summary(result, portfolio=portfolio_snap)
                else:
                    eod_msg = eod_summary_message(result, portfolio=portfolio_snap)
                notify(eod_msg)
                _emit_cycle(
                    _db, strategy_name or engine_config.strategy or "?",
                    "eod",
                    payload={"date": today, "count": result.get("count", 0)},
                    message=eod_msg[:500],
                    local_date=today,
                )
                if isinstance(portfolio_snap, dict):
                    _mirror_daily_report(
                        _db,
                        strategy=strategy_name or engine_config.strategy or "?",
                        source="eod",
                        snapshot=portfolio_snap,
                        raw_message=eod_msg,
                        local_date=today,
                    )
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
            _emit_cycle(
                _db, strategy_name or engine_config.strategy or "?",
                "heartbeat",
                payload={"date": today, "proxy_check": proxy_check,
                         "uptime_s": uptime_s, "pid": pid},
                local_date=today,
            )
            state.lastHeartbeatDate = today
            save_state(state, engine_config.dataDir, strategy_name=strategy_name)

        if once:
            return
        time.sleep(sleep_seconds)
