"""Portfolio snapshot helpers — fetch + format account state from DNSE.

Used in both dry-run and live modes so the bot always reports:
- cash available
- positions with live market value
- unrealized P&L
- total portfolio value
"""
from __future__ import annotations

from typing import Any
from datetime import datetime, UTC


def _first_field(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_call(client, method_name: str, *args, **kwargs):
    """Call a DnseClient method; return None on any failure (best-effort)."""
    try:
        return getattr(client, method_name)(*args, **kwargs)
    except Exception:
        return None


def build_portfolio_snapshot(
    client: Any,
    account_no: str,
    top_n_positions: int = 10,
) -> dict[str, Any]:
    """Fetch live account state from DNSE and compute P&L.

    Returns a dict with shape::

        {
            "account": "...",
            "name": "...",
            "fetchedAt": "ISO ts",
            "balance": {totalCash, availableCash, withdrawableCash,
                        secureAmount, orderSecured, ...},
            "positions": [
                {symbol, quantity, avgPrice, marketPrice,
                 marketValue, unrealizedPnl, unrealizedPnlPct}
            ],
            "totals": {stockValue, portfolioValue,
                       totalUnrealizedPnl, totalUnrealizedPnlPct,
                       positionCount},
            "errors": [<endpoints that failed>],
        }

    Always returns a dict — never raises. Failed endpoints are listed in
    `errors` so the caller can surface them.
    """
    snap: dict[str, Any] = {
        "account": account_no,
        "name": None,
        "fetchedAt": datetime.now(UTC).isoformat(),
        "balance": {
            "totalCash": 0,
            "availableCash": 0,
            "withdrawableCash": 0,
            "secureAmount": 0,
            "orderSecured": 0,
            "depositInterest": 0,
            "totalDebt": 0,
        },
        "positions": [],
        "totals": {
            "stockValue": 0,
            "portfolioValue": 0,
            "totalUnrealizedPnl": 0,
            "totalUnrealizedPnlPct": 0.0,
            "positionCount": 0,
        },
        "errors": [],
    }

    # 1. Account / investor name
    accounts = _safe_call(client, "get_accounts")
    if accounts is None:
        snap["errors"].append("/accounts")
    elif isinstance(accounts, dict):
        snap["name"] = accounts.get("name") or accounts.get("investorId")

    # 2. Balance
    bal = _safe_call(client, "get_balances", account_no)
    if bal is None:
        snap["errors"].append("/accounts/{acct}/balances")
    elif isinstance(bal, dict):
        stock = bal.get("stock", {}) or {}
        snap["balance"] = {
            "totalCash": _int(stock.get("totalCash")),
            "availableCash": _int(stock.get("availableCash")),
            "withdrawableCash": _int(stock.get("withdrawableCash")),
            "secureAmount": _int(stock.get("secureAmount")),
            "orderSecured": _int(stock.get("orderSecured")),
            "depositInterest": _int(stock.get("depositInterest")),
            "totalDebt": _int(stock.get("totalDebt")),
        }

    # 3. Positions + live prices
    pos_payload = _safe_call(client, "get_positions", account_no)
    if pos_payload is None:
        snap["errors"].append("/accounts/{acct}/positions")
        return snap

    rows: list[dict[str, Any]] = []
    for key in ("data", "items", "positions", "content"):
        value = pos_payload.get(key) if isinstance(pos_payload, dict) else None
        if isinstance(value, list):
            rows.extend(value)
        elif isinstance(value, dict):
            rows.append(value)
    if "symbol" in pos_payload or "stockSymbol" in pos_payload:
        rows.append(pos_payload)

    enriched: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(_first_field(row, ("symbol", "stockSymbol", "ticker", "secSymbol")) or "").strip().upper()
        if not symbol:
            continue
        quantity = _int(_first_field(row, ("quantity", "qty", "totalQuantity")))
        if quantity <= 0:
            continue
        avg_price = _float(_first_field(row, (
            "avgPrice", "averagePrice", "costPrice", "costBasisPrice",
            "costBasis", "avgCost",
        )))
        # Live market price (try multiple fallbacks)
        market_price = None
        for m in ("get_latest_quote", "get_close_price"):
            res = _safe_call(client, m, symbol)
            if isinstance(res, dict):
                boards = res.get("quotes") or []
                if boards and isinstance(boards[0], dict):
                    bid_levels = boards[0].get("bid") or []
                    offer_levels = boards[0].get("offer") or []
                    if bid_levels and offer_levels:
                        bid = _float(bid_levels[0].get("price"))
                        offer = _float(offer_levels[0].get("price"))
                        if bid > 0 and offer > 0:
                            market_price = (bid + offer) / 2.0
                            break
        market_value = market_price * quantity if market_price else 0.0
        cost_basis = avg_price * quantity if avg_price > 0 else 0.0
        unrealized_pnl = (market_price - avg_price) * quantity if (market_price and avg_price > 0) else 0.0
        unrealized_pnl_pct = (
            ((market_price - avg_price) / avg_price) * 100.0
            if (market_price and avg_price > 0) else 0.0
        )
        enriched.append({
            "symbol": symbol,
            "quantity": quantity,
            "avgPrice": avg_price,
            "marketPrice": market_price,
            "marketValue": market_value,
            "costBasis": cost_basis,
            "unrealizedPnl": unrealized_pnl,
            "unrealizedPnlPct": unrealized_pnl_pct,
        })

    # Sort by market value desc
    enriched.sort(key=lambda r: r["marketValue"], reverse=True)
    snap["positions"] = enriched
    snap["totals"]["positionCount"] = len(enriched)
    snap["totals"]["stockValue"] = int(sum(p["marketValue"] for p in enriched))

    bal = snap["balance"]
    snap["totals"]["portfolioValue"] = bal["availableCash"] + snap["totals"]["stockValue"]
    cost_basis_total = sum(p["costBasis"] for p in enriched)
    if cost_basis_total > 0:
        snap["totals"]["totalUnrealizedPnl"] = int(snap["totals"]["stockValue"] - cost_basis_total)
        snap["totals"]["totalUnrealizedPnlPct"] = (
            (snap["totals"]["stockValue"] - cost_basis_total) / cost_basis_total * 100.0
        )
    else:
        snap["totals"]["totalUnrealizedPnl"] = 0
        snap["totals"]["totalUnrealizedPnlPct"] = 0.0

    return snap
