from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from .dnse_client import DnseClient

if TYPE_CHECKING:
    from .trading_engine import DnseOrder


OPEN_ORDER_STATUSES = {
    "PENDING",
    "PENDING_NEW",
    "NEW",
    "PARTIALLY_FILLED",
    "PARTIAL_FILLED",
    "WAITING",
    "QUEUED",
    "OPEN",
}
DONE_ORDER_STATUSES = {"FILLED", "MATCHED", "CANCELLED", "REJECTED", "EXPIRED", "CLOSED"}


@dataclass(frozen=True)
class RepriceDecision:
    action: str
    price: int | None
    reason: str


def number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first_number(payload: Any, keys: tuple[str, ...]) -> float | None:
    if isinstance(payload, dict):
        for key in keys:
            value = number_or_none(payload.get(key))
            if value is not None:
                return value
        for value in payload.values():
            nested = first_number(value, keys)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = first_number(item, keys)
            if nested is not None:
                return nested
    return None


def quote_match_price(quote: dict[str, Any] | list[Any], side: str) -> int | None:
    if side == "NB":
        value = first_number(quote, ("askPrice1", "askPrice", "bestAsk", "offerPrice1", "sellPrice1"))
    else:
        value = first_number(quote, ("bidPrice1", "bidPrice", "bestBid", "buyPrice1"))
    return int(round(value)) if value and value > 0 else None


def price_within_drift(signal_price: float, market_price: float, side: str, max_drift_percent: float) -> bool:
    if signal_price <= 0 or market_price <= 0 or max_drift_percent <= 0:
        return True
    drift = max_drift_percent / 100
    if side == "NB":
        return market_price <= signal_price * (1 + drift)
    return market_price >= signal_price * (1 - drift)


def order_status(order_detail: dict[str, Any] | list[Any]) -> str:
    if isinstance(order_detail, dict):
        for key in ("status", "orderStatus", "state"):
            if key in order_detail:
                return str(order_detail[key]).upper()
        for value in order_detail.values():
            if isinstance(value, dict):
                status = order_status(value)
                if status:
                    return status
    return ""


def decide_reprice(
    order: "DnseOrder",
    order_detail: dict[str, Any] | list[Any],
    quote: dict[str, Any] | list[Any],
    signal_price: float | None = None,
    max_drift_percent: float = 0.0,
) -> RepriceDecision:
    status = order_status(order_detail)
    if status in DONE_ORDER_STATUSES:
        return RepriceDecision("done", None, f"order_status_{status.lower()}")
    market_price = quote_match_price(quote, order.side)
    if market_price is None:
        return RepriceDecision("wait", None, "market_price_unavailable")
    if signal_price is not None and not price_within_drift(signal_price, market_price, order.side, max_drift_percent):
        return RepriceDecision("wait", market_price, "market_price_exceeds_max_drift")
    if market_price == order.price:
        return RepriceDecision("wait", market_price, "already_at_best_price")
    return RepriceDecision("replace", market_price, "not_matching_current_orderbook")


def submit_and_monitor_order(
    client: DnseClient,
    account_no: str,
    trading_token: str,
    order: "DnseOrder",
    dry_run: bool = True,
    max_checks: int = 3,
    check_interval_seconds: float = 5.0,
    signal_price: float | None = None,
    max_drift_percent: float = 0.0,
    cancel_if_unfilled: bool = False,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if dry_run:
        quote = client.latest_quote(order.symbol) if not dry_run else {}
        decision = decide_reprice(order, {}, quote, signal_price=signal_price, max_drift_percent=max_drift_percent) if quote else RepriceDecision("dry_run", order.price, "not_submitted")
        return [{"event": "dry_run_submit", "order": asdict(order), "decision": asdict(decision)}]

    response = client.place_stock_order(
        account_no,
        trading_token,
        order.symbol,
        order.side,
        order.quantity,
        order.price,
        order.loanPackageId,
        order.orderType,
    )
    events.append({"event": "submitted", "order": asdict(order), "response": response})
    order_id = str(response.get("orderId") or response.get("id") or "") if isinstance(response, dict) else ""
    if not order_id:
        events.append({"event": "monitor_skipped", "reason": "order_id_missing"})
        return events

    current_order = order
    for _ in range(max_checks):
        time.sleep(check_interval_seconds)
        detail = client.order_detail(account_no, order_id)
        quote = client.latest_quote(order.symbol)
        decision = decide_reprice(current_order, detail, quote, signal_price=signal_price, max_drift_percent=max_drift_percent)
        events.append({"event": "checked", "orderId": order_id, "detail": detail, "decision": asdict(decision)})
        if decision.action == "done":
            break
        if decision.action == "replace" and decision.price is not None:
            response = client.replace_order(account_no, trading_token, order_id, decision.price, current_order.quantity, current_order.orderType)
            from .trading_engine import DnseOrder

            current_order = DnseOrder(
                symbol=current_order.symbol,
                side=current_order.side,
                quantity=current_order.quantity,
                price=decision.price,
                loanPackageId=current_order.loanPackageId,
                orderType=current_order.orderType,
                sourceOrderId=current_order.sourceOrderId,
                sourceDate=current_order.sourceDate,
                reason=current_order.reason,
            )
            events.append({"event": "replaced", "orderId": order_id, "newPrice": decision.price, "response": response})
    detail = client.order_detail(account_no, order_id)
    status = order_status(detail)
    if status not in DONE_ORDER_STATUSES:
        events.append({"event": "monitor_exhausted_unfilled", "orderId": order_id, "detail": detail, "status": status or "UNKNOWN"})
    if cancel_if_unfilled:
        if status not in DONE_ORDER_STATUSES:
            response = client.cancel_order(account_no, trading_token, order_id)
            events.append({"event": "cancelled_unfilled", "orderId": order_id, "detail": detail, "response": response})
    return events
