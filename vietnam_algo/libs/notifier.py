from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TelegramConfig:
    botToken: str
    chatId: str
    enabled: bool = True


def telegram_config_from_env() -> TelegramConfig | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    enabled = os.environ.get("TELEGRAM_NOTIFICATIONS", "1").strip() not in {"0", "false", "False", "no"}
    if not token or not chat_id or not enabled:
        return None
    return TelegramConfig(token, chat_id, enabled=True)


def send_telegram_message(config: TelegramConfig, text: str, timeout: float = 10.0) -> dict[str, Any]:
    if not config.enabled:
        return {"ok": False, "skipped": True, "reason": "telegram_disabled"}
    url = f"https://api.telegram.org/bot{config.botToken}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": config.chatId,
            "text": text[:3900],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8", errors="replace")
    return json.loads(payload) if payload else {"ok": True}


def notify(text: str, config: TelegramConfig | None = None) -> dict[str, Any] | None:
    config = config or telegram_config_from_env()
    if not config:
        return None
    try:
        return send_telegram_message(config, text)
    except Exception as exc:
        print(f"Telegram notification failed: {exc}")
        return {"ok": False, "error": str(exc)}


def format_money(value: Any) -> str:
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def format_percent(value: Any) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return str(value)


def order_summary(orders: list[dict[str, Any]], limit: int = 10) -> str:
    lines: list[str] = []
    for order in orders[:limit]:
        lines.append(
            f"{order.get('side')} {order.get('symbol')} qty={order.get('quantity')} price={format_money(order.get('price'))} reason={order.get('reason')}"
        )
    if len(orders) > limit:
        lines.append(f"... and {len(orders) - limit} more")
    return "\n".join(lines) if lines else "No orders"


def nested_value(payload: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(payload, dict):
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
        for value in payload.values():
            nested = nested_value(value, keys)
            if nested not in (None, ""):
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = nested_value(item, keys)
            if nested not in (None, ""):
                return nested
    return None


def order_status_from_payload(payload: Any) -> str:
    value = nested_value(payload, ("status", "orderStatus", "state"))
    return str(value).upper() if value not in (None, "") else "UNKNOWN"


def order_execution_line(result: dict[str, Any]) -> str:
    order = result.get("order") if isinstance(result.get("order"), dict) else {}
    events = result.get("events") if isinstance(result.get("events"), list) else []
    response = result.get("response")
    status = str(result.get("status") or "").upper()
    reprice_count = sum(1 for event in events if isinstance(event, dict) and event.get("event") == "replaced")
    cancelled_unfilled = any(isinstance(event, dict) and event.get("event") == "cancelled_unfilled" for event in events)
    monitor_exhausted = any(isinstance(event, dict) and event.get("event") == "monitor_exhausted_unfilled" for event in events)
    final_detail: Any = response
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        if event.get("event") == "cancelled_unfilled":
            final_detail = event.get("detail") or event.get("response") or event
            status = "CANCELLED_UNFILLED"
            break
        if event.get("event") == "monitor_exhausted_unfilled":
            final_detail = event.get("detail") or event
            status = "UNFILLED_AFTER_REPRICE_LIMIT"
            break
        if event.get("event") == "checked":
            final_detail = event.get("detail") or event
            status = order_status_from_payload(final_detail)
            break
        if event.get("event") == "submitted" and final_detail is None:
            final_detail = event.get("response")
    if cancelled_unfilled:
        status = "CANCELLED_UNFILLED"
    elif monitor_exhausted:
        status = "UNFILLED_AFTER_REPRICE_LIMIT"
    elif status in {"", "SUBMITTED"} and final_detail is not None:
        status = order_status_from_payload(final_detail)
        if status == "UNKNOWN" and result.get("status"):
            status = str(result.get("status")).upper()
    matched_qty = nested_value(final_detail, ("matchedQuantity", "filledQuantity", "matchQuantity", "filledQty", "matchedQty"))
    avg_price = nested_value(final_detail, ("avgPrice", "averagePrice", "matchedPrice", "filledPrice"))
    parts = [
        f"{order.get('side')} {order.get('symbol')} qty={order.get('quantity')} price={format_money(order.get('price'))}",
        f"status={status}",
    ]
    if matched_qty not in (None, ""):
        parts.append(f"matchedQty={matched_qty}")
    if avg_price not in (None, ""):
        parts.append(f"avgPrice={format_money(avg_price)}")
    if reprice_count:
        parts.append(f"repriceCount={reprice_count}")
    return " | ".join(parts)


def unfilled_warning_lines(submit_results: list[Any]) -> list[str]:
    warnings: list[str] = []
    for item in submit_results:
        if not isinstance(item, dict):
            continue
        events = item.get("events") if isinstance(item.get("events"), list) else []
        has_warning = any(
            isinstance(event, dict) and event.get("event") in {"monitor_exhausted_unfilled", "cancelled_unfilled"}
            for event in events
        )
        if has_warning:
            warnings.append(order_execution_line(item))
    return warnings


def execution_summary(submit_results: list[Any], limit: int = 10) -> str:
    lines: list[str] = []
    for item in submit_results[:limit]:
        if isinstance(item, dict):
            lines.append(order_execution_line(item))
    if len(submit_results) > limit:
        lines.append(f"... and {len(submit_results) - limit} more")
    return "\n".join(lines) if lines else "No submit results"


def daily_generation_message(result: dict[str, Any]) -> str:
    metrics = result.get("metrics", {}) if isinstance(result.get("metrics"), dict) else {}
    return "\n".join(
        [
            "Vietnam Algo: orders generated",
            f"status: {result.get('status', 'generated')}",
            f"tradeDate: {result.get('tradeDate')}",
            f"orderCount: {result.get('count')}",
            f"totalReturn: {format_percent(metrics.get('totalReturnPercent'))}",
            f"maxDrawdown: {format_percent(metrics.get('maxDrawdownPercent'))}",
            f"finalEquity: {format_money(metrics.get('finalEquity'))}",
            f"ordersFile: {result.get('latestOrdersCsv')}",
        ]
    )


def submit_message(result: dict[str, Any]) -> str:
    submit_results = result.get("submitResults", []) if isinstance(result.get("submitResults"), list) else []
    warnings = unfilled_warning_lines(submit_results)
    warning_section = "\n".join(["WARNING: unfilled orders after reprice limit", *warnings]) if warnings else "unfilledWarnings: 0"
    return "\n".join(
        [
            "Vietnam Algo: submit completed",
            f"status: {result.get('status', 'submitted')}",
            f"tradeDate: {result.get('tradeDate')}",
            f"submittedCount: {len(submit_results)}",
            f"corporateActionDecisions: {len(result.get('corporateActionDecisions', []) or [])}",
            f"sellableDecisions: {len(result.get('sellableDecisions', []) or [])}",
            f"pricingDecisions: {len(result.get('pricingDecisions', []) or [])}",
            execution_summary(submit_results),
            warning_section,
        ]
    )


def order_failures_message(submit_results: list[Any], trade_date: str | None = None) -> str | None:
    """Build a Telegram message for orders that failed to submit.

    Returns None when there are no failures (so the caller can skip notify()).
    """
    if not submit_results:
        return None
    failures = [r for r in submit_results if isinstance(r, dict) and r.get("status") in {"unknown", "error"}]
    if not failures:
        return None
    lines = ["🚨 Vietnam Algo: ORDER FAILURES"]
    if trade_date:
        lines.append(f"tradeDate: {trade_date}")
    lines.append(f"failureCount: {len(failures)}")
    for f in failures[:10]:
        order = f.get("order", {}) if isinstance(f.get("order"), dict) else {}
        err = (f.get("error") or "").split("\n")[0]
        lines.append(
            f"  - {order.get('side')} {order.get('symbol')} qty={order.get('quantity')} "
            f"price={order.get('price')} reason={order.get('reason')}\n    err: {err[:200]}"
        )
    if len(failures) > 10:
        lines.append(f"  ... and {len(failures) - 10} more failures")
    return "\n".join(lines)


def risk_block_message(result: dict[str, Any]) -> str:
    """Notify when the live-risk gate rejected a submit cycle."""
    violations = result.get("riskViolations", []) or []
    return "\n".join(
        [
            "🚨 Vietnam Algo: SUBMIT BLOCKED BY LIVE-RISK GATE",
            f"tradeDate: {result.get('tradeDate')}",
            f"violationCount: {len(violations)}",
            *[
                f"  - {v.get('symbol')} {v.get('side')} qty={v.get('quantity')} "
                f"price={v.get('price')} :: {v.get('reason')}"
                for v in violations[:10]
            ],
        ]
    )


def ca_forced_exit_message(decisions: list[Any], trade_date: str | None = None) -> str | None:
    """Build a Telegram message listing CA-driven forced exits.

    Returns None when no forced exits exist.
    """
    if not decisions:
        return None
    exits = [
        d for d in decisions
        if isinstance(d, dict) and (
            d.get("action") in {"forced_exit_added", "forced_exit_emitted"}
            or d.get("reason") == "corporate_action_forced_exit"
        )
    ]
    if not exits:
        return None
    lines = ["🚨 Vietnam Algo: CA FORCED EXITS"]
    if trade_date:
        lines.append(f"tradeDate: {trade_date}")
    lines.append(f"exitCount: {len(exits)}")
    for d in exits[:10]:
        events = d.get("events") or []
        ev_titles = []
        for e in events[:3]:
            if isinstance(e, dict):
                ev_titles.append(f"{e.get('symbol')}:{e.get('name', '')}")
        lines.append(
            f"  - {d.get('symbol')} {d.get('side')} action={d.get('action')} reason={d.get('reason')} events={', '.join(ev_titles)}"
        )
    if len(exits) > 10:
        lines.append(f"  ... and {len(exits) - 10} more")
    return "\n".join(lines)


def token_refresh_message(account_no: str, live_submit: bool, ok: bool) -> str:
    status = "✅ refreshed" if ok else "❌ FAILED"
    label = "live" if live_submit else "dry-run (no DNSE call)"
    return "\n".join(
        [
            f"Vietnam Algo: DNSE trading token {status}",
            f"account: {account_no}",
            f"mode: {label}",
        ]
    )


def heartbeat_message(
    account_no: str,
    live_submit: bool,
    state: dict[str, Any] | None,
    proxy_url: str | None,
    proxy_check: dict[str, Any] | None,
    pid: int | None = None,
    uptime_seconds: float | None = None,
    freshness: dict[str, Any] | None = None,
) -> str:
    """Daily "I'm alive" notification with current bot status snapshot.

    Includes:
      - Bot uptime (process start time)
      - Last successful cycle dates (token, submit, EOD, CA sync, market holidays)
      - Proxy configuration and (optional) last connectivity check
      - Market-data freshness (sentinel age + sample-symbol bar ages)
      - Account + mode
    """
    lines = ["💓 Vietnam Algo: DAILY HEARTBEAT"]

    # uptime
    if pid is not None:
        lines.append(f"pid: {pid}")
    if uptime_seconds is not None:
        hours, rem = divmod(int(uptime_seconds), 3600)
        minutes, secs = divmod(rem, 60)
        days, hours = divmod(hours, 24)
        if days > 0:
            uptime_str = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            uptime_str = f"{hours}h {minutes}m"
        else:
            uptime_str = f"{minutes}m {secs}s"
        lines.append(f"uptime: {uptime_str}")

    lines.append(f"account: {account_no}")
    lines.append(f"mode: {'live' if live_submit else 'dry-run'}")

    # last cycle dates
    if isinstance(state, dict):
        for key, label in [
            ("marketHolidaySyncDate", "market-holidays"),
            ("tokenDate", "trading-token"),
            ("orderSubmitDate", "submit"),
            ("eodDate", "EOD"),
            ("corporateActionDate", "CA-sync"),
            ("lastHeartbeatDate", "heartbeat"),
        ]:
            lines.append(f"last-{label}: {state.get(key) or '(none yet)'}")

    # market-data freshness
    if freshness is not None:
        fresh = freshness.get("fresh")
        sentinel_age = freshness.get("sentinel_age_hours")
        median_age = freshness.get("median_latest_bar_age_days")
        sentinel_mtime = freshness.get("sentinel_mtime")
        if fresh is True:
            lines.append("data: fresh")
        elif fresh is False:
            bits = []
            sentinel_td = freshness.get("sentinel_age_trading_days")
            if sentinel_td is not None:
                # 2026-10-25: trading-day age is the gate (weekends
                # don't count). Raw hours shown alongside for context.
                bits.append(f"sentinel={sentinel_td}d-trading")
                if sentinel_age is not None:
                    bits.append(f"({sentinel_age}h-wall)")
            elif sentinel_age is not None:
                bits.append(f"sentinel={sentinel_age}h")
            elif sentinel_mtime is None and not freshness.get("sentinel_exists"):
                bits.append("sentinel=MISSING")
            if median_age is not None:
                cal_age = freshness.get(
                    "median_latest_bar_age_calendar_days",
                )
            # Trading-day age is the freshness gate (so a Friday close
            # on a Monday morning isn't flagged). Calendar-day age is
            # shown in the alert text for context — that's what most
            # operators intuit. 2026-09-19 user feedback: "the day
            # before is weekend so we can not say that the data is
            # stale, we only compare with trading day not holiday".
            if cal_age is not None and abs(cal_age - median_age) > 0.01:
                bits.append(
                    f"sample-bar-median={median_age}d-trading "
                    f"({cal_age}d-cal)"
                )
            else:
                bits.append(f"sample-bar-median={median_age}d")
            if freshness.get("oldest_sample_symbol"):
                bits.append(f"oldest={freshness['oldest_sample_symbol']}")
            if "error" in freshness:
                bits.append(f"probe-err={freshness['error'][:40]}")
            lines.append("data: STALE (" + ", ".join(bits) + ")")
        # (None fresh state is treated as unknown; emit nothing.)

    # proxy
    if proxy_url:
        # strip credentials before displaying
        from urllib.parse import urlparse
        try:
            parsed = urlparse(proxy_url)
            host_part = parsed.hostname or "?"
            port_part = f":{parsed.port}" if parsed.port else ""
            proxy_label = f"{parsed.scheme}://{host_part}{port_part}"
        except Exception:
            proxy_label = "(unparseable)"
        proxy_line = f"proxy: {proxy_label}"
        if proxy_check:
            ok = proxy_check.get("ok")
            err = proxy_check.get("error")
            ms = proxy_check.get("latency_ms")
            extra = []
            if ok is True:
                extra.append("ok")
            elif ok is False:
                extra.append("FAIL")
            if err:
                extra.append(f"err={str(err)[:60]}")
            if ms is not None:
                extra.append(f"{ms:.0f}ms")
            if extra:
                proxy_line += " (" + ", ".join(extra) + ")"
        lines.append(proxy_line)

    return "\n".join(lines)


def _filter_orders_by_portfolio(
    items: list[dict[str, Any]],
    portfolio: dict[str, Any] | None,
    board_lot: int = 100,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split ``items`` into (fillable, skipped_reasons).

    A SELL is fillable when the portfolio holds positive qty of that
    symbol. A BUY is fillable when ``qty * price`` (rounded to a
    board lot) fits inside ``available_cash``. When ``portfolio`` is
    missing or non-dict, no filtering is applied (legacy behaviour).
    """
    if not isinstance(portfolio, dict) or not items:
        return list(items), []
    positions_raw = portfolio.get("positions") or []
    balance = portfolio.get("balance") or {}
    available_cash = float(
        balance.get("availableCash")
        if balance.get("availableCash") is not None
        else balance.get("totalCash")
        or 0
    )
    held: dict[str, int] = {}
    for p in positions_raw:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").upper().strip()
        qty = int(p.get("quantity") or 0)
        if sym and qty > 0:
            held[sym] = max(held.get(sym, 0), qty)
    fillable: list[dict[str, Any]] = []
    skipped: list[str] = []
    for o in items:
        sym = str(o.get("symbol") or "").upper().strip()
        side = str(o.get("side") or "").upper().strip()
        try:
            qty = int(float(o.get("quantity") or 0))
            price = float(o.get("price") or 0)
        except (TypeError, ValueError):
            skipped.append(f"{side or '?'} {sym or '?'}: malformed qty/price")
            continue
        if qty <= 0 or price <= 0 or not sym:
            skipped.append(f"{side or '?'} {sym or '?'}: missing qty/price")
            continue
        # Normalise side to BUY/SELL (autofin uses NB/NS as legacy codes).
        if side in ("NB", "B"):
            side = "BUY"
        elif side in ("NS", "S"):
            side = "SELL"
        if side == "SELL":
            if held.get(sym, 0) <= 0:
                skipped.append(
                    f"SELL {qty} {sym} @ {int(price):,} VND (no position held)"
                )
                continue
        elif side == "BUY":
            # Round down to nearest board lot for the cost check (the
            # apply phase also rounds to lots, so this matches what the
            # bot would actually fill).
            lot_qty = (qty // max(board_lot, 1)) * max(board_lot, 1)
            cost = lot_qty * price * 1.0015  # include 15bps fees
            if available_cash > 0 and cost > available_cash:
                skipped.append(
                    f"BUY {qty} {sym} @ {int(price):,} VND "
                    f"(cost ~{int(cost):,} VND > cash {int(available_cash):,} VND)"
                )
                continue
        fillable.append(o)
    return fillable, skipped


def portfolio_section_lines(
    portfolio: dict[str, Any] | None,
    top_n: int = 8,
    label: str = "CURRENT PORTFOLIO",
) -> list[str]:
    """Render a Telegram section showing cash, NAV, P&L, and per-position P&L.

    Returns a list of message lines (no trailing newline). If ``portfolio`` is
    None or missing data, returns a single-line note that the snapshot
    couldn't be fetched.
    """
    if not isinstance(portfolio, dict):
        return ["💼 portfolio snapshot: unavailable"]
    bal = portfolio.get("balance", {}) or {}
    totals = portfolio.get("totals", {}) or {}
    positions = portfolio.get("positions", []) or []
    errors = portfolio.get("errors", []) or []
    mode = portfolio.get("mode") or ""

    # Section header — show DRY-RUN vs LIVE up front so it's obvious.
    header_label = label
    if mode == "dry_run":
        header_label = f"{label}  🔸 DRY-RUN SIMULATED"
    lines: list[str] = [f"💼 {header_label}"]
    cash_total = bal.get("totalCash") or 0
    stock_value = totals.get("stockValue") or 0
    nav = totals.get("portfolioValue") or 0
    # Dry-run extras: show initial NAV, realized P&L, total return.
    if mode == "dry_run":
        initial = totals.get("initialNav") or 0
        realized = totals.get("realizedPnl") or 0
        ret = totals.get("totalReturnPct") or 0
        lines.append(f"  initial NAV:      {format_money(initial)} VND")
    lines.append(f"  cash (available): {format_money(bal.get('availableCash'))} VND")
    lines.append(f"  cash (total):     {format_money(cash_total)} VND")
    lines.append(f"  stock value:      {format_money(stock_value)} VND")
    lines.append(f"  NAV:              {format_money(nav)} VND")
    upnl = totals.get("totalUnrealizedPnl", 0) or 0
    upnl_pct = totals.get("totalUnrealizedPnlPct", 0.0) or 0.0
    lines.append(
        f"  unrealized P&L:   {format_money(upnl)} VND  ({upnl_pct:+.2f}%)"
    )
    if mode == "dry_run":
        total_pnl = totals.get("totalPnl") or 0
        ret_pct = totals.get("totalReturnPct") or 0
        lines.append(
            f"  realized P&L:     {format_money(realized)} VND"
        )
        lines.append(
            f"  total P&L:        {format_money(total_pnl)} VND  ({ret_pct:+.2%} vs initial)"
        )
    # When the live account is empty, surface that explicitly so users
    # don't confuse the backtest metrics above with live state.
    if cash_total == 0 and stock_value == 0 and not positions:
        lines.append("")
        if mode == "dry_run":
            lines.append(
                "  ⚠️  Dry-run account is EMPTY — no simulated positions yet."
                " Submit orders (or wait for the bot to fill some) to populate."
            )
        else:
            lines.append(
                "  ⚠️  Live account is EMPTY — no cash, no positions."
                " No live orders can be submitted."
            )
    if errors:
        lines.append("")
        lines.append(f"  ⚠️  Snapshot errors: {', '.join(errors)}")
    if positions:
        lines.append("")
        lines.append(f"  📊 Positions ({len(positions)}):")
        for p in positions[:top_n]:
            sym = p.get("symbol", "?")
            qty = p.get("quantity", 0)
            avg = p.get("avgPrice") or 0
            mkt = p.get("marketPrice") or 0
            mv = p.get("marketValue") or 0
            pnl = p.get("unrealizedPnl") or 0
            pnl_pct = p.get("unrealizedPnlPct") or 0.0
            lines.append(
                f"    • {sym}: qty={qty:,}  avg={format_money(avg)}  "
                f"mkt={format_money(mkt)}  MV={format_money(mv)} VND  "
                f"uPnL={format_money(pnl)} VND ({pnl_pct:+.2f}%)"
            )
        if len(positions) > top_n:
            lines.append(f"    ... and {len(positions) - top_n} more")
    return lines


def eod_summary_message(
    result: dict[str, Any],
    portfolio: dict[str, Any] | None = None,
) -> str:
    """Default EOD cycle summary (used by momentum / hybrid etc.).

    Reads the per-trade rows from the latest_orders JSON file so the
    Telegram message shows the actual BUY/SELL orders being submitted,
    not just a count. When ``portfolio`` is provided, includes cash, NAV,
    and per-position unrealized P&L.

    NOTE: the ``metrics`` block in ``result`` is the **backtest**
    simulation result (initial 1B VND NAV run daily through the
    strategy), NOT the live DNSE account. We label it as such so it
    doesn't get confused with the live portfolio.
    """
    metrics = result.get("metrics", {}) if isinstance(result.get("metrics"), dict) else {}
    orders_info = result.get("latestOrdersCsv")
    is_dry_run = isinstance(portfolio, dict) and portfolio.get("mode") == "dry_run"
    lines: list[str] = [
        "📊 Vietnam Algo: EOD SUMMARY",
        f"tradeDate: {result.get('tradeDate')}",
        f"strategy: {result.get('strategy')}",
        f"orderCount: {result.get('count')}",
    ]
    # In dry-run mode, the portfolio section already shows the simulated
    # P&L — don't show the misleading backtest block (which is from a
    # 1B-VND historical backtest, unrelated to dry-run state).
    if not is_dry_run:
        # Backtest simulation metrics (NOT live P&L). Only show when the
        # strategy actually ran a backtest (it sets initialCash/finalEquity).
        has_backtest = (
            metrics.get("initialCash") not in (None, 0)
            or metrics.get("finalEquity") not in (None, 0)
        )
        if has_backtest:
            lines.append("")
            lines.append("  📈 Backtest simulation (1B VND start, NOT live P&L):")
            lines.append(f"    totalReturn: {format_percent(metrics.get('totalReturnPercent'))}")
            lines.append(f"    maxDrawdown: {format_percent(metrics.get('maxDrawdownPercent'))}")
            lines.append(f"    finalEquity: {format_money(metrics.get('finalEquity'))} VND")
    # current portfolio (cash, NAV, positions)
    lines.append("")
    lines.extend(portfolio_section_lines(portfolio, label="PORTFOLIO"))
    # orders to submit (read from latest_orders JSON)
    items: list[dict[str, Any]] = []
    json_path = result.get("latestOrdersJson")
    if json_path:
        try:
            payload = json.loads(Path(json_path).read_text())
            items = list(payload.get("items") or [])
        except Exception:
            items = []
    if items:
        lines.append("")
        lines.append(f"📋 Orders ({len(items)}):")
        # In dry-run mode, distinguish applied orders from clipped/skipped
        # ones so the user can see why a BUY they expected to fill did
        # not (e.g., insufficient cash after a previous fill in the
        # same cycle). The dry-run state records skipped/clipped fills
        # in its closed_trades marker entry — match them up here.
        skipped_lookup: dict[tuple, str] = {}
        clipped_lookup: dict[tuple, str] = {}
        if is_dry_run:
            try:
                # dry-run state is per-strategy; the orders csv/json
                # live at data/live/latest_orders[.<strategy>].{csv,json}
                # so we can recover the strategy name from the file
                # stem (e.g. "latest_orders.autofin_signal.json" →
                # "autofin_signal"). Falls back to the legacy
                # un-suffixed dry_run_state.json if that doesn't
                # exist or isn't readable.
                orders_stem = Path(orders_info).stem
                strategy_suffix = ""
                if orders_stem.startswith("latest_orders.") and orders_stem != "latest_orders":
                    strategy_suffix = orders_stem[len("latest_orders."):]
                live_dir = Path(orders_info).parent
                if strategy_suffix:
                    state_path = live_dir / f"dry_run_state.{strategy_suffix}.json"
                else:
                    state_path = live_dir / "dry_run_state.json"
                if not state_path.exists():
                    # Fall back: try the legacy un-suffixed path.
                    state_path = live_dir / "dry_run_state.json"
                if state_path.exists():
                    raw = json.loads(state_path.read_text())
                    for entry in reversed(raw.get("closed_trades") or []):
                        if not isinstance(entry, dict):
                            continue
                        note = entry.get("note", "")
                        if note.startswith("_applied:"):
                            for sk in entry.get("skipped") or []:
                                # The skipped order is keyed by the
                                # requested qty (what latest_orders.json
                                # has). For BUYs that fail with no cash
                                # there is no requested_qty, so qty IS
                                # the requested qty.
                                k = (
                                    sk.get("symbol"),
                                    sk.get("side"),
                                    sk.get("price"),
                                    sk.get("requested_qty") or sk.get("qty"),
                                )
                                skipped_lookup.setdefault(k, str(sk.get("reason", "skipped")))
                            for cl in entry.get("clipped") or []:
                                # Clipped fills carry requested_qty
                                # separately because qty is the
                                # post-clip lot-rounded qty. Match by
                                # requested so we can decorate the row
                                # in latest_orders.json.
                                k = (
                                    cl.get("symbol"),
                                    cl.get("side"),
                                    cl.get("price"),
                                    cl.get("requested_qty") or cl.get("qty"),
                                )
                                clipped_lookup.setdefault(k, f"clipped to {cl.get('qty')}")
                            break
            except Exception:
                pass
            except Exception:
                pass
        for o in items[:20]:
            sym = o.get("symbol", "?")
            side = o.get("side", "?")
            price = o.get("price", 0)
            try:
                price_s = f"{int(float(price)):,}"
            except (TypeError, ValueError):
                price_s = str(price)
            qty = o.get("quantity", 0)
            reason = str(o.get("reason") or "")[:30]
            tag = ""
            if is_dry_run:
                try:
                    key = (sym, side, float(price), float(qty))
                except (TypeError, ValueError):
                    key = None
                if key in skipped_lookup:
                    tag = f"  ⚠ SKIPPED ({skipped_lookup[key]})"
                elif key in clipped_lookup:
                    tag = f"  ✂️ {clipped_lookup[key]}"
                elif side == "BUY":
                    tag = "  ✓ filled"
            lines.append(f"  • {side} {qty} {sym} @ {price_s} VND  ({reason}){tag}")
        if len(items) > 20:
            lines.append(f"  ... and {len(items) - 20} more")
    lines.append("")
    lines.append(f"ordersFile: {orders_info}")
    return "\n".join(lines)


def start_of_day_message(
    portfolio: dict[str, Any] | None = None,
    today: str | None = None,
    orders_json_path: str | None = None,
    strategy_name: str = "momentum",
    account: str = "?",
    board_lot: int = 100,
) -> str:
    """Morning Telegram briefing sent before the 09:16 submit cycle.

    Shows:
      - current portfolio: cash, NAV, positions, unrealized P&L
      - today's planned orders (read from latest_orders JSON — these
        were generated at yesterday's EOD and will be submitted at
        09:16 ICT)

    Order filtering (added 2026-09-16, user feedback on momentum
    dry-run reset): the latest_orders.json is generated from a
    backtest that walks the strategy's full history. When the
    dry-run state has been reset to NAV=100M with empty positions
    (or a smaller portfolio than the backtest assumes), some
    orders in the JSON would never actually fill: a SELL for a
    symbol we don't hold (the apply phase silently drops it with
    ``no_position``), or a BUY that exceeds available cash.

    To keep the briefing honest, we filter the items list against
    the current portfolio snapshot:

      - ``SELL sym X``: skipped if the portfolio holds 0 shares of
        ``sym``.
      - ``BUY sym X``: skipped if ``qty * price > available_cash``
        (rough check, ignoring fees — the apply phase does the
        authoritative clip).

    If all items are filtered out, the message reads "No fillable
    orders today" instead of listing an empty plan.

    Skipped orders are summarised in a separate line so the user
    still sees the backtest's intent — but the actionable section
    only contains orders that can actually execute.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    lines: list[str] = [
        "🌅 Vietnam Algo: START OF DAY",
        f"📅 {today}",
        f"💼 Account: {account}  |  strategy: {strategy_name}",
    ]
    lines.append("")
    lines.extend(portfolio_section_lines(portfolio, label="CURRENT PORTFOLIO"))
    # today's planned orders (read from latest_orders JSON)
    items: list[dict[str, Any]] = []
    if orders_json_path:
        try:
            payload = json.loads(Path(orders_json_path).read_text())
            items = list(payload.get("items") or [])
        except Exception:
            items = []
    # Filter items against the current portfolio so the briefing only
    # lists orders that can actually fill.
    fillable, skipped = _filter_orders_by_portfolio(items, portfolio, board_lot=board_lot)
    if fillable:
        lines.append("")
        lines.append(
            f"📋 Today's planned orders ({len(fillable)}) — submitted at 09:16 ICT:"
        )
        for o in fillable[:20]:
            sym = o.get("symbol", "?")
            side = o.get("side", "?")
            price = o.get("price", 0)
            try:
                price_s = f"{int(float(price)):,}"
            except (TypeError, ValueError):
                price_s = str(price)
            qty = o.get("quantity", 0)
            reason = str(o.get("reason") or "")[:30]
            lines.append(f"  • {side} {qty} {sym} @ {price_s} VND  ({reason})")
        if len(fillable) > 20:
            lines.append(f"  ... and {len(fillable) - 20} more")
    elif items:
        # All items filtered out — say so instead of pretending we have a
        # plan that would actually do nothing.
        lines.append("")
        lines.append(
            "📋 No fillable orders today "
            f"({len(items)} plan(s) skipped — see reasons below)."
        )
    if skipped:
        lines.append("")
        lines.append(
            f"  ⏭ Skipped (no portfolio state to act on):"
        )
        for s in skipped[:10]:
            lines.append(f"    • {s}")
        if len(skipped) > 10:
            lines.append(f"    ... and {len(skipped) - 10} more")
    else:
        lines.append("")
        lines.append("ℹ️  No orders planned for today "
                     "(latest_orders.json empty or unreadable)")
    lines.append("")
    lines.append("🕒 Next: 09:16 ICT submit cycle → 15:15 ICT EOD update")
    return "\n".join(lines)


def portfolio_snapshot_message(snap: dict[str, Any], top_n: int = 8) -> str:
    """Format a Telegram message for a live DNSE portfolio snapshot.

    Best-effort: never raises. Shows:
      - Account info, cash, withdrawable
      - Top N positions with live market value + unrealized P&L
      - Portfolio totals
      - Endpoints that failed (if any)
    """
    if not isinstance(snap, dict):
        return "❌ Vietnam Algo: portfolio snapshot unavailable"

    bal = snap.get("balance", {}) or {}
    totals = snap.get("totals", {}) or {}
    positions = snap.get("positions", []) or []
    errors = snap.get("errors", []) or []

    name = snap.get("name") or "?"
    account = snap.get("account") or "?"

    lines = [
        "💼 Vietnam Algo: PORTFOLIO SNAPSHOT",
        f"account: {account} ({name})",
        f"fetchedAt: {snap.get('fetchedAt', '?')}",
        "",
        "💰 CASH",
        f"  totalCash:        {format_money(bal.get('totalCash'))} VND",
        f"  availableCash:    {format_money(bal.get('availableCash'))} VND",
        f"  withdrawableCash: {format_money(bal.get('withdrawableCash'))} VND",
        f"  secureAmount:     {format_money(bal.get('secureAmount'))} VND",
        f"  orderSecured:     {format_money(bal.get('orderSecured'))} VND",
        f"  totalDebt:        {format_money(bal.get('totalDebt'))} VND",
        "",
        f"📈 POSITIONS ({totals.get('positionCount', 0)})",
    ]

    if positions:
        for p in positions[:top_n]:
            sym = p.get("symbol", "?")
            qty = p.get("quantity", 0)
            avg = p.get("avgPrice")
            mkt = p.get("marketPrice")
            mv = p.get("marketValue", 0)
            upnl = p.get("unrealizedPnl", 0)
            upct = p.get("unrealizedPnlPct", 0)
            avg_str = f"{avg:>10.1f}" if avg else "       N/A"
            mkt_str = f"{mkt:>10.1f}" if mkt else "       N/A"
            lines.append(
                f"  {sym:<6} qty={qty:>5}  avg={avg_str}  mkt={mkt_str}\n"
                f"          value={format_money(mv)} VND  "
                f"PnL={format_money(upnl)} ({upct:+.2f}%)"
            )
        if len(positions) > top_n:
            lines.append(f"  ... and {len(positions) - top_n} more positions")
    else:
        lines.append("  (no positions)")

    lines += [
        "",
        "💵 TOTALS",
        f"  stockValue:        {format_money(totals.get('stockValue'))} VND",
        f"  portfolioValue:    {format_money(totals.get('portfolioValue'))} VND  "
        f"(cash + stocks)",
        f"  unrealizedPnL:     {format_money(totals.get('totalUnrealizedPnl'))} VND  "
        f"({totals.get('totalUnrealizedPnlPct', 0):+.2f}%)",
    ]

    if errors:
        lines.append("")
        lines.append(f"⚠️ endpoint errors: {', '.join(errors)}")

    return "\n".join(lines)


def force_run_summary_message(
    strategy_name: str,
    trade_date: str,
    poll_result: dict[str, Any],
    submit_result: dict[str, Any] | None,
    live_submit: bool,
) -> str:
    """Telegram message for a ``force-run-trading`` invocation.

    Shown when the operator triggers an ad-hoc bot re-run via
    ``python3 -m vietnam_algo.main force-run-trading ...``. Surfaces:
      - the strategy name + trade date
      - how many orders the strategy generated
      - what kind of orders (new entry / partial close / full close)
      - the submit result (if --live-submit) — how many went through,
        how many were skipped, how many failed
      - the dry-run notice (if dry-run)
    """
    count = poll_result.get("count", 0)
    lines: list[str] = [
        "🔧 Vietnam Algo: FORCE-RUN trading decision pipeline",
        f"strategy: {strategy_name}",
        f"date: {trade_date}",
        f"liveSubmit: {live_submit}",
        "",
        f"📊 strategy result: {count} order(s) planned",
    ]
    for label, key in (
        ("new entries (NB)", "newOrders"),
        ("partial closes (NS)", "partialOrders"),
        ("full closes (NS)", "closeOrders"),
        ("signals fetched", "signalsFetched"),
        ("deals tracked", "dealsTracked"),
    ):
        if key in poll_result:
            lines.append(f"  {label}: {poll_result[key]}")
    if poll_result.get("latestOrdersJson"):
        lines.append(f"  latestOrdersJson: {poll_result['latestOrdersJson']}")

    lines.append("")
    if submit_result is None:
        if live_submit:
            lines.append("⚠️ submit: SKIPPED (no deals_actioned from strategy)")
        else:
            lines.append("ℹ️ submit: SKIPPED (dry-run; pass --live-submit to place for real)")
    elif "error" in submit_result:
        lines.append(f"❌ submit: ERROR — {submit_result['error']}")
    else:
        submitted = submit_result.get("submitted", [])
        skipped = submit_result.get("skipped_existing", 0)
        unknown = submit_result.get("unknown", 0)
        total = submit_result.get("total", 0)
        icon = "✅" if unknown == 0 and len(submitted) > 0 else "🟡" if unknown == 0 else "🚨"
        lines.append(f"{icon} submit: {len(submitted)}/{total} submitted, "
                     f"{skipped} skipped-existing, {unknown} unknown")
        if submitted:
            lines.append(f"  dnseOrderIds: {','.join(str(x) for x in submitted[:10])}"
                         + (f" +{len(submitted) - 10} more" if len(submitted) > 10 else ""))
        if submit_result.get("out_of_zone"):
            lines.append(f"  out_of_zone: {len(submit_result['out_of_zone'])} (live-quote gate)")
        if submit_result.get("skipped_off_hours"):
            lines.append(f"  skipped_off_hours: {submit_result['skipped_off_hours']} (current time is outside trading window)")

    return "\n".join(lines)
