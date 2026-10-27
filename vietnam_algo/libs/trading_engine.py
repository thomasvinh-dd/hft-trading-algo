from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .corporate_actions import (
    CorporateAction,
    closes_since_ex_window,
    corporate_action_path,
    corporate_action_risk_type,
    evaluate_post_ca_rebuy_signal,
    load_corporate_actions,
    non_cash_events_in_trading_window,
    non_cash_events_in_window,
)
from .ca_rebuy_state import CaRebuyState, PendingRebuy, ReservedCash
from .config import StrategyConfig
from .data import DEFAULT_DATA_DIR
from .dnse.client import DnseClient
from .order_monitor import quote_match_price, submit_and_monitor_order
from .universe import build_symbol_sector_map, load_symbol_closes


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
    precomputed_ex_index: dict[int, int] | None = None,
) -> list[DnseOrder]:
    orders = filter_orders(load_algo_orders(orders_path), trade_date=trade_date, limit=limit)
    actions = load_corporate_actions(corporate_actions_path) if corporate_actions_path else []
    orders, _ = apply_corporate_action_guard(
        orders,
        actions,
        buy_window_days=corporate_action_buy_window_days if corporate_action_buy_window_days is not None else corporate_action_window_days,
        sell_window_days=corporate_action_sell_window_days if corporate_action_sell_window_days is not None else corporate_action_window_days,
        trading_dates=trading_dates,
        precomputed_ex_index=precomputed_ex_index,
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
    corporate_action_window_days: int = 14,
    corporate_action_buy_window_days: int | None = None,
    corporate_action_sell_window_days: int | None = None,
    corporate_action_pre_exit_days: int | None = None,
    corporate_action_high_risk_buy_window_days: int | None = None,
    corporate_action_high_risk_sell_window_days: int | None = None,
    trading_dates: list[str] | None = None,
    precomputed_ex_index: dict[int, int] | None = None,
) -> tuple[list[DnseOrder], list[CorporateActionDecision]]:
    orders = filter_orders(load_algo_orders(orders_path), trade_date=trade_date, limit=limit)
    actions = load_corporate_actions(corporate_actions_path) if corporate_actions_path else []
    guarded, decisions = apply_corporate_action_guard(
        orders,
        actions,
        buy_window_days=corporate_action_buy_window_days if corporate_action_buy_window_days is not None else corporate_action_window_days,
        sell_window_days=corporate_action_sell_window_days if corporate_action_sell_window_days is not None else corporate_action_window_days,
        pre_exit_days=corporate_action_pre_exit_days,
        high_risk_buy_window_days=corporate_action_high_risk_buy_window_days,
        high_risk_sell_window_days=corporate_action_high_risk_sell_window_days,
        trading_dates=trading_dates,
        precomputed_ex_index=precomputed_ex_index,
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
    buy_window_days: int = 14,
    sell_window_days: int = 3,
    pre_exit_days: int | None = None,
    high_risk_buy_window_days: int | None = None,
    high_risk_sell_window_days: int | None = None,
    trading_dates: list[str] | None = None,
    precomputed_ex_index: dict[int, int] | None = None,
) -> tuple[list[AlgoOrder], list[CorporateActionDecision]]:
    """Apply the corporate-action guard to a list of orders.

    Mirrors the backtest's two-tier window logic:

    - Standard events: BUY is blocked within ``buy_window_days`` (default 14)
      of any upcoming CA, SELL is allowed within ``pre_exit_days`` (default
      matches buy_window_days). Wider than legacy ``sell_window_days`` so
      the strategy exits earlier, capturing the pre-CA premium.
    - High-risk events: BUY is blocked within ``high_risk_buy_window_days``
      (default 5). SELL is allowed within ``high_risk_sell_window_days``
      (default 5). High-risk events stay close to ex-date since the
      volatility is concentrated in the last few sessions.
    - Risk-type classification is done via :func:`corporate_action_risk_type`.

    Args:
        orders:  BUY/SELL orders to filter.
        actions: full list of corporate-action events.
        buy_window_days:           standard BUY-block window.
        sell_window_days:          legacy narrow SELL window (still honoured).
        pre_exit_days:             SELL-allow window for standard events; if
                                  ``None`` falls back to ``buy_window_days``.
        high_risk_buy_window_days: extra BUY-block window for high-risk events.
        high_risk_sell_window_days: SELL-allow window for high-risk events.
        trading_dates:             sorted list of trading-session dates.
        precomputed_ex_index:      fast event_id -> trading-date index lookup.

    Returns:
        Tuple of (kept_orders, decisions_log).
    """
    if not orders or not actions:
        return orders, []
    guarded: list[AlgoOrder] = []
    decisions: list[CorporateActionDecision] = []
    for order in orders:
        if order.side == "BUY":
            standard_window = buy_window_days
            high_risk_window = high_risk_buy_window_days if high_risk_buy_window_days is not None else 0
        else:
            standard_window = pre_exit_days if pre_exit_days is not None else sell_window_days
            high_risk_window = high_risk_sell_window_days if high_risk_sell_window_days is not None else 0
        union_window = max(standard_window, high_risk_window)
        if union_window <= 0:
            guarded.append(order)
            continue
        risky_by_symbol = corporate_action_window_lookup(
            actions,
            order.date,
            union_window,
            trading_dates,
            precomputed_ex_index=precomputed_ex_index,
        )
        risky_events_all = risky_by_symbol.get(order.symbol, [])
        if not risky_events_all:
            guarded.append(order)
            continue
        standard_events: list[CorporateAction] = []
        high_risk_events: list[CorporateAction] = []
        for event in risky_events_all:
            if corporate_action_risk_type(event) == "high":
                high_risk_events.append(event)
            else:
                standard_events.append(event)
        event_payloads = [corporate_action_event_payload(event) for event in risky_events_all]
        effective_high_risk_window = high_risk_window > 0
        if order.side == "BUY":
            blocked_reason = (
                f"non_cash_corporate_action_within_{union_window}_days"
                if not (effective_high_risk_window and high_risk_events and not standard_events)
                else f"high_risk_non_cash_corporate_action_within_{union_window}_days"
            )
            decisions.append(
                CorporateActionDecision(
                    sourceOrderId=order.orderId,
                    symbol=order.symbol,
                    side=order.side,
                    action="blocked",
                    reason=blocked_reason,
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
                reason=f"sell_allowed_before_non_cash_corporate_action_within_{union_window}_days",
                events=event_payloads,
            )
        )
    return guarded, decisions


def corporate_action_window_lookup(
    actions: list[CorporateAction],
    trade_date: str,
    window_days: int,
    trading_dates: list[str] | None = None,
    precomputed_ex_index: dict[int, int] | None = None,
) -> dict[str, list[CorporateAction]]:
    if trading_dates:
        return non_cash_events_in_trading_window(
            actions,
            trade_date,
            trading_dates,
            window_days=window_days,
            include_ex_date=True,
            precomputed_ex_index=precomputed_ex_index,
        )
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
    window_days: int = 14,
    sell_window_days: int | None = None,
    pre_exit_days: int | None = None,
    high_risk_sell_window_days: int | None = None,
    trading_dates: list[str] | None = None,
    precomputed_ex_index: dict[int, int] | None = None,
) -> tuple[list[DnseOrder], list[CorporateActionDecision]]:
    """Force-sell positions whose symbol has a non-cash CA within the wider of
    the standard pre-exit window or the high-risk window.

    The pre-exit window defaults to ``window_days`` (the backtest's
    ``corporateActionPreExitDays`` = 14) so the strategy exits standard
    events well in advance of the ex-date, capturing the pre-CA premium.
    High-risk events use the narrower ``high_risk_sell_window_days`` (5
    days) because their price action is concentrated in the last few
    sessions.
    """
    legacy = sell_window_days if sell_window_days is not None else window_days
    standard_window = pre_exit_days if pre_exit_days is not None else legacy
    high_risk_window = high_risk_sell_window_days if high_risk_sell_window_days is not None else 0
    exit_window_days = max(standard_window, high_risk_window)
    risky = corporate_action_window_lookup(
        actions,
        trade_date,
        exit_window_days,
        trading_dates,
        precomputed_ex_index=precomputed_ex_index,
    )
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
        # Tag high-risk events separately for the decision log.
        high_risk_events = [e for e in events if corporate_action_risk_type(e) == "high"]
        standard_events = [e for e in events if e not in high_risk_events]
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
                reason=(
                    f"high_risk_non_cash_corporate_action_within_{exit_window_days}_days"
                    if high_risk_events and not standard_events
                    else f"non_cash_corporate_action_within_{exit_window_days}_days"
                ),
                events=event_payloads,
            )
        )
    return forced_orders, decisions


def build_post_ca_rebuy_orders(
    state: CaRebuyState,
    data_dir: Path,
    trade_date: str,
    trading_dates: list[str],
    current_trading_index: int,
    config: StrategyConfig,
    loan_package_id: int,
    order_type: str = "LO",
    board_lot_size: int = 100,
) -> tuple[list["DnseOrder"], list[CorporateActionDecision]]:
    """Build BUY orders for any pending post-CA rebays ready to fire today.

    This is the live counterpart of the rebuy loop in ``backtest.py``. The
    state is mutated in-place: expired rebays are dropped, fired rebays are
    dropped (along with their reserves). The caller is responsible for
    ``state.save(...)`` after the call returns.

    Args:
        state:                  the live ``CaRebuyState`` instance.
        data_dir:               data directory (for cached price JSON files).
        trade_date:             today's trading session date (ISO string).
        trading_dates:          sorted list of trading-session dates.
        current_trading_index:  index of ``trade_date`` in ``trading_dates``.
        config:                 strategy config (drives the rebuy signal /
                                fixed-delay logic).
        loan_package_id:        DNSE loan package id attached to each order.
        order_type:             DNSE order type ("LO"/"ATO"/...).
        board_lot_size:         DNSE board lot size (100 default).

    Returns:
        (rebuy_dnse_orders, decisions_log)
    """
    decisions: list[CorporateActionDecision] = []
    orders: list[DnseOrder] = []

    # 1. Drop expired rebays first so the loop only sees live ones.
    expired_symbols = state.drop_expired(current_trading_index)
    for sym in expired_symbols:
        decisions.append(
            CorporateActionDecision(
                sourceOrderId=-(abs(hash(sym)) % 9_999_999),
                symbol=sym,
                side="BUY",
                action="rebuy_dropped",
                reason="reserve_expired",
                events=[],
            )
        )

    if not state.pendingRebuys:
        return orders, decisions

    min_days = max(0, getattr(config, "corporateActionRebuySignalMinDays", 0))
    max_days = max(min_days + 1, getattr(config, "corporateActionRebuySignalMaxDays", 10))
    delay_days = getattr(config, "corporateActionRebuyDelayDays", 0)
    delay_high_risk_only = getattr(config, "corporateActionRebuyDelayHighRiskOnly", False)
    signal_mode = getattr(config, "corporateActionRebuySignalMode", "fixed_delay")
    sector_by_symbol = build_symbol_sector_map(data_dir)

    for pending in list(state.pendingRebuys):
        try:
            ex_idx = next(i for i, d in enumerate(trading_dates) if d == pending.exDate)
        except StopIteration:
            # Ex-date unknown in trading calendar; skip and decide on next cycle.
            continue
        days_since_ex = current_trading_index - ex_idx

        # 2. Enforce min/max-days gates (only relevant for non-fixed-delay modes).
        if signal_mode != "fixed_delay":
            if days_since_ex < min_days:
                continue
            forced = days_since_ex >= max_days
            if not forced:
                sym_closes = load_symbol_closes(pending.symbol, data_dir)
                closes_window = closes_since_ex_window(
                    pending.symbol, pending.exDate, trading_dates, {pending.symbol: sym_closes},
                )
                triggered, reason = evaluate_post_ca_rebuy_signal(closes_window, config)
                if not triggered:
                    continue
                fire_reason = f"signal:{reason}"
        else:
            # Legacy fixed-delay path.
            effective_delay = delay_days
            if delay_high_risk_only and not pending.hasHighRisk:
                effective_delay = 0
            if days_since_ex < effective_delay:
                continue
            fire_reason = f"fixed_delay:{effective_delay}d"

        # 3. Build a BUY order at the latest cached close for the symbol.
        sym_closes = load_symbol_closes(pending.symbol, data_dir)
        latest_close = sym_closes.get(trade_date)
        if latest_close is None:
            # Fall back to the most recent close we have.
            for d in reversed(trading_dates):
                if d in sym_closes:
                    latest_close = sym_closes[d]
                    break
        if latest_close is None or latest_close <= 0:
            decisions.append(
                CorporateActionDecision(
                    sourceOrderId=-(abs(hash(pending.symbol)) % 9_999_999),
                    symbol=pending.symbol,
                    side="BUY",
                    action="rebuy_skipped",
                    reason="no_recent_close_for_rebuy",
                    events=[],
                )
            )
            continue

        # Round price down to the nearest 100 VND (DNSE tick).
        dnse_price = max(board_lot_size, int(latest_close // 100 * 100))
        # Round quantity to a board lot.
        quantity = max(board_lot_size, int(pending.targetValue // dnse_price // board_lot_size * board_lot_size))
        if quantity <= 0:
            decisions.append(
                CorporateActionDecision(
                    sourceOrderId=-(abs(hash(pending.symbol)) % 9_999_999),
                    symbol=pending.symbol,
                    side="BUY",
                    action="rebuy_skipped",
                    reason="target_value_too_small_for_lot",
                    events=[],
                )
            )
            continue

        order = DnseOrder(
            symbol=pending.symbol,
            side="NB",
            quantity=quantity,
            price=dnse_price,
            loanPackageId=loan_package_id,
            orderType=order_type,
            sourceOrderId=-(abs(hash(pending.symbol + trade_date)) % 9_999_999),
            sourceDate=trade_date,
            reason=f"corporate_action_rebuy_{fire_reason}",
        )
        orders.append(order)
        decisions.append(
            CorporateActionDecision(
                sourceOrderId=order.sourceOrderId,
                symbol=pending.symbol,
                side="BUY",
                action="rebuy_emitted",
                reason=fire_reason,
                events=[],
            )
        )

        # Mark the pending as fired so subsequent cycles don't re-emit.
        # The actual drop happens after we know whether the order filled.
        pending.note = (pending.note + ";" if pending.note else "") + f"fired:{trade_date}"

    return orders, decisions


def mark_pending_rebuys_filled(
    state: CaRebuyState,
    filled_order_ids_by_symbol: dict[str, int],
) -> int:
    """Drop any pending rebuy whose BUY order has been confirmed filled.

    Called by the daily pipeline after ``execute_orders_with_ledger`` so that
    pending rebays whose BUY never filled stay queued for the next cycle.
    Returns the number of pending rebays dropped.
    """
    dropped = 0
    for symbol in list(filled_order_ids_by_symbol.keys()):
        pending = state.pending_for(symbol)
        if pending is None:
            continue
        state.drop(symbol)
        dropped += 1
    return dropped


def enqueue_pending_rebuys_from_force_exits(
    state: CaRebuyState,
    forced_exit_orders: list["DnseOrder"],
    trading_dates: list[str],
    current_trading_index: int,
    config: StrategyConfig,
    symbol_ex_dates: dict[str, str],
    symbol_high_risk: dict[str, bool],
) -> int:
    """Enqueue a pending rebuy for each forced-exit order.

    Called by the daily pipeline right after ``build_corporate_action_exit_orders``
    so the rebuy queue mirrors the force-sells the live bot actually emits.
    Returns the number of newly-queued pending rebays.
    """
    reserve_expiry_days = getattr(config, "corporateActionReserveExpiryDays", 10)
    queued = 0
    for order in forced_exit_orders:
        sym = order.symbol
        ex_date = symbol_ex_dates.get(sym)
        if not ex_date:
            continue
        target_value = float(order.price * order.quantity)
        has_high_risk = symbol_high_risk.get(sym, False)
        expiry_index = current_trading_index + reserve_expiry_days
        state.enqueue(
            symbol=sym,
            exit_date=trading_dates[current_trading_index],
            exit_price=float(order.price),
            target_value=target_value,
            ex_date=ex_date,
            has_high_risk=has_high_risk,
            sector="unknown",
            expiry_index=expiry_index,
            note=f"queued_after_force_exit:{order.sourceDate}",
        )
        queued += 1
    return queued


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
