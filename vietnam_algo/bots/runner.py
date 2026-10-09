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
from vietnam_algo.libs.data import get_market_data_freshness
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
from vietnam_algo.libs.order_ledger import (
    LedgerOrder,
    TERMINAL_LEDGER_STATUSES,
    load_ledger,
    save_ledger,
    normalize_ledger_status,
    order_status_from_detail,
)
from vietnam_algo.libs.schedule import (
    TradingSchedule,
    TradingSession,
    current_session,
    local_date,
    order_type_for_session,
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


def _compute_dry_run_qty_scale(
    backtest_result_path: "Path | str | None",
    state: "DryRunState",
) -> float:
    """Return the quantity scale factor for a dry-run apply.

    For strategies whose full backtest runs at 1B VND (``initialCash``
    on the backtest side) but whose dry-run state starts at 100M VND
    (the user's chosen ``DRY_RUN_INITIAL_NAV``), the planned orders
    are sized for the larger scale and would mostly be clipped as
    ``insufficient_cash`` / ``below_board_lot`` on apply. The fix is
    to scale every BUY/SELL qty by
    ``state.initial_nav / backtest_initial_cash`` before applying.

    The function reads ``equityCurve[0].cash`` from the saved
    backtest JSON to learn the source scale. Returns 1.0 when:
      * the backtest result path is missing,
      * the file can't be parsed,
      * the backtest initial cash isn't materially larger than the
        dry-run NAV (the "1.5x" guard matches the historical
        threshold below which scaling doesn't help),
      * the dry-run ``state.initial_nav`` is unset.

    This used to live inside ``_apply_dry_run_fills`` and was the
    reason the function took a full ``result`` dict (it pulled
    ``result["result"]`` to get the backtest path). Splitting it out
    lets callers pass ``qty_scale`` directly and eliminates the
    ``_fake_result`` synthesis the SOD path used to build just to
    satisfy the ``result.get("result")`` lookup.
    """
    if not backtest_result_path:
        return 1.0
    try:
        from vietnam_algo.libs.backtest import _initial_cash_from_result
        backtest_initial_cash = _initial_cash_from_result(Path(str(backtest_result_path)))
    except Exception:
        return 1.0
    if (
        backtest_initial_cash
        and backtest_initial_cash > 0
        and state.initial_nav > 0
        and backtest_initial_cash > state.initial_nav * 1.5
    ):
        scale = float(state.initial_nav) / float(backtest_initial_cash)
        print(
            f"[dry-run] scaling order quantities by {scale:.4f} "
            f"(dry-run NAV={state.initial_nav:,.0f} vs "
            f"backtest initial={backtest_initial_cash:,.0f})"
        )
        return scale
    return 1.0


def _apply_dry_run_fills(
    orders_json_path: "Path | str",
    engine_config: "EngineConfig",
    account_no: str,
    strategy_config: "StrategyConfig | None" = None,
    strategy_name: str = "",
    *,
    qty_scale: float = 1.0,
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
                    # Recover dedup keys from the legacy marker entries
                    # stored inside ``closed_trades``. Older bots kept the
                    # applied_keys there before this commit. Empty set
                    # is fine — apply_orders_to_state will treat every
                    # trade as new and re-apply once on the first cycle
                    # after migration, then dedup going forward.
                    legacy_keys: set[str] = set()
                    for t in state.closed_trades:
                        if isinstance(t, dict) and t.get("applied_keys"):
                            legacy_keys.update(str(k) for k in t["applied_keys"])
                    state.applied_keys.update(legacy_keys)
                    save_dry_run_state(
                        state, engine_config.dataDir, strategy_name=strategy_name,
                    )
            except Exception as exc:
                print(f"[dry-run] legacy-state migration failed: {exc}")
    # The dry-run replays the per-date filtered JSON only — i.e.
    # orders the bot ACTUALLY generated TODAY (or the date the bot's
    # submit cycle produced). Replaying the full backtest CSV (6+
    # years of trades, all 740 rows) was the original intent for an
    # offline replay, but it inflated the dry-run state with the full
    # backtest's cumulative P&L, which doesn't match what a user
    # starting the bot today with ``DRY_RUN_INITIAL_NAV=100M``
    # expects. See the 2026-09-15 incident where realized_pnl was
    # 102M while cash only went up 21M from initial.
    #
    # The dry-run respects the user-configured DRY_RUN_INITIAL_NAV /
    # MOCK_NAV env var. Quantities are SCALED by the caller-supplied
    # ``qty_scale`` (computed in ``_compute_dry_run_qty_scale``) so
    # a 1B-VND-scale backtest output fits a 100M-VND dry-run budget
    # without clipping most BUYs as insufficient_cash /
    # below_board_lot.
    #
    # Before this fix the function took a full ``result`` dict and
    # pulled ``result["result"]`` (the backtest JSON path) here to
    # compute the scale. That forced the start-of-day path to
    # synthesise a ``_fake_result`` dict just to satisfy this lookup,
    # even though the SOD path doesn't run a backtest. Splitting the
    # scale computation into ``_compute_dry_run_qty_scale`` lets
    # callers pass the scale directly.
    json_path = Path(str(orders_json_path))
    payload: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    if json_path.exists():
        try:
            payload = _json.loads(json_path.read_text(encoding="utf-8"))
            items = list(payload.get("items") or [])
        except Exception as exc:
            print(f"[dry-run] failed to read latest orders json {json_path}: {exc}")
            items = []
    if not items:
        return
    if not isinstance(items, list) or not items:
        return
    if qty_scale < 1.0:
        # Round scaled quantities DOWN to the board lot so the
        # resulting fill matches a valid Vietnamese lot size.
        # 2026-09-19 incident: scaling 6200 × 0.1 = 620 shares (6.2
        # lots — invalid). The new EOD path overrides initialCash
        # so this branch usually doesn't fire, but we keep it as a
        # safety net for callers that don't take that path.
        # Use 100 as default since ``board_lot`` is computed later
        # in the function — at this point we haven't seen the
        # strategy config yet.
        _scale_lot = 100
        for o in items:
            raw_qty = o.get("quantity")
            try:
                q = int(float(raw_qty)) if raw_qty not in (None, "") else 0
            except (TypeError, ValueError):
                q = 0
            if q <= 0:
                continue
            scaled = q * qty_scale
            o["quantity"] = max(_scale_lot, int(scaled // _scale_lot) * _scale_lot)
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
        or ""
    )
    marker_key = f"_applied:{trade_date}"
    # Dedup: skip if the same orders (by per-order key) have already
    # been applied for this tradeDate. We rely on the per-order
    # dedup inside apply_orders_to_state (state.applied_keys) rather
    # than a coarse trade_date marker.
    #
    # The previous marker-based dedup was too aggressive: when the
    # backtest was rerun with different parameters (e.g. initialCash
    # overridden to the user's dry-run NAV), the new orders had
    # different qtys/symbols but the same trade_date — and the
    # marker dedup blocked every order from being applied, leaving
    # the dry-run portfolio stuck on stale positions. See the
    # 2026-09-19 momentum incident: BUY 600 BSR @ 28,679 (new
    # 100M-NAV backtest) was silently dropped because the prior
    # run had stamped _applied:2026-09-15 with qty=6200.
    #
    # The per-order dedup (date+symbol+side+qty+price) is sufficient
    # to prevent double-applies on bot restarts because the keys
    # are stable as long as the order is unchanged. New orders get
    # fresh keys and apply normally.
    if any(
        isinstance(t, dict) and t.get("note") == marker_key
        for t in state.closed_trades[-100:]
    ):
        # Already applied once today — but check if any of today's
        # planned orders haven't been seen yet. If yes, log a note
        # and continue past the marker so the new orders apply.
        existing_keys = set(state.applied_keys)
        new_keys = {
            f"{it.get('date', '')}|{it.get('symbol', '')}|"
            f"{it.get('side', '')}|{int(float(it.get('quantity', 0)))}|"
            f"{float(it.get('price', 0))}"
            for it in items
        }
        if new_keys.issubset(existing_keys):
            return
        print(
            f"[dry-run] marker {marker_key} present but {len(new_keys - existing_keys)} "
            f"new order(s) detected — re-applying today's plan"
        )
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


def _extract_best_ask_in_vnd(quote: object) -> int | None:
    """Extract the best-ask price (in absolute VND) from a DNSE
    ``/price/{sym}/quotes/latest`` response.

    DNSE returns the quote in this shape::

        {"quotes": [
            {"symbol": "ABW", "boardId": "G1",
             "bid":   [{"price": 14.4, "quantity": ...}, ...],
             "offer": [{"price": 14.5, "quantity": ...}, ...],  ← best ask
             ...
            },
            ...  # may have G1 + G4 boards
        ]}

    Prices inside ``bid``/``offer`` are in *thousands of VND*
    (e.g. ``14.5`` = 14,500 VND). The order_monitor helper
    ``quote_match_price`` rounds these with ``int(round(...))`` which
    silently drops the unit factor — we must multiply by 1000 here so
    the value is comparable to the autofin ``supply_F/supply_T`` zone
    (which is in absolute VND, e.g. 13,800 = 13,800 VND).
    """
    if not isinstance(quote, dict):
        return None
    boards = quote.get("quotes")
    if not isinstance(boards, list) or not boards:
        return None
    # Prefer the G1 board (HSX main). If unavailable fall back to the
    # first board returned.
    target = None
    for b in boards:
        if isinstance(b, dict) and b.get("boardId") == "G1":
            target = b
            break
    if target is None:
        for b in boards:
            if isinstance(b, dict) and isinstance(b.get("offer"), list) and b["offer"]:
                target = b
                break
    if target is None:
        return None
    offer_levels = target.get("offer")
    if not isinstance(offer_levels, list) or not offer_levels:
        return None
    first = offer_levels[0]
    if not isinstance(first, dict):
        return None
    try:
        v = float(first.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    # DNSE returns kVND; multiply by 1000 → absolute VND.
    return int(round(v * 1000.0))


def _submit_autofin_deals_to_dnse(
    deals_actioned: list[dict],
    *,
    account_no: str,
    loan_package_id: int,
    trading_token: str,
    client: "DnseClient",
    data_dir: "Path",
    trade_date: str,
    loan_package_id_by_symbol: dict[str, int] | None = None,
    live_quote_tolerance_pct: float = 0.03,
) -> dict:
    """Convert autofin adapter's ``deals_actioned`` into ``DnseOrder``
    records and submit them to DNSE via ``execute_orders_with_ledger``.

    Autofin adapter produces ``dnse_orders`` dicts of shape::

        {"symbol", "side", "price", "quantity", "kind", "reason"}

    but ``execute_orders_with_ledger`` consumes ``DnseOrder`` (frozen
    dataclass) with required ``loanPackageId``, ``orderType``,
    ``sourceOrderId``, ``sourceDate``. This helper bridges the two.

    ``sourceOrderId`` is a stable hash of
    ``(deal_id, side, price, kind)`` so the same logical order has the
    same ID across cycles — the ledger's dedup key
    ``(run_id, sourceOrderId, symbol, side)`` then prevents duplicate
    submissions if the bot restarts mid-cycle.

    2026-10-07: for new-entry deals (``deal["action"] == "new_entry"``),
    this function now fetches a LIVE DNSE quote via ``client.get_latest_quote``
    immediately before submitting, and re-checks the buy-zone gate against
    the BEST-ASK price (what we'd actually pay to fill). The planning
    step uses the local cached close, which can be hours stale by mid-day
    — a real-time quote catches the case where the price has since moved
    out of the zone since planning. If the live best-ask is outside
    ``[supply_F * (1 - tol), supply_T * (1 + tol)]``, the entire new-entry
    deal is dropped with a Telegram notification. The local-file price
    is treated as authoritative only when the DNSE API fails (graceful
    degradation — log + proceed, not a hard block).

    Partial-close and full-close actions are NOT gated by this check —
    we always want to SELL regardless of where the live ask is, because
    (a) the partial-close TP is by definition a "we're up" signal, and
    (b) if price has dropped to the SL zone, the order should still
    execute.

    Parameters
    ----------
    deals_actioned:
        From ``strategy_adapter.generate_orders(...)["deals_actioned"]``.
    loan_package_id:
        The DNSE loan package id (1775 for HDB margin).
    loan_package_id_by_symbol:
        Optional per-symbol override for stocks whose loan package
        differs from the default. Falls back to ``loan_package_id``.
    trade_date:
        ISO date used as ``sourceDate`` on every order in this batch.
    live_quote_tolerance_pct:
        Per-side tolerance (default 5 %) for the live-quote gate.
        ``supply_T * (1 + tol)`` and ``supply_F * (1 - tol)`` form the
        upper / lower edges the live best-ask must stay within.

    Returns
    -------
    A dict with ``submitted`` (list of dnseOrderId strings),
    ``skipped_existing`` (count), ``unknown`` (count of orders that
    failed with unknown status), and ``total`` (count of orders in
    input). Empty ``deals_actioned`` returns
    ``{"submitted": [], "skipped_existing": 0, "unknown": 0, "total": 0}``.
    """
    from vietnam_algo.libs.daily_engine import execute_orders_with_ledger
    from vietnam_algo.libs.trading_engine import DnseOrder

    if not deals_actioned:
        return {"submitted": [], "skipped_existing": 0, "unknown": 0, "total": 0}

    # 2026-10-07: filter new_entry deals whose LIVE best-ask has moved
    # out of the supply zone since planning. ``out_of_zone`` collects
    # deals we drop here so the caller can notify.
    out_of_zone: list[dict] = []
    if os.environ.get("AUTOFIN_LIVE_QUOTE_GATE", "1") != "0":
        try:
            for deal in deals_actioned:
                if deal.get("action") != "new_entry":
                    continue
                sym = str(deal.get("symbol") or "").upper().strip()
                s_lo = float(deal.get("supply_F") or 0)
                s_hi = float(deal.get("supply_T") or 0)
                if not sym or s_lo <= 0 or s_hi <= 0:
                    continue  # no zone declared → don't gate
                try:
                    quote = client.get_latest_quote(sym)
                except Exception as exc:
                    # Graceful degradation: API failure → trust the
                    # local-file price the planning step already used.
                    print(
                        f"[autofin_live_gate] {sym}: get_latest_quote "
                        f"failed ({type(exc).__name__}: {str(exc)[:200]}); "
                        f"proceeding with local-cache price"
                    )
                    continue
                ask = _extract_best_ask_in_vnd(quote)
                if ask is None or ask <= 0:
                    print(
                        f"[autofin_live_gate] {sym}: no best-ask in quote; "
                        f"proceeding with local-cache price"
                    )
                    continue
                lo_edge = s_lo * (1.0 - live_quote_tolerance_pct)
                hi_edge = s_hi * (1.0 + live_quote_tolerance_pct)
                if ask < lo_edge or ask > hi_edge:
                    out_of_zone.append({
                        "deal": deal,
                        "live_ask": ask,
                        "lo_edge": lo_edge,
                        "hi_edge": hi_edge,
                    })
                    print(
                        f"[autofin_live_gate] {sym}: live ask {ask:.0f} "
                        f"VND is outside [{lo_edge:.0f}, {hi_edge:.0f}] → "
                        f"dropping new-entry deal "
                        f"(zone [{s_lo:.0f}, {s_hi:.0f}], tol "
                        f"{live_quote_tolerance_pct*100:.1f}%)"
                    )
        except Exception as exc:
            # Defensive: any failure in the gate itself must not block
            # the submit. Log and proceed.
            print(f"[autofin_live_gate] unexpected error: {exc}")

    if out_of_zone:
        out_of_zone_symbols = {d["deal"].get("symbol", "?") for d in out_of_zone}
        # Filter the deals_actioned in-place so the rest of the submit
        # flow only sees the survivors.
        deals_actioned = [
            d for d in deals_actioned
            if d.get("symbol") not in out_of_zone_symbols
        ]
        if not deals_actioned:
            # All deals dropped — return zero result. Caller is expected
            # to send a Telegram summary combining submit + out_of_zone.
            return {
                "submitted": [],
                "skipped_existing": 0,
                "unknown": 0,
                "total": 0,
                "out_of_zone": out_of_zone,
            }

    # Build a stable sourceOrderId for each (deal, side, price, kind) tuple
    # so the ledger dedup works across bot restarts.
    def _stable_id(deal: dict, o: dict) -> int:
        import hashlib
        key = "|".join([
            str(deal.get("deal_id", "")),
            str(o.get("side", "")),
            str(o.get("price", "")),
            str(o.get("kind", "")),
        ])
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        # Convert the first 12 hex chars to an int (48-bit fits in 8 bytes
        # and gives ~2.8e14 unique IDs — enough for the daily batch).
        return int(digest, 16)

    orders: list[DnseOrder] = []
    # 2026-10-08 extension: the bot now places orders during the full
    # HOSE/HNX trading day (09:00-15:00 ICT), not just the continuous
    # sub-windows. The orderType chosen depends on the current
    # trading session:
    #   - ATO (09:00-09:15) → ATO, price=0 (auction determines fill)
    #   - CONTINUOUS (09:15-11:30, 13:00-14:30) → LO, price=signal
    #   - ATC (14:30-14:45) → ATC, price=0
    #   - POST_CLOSE (14:45-15:00) → LO, price=signal (HOSE/HNX;
    #     UPCOM rejects — DNSE surfaces that as a 4xx)
    # If we're outside the trading day entirely (off_hours), skip
    # the submit — the order is preserved in the deals_actioned
    # payload so the next mid-day poll can re-attempt it.
    #
    # The off-hours check is NOT bypassed by force-run-trading —
    # DNSE will reject any order submitted outside the 09:00-15:00 ICT
    # trading window (lunch break, weekends, holidays), so the right
    # thing to do is short-circuit. The user should wait for the
    # next trading session, OR use the dry-run flag to inspect
    # what WOULD have been submitted.
    session = current_session(vietnam_now())
    if session == TradingSession.OFF_HOURS:
        notify(
            "\n".join([
                "⚠️ Vietnam Algo: autofin LIVE submit SKIPPED (off-hours)",
                f"date: {trade_date}",
                f"orders: {sum(1 for d in deals_actioned for _ in (d.get('dnse_orders') or []))}",
                "reason: current time is outside the trading window (09:00-15:00 ICT).",
                "next attempt: next mid-day poll during the next trading session.",
            ])
        )
        return {
            "submitted": [],
            "skipped_existing": 0,
            "unknown": 0,
            "total": 0,
            "out_of_zone": out_of_zone,
            "skipped_off_hours": sum(
                1 for d in deals_actioned for _ in (d.get("dnse_orders") or [])
            ),
        }
    session_order_type = order_type_for_session(session)
    is_auction = session in (TradingSession.ATO, TradingSession.ATC)

    for deal in deals_actioned:
        for o in deal.get("dnse_orders", []) or []:
            sym = str(o.get("symbol") or "").upper().strip()
            side = str(o.get("side") or "").upper().strip()
            if not sym or side not in ("NB", "NS"):
                continue
            try:
                quantity = int(o.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            if quantity <= 0:
                continue
            # In auction windows the fill price is determined by the
            # exchange; the bot must send price=0. The signal price
            # is preserved in the reason string for the audit log.
            signal_price = int(o.get("price") or 0)
            if is_auction:
                price = 0
            elif signal_price <= 0:
                continue
            else:
                price = signal_price
            # 2026-10-09: the bot does NOT use margin. Always submit
            # as a cash order (``loanPackageId=0`` → field omitted
            # from the body by ``place_stock_order``). The launch
            # script's ``--loan-package-id`` is preserved for
            # back-compat with the existing CLI but is effectively a
            # no-op now; if you ever need to enable margin for a
            # specific strategy, pass a positive id via
            # ``loan_package_id_by_symbol`` (per-symbol override).
            lp = (loan_package_id_by_symbol or {}).get(sym, 0)
            # The session's orderType (ATO/ATC/LO) overrides whatever
            # the autofin adapter emitted — the strategy can't know
            # which session it's submitting in without leaking
            # clock-time decisions into the signal layer.
            otype = session_order_type
            reason = str(o.get("reason") or f"autofin:{deal.get('action', '?')}")
            if is_auction and signal_price > 0:
                reason = f"{reason} [session={session.value} signal_price={signal_price}]"
            orders.append(DnseOrder(
                symbol=sym,
                side=side,
                quantity=quantity,
                price=price,
                loanPackageId=lp,
                orderType=otype,
                sourceOrderId=_stable_id(deal, o),
                sourceDate=trade_date,
                reason=reason,
            ))

    if not orders:
        result = {
            "submitted": [],
            "skipped_existing": 0,
            "unknown": 0,
            "total": 0,
        }
        if out_of_zone:
            result["out_of_zone"] = out_of_zone
        return result

    results, _reconciliation, _submit_complete = execute_orders_with_ledger(
        client=client,
        account_no=account_no,
        trading_token=trading_token,
        orders=orders,
        data_dir=data_dir,
        run_id=trade_date,
    )

    submitted_ids: list[str] = []
    skipped_existing = 0
    unknown = 0
    for r in results:
        status = str(r.get("status", ""))
        if status == "submitted":
            dnse_id = r.get("detail", {}).get("id") if isinstance(r.get("detail"), dict) else None
            if not dnse_id:
                # Some endpoints return the id at top level
                resp = r.get("response")
                if isinstance(resp, dict):
                    dnse_id = resp.get("id") or resp.get("orderId")
            if dnse_id:
                submitted_ids.append(str(dnse_id))
        elif status == "skipped_existing_ledger":
            skipped_existing += 1
        elif status in ("unknown",):
            unknown += 1

    return {
        "submitted": submitted_ids,
        "skipped_existing": skipped_existing,
        "unknown": unknown,
        "total": len(orders),
        "out_of_zone": out_of_zone,
    }


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
            save_dry_run_state,
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
            # 2026-10-01 fix: persist the mark-to-market updates back to
            # the dry-run state file so subsequent cycles don't see an
            # outdated ``last_price``. Previously the in-memory
            # ``mark_to_market`` updated the snapshot's ``marketPrice``
            # correctly (so the Telegram message showed fresh prices)
            # but the on-disk ``last_price`` stayed at the last BUY/SELL
            # price forever. The user noticed ABB and BSR showing
            # stale prices: ABB last_price=17,317 (the Sep 15 BUY
            # price, never updated since) and BSR last_price=31,518
            # (the Sep 22 SELL price, never updated since). The fix is
            # to save the state right after mark_to_market so the
            # on-disk and in-memory values stay in sync.
            save_dry_run_state(
                state, engine_config.dataDir, strategy_name=strategy_name,
            )
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


# In-memory throttle cache: maps ledger key → last-polled-at epoch (seconds).
# Lives for the duration of the bot process; reset on bot restart (which
# is the right behaviour — we want to re-poll everything after a restart
# in case status changed while the bot was down).
_ORDER_POLL_THROTTLE: dict[str, float] = {}


def _format_status_change_message(
    item: LedgerOrder,
    previous: str,
    current: str,
    detail: dict | list | None,
) -> str:
    """Build a Telegram message for an order status change.

    The message is human-friendly: a short header line, the previous →
    current transition, and the key order fields (symbol, side, qty,
    price, dnseOrderId). If the detail carries a useful explanation
    (rejectReason, cancelReason, filledQuantity, averagePrice) we
    include that too — operators want to know WHY an order rejected
    without opening the DNSE web UI.
    """
    side_label = "BUY" if item.side == "NB" else "SELL"
    header_icon = {
        "filled": "✅",
        "cancelled": "🚫",
        "rejected": "❌",
        "expired": "⏰",
        "partial": "🟡",
        "open": "📋",
        "unknown": "❓",
    }.get(current, "🔄")

    lines = [
        f"{header_icon} Vietnam Algo: order {item.symbol} → {current.upper()}",
        f"orderId: {item.dnseOrderId or '?'}",
        f"transition: {previous.upper()} → {current.upper()}",
        f"intent: {side_label} {item.quantity:,} {item.symbol} @ {item.price:,} VND",
    ]
    if isinstance(detail, dict):
        # Show extras that operators care about.
        for key in (
            "filledQuantity", "fillQuantity", "matchedQuantity",
            "averagePrice", "fillPrice", "lastPrice",
            "canceledQuantity", "leaveQuantity",
            "rejectReason", "cancelReason", "rejectedReason", "errorReason",
        ):
            value = detail.get(key)
            if value is not None and value != "":
                pretty = key.replace("Quantity", " qty").replace("Price", " (VND)")
                lines.append(f"  {key}: {value}")
        # also surface the raw exchange reason if DNSE attached one
        if "metadata" in detail and isinstance(detail["metadata"], str):
            meta = detail["metadata"]
            if "rejected" in meta.lower() or "reject" in meta.lower():
                lines.append(f"  metadata: {meta[:200]}")
    return "\n".join(lines)


def _monitor_order_statuses(
    client: "DnseClient | None",
    account_no: str,
    trading_token: str | None,
    data_dir: Path,
    *,
    throttle_seconds: int = 60,
) -> dict[str, Any]:
    """Poll DNSE for status updates on every non-terminal order in the ledger.

    Sends a Telegram notification on every status change
    (``pendingNew`` → ``new`` → ``partiallyFilled`` → ``filled``, or
    transitions to ``cancelled`` / ``rejected`` / ``expired``). Also
    sends a follow-up notification on the first ``filled`` for partial
    fills so the operator can react.

    Per-order throttle (default 60 s) prevents hammering DNSE. The
    throttle is in-memory only — bot restart resets it, which is the
    right behaviour: after a restart we want to re-poll every order
    because status may have changed while we were down.

    Returns a summary dict for the cycle log:

        {
            "polled": int,        # number of orders polled this cycle
            "skipped_throttle": int,
            "changes": [{"key": ..., "previous": ..., "current": ...}],
            "errors": [{"key": ..., "err": ...}],
            "terminal_transitions": int,  # # of orders that just hit a terminal state
        }
    """
    summary: dict[str, Any] = {
        "polled": 0,
        "skipped_throttle": 0,
        "changes": [],
        "errors": [],
        "terminal_transitions": 0,
    }
    if client is None:
        return summary
    try:
        ledger = load_ledger(data_dir)
    except Exception as exc:
        summary["errors"].append({"key": "<load_ledger>", "err": str(exc)[:200]})
        return summary

    if not ledger:
        return summary

    now_epoch = time.time()
    dirty = False  # ledger changed → save at the end

    for key, item in list(ledger.items()):
        # Only poll orders that DNSE knows about AND that haven't reached
        # a terminal state. Terminal-state orders are "done" — polling
        # them would just confirm what we already know.
        if not item.dnseOrderId:
            continue
        if item.status in TERMINAL_LEDGER_STATUSES:
            continue

        # Per-order throttle. Skip if we polled this order recently.
        last_polled = _ORDER_POLL_THROTTLE.get(key, 0.0)
        if now_epoch - last_polled < throttle_seconds:
            summary["skipped_throttle"] += 1
            continue

        try:
            detail = client.order_detail(
                account_no, item.dnseOrderId, trading_token=trading_token,
            )
        except Exception as exc:
            # Network blip or DNSE down. Don't spam Telegram on transient
            # errors — just record the error on the ledger so the cycle
            # log surfaces it. The next cycle will retry.
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
            summary["errors"].append({"key": key, "err": err})
            ledger[key] = LedgerOrder(**{**item.__dict__, "error": err, "updatedAt": _utc_iso()})
            dirty = True
            continue

        _ORDER_POLL_THROTTLE[key] = now_epoch
        summary["polled"] += 1

        # DNSE returns the order status under `orderStatus` (or `status`
        # on some endpoints). `order_status_from_detail` already
        # recurses into nested dicts/lists so it handles both shapes.
        raw_status = order_status_from_detail(detail) or ""
        new_status = normalize_ledger_status(raw_status)

        previous_status = item.status
        if new_status != previous_status:
            summary["changes"].append({
                "key": key,
                "symbol": item.symbol,
                "previous": previous_status,
                "current": new_status,
            })
            if new_status in TERMINAL_LEDGER_STATUSES:
                summary["terminal_transitions"] += 1
            try:
                notify(_format_status_change_message(
                    item, previous_status, new_status, detail,
                ))
            except Exception as exc:
                # Telegram failure must not break the poll cycle.
                summary["errors"].append({"key": key, "err": f"notify failed: {str(exc)[:120]}"})

        # Always refresh lastDetail + updatedAt so the on-disk ledger
        # reflects the most recent DNSE view (even when status didn't
        # change — useful for "filledQuantity" updates on partial fills).
        ledger[key] = LedgerOrder(**{**item.__dict__, "status": new_status, "lastDetail": detail, "updatedAt": _utc_iso()})
        dirty = True

    if dirty:
        try:
            save_ledger(ledger, data_dir)
        except Exception as exc:
            summary["errors"].append({"key": "<save_ledger>", "err": str(exc)[:200]})

    return summary


def _utc_iso() -> str:
    """Return current UTC time as an ISO-8601 string (used for ledger updates)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    # Sleep at the TOP of the loop so any ``continue`` statements below
    # still respect the cadence. The previous layout put time.sleep at
    # the bottom, which meant the SOD/EOD data-ready gates'
    # ``if not data_ready: continue`` jumped back to the top and spun
    # without ever sleeping — the 2026-09-23 incident that filled the
    # log to 6 GB before common_sync re-stamped the sentinel. First
    # iteration is skipped so ``--once`` mode still terminates
    # immediately without a phantom pre-cycle sleep.
    first_iter = True
    while True:
        if not first_iter:
            time.sleep(sleep_seconds)
        first_iter = False
        now = vietnam_now()
        today = local_date(now)
        state = load_state(engine_config.dataDir, strategy_name=strategy_name)

        # Order-status monitor — runs every cycle (~60s). Polls DNSE for
        # status of every non-terminal order in the ledger; sends a
        # Telegram notification on every status change. The per-order
        # throttle inside ``_monitor_order_statuses`` prevents hammering
        # DNSE. Always runs (even off-hours) so we catch late fills and
        # rejections from after-hours cancel/replace attempts.
        #
        # The trading token: the monitor runs at the TOP of the loop,
        # before ``ensure_fresh_trading_token`` has had a chance to run
        # in the ``should_submit_orders`` block below. So we read the
        # shared token file that the ``get_otp`` cron writes
        # (data/live/dnse_trading_token.json) — same one the submit path
        # would use. If the file is missing (e.g. first cycle after VPS
        # reboot, before the next get_otp cron) we pass None and let
        # DNSE accept the GET without it (it does on some sessions).
        try:
            _monitor_token = None
            try:
                _shared = load_shared_trading_token(engine_config.dataDir)
                if _shared and _shared.get("token"):
                    _monitor_token = str(_shared["token"])
            except Exception:
                pass
            monitor_summary = _monitor_order_statuses(
                client, account_no, _monitor_token, engine_config.dataDir,
                throttle_seconds=int(os.environ.get("ORDER_MONITOR_THROTTLE_SECONDS", "60")),
            )
            if monitor_summary.get("polled") or monitor_summary.get("changes") or monitor_summary.get("errors"):
                print(
                    f"[{now.isoformat()}] order monitor: "
                    f"polled={monitor_summary['polled']} "
                    f"throttle_skipped={monitor_summary['skipped_throttle']} "
                    f"changes={len(monitor_summary['changes'])} "
                    f"terminal={monitor_summary['terminal_transitions']} "
                    f"errors={len(monitor_summary['errors'])}"
                )
        except Exception as exc:
            print(f"[{now.isoformat()}] order monitor crashed: {type(exc).__name__}: {str(exc)[:200]}")
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
            #
            # 2026-09-23 review: the previous age threshold
            # ``> 60 * 18`` (18 hours) was always breached on the
            # first SOD after the cron run, because common_sync fires
            # at 13:15 ICT and SOD fires at 09:00 ICT — ~19h 45min
            # apart. The bot then tight-looped waiting on the sentinel
            # (the ``continue`` bypassed the bottom-of-loop sleep; that
            # sleep now lives at the top of the loop, but the age
            # threshold was still wrong by design). Now we only gate
            # on sentinel existence — common_sync refuses to stamp on
            # bad sync days, so a stale sentinel means genuine data
            # trouble, and waiting until the next 13:15 cron is OK.
            data_ready = True
            if not engine_config.syncData:
                try:
                    _ready = engine_config.dataDir / engine_config.liveDirName / "data_ready_for_eod"
                    if not _ready.exists():
                        print(
                            f"[{now.isoformat()}] waiting for data-sync owner "
                            f"to refresh prices before start-of-day briefing"
                        )
                        data_ready = False
                except Exception as exc:
                    print(f"[{now.isoformat()}] data-ready probe failed: {exc}")
            if not data_ready:
                continue
            print(f"[{now.isoformat()}] Sending start-of-day briefing")
            try:
                from vietnam_algo.libs.notifier import start_of_day_message
                _items: list[dict[str, object]] = []
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
                                # SOD replays the orders JSON the previous
                                # EOD already wrote. The qty_scale has
                                # already been baked into those qtys by the
                                # EOD run (which had access to the backtest
                                # initial cash via the result dict), so
                                # pass 1.0 here. No ``_fake_result``
                                # synthesis needed — the function takes the
                                # orders JSON path directly. See issue #3
                                # in the 2026-09-25 review.
                                _apply_dry_run_fills(
                                    _json_path, engine_config, account_no,
                                    strategy_config, strategy_name=strategy_name,
                                    qty_scale=1.0,
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
                    # ``cycle_events.message`` is TEXT (unbounded — see
                    # libs/schema_sql.py:147), so pass the full message.
                    # The Telegram alert above (operator-visible) is the
                    # authoritative copy and is also unbounded; storing
                    # a truncated copy here would silently corrupt the
                    # audit trail because the message can be re-fetched
                    # later from the table by dashboards / log viewers.
                    # See 2026-09-25 review issue #4.
                    message=msg,
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
                            live_submit=live_submit,
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
                    # Live mode: actually submit the planned orders to
                    # DNSE via execute_orders_with_ledger. We do this
                    # BEFORE the dry-run apply so the dry-run state
                    # stays untouched (the orders are real, not simulated).
                    # Skip if no trading_token — that means
                    # ensure_fresh_trading_token hasn't run yet for
                    # this cycle (e.g. token refresh failed).
                    if (
                        live_submit
                        and isinstance(poll_result, dict)
                        and poll_result.get("deals_actioned")
                        and token
                    ):
                        try:
                            submit_result = _submit_autofin_deals_to_dnse(
                                poll_result.get("deals_actioned") or [],
                                account_no=account_no,
                                loan_package_id=loan_package_id,
                                trading_token=token,
                                client=client,
                                data_dir=engine_config.dataDir,
                                trade_date=today,
                            )
                            print(
                                f"[{now.isoformat()}] mid-day submit: "
                                f"{len(submit_result['submitted'])} submitted, "
                                f"{submit_result['skipped_existing']} skipped-existing, "
                                f"{submit_result['unknown']} unknown "
                                f"(of {submit_result['total']} planned)"
                            )
                            if submit_result["submitted"]:
                                notify(
                                    "Vietnam Algo: autofin mid-day SUBMITTED to DNSE\n"
                                    f"date: {today}\n"
                                    f"count: {len(submit_result['submitted'])}\n"
                                    f"dnseOrderIds: {','.join(submit_result['submitted'][:10])}"
                                    + (
                                        f" + {len(submit_result['submitted']) - 10} more"
                                        if len(submit_result["submitted"]) > 10
                                        else ""
                                    )
                                )
                            # 2026-10-08 fix: surface the silent failure
                            # path. Before this, if ``place_stock_order``
                            # raised (e.g. AttributeError because the
                            # method was never implemented, or a 5xx
                            # from DNSE that the bot's try/except
                            # swallowed), ``unknown`` would be > 0 and
                            # the cycle would end with no Telegram
                            # notification. The user would see the
                            # per-order "→ DNSE: BUY ..." preview
                            # (which fires BEFORE the submit) and
                            # assume the order went through. Pin the
                            # failure-notify here so a future
                            # regression cannot silently swallow
                            # submission errors again.
                            if submit_result.get("unknown", 0) > 0:
                                notify(
                                    "\n".join([
                                        "🚨 Vietnam Algo: autofin mid-day SUBMIT UNKNOWN STATUS",
                                        f"date: {today}",
                                        f"unknown: {submit_result['unknown']} of {submit_result['total']}",
                                        "orders did not get a confirmed submit/reject from DNSE.",
                                        "Check data/live/order_ledger.json for the per-order error.",
                                    ])
                                )
                            # 2026-10-07: notify when the live-quote
                            # gate dropped a new-entry deal because the
                            # live best-ask had moved out of the
                            # supply zone since planning.
                            out_of_zone = submit_result.get("out_of_zone") or []
                            if out_of_zone:
                                lines = [
                                    "⚠️ Vietnam Algo: autofin LIVE-QUOTE gate dropped new entries",
                                    f"date: {today}",
                                    f"reason: live best-ask outside [supply_F*0.95, supply_T*1.05]",
                                ]
                                for o in out_of_zone[:10]:
                                    d = o.get("deal", {})
                                    lines.append(
                                        f"  {d.get('symbol', '?')}: "
                                        f"live ask {o.get('live_ask', 0):,.0f} VND "
                                        f"(zone {d.get('supply_F', 0):,.0f}–{d.get('supply_T', 0):,.0f})"
                                    )
                                if len(out_of_zone) > 10:
                                    lines.append(f"  + {len(out_of_zone) - 10} more")
                                notify("\n".join(lines))
                        except Exception as exc:
                            notify(
                                "\n".join([
                                    "🚨 Vietnam Algo: autofin mid-day SUBMIT FAILED",
                                    f"date: {today}",
                                    f"err: {str(exc)[:400]}",
                                ])
                            )
                            print(f"[{now.isoformat()}] mid-day submit failed: {exc}")
                    # Mid-day polls also need to apply the planned orders
                    # to the dry-run state immediately. Without this,
                    # the JSON is overwritten with [] on the next poll
                    # (which plans nothing new) and the partial close
                    # is never applied to the simulated portfolio —
                    # so the EOD's positions section keeps showing the
                    # pre-close qty and the operator can't see what was
                    # actually sold during the day. See the 2026-09-22
                    # ASP partial-close incident where the Telegram
                    # alert said "SELL 100 ASP" but the EOD showed
                    # ASP qty unchanged.
                    if not live_submit and isinstance(poll_result, dict):
                        try:
                            # Autofin's poll_result has ``latestOrdersJson``
                            # pointing at the JSON it just wrote; the
                            # orders there are sized for the current NAV
                            # scale (autofin doesn't run a backtest that
                            # needs rescaling). qty_scale=1.0 is correct.
                            poll_orders_json = (
                                poll_result.get("latestOrdersJson")
                                if isinstance(poll_result, dict)
                                else None
                            )
                            if poll_orders_json:
                                _apply_dry_run_fills(
                                    poll_orders_json, engine_config, account_no,
                                    strategy_config, strategy_name=strategy_name,
                                    qty_scale=1.0,
                                )
                        except Exception as exc:
                            print(
                                f"[{now.isoformat()}] mid-day dry-run apply failed: {exc}"
                            )
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
            # ``data_ready_for_eod`` written by the owner.
            #
            # 2026-10-08 fix: the previous code only SKIPPED on
            # ABSENT sentinel; for a STALE sentinel it just printed a
            # warning and proceeded with EOD using yesterday's prices
            # (the 2026-10-08 incident: BSR reported 31,750 instead
            # of 32,000). The autofin sync takes ~26 min; the sentinel
            # can be hours stale before the sync completes. Now we
            # treat a stale sentinel the same as an absent one and
            # ``continue`` to retry on the next 60s cycle. The first
            # ``should_run_eod`` check (above) still re-fires each
            # cycle because state.eodDate is unchanged until EOD
            # actually completes.
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
                                f"{age_min:.0f} min old — owner may be stuck, "
                                f"skipping EOD to avoid stale prices; will retry "
                                f"next cycle (60s)"
                            )
                            data_ready = False
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
                # 2026-10-08: the previous ``except Exception: pass``
                # silently swallowed disk-full / permission errors and
                # left momentum running EOD against yesterday's close
                # (the 2026-10-08 incident). Log the error and retry
                # once before giving up — if the disk write still
                # fails, raise so the bot crashes and the watchdog
                # relaunches it under a healthy state.
                _stamp = (
                    engine_config.dataDir
                    / engine_config.liveDirName
                    / "data_ready_for_eod"
                )
                _stamp.parent.mkdir(parents=True, exist_ok=True)
                from datetime import datetime as _dt2, timezone as _tz2
                _stamp_payload = _dt2.now(tz=_tz2).isoformat()
                _stamp_err: Exception | None = None
                for _stamp_attempt in range(2):
                    try:
                        _stamp.write_text(_stamp_payload)
                        _stamp_err = None
                        break
                    except Exception as _exc:
                        _stamp_err = _exc
                        print(
                            f"[{now.isoformat()}] data_ready_for_eod "
                            f"stamp attempt {_stamp_attempt + 1}/2 failed: "
                            f"{type(_exc).__name__}: {_exc}"
                        )
                if _stamp_err is not None:
                    notify(
                        "\n".join([
                            "🚨 Vietnam Algo: data_ready_for_eod stamp FAILED",
                            f"strategy: {engine_config.strategy}",
                            f"err: {str(_stamp_err)[:300]}",
                            "consequence: peer bots (momentum) will use stale prices",
                        ])
                    )
                    raise _stamp_err
            # Use the strategy adapter's generate_orders() when available.
            # This is the right path for strategies like autofin_signal
            # which don't go through run_daily_once → run_momentum_rotation.
            # Fall back to the legacy generate_live_orders() for momentum /
            # hybrid strategies.
            #
            # Pass ``trade_date=today`` so the momentum backtest's
            # ``export_orders_for_date`` filters the historical CSV to
            # TODAY's date. Without it, the bot silently picks the
            # latest CSV row (often days stale — e.g. 2026-09-16
            # when today is 2026-09-22) and reports that as
            # "tradeDate", which the operator flagged as confusing.
            # When the backtest has no signals for today, count=0
            # and the briefing reports "No orders today" — which is
            # the truthful answer for a backtest-driven strategy.
            #
            # In dry-run mode the bot's order sizes should match
            # the operator's configured NAV scale (DRY_RUN_INITIAL_NAV
            # / MOCK_NAV), not the 1B-VND default. Otherwise the
            # backtest emits 6200-share BUYs that get scaled to
            # 620 shares (6.2 lots — invalid Vietnamese board lot)
            # during the dry-run fill replay, leaving the
            # order file's quantity out of sync with the
            # resulting position. Run the backtest directly at
            # the dry-run NAV so lots come out native.
            # Pre-2026-09-19 the runner did this via a
            # post-hoc qty_scale in _apply_dry_run_fills, which
            # broke the board-lot rounding and produced the
            # mismatch the operator flagged in the
            # BSR 6200/620 incident.
            strat_cfg = strategy_config
            if not live_submit:
                try:
                    from vietnam_algo.libs.dry_run_state import (
                        init_dry_run_state,
                    )
                    _dry = init_dry_run_state(
                        engine_config.dataDir,
                        strategy_name=strategy_name,
                    )
                    if (
                        _dry.initial_nav > 0
                        and _dry.initial_nav
                        < float(strat_cfg.initialCash) * 0.99
                    ):
                        from dataclasses import replace as _dc_replace
                        strat_cfg = _dc_replace(
                            strat_cfg,
                            initialCash=float(_dry.initial_nav),
                        )
                        print(
                            f"[eod] dry-run: backtest initialCash "
                            f"overridden to {strat_cfg.initialCash:,.0f}"
                        )
                except Exception as exc:
                    print(f"[eod] dry-run initialCash override failed: {exc}")
            if strategy_adapter is not None:
                result = strategy_adapter.generate_orders(
                    engine_config, strat_cfg, trade_date=today,
                    # EOD reuses the orders CSV the 09:16 submit
                    # cycle wrote. The full ~3-min backtest has
                    # already run; running it again at 15:15 would
                    # be wasted compute AND could drift if any
                    # underlying data shifted. See 2026-09-25
                    # review issue #1.
                    skip_backtest=True,
                    live_submit=live_submit,
                )
            else:
                result = generate_live_orders(
                    engine_config, strat_cfg, trade_date=today,
                    strategy_name=strategy_name,
                    notify_telegram=False,
                    skip_backtest=True,
                )
            print(result)
            # Live mode: actually submit the planned orders to DNSE.
            # Mirrors the mid-day-poll logic above: the ledger dedups
            # by (run_id, sourceOrderId, symbol, side) so the same
            # logical order can't be double-submitted if the bot
            # restarts mid-day and the EOD replans it.
            if (
                live_submit
                and isinstance(result, dict)
                and result.get("deals_actioned")
                and token
            ):
                try:
                    submit_result = _submit_autofin_deals_to_dnse(
                        result.get("deals_actioned") or [],
                        account_no=account_no,
                        loan_package_id=loan_package_id,
                        trading_token=token,
                        client=client,
                        data_dir=engine_config.dataDir,
                        trade_date=today,
                    )
                    print(
                        f"[{now.isoformat()}] eod submit: "
                        f"{len(submit_result['submitted'])} submitted, "
                        f"{submit_result['skipped_existing']} skipped-existing, "
                        f"{submit_result['unknown']} unknown "
                        f"(of {submit_result['total']} planned)"
                    )
                    if submit_result["submitted"]:
                        notify(
                            "Vietnam Algo: autofin EOD SUBMITTED to DNSE\n"
                            f"date: {today}\n"
                            f"count: {len(submit_result['submitted'])}\n"
                            f"dnseOrderIds: {','.join(submit_result['submitted'][:10])}"
                            + (
                                f" + {len(submit_result['submitted']) - 10} more"
                                if len(submit_result["submitted"]) > 10 else ""
                            )
                        )
                    # 2026-10-08 fix: surface the silent failure path on
                    # the EOD submit too. See mid-day equivalent above
                    # for the 2026-10-08 04:30 incident backstory.
                    if submit_result.get("unknown", 0) > 0:
                        notify(
                            "\n".join([
                                "🚨 Vietnam Algo: autofin EOD SUBMIT UNKNOWN STATUS",
                                f"date: {today}",
                                f"unknown: {submit_result['unknown']} of {submit_result['total']}",
                                "orders did not get a confirmed submit/reject from DNSE.",
                                "Check data/live/order_ledger.json for the per-order error.",
                            ])
                        )
                    out_of_zone = submit_result.get("out_of_zone") or []
                    if out_of_zone:
                        lines = [
                            "⚠️ Vietnam Algo: autofin LIVE-QUOTE gate dropped new entries (EOD)",
                            f"date: {today}",
                            f"reason: live best-ask outside [supply_F*0.95, supply_T*1.05]",
                        ]
                        for o in out_of_zone[:10]:
                            d = o.get("deal", {})
                            lines.append(
                                f"  {d.get('symbol', '?')}: "
                                f"live ask {o.get('live_ask', 0):,.0f} VND "
                                f"(zone {d.get('supply_F', 0):,.0f}–{d.get('supply_T', 0):,.0f})"
                            )
                        if len(out_of_zone) > 10:
                            lines.append(f"  + {len(out_of_zone) - 10} more")
                        notify("\n".join(lines))
                except Exception as exc:
                    notify(
                        "\n".join([
                            "🚨 Vietnam Algo: autofin EOD SUBMIT FAILED",
                            f"date: {today}",
                            f"err: {str(exc)[:400]}",
                        ])
                    )
                    print(f"[{now.isoformat()}] eod submit failed: {exc}")
            # In dry-run mode, REPLAY today's planned orders against the
            # simulated state BEFORE building the snapshot, so the EOD
            # summary shows cash + positions post-fill (matching what the
            # bot would have entered if it were live).
            if not live_submit and isinstance(result, dict):
                try:
                    orders_json_path = result.get("latestOrdersJson")
                    if orders_json_path:
                        # Compute the qty scale outside the function so
                        # ``_apply_dry_run_fills`` no longer needs the
                        # ``result`` dict (and the SOD path can stop
                        # building a ``_fake_result`` to satisfy the
                        # function's previous ``result.get("result")``
                        # lookup). See 2026-09-25 review issue #3.
                        backtest_result_path = result.get("result")
                        qty_scale = _compute_dry_run_qty_scale(
                            backtest_result_path, _dry,
                        )
                        _apply_dry_run_fills(
                            orders_json_path, engine_config, account_no,
                            strategy_config, strategy_name=strategy_name,
                            qty_scale=qty_scale,
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
                    # ``cycle_events.message`` is TEXT (unbounded — see
                    # libs/schema_sql.py:147), so pass the full EOD
                    # summary. Pre-2026-09-25 this was truncated to
                    # ``eod_msg[:500]`` — silently lossy when the
                    # operator-visible Telegram alert was longer (which
                    # it almost always was for the momentum strategy
                    # where the backtest metrics + orders table easily
                    # exceeds 500 chars). The full text is also mirrored
                    # to ``daily_reports.raw_message`` so the audit
                    # trail is consistent across both tables. See
                    # 2026-09-25 review issue #4.
                    message=eod_msg,
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
            # Probe data freshness (sentinel age + sample-symbol latest
            # bars). Once-per-day alert if stale — so an operator
            # notices when the cron job or upstream is broken even if
            # the bot is otherwise healthy.
            try:
                freshness = get_market_data_freshness(engine_config.dataDir)
            except Exception as exc:
                freshness = {"fresh": False, "error": str(exc)[:120]}
            if not freshness.get("fresh", False):
                if state.dataStaleAlertSentDate != today:
                    sentinel_age = freshness.get("sentinel_age_hours")
                    median_age = freshness.get("median_latest_bar_age_days")
                    parts = [
                        "⚠️ Vietnam Algo: market data STALE",
                        f"strategy: {strategy_name or engine_config.strategy or '?'}",
                        f"date: {today}",
                    ]
                    if sentinel_age is not None:
                        parts.append(f"data_ready_for_eod age: {sentinel_age}h (threshold 25h)")
                    elif freshness.get("sentinel_exists") is False:
                        parts.append("data_ready_for_eod sentinel missing")
                    if median_age is not None:
                        cal_age = freshness.get(
                            "median_latest_bar_age_calendar_days",
                        )
                        # Trading-day age is the freshness gate (so a
                        # Friday close on a Monday morning isn't
                        # flagged as stale). Calendar-day age is
                        # shown in the alert text for context — it's
                        # what most operators intuit. 2026-09-19
                        # user feedback: "the day before is weekend
                        # so we can not say that the data is stale,
                        # we only compare with trading day not
                        # holiday".
                        if cal_age is not None and abs(cal_age - median_age) > 0.01:
                            parts.append(
                                f"sample latest-bar median age: {median_age} "
                                f"trading-days ({cal_age} calendar-days, "
                                f"threshold 2d)"
                            )
                        else:
                            parts.append(
                                f"sample latest-bar median age: {median_age}d "
                                f"(threshold 2d)"
                            )
                        if freshness.get("oldest_sample_symbol"):
                            parts.append(
                                f"oldest sample: {freshness['oldest_sample_symbol']}"
                            )
                    if "error" in freshness:
                        parts.append(f"probe error: {freshness['error']}")
                    notify("\n".join(parts))
                    state.dataStaleAlertSentDate = today
                    save_state(
                        state, engine_config.dataDir, strategy_name=strategy_name
                    )
            else:
                # Healthy — clear any prior suppression so we'll alert
                # again if it goes stale in the future.
                if state.dataStaleAlertSentDate:
                    state.dataStaleAlertSentDate = ""
                    save_state(
                        state, engine_config.dataDir, strategy_name=strategy_name
                    )
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
                freshness=freshness,
            )
            notify(msg)
            _emit_cycle(
                _db, strategy_name or engine_config.strategy or "?",
                "heartbeat",
                payload={"date": today, "proxy_check": proxy_check,
                         "uptime_s": uptime_s, "pid": pid,
                         "data_fresh": freshness.get("fresh")},
                local_date=today,
            )
            state.lastHeartbeatDate = today
            save_state(state, engine_config.dataDir, strategy_name=strategy_name)

        if once:
            return
        # NB: time.sleep lives at the TOP of the while-loop now (see
        # the comment block above the loop). This makes any
        # ``continue`` in the gates above still respect the cadence.
