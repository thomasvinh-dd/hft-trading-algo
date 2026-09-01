from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
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


def eod_summary_message(result: dict[str, Any]) -> str:
    metrics = result.get("metrics", {}) if isinstance(result.get("metrics"), dict) else {}
    orders_info = result.get("latestOrdersCsv")
    return "\n".join(
        [
            "📊 Vietnam Algo: EOD SUMMARY",
            f"tradeDate: {result.get('tradeDate')}",
            f"orderCount: {result.get('count')}",
            f"totalReturn: {format_percent(metrics.get('totalReturnPercent'))}",
            f"maxDrawdown: {format_percent(metrics.get('maxDrawdownPercent'))}",
            f"finalEquity: {format_money(metrics.get('finalEquity'))}",
            f"ordersFile: {orders_info}",
        ]
    )
