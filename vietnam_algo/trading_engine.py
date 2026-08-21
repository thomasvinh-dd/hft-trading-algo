from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .corporate_actions import CorporateAction, corporate_action_path, load_corporate_actions, non_cash_events_in_trading_window, non_cash_events_in_window
from .data import DEFAULT_DATA_DIR
from .dnse_client import DnseClient
from .order_monitor import quote_match_price, submit_and_monitor_order


@dataclass(frozen=True)
class AlgoOrder:
    orderId: int
    date: str
    symbol: str
    side: str
    price: float
    quantity: int
    value: float
    fee: float
    reason: str


@dataclass(frozen=True)
class DnseOrder:
    symbol: str
    side: str
    quantity: int
    price: int
    loanPackageId: int
    orderType: str
    sourceOrderId: int
    sourceDate: str
    reason: str


@dataclass(frozen=True)
class CorporateActionDecision:
    sourceOrderId: int
    symbol: str
    side: str
    action: str
    reason: str
    events: list[dict[str, Any]]


def load_algo_orders(path: Path) -> list[AlgoOrder]:
    with path.open("r", encoding="utf-8", newline="") as file:
        rows = csv.DictReader(file)
        return [
            AlgoOrder(
                orderId=int(row["orderId"]),
                date=str(row["date"]),
                symbol=str(row["symbol"]).upper(),
                side=str(row["side"]).upper(),
                price=float(row["price"]),
                quantity=int(float(row["quantity"])),
                value=float(row["value"]),
                fee=float(row["fee"]),
                reason=str(row["reason"]),
            )
            for row in rows
        ]


def dnse_side(side: str) -> str:
    if side == "BUY":
        return "NB"
    if side == "SELL":
        return "NS"
    raise ValueError(f"unsupported side: {side}")


def round_board_lot(quantity: int, board_lot_size: int = 100) -> int:
    if board_lot_size <= 0:
        raise ValueError("board lot size must be greater than zero")
    return quantity // board_lot_size * board_lot_size


def convert_order(order: AlgoOrder, loan_package_id: int, order_type: str = "LO", board_lot_size: int = 100) -> DnseOrder | None:
    quantity = round_board_lot(order.quantity, board_lot_size)
    if quantity <= 0:
        return None
    return DnseOrder(
        symbol=order.symbol,
        side=dnse_side(order.side),
        quantity=quantity,
        price=int(round(order.price)),
        loanPackageId=loan_package_id,
        orderType=order_type,
        sourceOrderId=order.orderId,
        sourceDate=order.date,
        reason=order.reason,
    )


def filter_orders(orders: list[AlgoOrder], trade_date: str | None = None, limit: int = 0) -> list[AlgoOrder]:
    filtered = [order for order in orders if trade_date is None or order.date == trade_date]
    return filtered[:limit] if limit > 0 else filtered


def prepare_dnse_orders(
    orders_path: Path,
    loan_package_id: int,
    trade_date: str | None = None,
    limit: int = 0,
    order_type: str = "LO",
    board_lot_size: int = 100,
    corporate_actions_path: Path | None = None,
    corporate_action_window_days: int = 3,
    corporate_action_buy_window_days: int | None = None,
    corporate_action_sell_window_days: int | None = None,
    trading_dates: list[str] | None = None,
) -> list[DnseOrder]:
    orders = filter_orders(load_algo_orders(orders_path), trade_date=trade_date, limit=limit)
    actions = load_corporate_actions(corporate_actions_path) if corporate_actions_path else []
    orders, _ = apply_corporate_action_guard(
        orders,
        actions,
        buy_window_days=corporate_action_buy_window_days if corporate_action_buy_window_days is not None else corporate_action_window_days,
        sell_window_days=corporate_action_sell_window_days if corporate_action_sell_window_days is not None else corporate_action_window_days,
        trading_dates=trading_dates,
    )
    converted: list[DnseOrder] = []
    for order in orders:
        dnse_order = convert_order(order, loan_package_id, order_type=order_type, board_lot_size=board_lot_size)
        if dnse_order:
            converted.append(dnse_order)
    return converted


def selected_order_date(orders_path: Path, trade_date: str | None = None) -> str | None:
    orders = filter_orders(load_algo_orders(orders_path), trade_date=trade_date)
    if not orders:
        return trade_date
    return max(order.date for order in orders)


def prepare_dnse_orders_with_risk_report(
    orders_path: Path,
    loan_package_id: int,
    trade_date: str | None = None,
    limit: int = 0,
    order_type: str = "LO",
    board_lot_size: int = 100,
    corporate_actions_path: Path | None = None,
    corporate_action_window_days: int = 3,
    corporate_action_buy_window_days: int | None = None,
    corporate_action_sell_window_days: int | None = None,
    trading_dates: list[str] | None = None,
) -> tuple[list[DnseOrder], list[CorporateActionDecision]]:
    orders = filter_orders(load_algo_orders(orders_path), trade_date=trade_date, limit=limit)
    actions = load_corporate_actions(corporate_actions_path) if corporate_actions_path else []
    guarded, decisions = apply_corporate_action_guard(
        orders,
        actions,
        buy_window_days=corporate_action_buy_window_days if corporate_action_buy_window_days is not None else corporate_action_window_days,
        sell_window_days=corporate_action_sell_window_days if corporate_action_sell_window_days is not None else corporate_action_window_days,
        trading_dates=trading_dates,
    )
    converted: list[DnseOrder] = []
    for order in guarded:
        dnse_order = convert_order(order, loan_package_id, order_type=order_type, board_lot_size=board_lot_size)
        if dnse_order:
            converted.append(dnse_order)
    return converted, decisions


def default_corporate_actions_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return corporate_action_path(data_dir)


def apply_corporate_action_guard(
    orders: list[AlgoOrder],
    actions: list[CorporateAction],
    buy_window_days: int = 2,
    sell_window_days: int = 3,
    trading_dates: list[str] | None = None,
) -> tuple[list[AlgoOrder], list[CorporateActionDecision]]:
    if not orders or not actions:
        return orders, []
    guarded: list[AlgoOrder] = []
    decisions: list[CorporateActionDecision] = []
    for order in orders:
        window_days = buy_window_days if order.side == "BUY" else sell_window_days
        risky_by_symbol = corporate_action_window_lookup(actions, order.date, window_days, trading_dates)
        risky_events = risky_by_symbol.get(order.symbol, [])
        if not risky_events:
            guarded.append(order)
            continue
        event_payloads = [corporate_action_event_payload(event) for event in risky_events]
        if order.side == "BUY":
            decisions.append(
                CorporateActionDecision(
                    sourceOrderId=order.orderId,
                    symbol=order.symbol,
                    side=order.side,
                    action="blocked",
                    reason=f"non_cash_corporate_action_within_{window_days}_days",
                    events=event_payloads,
                )
            )
            continue
        guarded.append(order)
        decisions.append(
            CorporateActionDecision(
                sourceOrderId=order.orderId,
                symbol=order.symbol,
                side=order.side,
                action="allowed_exit",
                reason=f"sell_allowed_before_non_cash_corporate_action_within_{window_days}_days",
                events=event_payloads,
            )
        )
    return guarded, decisions


def corporate_action_window_lookup(
    actions: list[CorporateAction],
    trade_date: str,
    window_days: int,
    trading_dates: list[str] | None = None,
) -> dict[str, list[CorporateAction]]:
    if trading_dates:
        return non_cash_events_in_trading_window(actions, trade_date, trading_dates, window_days=window_days, include_ex_date=True)
    return non_cash_events_in_window(actions, trade_date, window_days=window_days, include_ex_date=True)


def corporate_action_event_payload(action: CorporateAction) -> dict[str, Any]:
    return {
        "eventId": action.eventId,
        "symbol": action.symbol,
        "exDate": action.exDate,
        "recordDate": action.recordDate,
        "name": action.name,
        "note": action.note,
        "title": action.title,
    }


def position_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for item in payload for row in position_rows(item)]
    if not isinstance(payload, dict):
        return []
    rows: list[dict[str, Any]] = []
    if any(key in payload for key in ("symbol", "stockSymbol", "ticker", "secSymbol")):
        rows.append(payload)
    for key in ("data", "items", "positions", "content"):
        value = payload.get(key)
        if isinstance(value, (list, dict)):
            rows.extend(position_rows(value))
    return rows


def first_field(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def extract_sellable_positions(payload: Any, board_lot_size: int = 100) -> dict[str, int]:
    positions: dict[str, int] = {}
    for row in position_rows(payload):
        symbol = str(first_field(row, ("symbol", "stockSymbol", "ticker", "secSymbol")) or "").strip().upper()
        quantity = int_or_zero(
            first_field(
                row,
                (
                    "sellableQuantity",
                    "availableQuantity",
                    "availableQty",
                    "tradeableQuantity",
                    "quantity",
                    "volume",
                ),
            )
        )
        rounded = round_board_lot(quantity, board_lot_size) if quantity > 0 else 0
        if symbol and rounded > 0:
            positions[symbol] = positions.get(symbol, 0) + rounded
    return positions


def build_corporate_action_exit_orders(
    client: DnseClient,
    account_no: str,
    loan_package_id: int,
    trade_date: str,
    actions: list[CorporateAction],
    existing_orders: list[DnseOrder],
    order_type: str = "LO",
    board_lot_size: int = 100,
    window_days: int = 3,
    sell_window_days: int | None = None,
    trading_dates: list[str] | None = None,
) -> tuple[list[DnseOrder], list[CorporateActionDecision]]:
    exit_window_days = sell_window_days if sell_window_days is not None else window_days
    risky = corporate_action_window_lookup(actions, trade_date, exit_window_days, trading_dates)
    if not risky:
        return [], []
    existing_sell_symbols = {order.symbol for order in existing_orders if order.side == "NS"}
    sellable = extract_sellable_positions(client.positions(account_no), board_lot_size)
    forced_orders: list[DnseOrder] = []
    decisions: list[CorporateActionDecision] = []
    for symbol, events in sorted(risky.items()):
        if symbol in existing_sell_symbols:
            continue
        quantity = sellable.get(symbol, 0)
        event_payloads = [corporate_action_event_payload(event) for event in events]
        if quantity <= 0:
            decisions.append(
                CorporateActionDecision(
                    sourceOrderId=-(events[0].eventId or 1),
                    symbol=symbol,
                    side="SELL",
                    action="forced_exit_skipped",
                    reason="no_sellable_position_for_non_cash_corporate_action",
                    events=event_payloads,
                )
            )
            continue
        price = quote_match_price(client.latest_quote(symbol), "NS")
        if price is None:
            decisions.append(
                CorporateActionDecision(
                    sourceOrderId=-(events[0].eventId or 1),
                    symbol=symbol,
                    side="SELL",
                    action="forced_exit_skipped",
                    reason="best_bid_unavailable_for_non_cash_corporate_action_exit",
                    events=event_payloads,
                )
            )
            continue
        forced_order = DnseOrder(
            symbol=symbol,
            side="NS",
            quantity=quantity,
            price=price,
            loanPackageId=loan_package_id,
            orderType=order_type,
            sourceOrderId=-(events[0].eventId or 1),
            sourceDate=trade_date,
            reason="corporate_action_forced_exit",
        )
        forced_orders.append(forced_order)
        decisions.append(
            CorporateActionDecision(
                sourceOrderId=forced_order.sourceOrderId,
                symbol=symbol,
                side="SELL",
                action="forced_exit_added",
                reason=f"non_cash_corporate_action_within_{exit_window_days}_days",
                events=event_payloads,
            )
        )
    return forced_orders, decisions


def apply_sellable_quantity_cap(
    orders: list[DnseOrder],
    sellable_positions: dict[str, int],
    board_lot_size: int = 100,
) -> tuple[list[DnseOrder], list[dict[str, Any]]]:
    capped: list[DnseOrder] = []
    decisions: list[dict[str, Any]] = []
    remaining_sellable = dict(sellable_positions)
    for order in orders:
        if order.side != "NS":
            capped.append(order)
            continue
        available = remaining_sellable.get(order.symbol, 0)
        quantity = min(order.quantity, available)
        quantity = round_board_lot(quantity, board_lot_size) if quantity > 0 else 0
        if quantity <= 0:
            decisions.append({"symbol": order.symbol, "sourceOrderId": order.sourceOrderId, "action": "sell_skipped", "reason": "no_sellable_quantity"})
            continue
        remaining_sellable[order.symbol] = max(available - quantity, 0)
        if quantity != order.quantity:
            decisions.append(
                {
                    "symbol": order.symbol,
                    "sourceOrderId": order.sourceOrderId,
                    "action": "sell_quantity_reduced",
                    "reason": "dnse_sellable_quantity_cap",
                    "originalQuantity": order.quantity,
                    "newQuantity": quantity,
                }
            )
            capped.append(
                DnseOrder(
                    symbol=order.symbol,
                    side=order.side,
                    quantity=quantity,
                    price=order.price,
                    loanPackageId=order.loanPackageId,
                    orderType=order.orderType,
                    sourceOrderId=order.sourceOrderId,
                    sourceDate=order.sourceDate,
                    reason=order.reason,
                )
            )
            continue
        capped.append(order)
    return capped, decisions


def price_order_from_quote(
    order: DnseOrder,
    quote: dict[str, Any] | list[Any],
    max_drift_percent: float = 0.0,
) -> tuple[DnseOrder, dict[str, Any]]:
    market_price = quote_match_price(quote, order.side)
    if market_price is None:
        return order, {"symbol": order.symbol, "action": "price_kept", "reason": "market_price_unavailable", "price": order.price}
    from .order_monitor import price_within_drift

    if not price_within_drift(order.price, market_price, order.side, max_drift_percent):
        return order, {
            "symbol": order.symbol,
            "action": "price_kept",
            "reason": "market_price_exceeds_max_drift",
            "signalPrice": order.price,
            "marketPrice": market_price,
        }
    if market_price == order.price:
        return order, {"symbol": order.symbol, "action": "price_kept", "reason": "already_at_best_price", "price": order.price}
    priced = DnseOrder(
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        price=market_price,
        loanPackageId=order.loanPackageId,
        orderType=order.orderType,
        sourceOrderId=order.sourceOrderId,
        sourceDate=order.sourceDate,
        reason=order.reason,
    )
    return priced, {"symbol": order.symbol, "action": "price_changed", "reason": "initial_orderbook_price", "signalPrice": order.price, "marketPrice": market_price}


def price_orders_from_orderbook(
    client: DnseClient,
    orders: list[DnseOrder],
    max_drift_percent: float = 0.0,
) -> tuple[list[DnseOrder], list[dict[str, Any]]]:
    priced: list[DnseOrder] = []
    decisions: list[dict[str, Any]] = []
    for order in orders:
        priced_order, decision = price_order_from_quote(order, client.latest_quote(order.symbol), max_drift_percent=max_drift_percent)
        priced.append(priced_order)
        decisions.append(decision)
    return priced, decisions


def execute_dnse_orders(
    client: DnseClient,
    account_no: str,
    trading_token: str,
    orders: list[DnseOrder],
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for order in orders:
        payload = asdict(order)
        if dry_run:
            results.append({"status": "dry_run", "order": payload})
            continue
        response = client.place_stock_order(
            account_no=account_no,
            trading_token=trading_token,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            loan_package_id=order.loanPackageId,
            order_type=order.orderType,
        )
        results.append({"status": "submitted", "order": payload, "response": response})
    return results


def execute_dnse_orders_with_monitoring(
    client: DnseClient,
    account_no: str,
    trading_token: str,
    orders: list[DnseOrder],
    dry_run: bool = True,
    max_checks: int = 3,
    check_interval_seconds: float = 5.0,
    signal_prices: dict[int, float] | None = None,
    max_drift_percent: float = 0.0,
    cancel_if_unfilled: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for order in orders:
        results.append(
            {
                "order": asdict(order),
                "events": submit_and_monitor_order(
                    client,
                    account_no,
                    trading_token,
                    order,
                    dry_run=dry_run,
                    max_checks=max_checks,
                    check_interval_seconds=check_interval_seconds,
                    signal_price=(signal_prices or {}).get(order.sourceOrderId, order.price),
                    max_drift_percent=max_drift_percent,
                    cancel_if_unfilled=cancel_if_unfilled,
                ),
            }
        )
    return results
