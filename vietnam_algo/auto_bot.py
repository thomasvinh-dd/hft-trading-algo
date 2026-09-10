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

    Cash-constrained BUYs are clipped to a board lot (default 100 —
       Vietnamese market) instead of partial fractional shares.

    The dry-run state is per-strategy (autofin_signal vs momentum) so
    that running two bots on the same VPS doesn't have them clobber
    each other's simulated cash and positions.
    """
    from .dry_run_state import (
        apply_orders_to_state,
        init_dry_run_state,
        save_dry_run_state,
    )
    state = init_dry_run_state(engine_config.dataDir, strategy_name=strategy_name)
    json_path = result.get("latestOrdersJson")
    if not json_path:
        return
    try:
        from pathlib import Path
        import json as _json
        payload = _json.loads(Path(json_path).read_text(encoding="utf-8"))
    except Exception:
        return
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return
    # SL/TP brackets are CONDITIONAL orders — they should NOT fill at
    # submission time. Treating them as immediate fills caused the bot
    # to instantly close every new entry at a loss (autofin emits
    # BUY + SL SELL as a pair). Only the AUTO entry kind (BUY/NS) is
    # applied at plan time; bracket arms are tracked separately by
    # the live order ledger.
    items = [
        o for o in items
        if (str(o.get("kind") or "")).lower() not in ("sl", "tp", "tp_bracket")
    ]
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
        from .config import StrategyConfig
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
        from .dry_run_state import (
            build_dry_run_snapshot,
            init_dry_run_state,
            mark_to_market,
        )
        state = init_dry_run_state(engine_config.dataDir, strategy_name=strategy_name)
        # Mark-to-market any open positions against the most recent close
        # we have locally — this gives a meaningful "live" P&L in the
        # summary even though we're not actually connected to a feed.
        try:
            from .universe import load_price_bars
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
    from .portfolio import build_portfolio_snapshot
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
            # The legacy 09:16 submit cycle is only for strategies that
            # go through run_daily_once → run_momentum_rotation /
            # run_hybrid_*. Strategies like autofin_signal generate orders
            # continuously via mid-day polls + EOD; their CSV is the
            # source of truth and they have no DNSE submit path here.
            # Skip the submit cycle entirely to avoid spamming
            # "Unsupported live strategy: autofin_signal" errors.
            from .daily_engine import strategy_runner
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
            print(f"[{now.isoformat()}] Sending start-of-day briefing")
            try:
                from .notifier import start_of_day_message
                portfolio_snap = _build_portfolio_snapshot(
                    client, account_no, engine_config, live_submit,
                )
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
            # Use the strategy adapter's generate_orders() when available.
            # This is the right path for strategies like autofin_signal
            # which don't go through run_daily_once → run_momentum_rotation.
            # Fall back to the legacy generate_live_orders() for momentum /
            # hybrid strategies.
            if strategy_adapter is not None:
                result = strategy_adapter.generate_orders(
                    engine_config, strategy_config, trade_date=today,
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
