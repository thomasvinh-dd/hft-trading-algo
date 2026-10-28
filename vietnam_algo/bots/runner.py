from __future__ import annotations

import time
import os
import re
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
    filled_quantity,
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
    # 2026-10-25: keep the exact issuance timestamp so
    # ``ensure_fresh_trading_token`` can judge a DNSE UUID token
    # (which carries no JWT ``exp``) as fresh instead of always
    # falling through to the fragile lazy email-OTP refresh.
    if shared.get("issued_at"):
        state.tokenIssuedAt = shared["issued_at"]
    return True


def _load_dnse_trading_token_from_disk(data_dir) -> str | None:
    """Return the latest DNSE trading token from the shared file, or None.

    Used by the T+2 settlement SL retry path. We can't piggy-back on
    the local ``token`` variable inside ``run_auto_bot`` because that
    variable is set inside the conditional CA-sync branch and would
    either be unbound (when ``--no-sync-data`` skips the CA sync) or
    silently hold the VIETSTOCK token (the wrong token for DNSE
    calls). Reading the shared file directly is both correct and
    independent of the data-sync setting.

    The file is written by the ``run-get-otp`` cron (08:30 ICT) and
    is the same one the order monitor reads at the top of the loop.
    If the token is missing or expired we return None and the caller
    skips the T+2 retry (it will retry on the next SOD cycle).
    """
    try:
        shared = load_shared_trading_token(data_dir)
    except Exception:
        return None
    if not shared:
        return None
    tok = shared.get("token")
    if not tok:
        return None
    return str(tok)


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


def _hose_price_tick(price_vnd: int) -> int:
    """Return the HOSE tick size for a stock price, in VND.

    Per HOSE market rules, the minimum price increment depends on
    the absolute price level::

        <  10,000 VND →  10 VND
        10,000-49,950  →  50 VND
        >= 50,000 VND → 100 VND

    Anything outside HOSE (e.g. HNX, UPCOM) uses a 100-VND tick by
    default — DNSE rejects prices that don't respect the board's
    increment with HTTP 400 ``INVALID_PRICE_LOT``. Without this
    helper the SELL-at-market override (which prices a SELL at
    ``best_bid × 0.99``) frequently lands on a multiple of 10 but
    not 50/100, so the order is rejected on the first attempt.

    2026-10-21 BAF incident: the autofin-recommended BAF exit at
    30,950 produced ``sell_price = round(30950 × 0.99) = 30640``,
    which is a multiple of 10 but NOT of 50. DNSE responded
    ``{"code":"INVALID_PRICE_LOT","message":"Invalid price lot"}``
    and the SELL never reached the exchange. With this helper
    the price is rounded DOWN to the nearest tick (30,600) so it
    always executes (down-rounded but the SELL is marketable;
    the 1% ``AUTOFIN_SELL_BID_DISCOUNT_PCT`` already gives a
    buffer for the bid to tick down).
    """
    p = int(price_vnd)
    if p <= 0:
        return 0
    if p < 10_000:
        tick = 10
    elif p < 50_000:
        tick = 50
    else:
        tick = 100
    # Round DOWN to the nearest tick so the SELL crosses at or
    # BELOW the best-bid — this guarantees an immediate fill at
    # market. (Rounding up could leave the order resting above
    # the book if the bid ticks between the quote fetch and the
    # submit.)
    return p - (p % tick)


def _extract_best_bid_in_vnd(quote: object) -> int | None:
    """Extract the best-bid price (in absolute VND) from a DNSE
    ``/price/{sym}/quotes/latest`` response.

    Mirror of :func:`_extract_best_ask_in_vnd` but reads the
    ``bid[0].price`` (top of book) instead of ``offer``. Used
    by the order-monitor re-pricing logic to chase a falling
    SELL order (i.e. a SELL limit that nobody is hitting because
    the market is dropping — we re-price DOWN to follow the bid).
    """
    if not isinstance(quote, dict):
        return None
    boards = quote.get("quotes")
    if not isinstance(boards, list) or not boards:
        return None
    target = None
    for b in boards:
        if isinstance(b, dict) and b.get("boardId") == "G1":
            target = b
            break
    if target is None:
        for b in boards:
            if isinstance(b, dict) and isinstance(b.get("bid"), list) and b["bid"]:
                target = b
                break
    if target is None:
        return None
    bid_levels = target.get("bid")
    if not isinstance(bid_levels, list) or not bid_levels:
        return None
    first = bid_levels[0]
    if not isinstance(first, dict):
        return None
    try:
        v = float(first.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
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

    # 2026-10-12: the DNSE server REQUIRES ``loanPackageId`` in every
    # place_stock_order body, and treats ``0`` as "no value" (rejected
    # with ``HTTP 400 INVALID_INPUT loanPackageId is required``). The
    # only valid value is the actual cash-package id (e.g. ``1036``
    # for ABB on account 0003937868), which varies per symbol. Look
    # it up once per symbol and cache for the rest of the run.
    cash_pkg_cache: dict[str, int] = {}
    cash_pkg_lookup_errors: list[str] = []

    def _resolve_cash_pkg(sym: str) -> int:
        if sym in cash_pkg_cache:
            return cash_pkg_cache[sym]
        if sym in (loan_package_id_by_symbol or {}):
            cash_pkg_cache[sym] = int(loan_package_id_by_symbol[sym])
            return cash_pkg_cache[sym]
        try:
            pkg_id = client.get_cash_loan_package_id(account_no, sym)
        except Exception as exc:
            cash_pkg_lookup_errors.append(f"{sym}: {type(exc).__name__}: {str(exc)[:200]}")
            # Use the launch-script's default as a last resort — better
            # to send a wrong-but-valid number than to crash. The user
            # will see the lookup error in the submit result anyway.
            pkg_id = int(loan_package_id) if loan_package_id else 0
        cash_pkg_cache[sym] = pkg_id
        return pkg_id

    # 2026-10-12: orderType also depends on the SYMBOL's market, not
    # just the trading session. UPCOM (``UPX``) only accepts ``LO`` —
    # submitting ATC/ATO/MP on a UPCOM symbol returns HTTP 400
    # ``INVALID_ORDER_TYPE_FOR_THE_EXCHANGE`` (logged on the
    # 14:31 ICT ABB live-submit attempt). HNX (``HNX``) accepts ATC
    # but not ATO. HOSE (``HSX``) accepts both. Look up the market
    # per symbol and override the order type accordingly.
    market_cache: dict[str, str] = {}
    market_lookup_errors: list[str] = []

    def _order_type_for_symbol(sym: str, default_otype: str) -> str:
        """Return the orderType DNSE will accept for this symbol+session.
        UPCOM always uses ``LO`` (no auction support).
        """
        if sym in market_cache:
            market_id = market_cache[sym]
        else:
            try:
                market_id = client.get_market_id(sym)
                market_cache[sym] = market_id
            except Exception as exc:
                market_lookup_errors.append(f"{sym}: {type(exc).__name__}: {str(exc)[:200]}")
                market_cache[sym] = "UNKNOWN"
                market_id = "UNKNOWN"
        if market_id == "UPX":
            # UPCOM only supports LO. Send LO with the signal price.
            return "LO"
        if market_id == "HNX" and default_otype == "ATO":
            # HNX doesn't have an opening auction; downgrade to LO.
            return "LO"
        return default_otype

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
            # 2026-10-12: if the market-aware override downgraded the
            # orderType to LO (UPCOM doesn't have auctions, HNX has
            # no ATO), the price is no longer auction-determined —
            # send the signal price.
            signal_price = int(o.get("price") or 0)
            if is_auction:
                # Will be re-evaluated after we know the market
                price = 0
            # 2026-10-21 BAF fix: when ``signal_price`` is 0 AND
            # this is a SELL, the bot MUST still submit (the SELL
            # at-market override below fills in the live bid). The
            # previous ``elif signal_price <= 0: continue`` gate
            # silently dropped any SELL without an explicit price,
            # which broke the BAF capital_protect recomment path
            # — autofin's ``/signal-deals`` top-level
            # ``lastRecomment`` field carries only the SHORT version
            # (e.g. ``🛡️ Chốt lãi -1.75 % bảo toàn vốn``); the
            # ``tại P`` price lives in ``hisRecomment[0]`` HTML and
            # was never propagated to the order's price field. The
            # SELL-at-market path below takes care of pricing the
            # order against the live best-bid, so price=0 is a
            # valid input for a SELL and must NOT be dropped here.
            # BUY orders (NB) with no price still make no sense
            # outside auction — a BUY @ 0 would buy at any price —
            # so the gate remains in place for NB.
            elif signal_price <= 0 and side == "NB":
                continue
            # 2026-10-13: SELL orders (NS) are submitted at the LIVE
            # best-bid, not autofin's recomment price. Per user
            # feedback: when autofin publishes a SELL signal
            # (TP/reduce/SL/full-close), the bot should sell at
            # the current market price so the order always fills —
            # otherwise a falling market can leave the SELL resting
            # above the book (e.g. autofin suggests TP @ 15,000 but
            # the bid has dropped to 14,500; the LO @ 15,000 won't
            # fill, and the 3% re-pricer ceiling then abandons it
            # below 14,550, missing the exit entirely). Setting
            # price = best_bid (or best_bid * sell_bid_discount_pct
            # — a 1% safety margin against the bid ticking down
            # between quote-fetch and submit) makes the SELL
            # marketable: it crosses against the best-bid buyer
            # and fills immediately.
            #
            # Falls back to the signal_price (autofin's suggested
            # TP/SL price) when the live quote is unavailable, so
            # network blips don't block the SELL.
            sell_price = signal_price  # default
            # Env-var kill switch: setting AUTOFIN_SELL_AT_MARKET=0
            # reverts to the pre-2026-10-13 behavior (use autofin's
            # recomment price, no live-bid override). Default enabled.
            if (
                side == "NS"
                and not is_auction
                and os.environ.get("AUTOFIN_SELL_AT_MARKET", "1") != "0"
            ):
                try:
                    quote = client.get_latest_quote(sym)
                except Exception as exc:
                    print(
                        f"[autofin_sell_at_market] {sym}: get_latest_quote "
                        f"failed ({type(exc).__name__}: {str(exc)[:200]}); "
                        f"falling back to signal_price={signal_price}"
                    )
                else:
                    bid = _extract_best_bid_in_vnd(quote)
                    if bid and bid > 0:
                        sell_bid_discount_pct = float(
                            os.environ.get("AUTOFIN_SELL_BID_DISCOUNT_PCT", "0.99")
                        )
                        raw_sell_price = int(round(bid * sell_bid_discount_pct))
                        # 2026-10-21 BAF: round to the HOSE price tick
                        # so DNSE doesn't reject with
                        # ``INVALID_PRICE_LOT``. See ``_hose_price_tick``
                        # for the rule table.
                        sell_price = _hose_price_tick(raw_sell_price)
                        print(
                            f"[autofin_sell_at_market] {sym} SELL price "
                            f"override: signal={signal_price} → "
                            f"bid={bid:,.0f} VND × "
                            f"{sell_bid_discount_pct:.0%} = {raw_sell_price:,.0f} "
                            f"VND → tick-rounded={sell_price:,.0f} "
                            f"VND (ensures immediate fill at market)"
                        )
            if not is_auction:
                # SELL-at-market override (if it ran) takes precedence
                # over the signal price. For BUY orders the live-quote
                # path doesn't fire, so this is a no-op.
                price = sell_price
            # 2026-10-12: cash-package id is REQUIRED and per-symbol.
            # Look it up via DNSE's ``loan-packages`` endpoint; falls
            # back to ``loan_package_id_by_symbol`` then to the launch
            # script's default (``--loan-package-id``) for back-compat.
            lp = _resolve_cash_pkg(sym)
            # The session's orderType (ATO/ATC/LO) is the default,
            # but UPCOM symbols (e.g. ABB) only support ``LO`` and
            # DNSE rejects ATO/ATC with HTTP 400
            # ``INVALID_ORDER_TYPE_FOR_THE_EXCHANGE``. The market-
            # aware override is non-negotiable.
            otype = _order_type_for_symbol(sym, session_order_type)
            # If the orderType was downgraded from ATO/ATC to LO
            # (UPCOM, or HNX-during-ATO), use the signal price
            # instead of 0 — the LO orderType accepts a non-zero price.
            if is_auction and otype == "LO":
                price = signal_price
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
    # 2026-10-13 debug: surface the raw result statuses
    def _sym(r: dict) -> str:
        order = r.get("order")
        if hasattr(order, "symbol"):
            return str(order.symbol)
        if isinstance(order, dict):
            return str(order.get("symbol", "?"))
        return "?"
    print(f"[submit-debug] execute_orders_with_ledger returned {len(results)} results: {[(r.get('status'), _sym(r)) for r in results]}")

    submitted_ids: list[str] = []
    skipped_existing = 0
    unknown = 0
    rejected = 0
    for r in results:
        status = str(r.get("status", ""))
        if status == "rejected":
            # 2026-10-13: 4xx DNSE errors land here (was
            # "unknown" before — that blocked all subsequent
            # submits via ``unsafe_existing_orders``). The
            # submit itself succeeded (DNSE responded), it
            # just declined the order.
            rejected += 1
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
        "rejected": rejected,
        "total": len(orders),
        "out_of_zone": out_of_zone,
    }


# 2026-10-13: T+2 settlement SL retry
#
# Vietnam T+2 settlement: a SELL order for shares that were
# bought today (T) cannot be placed until T+2 (two calendar
# days later). DNSE rejects the SELL with HTTP 400
# ``TRADE_QUANTITY_NOT_ENOUGH`` until the BUY has settled.
#
# The autofin adapter plans BUY + SL in the same submit batch,
# so the SL is always rejected on T+0 and T+1. Without this
# retry, the user has to manually place the SL on T+2 (or
# wait for the autofin strategy to re-plan the exit, which
# only happens when autofin publishes a TP step).
#
# ``retry_t2_settlement_sl_orders`` scans the ledger for SL
# SELL orders rejected with TRADE_QUANTITY_NOT_ENOUGH, checks
# if the underlying BUY has settled (BUY.sourceDate + 2
# calendar days <= today), and re-submits the SL. Called
# from the bot's start-of-day cycle and on every mid-day
# cycle.
_T2_REJECT_CODES = ("TRADE_QUANTITY_NOT_ENOUGH",)

# 2026-10-23: matches the ``[t2-retry from <date>]`` provenance
# marker that ``retry_t2_settlement_sl_orders`` appends to each
# retried order's reason. Stripped before deriving the retry's
# ``sourceOrderId`` so an already-retried order hashes to the SAME
# id on the next pass (otherwise the ledger dedup never matches
# and the bot re-submits the same dead SL every hour — see the
# function's docstring).
_T2_RETRY_MARKER_RE = re.compile(r"\s*\[t2-retry from [^\]]+\]")

# 2026-10-28: recovers the intended price of an auction-planned SL.
#
# An SL planned during an ATO/ATC auction window is submitted with
# price=0 (the exchange sets the fill price) and the intended price
# survives only in the reason string:
#
#   autofin_user_sl:deal#1792713600:ORS:10%:14400 [session=ATO signal_price=14400]
#
# The T+2 retry replays it as a LIMIT order, and DNSE rejects a limit
# order with price 0 — so without this recovery the retry fails on a
# price error and the position stays unprotected. We prefer the
# explicit ``signal_price=`` marker and fall back to the
# ``:<pct>:<price>`` segment of the autofin reason.
_SL_SIGNAL_PRICE_RE = re.compile(r"signal_price=([0-9][0-9_,]*(?:\.[0-9]+)?)")
_SL_REASON_PRICE_RE = re.compile(r":\d+(?:\.\d+)?%?:([0-9][0-9_,]*(?:\.[0-9]+)?)")


def _is_t2_rejection(error: str) -> bool:
    """Return True if the error indicates a T+2 settlement block.

    DNSE returns ``TRADE_QUANTITY_NOT_ENOUGH`` (HTTP 400) when
    the account doesn't yet have the shares to sell — most
    commonly because the BUY from T-0 / T-1 hasn't settled.
    T+2 settlement clears this for T-2+ BUYs.
    """
    if not error:
        return False
    return any(code in error for code in _T2_REJECT_CODES)


def _extract_deal_id_from_reason(reason: str) -> str | None:
    """Pull the ``deal#NNNNNNNNNN:SYM`` token from a reason string.

    Examples (autofin emits these via the strategy):
      - ``autofin_buy:deal#1785888000:BAF:nav30pct:Khuyến nghị đầu tư``
      - ``autofin_sl:deal#1785888000:BAF:27900``
      - ``autofin_user_sl:deal#1789344000:ABB:10%:15930``
    """
    if not reason:
        return None
    import re as _re
    m = _re.search(r"deal#(\d+:[A-Z]+)", reason)
    return m.group(1) if m else None


def _buy_settled_by_t2(
    buy_source_date: str,
    today_iso: str,
    now_time: "_time | None" = None,
) -> bool:
    """Return True if the BUY's T+2.5 settlement has cleared.

    Vietnam's settlement cycle is T+2.5 (the 2026-10-13 update
    from the user: "vn stock market using t+2.5"). The BUY
    at T+0 settles by T+2.5, meaning the SELL order for the
    position is accepted starting from 12:00 PM (noon) on
    T+2. So:
      - If today is buy_date + 3 or more: settled (T+3 onward).
      - If today is buy_date + 2 and current time >= 12:00:
        settled (T+2.5 reached).
      - If today is buy_date + 2 and current time < 12:00: NOT
        settled yet — wait until noon.
      - If today is buy_date + 1 or earlier: NOT settled.

    The half-day logic is important: empirically, DNSE rejected
    the simulated T+2 retry on 2026-10-15 (Thu, 2 calendar days
    after the 10/13 BUY) with TRADE_QUANTITY_NOT_ENOUGH, even
    though the buyer's local clock said "T+2 cleared". The
    settlement is the half-day-later T+2.5.

    When ``now_time`` is None, we assume it's already past noon
    (the conservative default — over-retrying one half-day is
    safer than under-retrying and leaving the user unprotected).
    """
    from datetime import date as _date, time as _time
    try:
        buy_date = _date.fromisoformat(buy_source_date)
        today = _date.fromisoformat(today_iso)
    except ValueError:
        return False
    days_diff = (today - buy_date).days
    if days_diff >= 3:
        return True
    if days_diff == 2:
        cutoff = _time(12, 0)
        if now_time is None:
            return True  # assume past noon
        return now_time >= cutoff
    return False


def _buy_filled_and_matches_sl(buy: Any, sl_quantity: int) -> bool:
    """Return True if the BUY is filled AND the filled quantity
    matches the SL quantity (so the SL has shares to actually sell).

    Skips SLs whose underlying BUY never filled (expired / cancelled
    / rejected), and SLs whose quantity doesn't match the actual
    filled BUY (e.g. the BUY was re-priced at a different size, or
    we re-entered a smaller position). The point: don't retry
    SLs that DNSE will reject for non-T+2 reasons.
    """
    if buy.status != "filled":
        return False
    # DNSE wire field is ``fillQuantity`` (no "-ed"); the helper
    # falls back through ``filledQuantity`` and ``matchedQuantity``
    # too. Reading the wrong field silently returned 0 for every
    # fill — see the 2026-10-13 fix that added the helper.
    fill_qty = filled_quantity(buy.lastDetail)
    return fill_qty == sl_quantity and fill_qty > 0


def retry_t2_settlement_sl_orders(
    client: "DnseClient",
    account_no: str,
    trading_token: str,
    data_dir: "Path",
    today_iso: str,
    now_time: "_time | None" = None,
    loan_package_id_by_symbol: dict[str, int] | None = None,
    loan_package_id: int = 0,
) -> dict:
    """Re-submit SL SELL orders that were rejected due to T+2.5 settlement.

    Returns a summary dict for the operator::

        {
            "scanned": int,         # total SL SELLs in the ledger
            "t2_blocked": int,      # SL SELLs rejected with T+2 error
            "t2_pending": int,      # T+2.5 not yet cleared (too soon)
            "t2_due": int,          # T+2.5 cleared, eligible for retry
            "resubmitted": int,     # actually re-submitted this call
            "resubmit_dnse_ids": [str],  # dnseOrderIds of re-submitted SLs
            "resubmit_errors": [str],    # any 4xx/5xx messages from re-submit
        }

    ``now_time`` is the current time-of-day (datetime.time). When
    None, the function assumes it's already past 12:00 PM (the
    T+2.5 cutoff). Pass the real time when calling from a loop
    that runs before noon — the function will then correctly hold
    off the retry until the half-day-later cutoff.

    The function is best-effort: errors in scanning or re-submit
    are caught and recorded but do not raise. The ledger is
    only updated by ``execute_orders_with_ledger`` (called
    internally for the re-submits), so the bot's order monitor
    will pick up the new SL entries on its next poll.

    ``loan_package_id`` (2026-10-22) is the launcher's
    ``--loan-package-id`` default. It is used as the FALLBACK when
    ``get_cash_loan_package_id`` can't find a ``type == "N"`` (cash)
    package for the symbol — which happens for every HOSE/HNX symbol
    (BAF, TLG, ...) because DNSE tags their cash package ("GD Tiền
    mặt", id 1775) with ``type="M"`` not ``type="N"``. Without this
    fallback the retry submits ``loanPackageId=0``, which DNSE rejects
    with ``HTTP 400 INVALID_INPUT loanPackageId is required`` — the
    exact error the operator reported on 2026-10-22 after 9 days of
    the same daily failure.
    """
    from vietnam_algo.libs.order_ledger import load_ledger
    from vietnam_algo.libs.daily_engine import execute_orders_with_ledger
    from vietnam_algo.libs.trading_engine import DnseOrder

    summary: dict[str, Any] = {
        "scanned": 0,
        "t2_blocked": 0,
        "t2_pending": 0,
        "t2_due": 0,
        "stale_no_position": 0,
        "stale_details": [],
        # 2026-10-28: SLs whose price had to be recovered from the
        # auction ``signal_price=`` marker, and SLs we could not price
        # at all (so no limit order was placed).
        "recovered_price": [],
        "no_price": [],
        "resubmitted": 0,
        "resubmit_dnse_ids": [],
        "resubmit_errors": [],
        "pkg_lookup_errors": [],
    }

    # Cache of symbol -> held qty so a position appearing multiple
    # times in the ledger (several SL variants per deal) only costs
    # ONE DNSE positions call per retry pass.
    _live_pos_cache: dict[str, int | None] = {}

    def _fetch_live_position_qty(cli, acct: str, sym: str) -> int | None:
        """Return the live DNSE position qty for ``sym``, or ``None``
        when the lookup is unavailable.

        ``None`` means "don't know" — the caller must then fall back
        to the historical behaviour (attempt the retry) rather than
        assume the position is gone. Only a successful lookup that
        returns 0 (or fewer shares than the SL asks for) is treated
        as proof the SL is dead.
        """
        if sym in _live_pos_cache:
            return _live_pos_cache[sym]
        result: int | None = None
        try:
            raw = cli.get_positions(acct)
            # DNSE returns ``{"positions": [...]}`` (verified live
            # 2026-10-23); older/alternate shapes return a bare list
            # or ``{"data": [...]}``. Accept all three.
            if isinstance(raw, list):
                rows = raw
            elif isinstance(raw, dict):
                rows = (
                    raw.get("positions")
                    or raw.get("data")
                    or []
                )
            else:
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("symbol") or "").upper() != sym.upper():
                    continue
                # 2026-10-23: DNSE's /positions rows do NOT carry a
                # ``quantity`` key — the live payload uses
                # ``tradeQuantity`` (net held) and ``openQuantity``.
                # Reading ``quantity`` yielded None → 0 and would
                # mark EVERY held position as flat, silently
                # disabling the protective stop on a live holding.
                # Verified against account 0003937868:
                #   ABB → tradeQuantity=100, openQuantity=100
                #   TLG → tradeQuantity=100, openQuantity=100
                #
                # 2026-10-28 fix: prefer ``openQuantity`` (shares we
                # hold) over ``tradeQuantity`` (the SETTLED net
                # quantity). The ORS position bought 2026-10-26
                # reported ``tradeQuantity=0, openQuantity=200``
                # because the shares had not finished settling. The
                # old preference for ``tradeQuantity`` therefore
                # read 0 and the T+2 retry classified the ORS SL as
                # "position already closed" — skipping the retry for
                # a position we DO hold, leaving it unprotected.
                #
                # ``openQuantity`` is the right field for "can I sell
                # these shares": it is non-zero as soon as the BUY
                # fills. We fall back to ``tradeQuantity`` for older
                # payloads that lack it, and to ``quantity`` last.
                raw_qty = None
                for field in ("openQuantity", "tradeQuantity", "quantity"):
                    if row.get(field) is not None:
                        raw_qty = row.get(field)
                        break
                try:
                    result = int(float(raw_qty or 0))
                except (TypeError, ValueError):
                    result = 0
                break
            else:
                # Symbol absent from the positions list == flat.
                result = 0
        except Exception:
            result = None
        _live_pos_cache[sym] = result
        return result

    try:
        ledger = load_ledger(data_dir)
    except Exception as exc:
        summary["resubmit_errors"].append(f"load_ledger failed: {exc}")
        return summary

    # Index BUY orders by deal_id (latest BUY wins; we use the
    # most recent BUY for the settlement date check).
    buy_by_did: dict[str, Any] = {}
    for item in ledger.values():
        if item.side != "NB":
            continue
        did = _extract_deal_id_from_reason(item.reason or "")
        if not did:
            continue
        # Keep the latest (most recent updatedAt) BUY per deal.
        prev = buy_by_did.get(did)
        if prev is None or (item.updatedAt or "") > (prev.updatedAt or ""):
            buy_by_did[did] = item

    # Scan SL SELL orders in the rejected set.
    candidates: list[tuple[Any, Any]] = []  # (sl_order, buy_order)
    for item in ledger.values():
        if item.side != "NS":
            continue
        if item.status != "rejected":
            continue
        if not _is_t2_rejection(item.error or ""):
            continue
        summary["t2_blocked"] += 1
        summary["scanned"] += 1
        did = _extract_deal_id_from_reason(item.reason or "")
        if not did:
            continue
        buy = buy_by_did.get(did)
        if buy is None:
            # No matching BUY found — the BUY is also rejected/never
            # placed, so there's nothing to SELL. Skip.
            continue
        if not _buy_settled_by_t2(buy.sourceDate or "", today_iso, now_time=now_time):
            summary["t2_pending"] += 1
            continue
        # T+2.5 cleared — but the BUY must have actually filled for
        # the SL to be valid. SLs whose BUY expired/cancelled
        # have no shares to sell; re-submitting them would
        # always fail with TRADE_QUANTITY_NOT_ENOUGH (and that's
        # the bug we're trying to avoid confusing the operator
        # about).
        if not _buy_filled_and_matches_sl(buy, item.quantity):
            continue
        # 2026-10-23 fix (retry loop on a sold-out position): the
        # gate above only proves the BUY filled — it says nothing
        # about whether we STILL HOLD those shares today. Once the
        # position was sold by another path (e.g. the 2026-10-21
        # autofin capital_protect exit that liquidated all 200 BAF),
        # every subsequent retry is guaranteed to fail with
        # ``TRADE_QUANTITY_NOT_ENOUGH / deal not found``, forever.
        #
        # We saw exactly that on 2026-10-23: 19 rejected BAF/ABB SL
        # rows in the ledger, all T+2-blocked from 2026-10-13, with
        # ZERO BAF remaining on the DNSE account. The bot faithfully
        # re-submitted the dead SL every hour and the Telegram alert
        # repeated 4 identical failures per cycle.
        #
        # Fix: query live positions once per call and drop any
        # candidate whose held qty is 0 (or less than the SL qty).
        # The SL is then recorded as stale in the summary instead of
        # being retried forever. Requires a live DNSE call; if the
        # lookup fails we keep the previous behaviour (retry) so a
        # transient outage doesn't silently disable protection.
        held = _fetch_live_position_qty(client, account_no, item.symbol)
        if held is not None and held < int(item.quantity or 0):
            summary["stale_no_position"] += 1
            summary["stale_details"].append(
                f"{item.symbol}: SL {int(item.quantity or 0)} @ {item.price:,.0f} "
                f"but holding {held} — position already closed, "
                f"skipping T+2 retry"
            )
            continue
        # T+2.5 cleared AND the BUY actually filled at the matching
        # qty AND we still hold the shares — eligible for retry.
        summary["t2_due"] += 1
        candidates.append((item, buy))

    if not candidates:
        return summary

    # Build DnseOrder records. Use a fresh sourceOrderId so
    # the re-submit isn't dedup'd against the original
    # rejected entry (which has the same key).
    #
    # The loan package id is preserved from the original SL
    # order — the same broker account, the same cash vs
    # margin package. The original SL was rejected purely
    # for the T+2 settlement timing, NOT for any
    # account/package issue, so we replay it with the same
    # parameters. If the original ledger row has no
    # loanPackageId (e.g. an older schema), we fall back
    # to looking up the cash pkg via DNSE.
    #
    # 2026-10-22 fix (T+2 SL retry loanPackageId=0 bug): when the
    # original ledger row's ``lastDetail`` is ``None`` (which is the
    # case for every DNSE 4xx rejection — ``place_stock_order``
    # raises before the order detail can be fetched, so the ledger
    # records ``lastDetail=None``), we cannot recover the original
    # ``loanPackageId``. The DNSE live lookup then hits the
    # ``get_cash_loan_package_id`` ``type == "N"`` filter, which
    # finds NOTHING for HOSE/HNX symbols like BAF and TLG because
    # DNSE tags their cash package ("GD Tiền mặt", id=1775) with
    # ``type="M"`` rather than ``type="N"``. ABB (UPCoM) is the
    # only case where DNSE returns ``type="N"``.
    #
    # Result: ``_resolve_pkg`` caught the RuntimeError and returned
    # ``0``, so the retry submitted ``loanPackageId=0`` — which DNSE
    # rejects with ``HTTP 400 INVALID_INPUT loanPackageId is
    # required``. The retry failed every cycle for days (the daily
    # Telegram "T+2 SL retry errors" alert the operator reported on
    # 2026-10-22).
    #
    # Fix: mirror the main submit path's ``_resolve_cash_pkg`` (see
    # line ~871) — on lookup failure, fall back to the launcher's
    # ``--loan-package-id`` (1775, which IS the correct "GD Tiền mặt"
    # cash id for BAF/TLG), never ``0``.
    import hashlib as _hl
    cash_pkg_cache: dict[str, int] = dict(loan_package_id_by_symbol or {})
    pkg_lookup_errors: list[str] = []
    fallback_pkg_id = int(loan_package_id or 0)

    def _resolve_pkg(sym: str, original_pkg: int) -> int:
        if original_pkg:
            return int(original_pkg)
        if sym in cash_pkg_cache:
            return cash_pkg_cache[sym]
        try:
            pkg = client.get_cash_loan_package_id(account_no, sym)
        except Exception as exc:
            # Record the lookup failure for the operator-visible error
            # list, then fall back to the launch-script default.
            # NEVER return 0 — DNSE rejects 0 with
            # "loanPackageId is required" and the retry never lands.
            pkg_lookup_errors.append(f"{sym}: {type(exc).__name__}: {str(exc)[:160]}")
            pkg = fallback_pkg_id
        cash_pkg_cache[sym] = int(pkg or 0)
        return cash_pkg_cache[sym]

    orders_to_resubmit: list[DnseOrder] = []
    for sl, buy in candidates:
        # 2026-10-23 fix (unbounded retry loop): strip any
        # ``[t2-retry from ...]`` markers already appended by a
        # PREVIOUS retry before deriving the sourceOrderId and the
        # new reason.
        #
        # Before this fix both derivations used ``sl.reason`` verbatim,
        # and the retry itself appended another marker to it. So each
        # hourly cycle produced:
        #   run 1: "autofin_sl:deal#X:27900"                       → id A
        #   run 2: "autofin_sl:deal#X:27900 [t2-retry from 10-13]" → id B
        #   run 3: "... [t2-retry from 10-13] [t2-retry from 10-13]" → id C
        # Because ``new_source_id`` hashed the growing reason string,
        # the ledger dedup key (run_id, sourceOrderId, symbol, side)
        # NEVER matched a previous attempt, so the bot re-submitted
        # the same dead SL every hour forever. The user saw the
        # error counter climb 8 → 10 → 12 → 14 → 16 across
        # 2026-10-23's cycles, and the Telegram alert repeated the
        # same 4 failures every hour.
        #
        # Normalising to the BASE reason makes the id stable for a
        # given (symbol, price, quantity, today) so the ledger dedup
        # suppresses the second and later attempts.
        base_reason = _T2_RETRY_MARKER_RE.sub("", sl.reason or "").strip()

        # 2026-10-28 fix (retrying an auction-priced SL at price 0).
        #
        # An SL planned during an ATO/ATC auction window is submitted
        # with price=0 (the exchange determines the fill price) and
        # the intended price is recorded only in the reason string:
        #
        #   autofin_user_sl:deal#...:ORS:10%:14400 [session=ATO signal_price=14400]
        #
        # The T+2 retry replays the ledger row's ``price`` — which is
        # 0 — but as a LIMIT order (``orderType="LO"``). DNSE rejects
        # a LO with price 0, so the retry would fail with a price
        # error instead of succeeding once the T+2.5 settlement
        # cleared. The position would stay UNPROTECTED.
        #
        # This is exactly the ORS position bought on 2026-10-26: its
        # SL was rejected with TRADE_QUANTITY_NOT_ENOUGH (T+2), and
        # the retry would have replayed it at price 0.
        #
        # Fix: recover the intended price from the ``signal_price=``
        # marker (or the ``:<pct>:<price>`` segment of the autofin
        # reason) and use it for the LO retry. The recovery is
        # recorded in the summary so the operator can see it.
        retry_price = int(sl.price or 0)
        if retry_price <= 0:
            _m = _SL_SIGNAL_PRICE_RE.search(base_reason)
            if _m:
                try:
                    retry_price = int(float(_m.group(1).replace(",", "")))
                except (TypeError, ValueError):
                    retry_price = 0
            if retry_price <= 0:
                # Fall back to the ``:<pct>:<price>`` segment of the
                # autofin reason, e.g.
                # ``autofin_user_sl:deal#X:ORS:10%:14400`` -> 14400.
                _m2 = _SL_REASON_PRICE_RE.search(base_reason)
                if _m2:
                    try:
                        retry_price = int(float(_m2.group(1).replace(",", "")))
                    except (TypeError, ValueError):
                        retry_price = 0
            if retry_price > 0:
                summary["recovered_price"].append(
                    f"{sl.symbol}: SL price 0 -> {retry_price:,.0f} "
                    f"(recovered from the auction signal_price marker)"
                )
        if retry_price <= 0:
            # No price to retry with — a limit order needs one. Skip
            # rather than submit a doomed order; the operator is told
            # via the summary.
            summary["no_price"].append(
                f"{sl.symbol}: SL has no recoverable price (ledger price 0 "
                f"and no signal_price marker) — cannot place a limit SL"
            )
            continue

        new_source_id = int(
            _hl.sha1(
                f"t2-retry|{today_iso}|{sl.symbol}|{retry_price}|{sl.quantity}|{base_reason}".encode()
            ).hexdigest()[:12],
            16,
        )
        # The original ledger row has loanPackageId in lastDetail.
        # Extract it; fall back to live lookup.
        original_pkg = 0
        if isinstance(sl.lastDetail, dict):
            original_pkg = int(sl.lastDetail.get("loanPackageId") or 0)
        pkg = _resolve_pkg(sl.symbol, original_pkg)
        orders_to_resubmit.append(DnseOrder(
            symbol=sl.symbol, side=sl.side, quantity=sl.quantity,
            price=retry_price,
            loanPackageId=pkg, orderType="LO",
            sourceOrderId=new_source_id, sourceDate=today_iso,
            reason=f"{base_reason} [t2-retry from {sl.sourceDate}]",
        ))

    # Surface the package-lookup failures to the operator (they are
    # NOT fatal — we fell back to ``loan_package_id`` — but they
    # explain WHY the fallback was needed and would show up as
    # "we used the launcher default because DNSE doesn't tag a
    # cash package for this symbol").
    summary["pkg_lookup_errors"] = list(pkg_lookup_errors)

    try:
        results, _reconciliation, _complete = execute_orders_with_ledger(
            client=client,
            account_no=account_no,
            trading_token=trading_token,
            orders=orders_to_resubmit,
            data_dir=data_dir,
            run_id=today_iso,
        )
    except Exception as exc:
        summary["resubmit_errors"].append(f"execute_orders_with_ledger: {exc}")
        return summary

    for r in results:
        status = str(r.get("status", ""))
        order = r.get("order")
        sym = getattr(order, "symbol", "?") if order is not None else "?"
        if status == "submitted":
            summary["resubmitted"] += 1
            detail = r.get("detail") or {}
            dnse_id = detail.get("id") if isinstance(detail, dict) else None
            if dnse_id:
                summary["resubmit_dnse_ids"].append(str(dnse_id))
        elif status == "rejected":
            err = str(r.get("error", ""))
            summary["resubmit_errors"].append(f"{sym}: {err[:120]}")
        elif status == "skipped_existing_ledger":
            summary["resubmit_errors"].append(
                f"{sym}: dedup'd against existing ledger entry"
            )
        else:
            summary["resubmit_errors"].append(f"{sym}: unexpected status {status!r}")
    return summary


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


# In-memory re-price throttle: maps ledger key → last-repriced-at epoch
# (seconds). Distinct from ``_ORDER_POLL_THROTTLE`` because the re-price
# cadence is independent of the status-poll cadence — the re-price only
# fires when the order is "stale" (open for >= ORDER_REPRICE_AFTER_SECONDS).
# The persisted ``lastRepricedAt`` field on LedgerOrder is the source of
# truth across bot restarts; this in-memory map is a faster check inside
# the same process.
_ORDER_REPRICE_THROTTLE: dict[str, float] = {}


# In-memory cache of "this order has been abandoned (cancelled after the
# cap was exceeded)". We cancel such orders in the order monitor so they
# don't sit on the books forever, but we also want to skip them on
# subsequent polls to avoid spamming the user.
_ORDER_REPRICE_ABANDONED: set[str] = set()


def _format_reprice_message(
    item: LedgerOrder,
    new_price: int,
    new_dnse_order_id: str,
    cancelled_dnse_order_id: str,
    cap_price: int,
    market_price: int,
    final_action: str = "replaced",
) -> str:
    """Build a Telegram message for a re-price attempt.

    ``final_action`` is one of:
      - ``"replaced"``: cancelled old, placed new at ``new_price``
      - ``"abandoned"``: cancelled old, market beyond cap so we skip

    The ``market_price`` is the live best-ask for BUY orders or
    the live best-bid for SELL orders. The Telegram message says
    "best-ask" or "best-bid" accordingly so the operator can tell
    which side of the book we were following.
    """
    is_buy = item.side == "NB"
    side_label = "BUY" if is_buy else "SELL"
    market_label = "best-ask" if is_buy else "best-bid"
    if final_action == "replaced":
        icon = "🔁"
        arrow = "↑" if is_buy else "↓"
        header = (
            f"{icon} Vietnam Algo: order {item.symbol} RE-PRICED "
            f"{item.price:,} {arrow} {new_price:,}"
        )
    else:
        icon = "🛑"
        cap_label = "above cap" if is_buy else "below cap"
        header = (
            f"{icon} Vietnam Algo: order {item.symbol} ABANDONED "
            f"({market_label} {cap_label})"
        )
    lines: list[str] = [
        header,
        f"  side: {side_label}",
        f"  qty: {item.quantity}",
        f"  old price: {item.price:,} (dnseOrderId={cancelled_dnse_order_id})",
    ]
    if final_action == "replaced":
        lines.append(f"  new price: {new_price:,} (dnseOrderId={new_dnse_order_id})")
    lines.append(f"  {market_label}: {market_price:,}")
    lines.append(f"  cap: {cap_price:,}")
    lines.append(f"  re-price #{item.repriceCount + 1}")
    return "\n".join(lines)


def _maybe_reprice_stale_order(
    client: "DnseClient | None",
    account_no: str,
    trading_token: str | None,
    item: LedgerOrder,
    *,
    now_epoch: float,
    reprice_after_seconds: int = 900,
    max_reprices: int = 1,
    reprice_tolerance_pct: float = 0.03,
) -> dict[str, Any]:
    """Re-price a stale, unfilled limit order to chase the live market.

    2026-10-13: the autofin bot placed ABB BUY 100 @ 17,500 on
    2026-10-12 14:48 ICT (live ask was 17,500 at that time).
    Over the next 12 minutes the ask drifted to 17,600 and
    stayed there; the order never filled and was expired at
    EOD. The user asked for a way to chase a rising market
    without runaway behaviour.

    2026-10-13 (later): user asked to apply the same logic to
    SELL orders. Symmetric behaviour: a SELL limit that nobody
    is hitting because the market is dropping gets re-priced
    DOWN to follow the bid. The risk is identical (we exit at
    a worse price than planned) and the cap is the same 3 %
    (BELOW the original price for SELL, ABOVE for BUY).

    Algorithm
    ---------
    1. **Eligibility**: side ∈ {NB, NS}, status = open,
       dnseOrderId set, original price > 0.
    2. **Staleness**: order has been open for >= ``reprice_after_seconds``
       (default 15 min). The timer starts at ``createdAt`` so it
       survives bot restarts.
    3. **Cap**:
         - BUY:  ``new_price <= original_price * (1 + tol)`` (3 % above)
         - SELL: ``new_price >= original_price * (1 - tol)`` (3 % below)
       The cap is symmetric and the same 3 % envelope.
    4. **Direction**:
         - BUY:  only re-price UP. If ``best_ask < item.price``, skip.
         - SELL: only re-price DOWN. If ``best_bid > item.price``, skip.
    5. **Within cap**: if the relevant side's price is beyond the cap
       (BUY ask > cap, SELL bid < cap), ABANDON (cancel + skip).
    6. **Count**: at most ``max_reprices`` re-prices per order
       (default 1). This is the kill-switch against runaway chasing.

    On a successful re-price:
      - Cancel the old order via ``client.cancel_order``
      - Look up the cash package id via
        ``client.get_cash_loan_package_id``
      - Place a new order at the new market-following price
      - Update the ledger entry: ``price`` = new price,
        ``repriceCount`` +1, ``lastRepricedAt`` = now ISO,
        ``originalPrice`` preserved from first submit
      - Send Telegram via ``_format_reprice_message``

    On an abandon (price beyond cap):
      - Cancel the old order
      - Update ledger: ``status`` = "cancelled", ``error`` = "reprice cap exceeded"
      - Send Telegram saying we abandoned

    Returns a dict for the cycle log:
        {
            "action": "replaced" | "abandoned" | "skipped",
            "reason": str (only on skip/abandon),
            "old_price": int,
            "new_price": int,
            "market_price": int,  # best_ask for BUY, best_bid for SELL
            "cap": int,
            "new_dnse_order_id": str,
            "cancelled_dnse_order_id": str,
            "side": "NB" | "NS",
        }
    """
    summary: dict[str, Any] = {
        "action": "skipped",
        "reason": "",
        "old_price": item.price,
        "new_price": item.price,
        "market_price": 0,
        "cap": 0,
        "new_dnse_order_id": "",
        "cancelled_dnse_order_id": "",
        "side": item.side,
    }
    # 1. Eligibility
    if item.side not in ("NB", "NS"):
        summary["reason"] = f"side {item.side!r} is not BUY or SELL"
        return summary
    if item.status != "open":
        summary["reason"] = f"status is {item.status!r}, not open"
        return summary
    if not item.dnseOrderId:
        summary["reason"] = "no dnseOrderId"
        return summary
    if max_reprices <= 0:
        summary["reason"] = "re-price disabled (max_reprices <= 0)"
        return summary
    if item.repriceCount >= max_reprices:
        summary["reason"] = f"re-price cap reached ({item.repriceCount} >= {max_reprices})"
        return summary
    if client is None:
        summary["reason"] = "no client"
        return summary
    if trading_token is None:
        summary["reason"] = "no trading token"
        return summary

    is_buy = item.side == "NB"

    # 2. Staleness
    original_price = item.originalPrice if item.originalPrice > 0 else item.price
    placed_epoch = _parse_iso_to_epoch(item.createdAt)
    if placed_epoch is None:
        summary["reason"] = f"createdAt unparseable: {item.createdAt!r}"
        return summary
    age_seconds = now_epoch - placed_epoch
    if age_seconds < reprice_after_seconds:
        summary["reason"] = f"order too young ({age_seconds:.0f}s < {reprice_after_seconds}s)"
        return summary

    # 3. Cap (symmetric for BUY/SELL)
    if is_buy:
        cap_price = int(original_price * (1.0 + reprice_tolerance_pct))
    else:
        cap_price = int(original_price * (1.0 - reprice_tolerance_pct))
    summary["cap"] = cap_price

    # 4. Fetch live quote (best_ask for BUY, best_bid for SELL)
    try:
        quote = client.get_latest_quote(item.symbol)
    except Exception as exc:
        summary["reason"] = f"get_latest_quote failed: {type(exc).__name__}: {str(exc)[:120]}"
        return summary
    if is_buy:
        market_price = _extract_best_ask_in_vnd(quote)
        market_label = "best-ask"
    else:
        market_price = _extract_best_bid_in_vnd(quote)
        market_label = "best-bid"
    summary["market_price"] = market_price if market_price is not None else 0
    if market_price is None or market_price <= 0:
        summary["reason"] = f"no {market_label} in quote"
        return summary

    # 5. Direction
    if is_buy:
        # Only re-price UP — if the market is at or below our limit,
        # the order has a chance to fill naturally.
        if market_price <= item.price:
            summary["reason"] = (
                f"best-ask {market_price} not above current limit {item.price}"
            )
            return summary
    else:
        # Only re-price DOWN — if the market is at or above our limit,
        # the order has a chance to fill naturally.
        if market_price >= item.price:
            summary["reason"] = (
                f"best-bid {market_price} not below current limit {item.price}"
            )
            return summary

    # 6. Within cap?
    if is_buy and market_price > cap_price:
        # Ask is above the cap — abandon the position.
        try:
            client.cancel_order(account_no, trading_token, item.dnseOrderId)
        except Exception as exc:
            summary["reason"] = f"abandon-cancel failed: {type(exc).__name__}: {str(exc)[:120]}"
            return summary
        summary["action"] = "abandoned"
        summary["cancelled_dnse_order_id"] = item.dnseOrderId
        summary["reason"] = (
            f"best-ask {market_price} > cap {cap_price}; cancelled and abandoned"
        )
        return summary
    if (not is_buy) and market_price < cap_price:
        # Bid is below the cap — abandon the position.
        try:
            client.cancel_order(account_no, trading_token, item.dnseOrderId)
        except Exception as exc:
            summary["reason"] = f"abandon-cancel failed: {type(exc).__name__}: {str(exc)[:120]}"
            return summary
        summary["action"] = "abandoned"
        summary["cancelled_dnse_order_id"] = item.dnseOrderId
        summary["reason"] = (
            f"best-bid {market_price} < cap {cap_price}; cancelled and abandoned"
        )
        return summary

    # 7. Re-price: cancel old, place new
    old_dnse_order_id = item.dnseOrderId
    try:
        client.cancel_order(account_no, trading_token, old_dnse_order_id)
    except Exception as exc:
        summary["reason"] = f"cancel failed: {type(exc).__name__}: {str(exc)[:120]}"
        return summary
    summary["cancelled_dnse_order_id"] = old_dnse_order_id

    # Look up the cash package id for the symbol. The runner already
    # caches these per symbol via the live-quote gate path; reuse the
    # same cache so we don't make a second /loan-packages call.
    try:
        cash_pkg_id = client.get_cash_loan_package_id(account_no, item.symbol)
    except Exception:
        # Fall back to 0 (cash) — server treats 0 as "no value" not
        # "no package" so this will fail. Better to use the launch-script
        # default or skip the re-price.
        cash_pkg_id = 0

    try:
        new_resp = client.place_stock_order(
            account_no=account_no,
            trading_token=trading_token,
            symbol=item.symbol,
            side=item.side,
            quantity=item.quantity,
            price=market_price,
            loan_package_id=cash_pkg_id,
            order_type="LO",
        )
    except Exception as exc:
        summary["reason"] = f"place_stock_order failed: {type(exc).__name__}: {str(exc)[:200]}"
        return summary

    new_order_id = ""
    new_order_status = "unknown"
    if isinstance(new_resp, dict):
        new_order_id = str(new_resp.get("id") or new_resp.get("orderId") or "")
        new_order_status_raw = new_resp.get("orderStatus") or new_resp.get("status") or ""
        new_order_status = normalize_ledger_status(str(new_order_status_raw))

    summary["action"] = "replaced"
    summary["new_price"] = market_price
    summary["new_dnse_order_id"] = new_order_id
    summary["new_status"] = new_order_status
    return summary


def _parse_iso_to_epoch(iso_str: str) -> float | None:
    """Parse an ISO-8601 timestamp string to a Unix epoch (seconds).

    Returns None if the string is empty, malformed, or has no timezone.
    Naive datetimes (no tz) are treated as UTC for stability.
    """
    if not iso_str:
        return None
    try:
        from datetime import datetime, timezone
        # Handle both ``Z`` and ``+00:00`` UTC suffixes.
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


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
        "repriced": [],  # list of {key, symbol, old_price, new_price, new_order_id}
        "abandoned": [],  # list of {key, symbol, cap, best_ask}
        # 2026-10-13: force_replan is True when an order transitioned to
        # a terminal-without-fill state (cancelled, rejected, expired)
        # AND the underlying deal is still relevant (not a "close"
        # type — we don't re-plan exits). The main loop uses this
        # flag to bypass the 1-hour mid-day-poll throttle so the
        # bot re-plans and re-submits within the next 60s cycle.
        "force_replan": False,
        # Symbols that triggered the force_replan — informational,
        # not used by the main loop but logged for the operator.
        "force_replan_symbols": [],
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

    # Re-price config (env-driven). All three have safe defaults so the
    # monitor keeps working even if the operator never sets them.
    # 2026-10-13: 15-min staleness window is the default — long enough
    # to absorb the typical "ask briefly went up then back" whipsaw
    # but short enough to fill orders before EOD on most days.
    reprice_after_seconds = int(os.environ.get("ORDER_REPRICE_AFTER_SECONDS", "900"))
    max_reprices = int(os.environ.get("ORDER_MAX_REPRICES", "1"))
    reprice_tolerance_pct = float(os.environ.get("ORDER_REPRICE_TOLERANCE_PCT", "0.03"))

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
            # 2026-10-13: when an order transitions to a terminal state
            # WITHOUT a fill, the underlying deal is still "interesting"
            # (the bot should retry). Mark ``force_replan = True`` so
            # the main loop can clear the strategy's ``last_poll_at``,
            # which lets the next mid-day poll (within ~60 s) re-plan
            # and re-submit the order. The ``_prune_terminal_unfilled_deals``
            # helper inside ``_load_state`` already releases the deal
            # from ``actioned_deals`` so the next ``generate_orders``
            # will see it as a fresh signal.
            #
            # We exclude ``filled`` (already done — no retry needed) and
            # ``closed`` (DNSE-internal end-of-day state). SELL "close"
            # orders that complete a position are also excluded because
            # we don't want to re-open a closed position.
            #
            # ``fillQuantity`` is on ``detail`` (the DNSE response),
            # not on ``LedgerOrder`` itself. If the order filled any
            # quantity before being cancelled/rejected/expired, the
            # strategy already owns those shares and re-planning could
            # double-buy; skip the force_replan in that case. The
            # helper reads ``fillQuantity`` first (DNSE's actual
            # wire field), then falls back to ``filledQuantity`` /
            # ``matchedQuantity`` for older payloads. Reading the
            # wrong field silently returned 0 for every partial fill
            # — see the 2026-10-13 fix.
            _fill_qty = filled_quantity(detail)
            if (
                new_status in {"cancelled", "rejected", "expired"}
                and _fill_qty == 0
            ):
                summary["force_replan"] = True
                sym = item.symbol
                if sym and sym not in summary["force_replan_symbols"]:
                    summary["force_replan_symbols"].append(sym)
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

        # 2026-10-13: re-price stale unfilled limit orders so they
        # can fill when the market drifts away.
        #   - BUY  (NB): chase the live best-ask UP, cap = 3 % above
        #     the original price.
        #   - SELL (NS): chase the live best-bid DOWN, cap = 3 %
        #     below the original price.
        # The cap (reprice_tolerance_pct) prevents runaway chasing of
        # a runaway market. The kill-switch (max_reprices) prevents
        # infinite re-pricing. The 15-min staleness window
        # (reprice_after_seconds) filters out short-term whipsaw.
        if (
            new_status == "open"
            and item.side in ("NB", "NS")
            and key not in _ORDER_REPRICE_ABANDONED
        ):
            reprice_summary = _maybe_reprice_stale_order(
                client, account_no, trading_token, item,
                now_epoch=now_epoch,
                reprice_after_seconds=reprice_after_seconds,
                max_reprices=max_reprices,
                reprice_tolerance_pct=reprice_tolerance_pct,
            )
            action = reprice_summary.get("action", "skipped")
            if action == "replaced":
                # Re-price succeeded. Update the ledger entry: new price,
                # new dnseOrderId, repriceCount+1, lastRepricedAt.
                summary["repriced"].append({
                    "key": key,
                    "symbol": item.symbol,
                    "side": item.side,
                    "old_price": reprice_summary["old_price"],
                    "new_price": reprice_summary["new_price"],
                    "market_price": reprice_summary["market_price"],
                    "new_order_id": reprice_summary["new_dnse_order_id"],
                })
                ledger[key] = LedgerOrder(**{
                    **item.__dict__,
                    "price": reprice_summary["new_price"],
                    "dnseOrderId": reprice_summary["new_dnse_order_id"],
                    "repriceCount": item.repriceCount + 1,
                    "lastRepricedAt": _utc_iso(),
                    "originalPrice": item.originalPrice if item.originalPrice > 0 else item.price,
                    "status": reprice_summary.get("new_status", "open"),
                    "error": "",
                    "updatedAt": _utc_iso(),
                })
                dirty = True
                _ORDER_REPRICE_THROTTLE[key] = now_epoch
                # Clear the polled-throttle so the next cycle picks up
                # the new order_id immediately (the new order needs
                # status polling too).
                _ORDER_POLL_THROTTLE.pop(key, None)
                try:
                    notify(_format_reprice_message(
                        item,
                        new_price=reprice_summary["new_price"],
                        new_dnse_order_id=reprice_summary["new_dnse_order_id"],
                        cancelled_dnse_order_id=reprice_summary["cancelled_dnse_order_id"],
                        cap_price=reprice_summary["cap"],
                        market_price=reprice_summary["market_price"],
                        final_action="replaced",
                    ))
                except Exception as exc:
                    summary["errors"].append({"key": key, "err": f"reprice notify failed: {str(exc)[:120]}"})
            elif action == "abandoned":
                # Market beyond cap — we cancelled the order. Mark
                # terminal so the monitor won't poll it again. Send
                # Telegram.
                summary["abandoned"].append({
                    "key": key,
                    "symbol": item.symbol,
                    "side": item.side,
                    "cap": reprice_summary["cap"],
                    "market_price": reprice_summary["market_price"],
                })
                ledger[key] = LedgerOrder(**{
                    **item.__dict__,
                    "status": "cancelled",
                    "error": reprice_summary.get("reason", "reprice cap exceeded"),
                    "updatedAt": _utc_iso(),
                })
                dirty = True
                _ORDER_REPRICE_ABANDONED.add(key)
                try:
                    notify(_format_reprice_message(
                        item,
                        new_price=reprice_summary.get("new_price", 0),
                        new_dnse_order_id="",
                        cancelled_dnse_order_id=reprice_summary["cancelled_dnse_order_id"],
                        cap_price=reprice_summary["cap"],
                        market_price=reprice_summary["market_price"],
                        final_action="abandoned",
                    ))
                except Exception as exc:
                    summary["errors"].append({"key": key, "err": f"abandon notify failed: {str(exc)[:120]}"})
            else:
                # Skipped — not stale, not eligible, or transient error.
                # Log to errors only if the reason is an error (e.g. a
                # get_latest_quote 5xx), otherwise silent skip.
                reason = reprice_summary.get("reason", "")
                if "failed" in reason.lower() or "error" in reason.lower():
                    summary["errors"].append({"key": key, "err": f"reprice-skip: {reason}"})

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
    # 2026-10-28: loop-scoped state for the shared-token watcher.
    # ``_last_adopted_token_issued_at`` lets us log only when the token
    # ACTUALLY changes (the cron writes a new token at 08:30 ICT, so
    # we want one line, not one per minute).
    # ``_last_stale_token_warn_min`` throttles the "token is STALE"
    # warning to once per hour, and ``_stale_token_alert_date``
    # ensures the Telegram alert fires at most once per ICT day.
    _last_adopted_token_issued_at: str | None = None
    _last_stale_token_warn_min: int | None = None
    _stale_token_alert_date: str = ""
    while True:
        if not first_iter:
            time.sleep(sleep_seconds)
        first_iter = False
        now = vietnam_now()
        today = local_date(now)
        state = load_state(engine_config.dataDir, strategy_name=strategy_name)
        token: str | None = None
        # 2026-10-25: adopt the shared trading token (written by the
        # 08:30-ICT ``run-get-otp`` cron) into the bot state at
        # STARTUP, not only inside the daily submit cycle.
        #
        # Before this, ``state.tradingToken`` stayed empty until the
        # ``should_submit_orders`` block ran, which happens once per
        # day at SOD. That left the live autofin bot operating with a
        # blank token for the rest of the session and relying on the
        # fragile lazy email-OTP refresh (see the hydration fix in
        # the submit cycle). Hydrating at startup means the state
        # always reflects the token we actually have, and the
        # freshness check in ``ensure_fresh_trading_token`` can do
        # its job from the very first cycle.
        if live_submit:
            # 2026-10-28: the shared-token hydration runs on EVERY
            # cycle (the whole block lives inside the ``while True``
            # loop, and ``load_state`` reloads the state each
            # iteration). That was fine when it always succeeded —
            # it just logged one line per minute. But when the
            # token goes STALE it logged the same "token is STALE"
            # warning every 60 seconds, all night: the 2026-10-27
            # run produced 1,300+ identical lines and buried every
            # other message.
            #
            # We now:
            #   * log the STALE warning at most once per hour (and
            #     notify Telegram once per day, so a missed 08:30-ICT
            #     cron is visible to the operator);
            #   * keep watching for the cron to refresh the token and
            #     adopt it as soon as it appears — the bot recovers
            #     on its own the next morning instead of needing a
            #     manual restart.
            try:
                if _hydrate_shared_token_into_state(
                    state, engine_config.dataDir,
                ):
                    _adopted_at = getattr(state, "tokenIssuedAt", "?")
                    if _adopted_at != _last_adopted_token_issued_at:
                        _last_adopted_token_issued_at = _adopted_at
                        print(
                            f"[{now.isoformat()}] adopted shared DNSE "
                            f"trading token (issued_at={_adopted_at})"
                        )
                        # Persist it so the token survives a restart
                        # and so the state file reflects the token we
                        # are actually using.
                        try:
                            save_state(
                                state, engine_config.dataDir,
                                strategy_name=strategy_name,
                            )
                        except Exception as _exc:
                            print(
                                f"[{now.isoformat()}] saving state after "
                                f"token hydration failed: {_exc}"
                            )
                else:
                    _sh = load_shared_trading_token(engine_config.dataDir)
                    if _sh:
                        _issued = str(_sh.get("issued_at") or "?")
                        _now_min = now.hour * 60 + now.minute
                        if (
                            _last_stale_token_warn_min is None
                            or _now_min - _last_stale_token_warn_min >= 60
                        ):
                            _last_stale_token_warn_min = _now_min
                            print(
                                f"[{now.isoformat()}] shared DNSE trading "
                                f"token is STALE (issued_at={_issued}) — the "
                                f"08:30-ICT get_otp cron may have missed its "
                                f"window; orders will be skipped until it "
                                f"refreshes"
                            )
                        # Notify at most once per ICT day so a missed
                        # cron is visible without spamming.
                        if _stale_token_alert_date != today:
                            _stale_token_alert_date = today
                            notify(
                                "🚨 Vietnam Algo: DNSE trading token is STALE\n"
                                f"account: {account_no}\n"
                                f"issued_at: {_issued}\n"
                                "The 08:30-ICT get_otp cron may have missed "
                                "its window. Orders will be SKIPPED (not "
                                "sent with a bad token) until the token "
                                "refreshes."
                            )
            except Exception as exc:
                print(
                    f"[{now.isoformat()}] shared token hydration failed: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )
        # 2026-10-21 BAF incident: ``token`` is normally assigned by
        # ``ensure_fresh_trading_token`` inside the
        # ``should_submit_orders`` block, but ``should_poll_strategy``
        # also reads it to gate live submission of mid-day partial
        # closes. When the SOD/EOD block is skipped (e.g. first cycle
        # of a fresh bot instance, before any daily-token refresh
        # has run), ``token`` was unbound — referencing it in the
        # poll path raised ``UnboundLocalError`` and silently
        # dropped a freshly-detected partial-close SELL on the
        # floor. Initialise to an empty string at the top of each
        # loop so the submit gate ``if ... and token`` evaluates
        # cleanly (the empty string is falsy, so we still skip
        # submission when no token has been issued yet).

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
            # 2026-10-13: when an order transitions to a terminal-without-fill
            # state (cancelled, rejected, expired), the strategy should
            # re-plan and re-submit on the next cycle — not wait for the
            # 1-hour mid-day-poll throttle. The order monitor flags this
            # via ``monitor_summary["force_replan"]``; we clear the
            # strategy's ``last_poll_at`` here so the next poll bypasses
            # the throttle. ``_prune_terminal_unfilled_deals`` (in
            # ``_load_state``) will already have released the deal from
            # ``actioned_deals`` so the re-plan picks it up.
            if monitor_summary.get("force_replan") and engine_config.strategy == "autofin_signal":
                try:
                    _state_path = engine_config.dataDir / engine_config.liveDirName / "autofin_signal_state.json"
                    if _state_path.exists():
                        import json as _json_force
                        _state = _json_force.loads(_state_path.read_text(encoding="utf-8"))
                        if _state.get("last_poll_at"):
                            _state["last_poll_at"] = None
                            _state_path.write_text(
                                _json_force.dumps(_state, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                            print(
                                f"[{now.isoformat()}] order monitor cleared "
                                f"last_poll_at for autofin_signal (force_replan "
                                f"symbols={monitor_summary.get('force_replan_symbols', [])}); "
                                f"next mid-day poll will re-plan and re-submit"
                            )
                except Exception as exc:
                    print(f"[{now.isoformat()}] force_replan clear failed: {type(exc).__name__}: {str(exc)[:200]}")
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
                #
                # 2026-10-25 fix (inverted condition): the hydration
                # was gated on ``if not live_submit`` — i.e. the
                # shared token was only adopted in DRY-RUN mode,
                # while the LIVE bot (the one that actually places
                # DNSE orders) skipped it entirely and fell through
                # to the lazy email-OTP refresh.
                #
                # That made the live autofin bot depend on a Gmail
                # OTP round-trip (``trading_token_from_email`` →
                # send-email-otp → wait 20s → poll Gmail → exchange
                # OTP) for EVERY submit cycle. That path fails
                # whenever the network tunnel flaps (the
                # ``407 Proxy Authentication Required`` we saw on
                # 2026-10-23) or the email poll races, and the
                # resulting failure surfaced as
                # ``HTTP 400 INVALID_TRADING_TOKEN`` on the order
                # itself — e.g. the KDC BUY on 2026-10-22 that never
                # reached DNSE.
                #
                # The cron job already writes a FRESH token to the
                # shared file at 08:30 ICT (verified working on
                # 2026-10-25). Adopting it in live mode is strictly
                # more reliable than the lazy refresh, so we now
                # hydrate in BOTH modes. ``ensure_fresh_trading_token``
                # still runs afterwards and only refreshes lazily if
                # the shared token is genuinely missing/stale — so
                # this cannot regress the single-bot deployment that
                # has no cron job.
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
            # 2026-10-13 bug fix: this branch used to assign
            # ``token = os.environ.get("VIETSTOCK_REQUEST_TOKEN"...)``
            # which silently SHADOWED the DNSE ``token`` defined at
            # line 2237. The CA-sync block ran in the same function
            # scope, so every subsequent use of ``token`` (T+2
            # retry, mid-day/EOD order submits) got the VIETSTOCK
            # value instead of the DNSE trading token — every
            # downstream DNSE call returned 401/403. Rename the
            # CA-sync variable to ``vs_token`` so the shadowing is
            # impossible regardless of which order the branches fire.
            vs_token = os.environ.get("VIETSTOCK_REQUEST_TOKEN", "")
            vs_cookie = os.environ.get("VIETSTOCK_COOKIE", "")
            try:
                result = sync_corporate_actions_incremental(
                    engine_config.dataDir,
                    request_verification_token=vs_token,
                cookie=vs_cookie,
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
            #
            # 2026-10-18: in LIVE-SUBMIT mode the bot fetches prices
            # and portfolio data directly from DNSE, so the local
            # price files are not used for the SOD briefing either.
            # Bypass the sentinel check when live_submit=True for
            # the same reason as the EOD path (autofin_signal.py
            # uses build_portfolio_snapshot() and get_latest_quote()
            # to read DNSE live, not the disk-cached prices).
            data_ready = True
            if not engine_config.syncData and not live_submit:
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

            # 2026-10-25: proactive DNSE trading-token health check.
            #
            # The token refresh is owned by the ``run-get-otp`` cron
            # (08:30 ICT). If that job misses its window (DST
            # fall-back on 2026-10-25 moved every hard-coded
            # CEST hour one hour later; a tunnel ``407`` flapped the
            # OTP round-trip), the token goes stale and the FIRST
            # symptom is an order rejected with
            # ``HTTP 400 INVALID_TRADING_TOKEN`` — the order is
            # simply lost, with no explanation.
            #
            # We now check the token at the start of the trading
            # day and alert loudly BEFORE any order is attempted,
            # so a missed refresh is diagnosed as "the token cron
            # didn't run" rather than "an order mysteriously
            # failed".
            if live_submit:
                try:
                    _shared = load_shared_trading_token(engine_config.dataDir)
                    from vietnam_algo.libs.dnse.auth import is_token_fresh as _is_fresh
                    _tok_ok = bool(
                        _shared
                        and _is_fresh(
                            _shared.get("token"),
                            issued_at=_shared.get("issued_at"),
                        )
                    )
                    if not _tok_ok:
                        _issued = (_shared or {}).get("issued_at") or "unknown"
                        notify(
                            "🚨 Vietnam Algo: DNSE trading token is NOT ready\n"
                            f"account: {account_no}\n"
                            f"shared token issued_at: {_issued}\n"
                            "The 08:30-ICT get_otp cron may have missed its "
                            "window. Orders placed now will be rejected with "
                            "INVALID_TRADING_TOKEN."
                        )
                        print(
                            f"[{now.isoformat()}] DNSE trading token NOT fresh "
                            f"(issued_at={_issued}) — orders may be rejected "
                            f"with INVALID_TRADING_TOKEN"
                        )
                    else:
                        print(
                            f"[{now.isoformat()}] DNSE trading token OK "
                            f"(issued_at={_shared.get('issued_at')})"
                        )
                except Exception as exc:
                    print(
                        f"[{now.isoformat()}] trading-token health check "
                        f"failed: {exc}"
                    )

            # 2026-10-13: T+2 settlement SL retry. Vietnam's T+2
            # settlement blocks SELL orders until 2 calendar days
            # after the BUY. The autofin adapter plans BUY + SL
            # together, so the SL is rejected on T+0 / T+1 with
            # HTTP 400 TRADE_QUANTITY_NOT_ENOUGH. Now that the
            # start-of-day has fired for today, scan the ledger
            # for any SL SELLs that were T+2-blocked and the
            # corresponding BUY is now settled (>=2 days old),
            # then re-submit them. Runs at SOD so the SL is in
            # place before the trading session starts.
            #
            # 2026-10-13 bug fix: the prior code used the local
            # ``token`` variable that was only assigned inside the
            # CA-sync branch above (``token = os.environ.get(
            # "VIETSTOCK_REQUEST_TOKEN"...)``). With
            # ``--no-sync-data`` (the new launcher default), that
            # branch is skipped, so ``token`` is unbound and the
            # T+2 retry path crashed the bot at SOD with
            # ``UnboundLocalError: local variable 'token'``. Even
            # when CA-sync ran, the variable held the VIETSTOCK
            # token (a non-empty string), so the gate passed
            # silently and the T+2 retry function was called with
            # the wrong token — every retry would fail at the DNSE
            # call with an HTTP 401/403. The fix is to load the
            # shared DNSE trading token from the same file the
            # order monitor uses (written by the get_otp cron).
            t2_trading_token = _load_dnse_trading_token_from_disk(
                engine_config.dataDir,
            )
            if live_submit and t2_trading_token and client is not None:
                try:
                    t2_summary = retry_t2_settlement_sl_orders(
                        client=client,
                        account_no=account_no,
                        trading_token=t2_trading_token,
                        data_dir=engine_config.dataDir,
                        today_iso=today,
                        now_time=now.time(),
                        loan_package_id_by_symbol=None,
                        loan_package_id=loan_package_id,
                    )
                    if t2_summary.get("t2_blocked") or t2_summary.get("resubmit_errors"):
                        print(
                            f"[{now.isoformat()}] t2-sl-retry: "
                            f"scanned={t2_summary['scanned']} "
                            f"blocked={t2_summary['t2_blocked']} "
                            f"pending={t2_summary['t2_pending']} "
                            f"due={t2_summary['t2_due']} "
                            f"resubmitted={t2_summary['resubmitted']} "
                            f"errors={len(t2_summary['resubmit_errors'])}"
                        )
                    if t2_summary.get("resubmitted"):
                        notify(
                            "\n".join([
                                "🔁 Vietnam Algo: T+2 SL retry (start-of-day)",
                                f"resubmitted: {t2_summary['resubmitted']}",
                                f"dnseOrderIds: {t2_summary['resubmit_dnse_ids']}",
                                f"still pending: {t2_summary['t2_pending']}",
                            ])
                        )
                    if t2_summary.get("resubmit_errors"):
                        notify(
                            "⚠️ Vietnam Algo: T+2 SL retry errors:\n"
                            + "\n".join(t2_summary["resubmit_errors"][:5])
                        )
                    # 2026-10-23: tell the operator when an SL was
                    # dropped because we no longer hold the shares,
                    # so a dead protective order doesn't silently
                    # vanish from the report.
                    if t2_summary.get("stale_no_position"):
                        notify(
                            "ℹ️ Vietnam Algo: T+2 SL retry skipped "
                            f"{t2_summary['stale_no_position']} order(s) — "
                            "position already closed (no shares to protect):\n"
                            + "\n".join(t2_summary["stale_details"][:5])
                        )
                    # 2026-10-28: report SLs whose price had to be
                    # recovered from the auction marker, and SLs we
                    # could not price at all (so NO protective order
                    # was placed — the position is unprotected).
                    if t2_summary.get("recovered_price"):
                        notify(
                            "ℹ️ Vietnam Algo: T+2 SL retry recovered a price\n"
                            + "\n".join(t2_summary["recovered_price"][:5])
                        )
                    if t2_summary.get("no_price"):
                        notify(
                            "🚨 Vietnam Algo: T+2 SL retry could NOT place a "
                            "stop-loss (no price)\n"
                            "The position is UNPROTECTED — place the SL "
                            "manually:\n"
                            + "\n".join(t2_summary["no_price"][:5])
                        )
                except Exception as exc:
                    print(f"[{now.isoformat()}] t2-sl-retry crashed: {type(exc).__name__}: {str(exc)[:200]}")

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
                    # 2026-10-21 BAF incident: the mid-day poll
                    # path used to gate on ``and token``, but the
                    # trading token is normally populated only inside
                    # the ``should_submit_orders`` block — which fires
                    # once per day at SOD. Outside that window
                    # ``token`` was None (the UnboundLocalError fix
                    # above masks the absence by initialising it to
                    # ``None``), so mid-day partial-close SELLs were
                    # silently skipped. Re-hydrate from the shared
                    # cron-written token file before this check —
                    # ``load_shared_trading_token`` reads
                    # ``data/live/dnse_trading_token.json`` which the
                    # ``get_otp`` cron @ 08:30 ICT refreshes daily.
                    if live_submit and not token:
                        try:
                            shared = load_shared_trading_token(
                                engine_config.dataDir,
                            )
                            # 2026-10-25: check FRESHNESS before adopting
                            # the shared token. We previously took
                            # whatever was in the file — so on a day
                            # when the 08:30-ICT get_otp cron missed
                            # its window (DST fall-back, tunnel 407)
                            # the mid-day path would happily adopt the
                            # STALE token and every submit would be
                            # rejected with INVALID_TRADING_TOKEN.
                            # Adopting a stale token is worse than
                            # having no token: the latter makes the
                            # submit path skip the order loudly, the
                            # former makes it fail opaquely.
                            from vietnam_algo.libs.dnse.auth import (
                                is_token_fresh as _is_fresh,
                            )
                            if shared and shared.get("token") and _is_fresh(
                                shared.get("token"),
                                issued_at=shared.get("issued_at"),
                            ):
                                token = str(shared["token"])
                                print(
                                    f"[{now.isoformat()}] mid-day "
                                    f"poll hydrated trading token from "
                                    f"shared file (issued_at="
                                    f"{shared.get('issued_at','?')})"
                                )
                            elif shared and shared.get("token"):
                                print(
                                    f"[{now.isoformat()}] mid-day poll: "
                                    f"shared trading token is STALE "
                                    f"(issued_at="
                                    f"{shared.get('issued_at','?')}) — not "
                                    f"adopting it; orders will be skipped "
                                    f"rather than rejected with "
                                    f"INVALID_TRADING_TOKEN"
                                )
                        except Exception as exc:
                            print(
                                f"[{now.isoformat()}] mid-day poll "
                                f"token hydrate failed: "
                                f"{type(exc).__name__}: {str(exc)[:200]}"
                            )
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
                    # 2026-10-13: T+2 SL retry on every mid-day cycle.
                    # The start-of-day retry catches most cases, but
                    # BUYs placed mid-day (e.g. 10:00 ICT) won't
                    # have their SL retried at next-day's SOD
                    # because the BUY was today, not yesterday.
                    # A daily mid-day check is the safe cadence.
                    #
                    # See SOD fix above — read the DNSE token
                    # directly from disk instead of the local
                    # ``token`` (which is the VIETSTOCK token, set
                    # only when CA sync runs).
                    t2_trading_token = _load_dnse_trading_token_from_disk(
                        engine_config.dataDir,
                    )
                    if live_submit and t2_trading_token and client is not None:
                        try:
                            t2_summary = retry_t2_settlement_sl_orders(
                                client=client,
                                account_no=account_no,
                                trading_token=t2_trading_token,
                                data_dir=engine_config.dataDir,
                                today_iso=today,
                                now_time=now.time(),
                                loan_package_id_by_symbol=None,
                                loan_package_id=loan_package_id,
                            )
                            if t2_summary.get("resubmitted") or t2_summary.get("resubmit_errors"):
                                print(
                                    f"[{now.isoformat()}] t2-sl-retry (mid-day): "
                                    f"resubmitted={t2_summary['resubmitted']} "
                                    f"pending={t2_summary['t2_pending']} "
                                    f"stale_no_position={t2_summary.get('stale_no_position', 0)} "
                                    f"errors={len(t2_summary['resubmit_errors'])}"
                                )
                                if t2_summary.get("resubmitted"):
                                    notify(
                                        "🔁 Vietnam Algo: T+2 SL retry (mid-day)\n"
                                        f"resubmitted: {t2_summary['resubmitted']}\n"
                                        f"dnseOrderIds: {t2_summary['resubmit_dnse_ids']}"
                                    )
                        except Exception as exc:
                            print(f"[{now.isoformat()}] t2-sl-retry mid-day crashed: {type(exc).__name__}: {str(exc)[:200]}")
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
            # When this bot doesn't own data sync AND is in dry-run
            # mode, wait for the data-sync owner to finish refreshing
            # prices + CA before we proceed. We poll for a sentinel
            # file ``data_ready_for_eod`` written by the owner.
            #
            # 2026-10-18: in LIVE-SUBMIT mode the bot fetches prices
            # and portfolio data directly from DNSE (see
            # autofin_signal.py:3108 and portfolio.py:120), so the
            # local price files are not used for sizing or P&L. The
            # sentinel check is irrelevant — and worse, it can block
            # the EOD indefinitely when the sentinel is stale (the
            # 2026-10-13 to 2026-10-18 incidents where the cron
            # timezone bug kept the sentinel >60 min old, so the
            # autofin bot's EOD never fired for 3 days even though it
            # could have read fresh prices from DNSE). Bypass the
            # check entirely when live_submit=True.
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
            if not engine_config.syncData and not live_submit:
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
                # 2026-10-13 bug: the original ``tz=_tz2`` passed the
                # ``timezone`` class instead of an instance, raising
                # ``TypeError: tzinfo argument must be None or of a
                # tzinfo subclass, not type 'type'``. The correct call
                # is ``_dt2.now(tz=timezone.utc)`` — we want the UTC
                # zone, not the local zone. The downstream consumers
                # (momentum bot, dashboard) parse this as an ISO-8601
                # string and rely on the trailing ``+00:00`` to know
                # the stamp is UTC.
                _stamp_payload = _dt2.now(tz=_tz2.utc).isoformat()
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
                # 2026-10-21 incident (MST): the backtest routinely
                # produced ``count: 0`` orders when the price-feed /
                # universe was stale (e.g., MST's price file was 67+
                # days old, so the backtest never reached 2026-10-21
                # to plan a SELL). Pre-fix, the operator saw the
                # routine "No orders today" line and assumed the bot
                # was healthy — meanwhile the dry-run state held a
                # 9,500-share MST position it couldn't reprice.
                # We now emit a dedicated header line in the EOD
                # summary whenever the strategy emitted 0 orders
                # BUT still holds positions in dry-run state whose
                # latest price file is stale. This pins the silent
                # staleness to the operator's daily report instead of
                # relying on a separate ``dataStaleAlertSentDate``
                # path that only fires once per day.
                if (
                    isinstance(result, dict)
                    and result.get("count", 0) == 0
                    and not live_submit
                    and portfolio_snap is not None
                ):
                    held = portfolio_snap.get("positions") or []
                    held_symbols = sorted(
                        {p.get("symbol", "") for p in held if p.get("symbol")}
                    )
                    if held_symbols:
                        try:
                            stale_held_now = freshness.get(
                                "stale_held_positions"
                            ) or []
                        except (NameError, UnboundLocalError):
                            stale_held_now = []
                        stale_names = sorted(
                            {h.get("symbol") for h in stale_held_now}
                        )
                        if stale_names:
                            # Only the intersection with currently-held
                            # positions is actionable.
                            held_stale_now = sorted(
                                set(held_symbols) & set(stale_names)
                            )
                            if held_stale_now:
                                _alert_lines: list[str] = [
                                    "⚠️ Vietnam Algo: EOD emitted 0 orders "
                                    "but you STILL HOLD stale-priced positions:",
                                    f"  date: {today}",
                                    "  stale + held (action required):",
                                ]
                                for sym in held_stale_now[:10]:
                                    _alert_lines.append(f"    • {sym}")
                                if len(held_stale_now) > 10:
                                    _alert_lines.append(
                                        f"    + {len(held_stale_now) - 10} more"
                                    )
                                _alert_lines.append(
                                    "  fix: refresh the price file via "
                                    "vietnam_algo.libs.data.sync_prices_incremental"
                                )
                                notify("\n".join(_alert_lines))
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
                    sentinel_td = freshness.get("sentinel_age_trading_days")
                    median_age = freshness.get("median_latest_bar_age_days")
                    parts = [
                        "⚠️ Vietnam Algo: market data STALE",
                        f"strategy: {strategy_name or engine_config.strategy or '?'}",
                        f"date: {today}",
                    ]
                    if sentinel_td is not None:
                        # 2026-10-25: the freshness gate counts TRADING
                        # days (weekends + Vietnamese holidays don't
                        # count), so a Friday-stamped sentinel stays
                        # fresh through the weekend. The raw wall-clock
                        # age is still shown for context — it's what
                        # most operators intuit when they see "43h".
                        parts.append(
                            f"data_ready_for_eod age: {sentinel_td} trading-days "
                            f"(threshold 2d)"
                        )
                        if sentinel_age is not None:
                            parts.append(
                                f"data_ready_for_eod wall-clock age: {sentinel_age}h"
                            )
                    elif sentinel_age is not None:
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
                    # 2026-10-21 incident (MST): when the freshness
                    # sample is fresh but a HELD POSITION is stale,
                    # operators need to know which symbol + its
                    # price-file mtime / last-bar date so they can
                    # decide whether to (a) refresh the price file
                    # manually, or (b) close the position because
                    # repricing requires it. Without this list, the
                    # ``fresh: False`` alert just says "median is
                    # stale" — the operator has to grep the dry-run
                    # state to find which position is at risk.
                    stale_held = freshness.get("stale_held_positions") or []
                    if stale_held:
                        parts.append("")
                        parts.append(
                            f"stale HELD POSITIONS (action required "
                            f"if still open today, count={len(stale_held)}):"
                        )
                        for entry in stale_held[:10]:
                            sym = entry.get("symbol", "?")
                            reason = entry.get("reason")
                            last_bar = entry.get("last_bar_date")
                            td_age = entry.get("age_trading_days")
                            cd_age = entry.get("age_calendar_days")
                            extras: list[str] = []
                            if reason == "no_price_file":
                                extras.append("NO PRICE FILE")
                            else:
                                if last_bar:
                                    extras.append(f"last bar: {last_bar}")
                                if td_age is not None:
                                    extras.append(
                                        f"{td_age} trading-days old"
                                    )
                                if cd_age is not None:
                                    extras.append(
                                        f"{cd_age} calendar-days old"
                                    )
                            parts.append(f"  • {sym} — " + ", ".join(extras))
                        if len(stale_held) > 10:
                            parts.append(f"  + {len(stale_held) - 10} more")
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
