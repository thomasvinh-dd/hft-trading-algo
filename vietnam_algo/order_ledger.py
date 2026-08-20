from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .data import DEFAULT_DATA_DIR, read_json, utc_now, write_json


TERMINAL_LEDGER_STATUSES = {"filled", "cancelled", "rejected", "expired", "closed", "dry_run", "skipped"}
UNSAFE_LEDGER_STATUSES = {"prepared", "submitting", "submitted", "open", "partial", "unknown"}


@dataclass(frozen=True)
class LedgerOrder:
    runId: str
    sourceOrderId: int
    sourceDate: str
    symbol: str
    side: str
    quantity: int
    price: int
    reason: str
    status: str
    dnseOrderId: str = ""
    createdAt: str = ""
    updatedAt: str = ""
    lastDetail: dict[str, Any] | list[Any] | None = None
    error: str = ""


def ledger_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir / "live" / "order_ledger.json"


def ledger_key(run_id: str, source_order_id: int, symbol: str, side: str) -> str:
    return f"{run_id}:{source_order_id}:{symbol.upper()}:{side.upper()}"


def load_ledger(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, LedgerOrder]:
    path = ledger_path(data_dir)
    if not path.exists():
        return {}
    payload = read_json(path)
    rows = payload.get("items", []) if isinstance(payload, dict) else []
    ledger: dict[str, LedgerOrder] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            item = LedgerOrder(
                runId=str(row.get("runId") or ""),
                sourceOrderId=int(row.get("sourceOrderId") or 0),
                sourceDate=str(row.get("sourceDate") or ""),
                symbol=str(row.get("symbol") or "").upper(),
                side=str(row.get("side") or "").upper(),
                quantity=int(row.get("quantity") or 0),
                price=int(row.get("price") or 0),
                reason=str(row.get("reason") or ""),
                status=str(row.get("status") or "unknown"),
                dnseOrderId=str(row.get("dnseOrderId") or ""),
                createdAt=str(row.get("createdAt") or ""),
                updatedAt=str(row.get("updatedAt") or ""),
                lastDetail=row.get("lastDetail"),
                error=str(row.get("error") or ""),
            )
        except (TypeError, ValueError):
            continue
        if item.runId and item.symbol and item.side:
            ledger[ledger_key(item.runId, item.sourceOrderId, item.symbol, item.side)] = item
    return ledger


def save_ledger(ledger: dict[str, LedgerOrder], data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    rows = sorted(ledger.values(), key=lambda item: (item.runId, item.sourceOrderId, item.symbol, item.side))
    return write_json(
        ledger_path(data_dir),
        {
            "updatedAt": utc_now(),
            "count": len(rows),
            "items": [asdict(row) for row in rows],
        },
    )


def order_status_from_detail(detail: dict[str, Any] | list[Any]) -> str:
    if isinstance(detail, dict):
        for key in ("status", "orderStatus", "state"):
            if key in detail and detail[key] not in (None, ""):
                return str(detail[key]).upper()
        for value in detail.values():
            if isinstance(value, (dict, list)):
                status = order_status_from_detail(value)
                if status:
                    return status
    elif isinstance(detail, list):
        for item in detail:
            status = order_status_from_detail(item)
            if status:
                return status
    return ""


def normalize_ledger_status(status: str) -> str:
    clean = status.upper()
    if clean in {"FILLED", "MATCHED"}:
        return "filled"
    if clean in {"PARTIALLY_FILLED", "PARTIAL_FILLED"}:
        return "partial"
    if clean in {"CANCELLED", "CANCELED"}:
        return "cancelled"
    if clean == "REJECTED":
        return "rejected"
    if clean == "EXPIRED":
        return "expired"
    if clean == "CLOSED":
        return "closed"
    if clean in {"NEW", "OPEN", "PENDING", "PENDING_NEW", "WAITING", "QUEUED"}:
        return "open"
    return "unknown" if clean else "unknown"


def dnse_order_id(response: dict[str, Any] | list[Any] | None) -> str:
    if isinstance(response, dict):
        for key in ("orderId", "id", "orderNo"):
            if response.get(key):
                return str(response[key])
        for value in response.values():
            if isinstance(value, (dict, list)):
                found = dnse_order_id(value)
                if found:
                    return found
    elif isinstance(response, list):
        for item in response:
            found = dnse_order_id(item)
            if found:
                return found
    return ""


def unsafe_existing_orders(ledger: dict[str, LedgerOrder], run_id: str) -> list[LedgerOrder]:
    return [item for item in ledger.values() if item.runId == run_id and item.status in UNSAFE_LEDGER_STATUSES]


def submission_complete(ledger: dict[str, LedgerOrder], run_id: str) -> bool:
    run_orders = [item for item in ledger.values() if item.runId == run_id]
    return bool(run_orders) and all(item.status in TERMINAL_LEDGER_STATUSES for item in run_orders)
