from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .data import DEFAULT_DATA_DIR, read_json, utc_now, write_json


TERMINAL_LEDGER_STATUSES = {"filled", "cancelled", "rejected", "expired", "closed", "dry_run", "skipped"}
UNSAFE_LEDGER_STATUSES = {"prepared", "submitting", "submitted", "open", "partial", "unknown"}

# 2026-10-28: the subset of UNSAFE_LEDGER_STATUSES that genuinely
# makes a submit run AMBIGUOUS, and therefore worth blocking.
#
# ``unsafe_existing_orders`` short-circuits the ENTIRE submit run
# (every order for that run_id), not just the ambiguous one. That is
# the right behaviour for an order whose fate we don't know
# ("prepared" / "submitting" / "unknown" — DNSE may or may not have
# it, so re-submitting risks a double order).
#
# But "submitted" / "open" / "partial" are CONFIRMED live orders: we
# know DNSE holds them, and the per-order dedup in
# ``execute_orders_with_ledger`` already skips a matching
# (sourceOrderId, symbol, side). Blocking the whole run on them means
# one resting STOP-LOSS prevents every other order from being
# submitted.
#
# That is exactly what happened on 2026-10-28: the retried ORS
# stop-loss was correctly placed (DNSE order 216241) and recorded as
# "open", and from then on
# ``execute_orders_with_ledger`` returned 0 results for every
# subsequent submit — the MSB entry could not go through.
#
# So: only AMBIGUOUS states block the run. A confirmed working order
# is skipped per-order and lets the rest through.
AMBIGUOUS_LEDGER_STATUSES = {"prepared", "submitting", "unknown"}

# 2026-10-25: a dedicated status for orders DNSE rejected because the
# trading token was invalid.
#
# It is deliberately NEITHER terminal nor unsafe:
#
#   * Not TERMINAL — the ledger's dedup key
#     (run_id, sourceOrderId, symbol, side) matches on any terminal
#     status, so a terminal token rejection would make the order
#     permanently un-retryable. That is exactly how the KDC BUY on
#     2026-10-22 was silently lost (``HTTP 400
#     INVALID_TRADING_TOKEN`` → "rejected" → never retried).
#
#   * Not UNSAFE — ``unsafe_existing_orders`` short-circuits the
#     ENTIRE submit run, so an unsafe status would block every other
#     order for that run, not just the failed one.
#
# With this status the order is re-attemptable on the next cycle
# (once the token cron has refreshed the token) without wedging
# unrelated orders.
TOKEN_REJECTED_STATUS = "token_rejected"


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
    # 2026-10-13: re-pricing support for stale unfilled limit orders.
    # ``repriceCount`` is how many times the order monitor has
    # cancelled + re-placed this order at a higher limit. ``lastRepricedAt``
    # is the ISO timestamp of the last re-price attempt (used to throttle
    # the re-pricing cooldown across bot restarts). ``originalPrice`` is
    # the price at first submit, used to compute the re-price cap.
    repriceCount: int = 0
    lastRepricedAt: str = ""
    originalPrice: int = 0
    # 2026-10-28: DNSE order category. "NORMAL" for regular limit
    # orders; "STOP" for conditional stop orders (the DNSE
    # conditional-order API). ``daily_engine`` / ``trading_engine`` /
    # ``order_monitor`` read it to decide which DNSE endpoint and
    # query parameters to use, so the field must exist on every
    # ledger row.
    orderCategory: str = "NORMAL"
    # Trigger price for a conditional stop order (0 = not a stop
    # order). Kept alongside ``orderCategory`` so a stop order's
    # parameters survive a ledger round-trip.
    stopPrice: int = 0


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
                repriceCount=int(row.get("repriceCount") or 0),
                lastRepricedAt=str(row.get("lastRepricedAt") or ""),
                originalPrice=int(row.get("originalPrice") or 0) or int(row.get("price") or 0),
            )
        except (TypeError, ValueError):
            continue
        if item.runId and item.symbol and item.side:
            ledger[ledger_key(item.runId, item.sourceOrderId, item.symbol, item.side)] = item
    return ledger


def save_ledger(ledger: dict[str, LedgerOrder], data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    rows = sorted(ledger.values(), key=lambda item: (item.runId, item.sourceOrderId, item.symbol, item.side))
    path = write_json(
        ledger_path(data_dir),
        {
            "updatedAt": utc_now(),
            "count": len(rows),
            "items": [asdict(row) for row in rows],
        },
    )
    # Best-effort mirror into Postgres. Failures are logged but never
    # break the JSON write — see libs.db_sync.mirror_order_ledger.
    _maybe_mirror_to_db(rows)
    return path


def _maybe_mirror_to_db(rows: list[LedgerOrder]) -> None:
    """Mirror the freshly-written ledger rows into Postgres.

    Best-effort: a Postgres outage must not break the JSON pipeline.
    Each ``LedgerOrder`` is converted to a plain dict so we don't
    pull psycopg2 / DbClient into the order_ledger import graph.
    """
    import os

    if os.environ.get("HFT_MIRROR_TO_DB", "1") == "0":
        return
    try:
        from .db import DbClient, DbError
        from .db_sync import mirror_order_ledger
    except ImportError as exc:  # pragma: no cover — psycopg2 missing
        print(f"[save_ledger] skipping Postgres mirror (no psycopg2): {exc}")
        return

    db = DbClient.from_default()
    payload = [asdict(r) for r in rows]
    result = mirror_order_ledger(db, payload, run_id=None)
    if result is None:
        # mirror_order_ledger already logged via _safe
        return
    print(f"[save_ledger] mirrored {result} ledger rows to Postgres")


def order_status_from_detail(detail: dict[str, Any] | list[Any]) -> str:
    """Extract the order's status string from a DNSE order-detail payload.

    Returns the status in the **normalized** form expected by
    ``normalize_ledger_status``: camelCase split on the lowercase→uppercase
    boundary, then upper-cased. Examples:

      - ``"partiallyFilled"``  → ``"PARTIALLY_FILLED"``
      - ``"pendingNew"``       → ``"PENDING_NEW"``
      - ``"pendingCancel"``    → ``"PENDING_CANCEL"``
      - ``"cancelledByRightsEvent"`` → ``"CANCELLED_BY_RIGHTS_EVENT"``

    The 2026-10-08 monitoring fix added this normalization because the
    raw DNSE wire format is camelCase and the old "just upper-case
    it" path produced ``"PARTIALLYFILLED"`` which ``normalize_ledger_status``
    couldn't map. Without the fix, every partial fill was reported as
    ``"unknown"`` to the operator.
    """
    import re as _re
    raw = ""
    if isinstance(detail, dict):
        for key in ("status", "orderStatus", "state"):
            if key in detail and detail[key] not in (None, ""):
                raw = str(detail[key])
                break
        if not raw:
            for value in detail.values():
                if isinstance(value, (dict, list)):
                    raw = order_status_from_detail(value)
                    if raw:
                        break
    elif isinstance(detail, list):
        for item in detail:
            raw = order_status_from_detail(item)
            if raw:
                break
    if not raw:
        return ""
    # camelCase → snake_case → UPPER
    snake = _re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    return snake.upper()


def normalize_ledger_status(status: str) -> str:
    # DNSE returns the order status under ``orderStatus`` and the wire
    # format is camelCase (e.g. ``partiallyFilled``, ``pendingCancel``).
    # The 2026-10-08 monitoring fix added the camelCase → UPPER_SNAKE
    # conversion so ``normalize_ledger_status("partiallyFilled")``
    # returns ``"partial"``. Without this conversion, a partial fill
    # would be mapped to ``"unknown"`` and the operator would lose
    # visibility on the fill.
    #
    # 2026-10-12: added explicit handling for ``pendingCancel`` and
    # ``pendingReplace`` so the monitor keeps polling them (they are
    # non-terminal) and the operator gets a clear "cancel requested"
    # message. Before this fix, both mapped to ``"unknown"`` which
    # was also non-terminal but indistinguishable from a parse error
    # — operators couldn't tell whether the order was in-flight
    # transition or whether the monitor had failed to parse the
    # response.
    raw = (status or "").strip()
    if not raw:
        return "unknown"
    # camelCase → snake_case: insert "_" before each uppercase letter
    # that follows a lowercase letter, then uppercase the whole string.
    # Examples: "partiallyFilled" → "partially_filled", "pendingNew" →
    # "pending_new", "cancelledByRightsEvent" → "cancelled_by_rights_event".
    import re as _re
    snake = _re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    clean = snake.upper()
    if clean in {"FILLED", "MATCHED"}:
        return "filled"
    if clean in {"PARTIALLY_FILLED", "PARTIAL_FILLED", "PARTIALLY_MATCHED"}:
        return "partial"
    if clean in {"CANCELLED", "CANCELED", "CANCELLED_BY_RIGHTS_EVENT"}:
        return "cancelled"
    if clean == "REJECTED":
        return "rejected"
    if clean == "EXPIRED":
        return "expired"
    if clean == "CLOSED":
        return "closed"
    if clean == "DONE_FOR_DAY":
        return "closed"
    if clean in {"PENDING_CANCEL", "PENDING_REPLACE"}:
        # 2026-10-12: in-flight transitions, not terminal. Use a
        # distinct label so the operator can tell the cancel was
        # requested but the server hasn't confirmed yet. The monitor
        # keeps polling these.
        return "pending_cancel" if clean == "PENDING_CANCEL" else "pending_replace"
    if clean in {"NEW", "OPEN", "PENDING", "PENDING_NEW", "WAITING", "QUEUED"}:
        return "open"
    return "unknown"


def filled_quantity(detail: dict[str, Any] | None) -> int:
    """Extract the filled quantity from a DNSE order-detail payload.

    The 2026-10-13 fix: DNSE's order-detail response uses ``fillQuantity``
    (camelCase, no "-ed" suffix — e.g. ``"fillQuantity": 200`` for a
    200-share fill). The earlier code looked for ``filledQuantity``
    (with the "-ed" suffix that we'd see in some other systems), which
    silently returned 0 for every order on the books. The downstream
    consumer (autofin's ``_prune_terminal_unfilled_deals``) then
    reconciled ``_our_qty`` to 0 for every held deal, even though
    the position was real and live on DNSE — the autofin bot thought
    we held nothing and re-planned duplicate BUYs on every cycle.

    Search order is "fillQuantity" first (the actual DNSE wire field),
    then "filledQuantity" (the older name we used to check), then
    "matchedQuantity" (another valid DNSE field for cross-check).
    Falls back to 0 when none of the fields are present or the
    detail is not a dict.

    Returns an int ≥ 0. Coerces non-numeric values via ``int()`` so
    a string like ``"200"`` is handled gracefully.
    """
    if not isinstance(detail, dict):
        return 0
    for key in ("fillQuantity", "filledQuantity", "matchedQuantity"):
        val = detail.get(key)
        if val is None or val == "":
            continue
        try:
            qty = int(val)
        except (TypeError, ValueError):
            continue
        if qty >= 0:
            return qty
    return 0


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
    """Return the orders that make a submit run for ``run_id`` ambiguous.

    Only AMBIGUOUS statuses block the run (see
    ``AMBIGUOUS_LEDGER_STATUSES``): an order whose state we can't
    determine might be live at DNSE, so re-submitting risks a double
    order.

    A CONFIRMED working order ("submitted" / "open" / "partial") does
    NOT block the run — the per-order dedup in
    ``execute_orders_with_ledger`` already skips it, and blocking the
    whole run on it would let a single resting stop-loss prevent
    every other order from being submitted (2026-10-28 incident).
    """
    return [
        item for item in ledger.values()
        if item.runId == run_id and item.status in AMBIGUOUS_LEDGER_STATUSES
    ]


def submission_complete(ledger: dict[str, LedgerOrder], run_id: str) -> bool:
    run_orders = [item for item in ledger.values() if item.runId == run_id]
    return bool(run_orders) and all(item.status in TERMINAL_LEDGER_STATUSES for item in run_orders)
