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
) -> str:
    """Daily "I'm alive" notification with current bot status snapshot.

    Includes:
      - Bot uptime (process start time)
      - Last successful cycle dates (token, submit, EOD, CA sync, market holidays)
      - Proxy configuration and (optional) last connectivity check
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

    lines: list[str] = [f"💼 {label}"]
    lines.append(f"  cash (available): {format_money(bal.get('availableCash'))} VND")
    lines.append(f"  cash (total):     {format_money(bal.get('totalCash'))} VND")
    lines.append(f"  stock value:      {format_money(totals.get('stockValue'))} VND")
    lines.append(f"  NAV:              {format_money(totals.get('portfolioValue'))} VND")
    upnl = totals.get("totalUnrealizedPnl", 0) or 0
    upnl_pct = totals.get("totalUnrealizedPnlPct", 0.0) or 0.0
    lines.append(
        f"  unrealized P&L:   {format_money(upnl)} VND  ({upnl_pct:+.2f}%)"
    )
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
    """
    metrics = result.get("metrics", {}) if isinstance(result.get("metrics"), dict) else {}
    orders_info = result.get("latestOrdersCsv")
    lines: list[str] = [
        "📊 Vietnam Algo: EOD SUMMARY",
        f"tradeDate: {result.get('tradeDate')}",
        f"strategy: {result.get('strategy')}",
        f"orderCount: {result.get('count')}",
        f"totalReturn: {format_percent(metrics.get('totalReturnPercent'))}",
        f"maxDrawdown: {format_percent(metrics.get('maxDrawdownPercent'))}",
        f"finalEquity: {format_money(metrics.get('finalEquity'))}",
    ]
    # current portfolio (cash, NAV, P&L, positions)
    lines.append("")
    lines.extend(portfolio_section_lines(portfolio, label="CURRENT PORTFOLIO"))
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
            lines.append(f"  • {side} {qty} {sym} @ {price_s} VND  ({reason})")
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
) -> str:
    """Morning Telegram briefing sent before the 09:16 submit cycle.

    Shows:
      - current portfolio: cash, NAV, positions, unrealized P&L
      - today's planned orders (read from latest_orders JSON — these
        were generated at yesterday's EOD and will be submitted at
        09:16 ICT)
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
    if items:
        lines.append("")
        lines.append(f"📋 Today's planned orders ({len(items)}) — submitted at 09:16 ICT:")
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
            lines.append(f"  • {side} {qty} {sym} @ {price_s} VND  ({reason})")
        if len(items) > 20:
            lines.append(f"  ... and {len(items) - 20} more")
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
