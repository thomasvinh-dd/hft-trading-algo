from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .dnse_client import DnseClient
from .order_monitor import submit_and_monitor_order


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
) -> list[DnseOrder]:
    orders = filter_orders(load_algo_orders(orders_path), trade_date=trade_date, limit=limit)
    converted: list[DnseOrder] = []
    for order in orders:
        dnse_order = convert_order(order, loan_package_id, order_type=order_type, board_lot_size=board_lot_size)
        if dnse_order:
            converted.append(dnse_order)
    return converted


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
                ),
            }
        )
    return results
