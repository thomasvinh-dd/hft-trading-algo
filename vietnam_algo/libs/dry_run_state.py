"""Dry-run state — local simulated portfolio for the bot in dry-run mode.

In dry-run mode (no ``--live-submit`` flag, the default), the bot does NOT
talk to DNSE for cash/positions. Instead it tracks a local simulated
portfolio so the EOD/start-of-day summaries show a meaningful state and
the autofin sizing logic has cash to work with.

This module provides:

  * ``DryRunPosition`` — one per symbol, with quantity, avg cost, last
    price, cost basis, and unrealized P&L.
  * ``DryRunState`` — the full simulated portfolio: initial NAV, current
    cash, positions dict, realized P&L, last close-prices cache, and
    timestamp. Persists to ``data/live/dry_run_state.json``.
  * ``apply_orders_to_state()`` — apply a list of BUY/SELL orders from
    ``latest_orders.json`` to a DryRunState at the recommended price
    (assuming the order fills).
  * ``mark_to_market()`` — update each position's ``last_price`` and
    unrealized P&L from a price dict.
  * ``build_dry_run_snapshot()`` — return a dict shaped exactly like
    ``build_portfolio_snapshot()`` so the EOD/start-of-day formatters
    can use it transparently.

ENV vars
--------

  * ``DRY_RUN_INITIAL_NAV`` (int, default 100_000_000) — starting cash
    in VND. Used the first time the bot runs and persists thereafter.
  * ``MOCK_NAV`` (int, optional) — backward-compatible alias for
    ``DRY_RUN_INITIAL_NAV`` (used by the autofin adapter).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_INITIAL_NAV = 100_000_000  # 100M VND


@dataclass
class DryRunPosition:
    symbol: str
    quantity: int = 0
    avg_price: float = 0.0
    last_price: float = 0.0
    cost_basis: float = 0.0  # quantity * avg_price

    @property
    def market_value(self) -> float:
        return self.last_price * self.quantity if self.last_price > 0 else 0.0

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_price) * self.quantity if self.last_price > 0 else 0.0


@dataclass
class DryRunState:
    initial_nav: float = DEFAULT_INITIAL_NAV
    cash: float = DEFAULT_INITIAL_NAV
    positions: dict[str, DryRunPosition] = field(default_factory=dict)
    realized_pnl: float = 0.0
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    # ----- computed -----
    @property
    def stock_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def portfolio_value(self) -> float:
        return self.cash + self.stock_value

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    @property
    def total_pnl(self) -> float:
        # Total P&L = portfolio_value - initial_nav, NOT
        # realized + unrealized. The latter ignores trading fees
        # (BUYs deduct 0.15% from cash but SELLs credit only the
        # raw proceeds, so the fee becomes a silent loss unless
        # we measure against the initial NAV). See 2026-09-14
        # incident where the bot reported "total P&L: 0" while the
        # NAV was actually 148k below initial.
        return self.portfolio_value - self.initial_nav

    @property
    def total_return_pct(self) -> float:
        if self.initial_nav <= 0:
            return 0.0
        return (self.portfolio_value - self.initial_nav) / self.initial_nav

    # ----- persistence -----
    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_nav": self.initial_nav,
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "closed_trades": list(self.closed_trades)[-200:],
            "positions": {
                sym: asdict(p) for sym, p in self.positions.items()
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DryRunState:
        positions = {
            sym: DryRunPosition(**row)
            for sym, row in (payload.get("positions") or {}).items()
            if isinstance(row, dict)
        }
        return cls(
            initial_nav=float(payload.get("initial_nav") or DEFAULT_INITIAL_NAV),
            cash=float(payload.get("cash") or 0),
            positions=positions,
            realized_pnl=float(payload.get("realized_pnl") or 0),
            closed_trades=list(payload.get("closed_trades") or []),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )


# ----- file I/O -----


def _state_path(data_dir: Path, strategy_name: str = "") -> Path:
    """Per-strategy dry-run state file.

    Two strategies running on the same VPS (autofin_signal + momentum)
    must NOT share ``dry_run_state.json`` — otherwise each bot's
    ``_apply_dry_run_fills`` call clobbers the other bot's cash and
    positions. We default to a strategy-suffixed file
    (``dry_run_state.<strategy>.json``) and fall back to the legacy
    un-suffixed path when ``strategy_name`` is empty (used by unit
    tests that don't care about isolation).
    """
    if not strategy_name:
        return data_dir / "live" / "dry_run_state.json"
    safe = "".join(
        c if c.isalnum() or c in ("-", "_") else "_"
        for c in str(strategy_name)
    ).strip("_") or "default"
    return data_dir / "live" / f"dry_run_state.{safe}.json"


def load_dry_run_state(
    data_dir: Path, strategy_name: str = ""
) -> DryRunState | None:
    """Load the persisted dry-run state. Returns None if file missing/empty."""
    path = _state_path(data_dir, strategy_name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    return DryRunState.from_dict(payload)


def save_dry_run_state(
    state: DryRunState, data_dir: Path, strategy_name: str = ""
) -> Path:
    state.updated_at = datetime.now(UTC).isoformat()
    if not state.created_at:
        state.created_at = state.updated_at
    path = _state_path(data_dir, strategy_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    return path


def init_dry_run_state(
    data_dir: Path,
    initial_nav: float | None = None,
    strategy_name: str = "",
) -> DryRunState:
    """Initialize a fresh dry-run state. Honours ``DRY_RUN_INITIAL_NAV`` /
    ``MOCK_NAV`` env vars. If state already exists, returns it untouched
    (use ``reset_dry_run_state`` if you want to wipe and start over)."""
    existing = load_dry_run_state(data_dir, strategy_name)
    if existing is not None:
        return existing
    nav = initial_nav if initial_nav is not None else _initial_nav_from_env()
    state = DryRunState(initial_nav=nav, cash=nav)
    save_dry_run_state(state, data_dir, strategy_name)
    return state


def reset_dry_run_state(
    data_dir: Path,
    initial_nav: float | None = None,
    strategy_name: str = "",
) -> DryRunState:
    """Wipe the dry-run state and start fresh with the given NAV (or env default)."""
    nav = initial_nav if initial_nav is not None else _initial_nav_from_env()
    state = DryRunState(initial_nav=nav, cash=nav)
    save_dry_run_state(state, data_dir, strategy_name)
    return state


def _initial_nav_from_env() -> float:
    raw = (
        os.environ.get("DRY_RUN_INITIAL_NAV")
        or os.environ.get("MOCK_NAV")
        or str(DEFAULT_INITIAL_NAV)
    ).strip()
    try:
        return float(raw)
    except ValueError:
        return float(DEFAULT_INITIAL_NAV)


# ----- order application -----


def _dedup_key(order: dict[str, Any]) -> str:
    """Stable per-order key used to detect already-applied orders.

    Same shape as ``latest_orders.json`` rows: date + symbol + side +
    qty + price. The price/qty are kept in the key because the EOD
    summary shows the EXACT orders being submitted; if a row with the
    same shape has been applied already, we don't want to apply it again.
    """
    return (
        f"{order.get('date', '')}|{order.get('symbol', '')}|"
        f"{order.get('side', '')}|{order.get('quantity', '')}|"
        f"{order.get('price', '')}"
    )


def _get_applied_keys(state: DryRunState) -> set[str]:
    keys: set[str] = set()
    for t in state.closed_trades:
        if isinstance(t, dict) and t.get("applied_keys"):
            keys.update(str(k) for k in t["applied_keys"])
    return keys


def apply_orders_to_state(
    state: DryRunState,
    orders: Iterable[dict[str, Any]],
    prices: dict[str, float] | None = None,
    board_lot: int = 100,
    fees_bps: float = 0.0,
) -> list[dict[str, Any]]:
    """Apply BUY/SELL orders from ``latest_orders.json`` to a DryRunState.

    Each order dict is expected to have:
      * ``side`` ("BUY" or "SELL" or "NB"/"NS" — autofin's codes)
      * ``symbol``
      * ``price``
      * ``quantity``
      * ``date`` (optional — used for dedup; orders with the same
        date/symbol/side/qty/price are skipped if already applied)

    ``board_lot`` is the lot size to round to on cash-constrained
    clipped buys (default 100 — Vietnamese board lot). ``fees_bps`` is
    the round-trip fee in basis points added to the cash debit on each
    BUY so the cash column tracks the strategy's actual fill cost.

    Returns a list of fill dicts. Each fill has a ``status`` field:
      * ``"applied"`` — full qty filled
      * ``"clipped"``  — qty was reduced due to cash constraint, rounded
        down to a board lot. The fill has ``requested_qty`` recorded.
      * ``"skipped"``  — couldn't apply at all (no cash, no position,
        already applied, invalid input, etc.). Reason in ``reason``.
    """
    fills: list[dict[str, Any]] = []
    applied_keys = _get_applied_keys(state)
    applied_now: list[str] = []
    for o in orders:
        sym = str(o.get("symbol") or "").strip().upper()
        if not sym:
            continue
        side_raw = str(o.get("side") or "").strip().upper()
        side = "BUY" if side_raw in ("BUY", "NB", "B") else (
            "SELL" if side_raw in ("SELL", "NS", "S") else ""
        )
        if not side:
            continue
        try:
            qty = int(float(o.get("quantity") or 0))
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        try:
            price = float(o.get("price") or 0)
        except (TypeError, ValueError):
            continue
        # Market SELLs (full_close / reduce) come in with price=0
        # because autofin emits "sell at market". Falling back to the
        # position's last_price keeps the dry-run state in sync;
        # falling back to avg_price is the last-resort. BUYs still
        # require a real price (>0) — we can't spend money we don't
        # know the cost of.
        if price <= 0 and side == "BUY":
            continue
        pos_for_price = state.positions.get(sym)
        if price <= 0:
            if pos_for_price is not None:
                price = (
                    pos_for_price.last_price
                    if pos_for_price.last_price > 0
                    else pos_for_price.avg_price
                )
            if price <= 0:
                continue

        # Dedup: skip if we've already applied this exact order.
        dedup = _dedup_key({**o, "symbol": sym, "side": side_raw})
        if dedup in applied_keys:
            fills.append({
                "status": "skipped", "reason": "already_applied",
                "symbol": sym, "side": side, "qty": qty, "price": price,
            })
            continue

        pos = state.positions.get(sym) or DryRunPosition(symbol=sym)
        requested_qty = qty
        if side == "BUY":
            cost = qty * price * (1 + fees_bps / 10_000)
            clipped = False
            if state.cash < cost:
                # cash-constrained: clip qty to what we can afford, then
                # round down to the nearest board lot. Without the board
                # lot step, the simulated portfolio ends up holding
                # 146-share VIC positions etc., which is unrealistic.
                max_qty = int(state.cash // (price * (1 + fees_bps / 10_000)))
                if max_qty <= 0:
                    fills.append({
                        "status": "skipped", "reason": "insufficient_cash",
                        "symbol": sym, "side": side, "qty": qty, "price": price,
                        "requested_qty": requested_qty,
                    })
                    continue
                if board_lot > 1:
                    lot_qty = (max_qty // board_lot) * board_lot
                else:
                    lot_qty = max_qty
                if lot_qty <= 0:
                    fills.append({
                        "status": "skipped", "reason": "below_board_lot",
                        "symbol": sym, "side": side, "qty": qty, "price": price,
                        "requested_qty": requested_qty,
                    })
                    continue
                qty = min(qty, lot_qty)
                cost = qty * price * (1 + fees_bps / 10_000)
                clipped = qty < requested_qty
            new_qty = pos.quantity + qty
            pos.avg_price = (
                (pos.avg_price * pos.quantity + price * qty) / new_qty
                if new_qty > 0 else 0.0
            )
            pos.quantity = new_qty
            pos.cost_basis = pos.quantity * pos.avg_price
            state.cash -= cost
            fill = {
                "status": "clipped" if clipped else "applied",
                "side": "BUY", "symbol": sym, "qty": qty,
                "price": price, "value": cost,
            }
            if clipped:
                fill["requested_qty"] = requested_qty
            fills.append(fill)
            applied_keys.add(dedup)
            applied_now.append(dedup)
        else:  # SELL
            sell_qty = min(qty, pos.quantity)
            if sell_qty <= 0:
                fills.append({
                    "status": "skipped", "reason": "no_position",
                    "symbol": sym, "side": side, "qty": qty, "price": price,
                    "requested_qty": requested_qty,
                })
                continue
            proceeds = sell_qty * price
            realized = (price - pos.avg_price) * sell_qty if pos.avg_price > 0 else 0.0
            pos.quantity -= sell_qty
            pos.cost_basis = pos.quantity * pos.avg_price
            if pos.quantity == 0:
                state.closed_trades.append({
                    "symbol": sym,
                    "avg_cost": pos.avg_price,
                    "sell_price": price,
                    "qty": sell_qty,
                    "realized_pnl": realized,
                })
                pos.avg_price = 0.0
            state.cash += proceeds
            state.realized_pnl += realized
            fills.append({
                "status": "applied",
                "side": "SELL", "symbol": sym, "qty": sell_qty,
                "price": price, "value": proceeds,
                "realized_pnl": realized,
            })
            applied_keys.add(dedup)
            applied_now.append(dedup)

        # update last_price from prices dict or fill price
        pos.last_price = (
            float(prices.get(sym, 0)) if prices and prices.get(sym, 0) > 0
            else price
        )
        if pos.quantity <= 0 and pos.avg_price <= 0:
            state.positions.pop(sym, None)
        else:
            state.positions[sym] = pos

    # Persist the dedup keys in a closed_trades marker entry so the
    # next call to apply_orders_to_state sees them.
    if applied_now:
        state.closed_trades.append({
            "note": "_applied_keys",
            "applied_keys": applied_now,
        })
    return fills


def mark_to_market(state: DryRunState, prices: dict[str, float]) -> None:
    """Update each position's last_price and unrealized P&L from a prices dict."""
    for sym, pos in state.positions.items():
        if sym in prices and prices[sym] > 0:
            pos.last_price = float(prices[sym])


# ----- snapshot adapter (same shape as build_portfolio_snapshot) -----


def build_dry_run_snapshot(
    state: DryRunState,
    account_no: str = "",
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Return a dict that matches ``build_portfolio_snapshot``'s shape so
    the same EOD/start-of-day formatters work for both modes.

    Adds a ``mode`` field set to ``"dry_run"`` so callers can branch if
    they need to.
    """
    enriched = sorted(
        [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avgPrice": p.avg_price,
                "marketPrice": p.last_price,
                "marketValue": p.market_value,
                "costBasis": p.cost_basis,
                "unrealizedPnl": p.unrealized_pnl,
                "unrealizedPnlPct": (
                    (p.last_price - p.avg_price) / p.avg_price * 100.0
                    if p.avg_price > 0 and p.last_price > 0 else 0.0
                ),
            }
            for p in state.positions.values()
            if p.quantity > 0
        ],
        key=lambda r: r["marketValue"],
        reverse=True,
    )
    stock_value = sum(p.market_value for p in state.positions.values())
    cost_basis_total = sum(p.cost_basis for p in state.positions.values())
    total_unrealized = sum(p.unrealized_pnl for p in state.positions.values())

    return {
        "mode": "dry_run",
        "account": account_no,
        "name": "DRY-RUN simulated account",
        "fetchedAt": state.updated_at or datetime.now(UTC).isoformat(),
        "balance": {
            "totalCash": int(state.cash),
            "availableCash": int(state.cash),
            "withdrawableCash": int(state.cash),
            "secureAmount": 0,
            "orderSecured": 0,
            "depositInterest": 0,
            "totalDebt": 0,
        },
        "positions": enriched,
        "totals": {
            "stockValue": int(stock_value),
            "portfolioValue": int(state.portfolio_value),
            "totalUnrealizedPnl": int(total_unrealized),
            "totalUnrealizedPnlPct": (
                (total_unrealized / cost_basis_total * 100.0)
                if cost_basis_total > 0 else 0.0
            ),
            "positionCount": len(enriched),
            "initialNav": int(state.initial_nav),
            "realizedPnl": int(state.realized_pnl),
            "totalPnl": int(state.total_pnl),
            "totalReturnPct": state.total_return_pct,
        },
        "errors": list(errors or []),
    }
