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


def _session_close_vnd(client: Any, symbol: str) -> float | None:
    """Return the current session close in absolute VND, preferring G1.

    DNSE's positions endpoint can contain a stale ``marketPrice`` when the
    position was not touched during the latest session (2026-11-10: AAS
    reported 9,900 from the prior close instead of 10,700). The close
    endpoint is authoritative for the current session and returns kVND.
    """
    res = _safe_call(client, "get_close_price", symbol)
    if not isinstance(res, dict):
        return None

    rows = res.get("prices") or []
    fallback: float | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        close = _float(row.get("closePrice"))
        if close <= 0:
            continue
        if fallback is None:
            fallback = close
        if str(row.get("boardId") or "").upper() == "G1":
            return close * 1000.0
    return fallback * 1000.0 if fallback else None


def _quote_mid_vnd(client: Any, symbol: str) -> float | None:
    """Return the live bid/offer midpoint in absolute VND, preferring G1.

    DNSE does not guarantee board ordering; G4 may precede G1 and can have
    a different (or empty) order book. Quote levels are returned in kVND.
    """
    res = _safe_call(client, "get_latest_quote", symbol)
    if not isinstance(res, dict):
        return None

    boards = [b for b in (res.get("quotes") or []) if isinstance(b, dict)]
    target = next(
        (b for b in boards if str(b.get("boardId") or "").upper() == "G1"),
        None,
    )
    if target is None:
        target = next(
            (b for b in boards if b.get("bid") and b.get("offer")),
            None,
        )
    if target is None:
        return None

    bid_levels = target.get("bid") or []
    offer_levels = target.get("offer") or []
    if not bid_levels or not offer_levels:
        return None
    bid = _float(bid_levels[0].get("price"))
    offer = _float(offer_levels[0].get("price"))
    if bid <= 0 or offer <= 0:
        return None
    return (bid + offer) / 2.0 * 1000.0


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
        # 2026-10-13: DNSE's positions endpoint uses
        # ``openQuantity`` (currently-held shares) and
        # ``accumulateQuantity`` (lifetime buy total, may
        # include sold lots). The previous parser only checked
        # ``quantity`` / ``qty`` / ``totalQuantity``, none of
        # which exist in the actual response — so positions
        # were silently dropped and the strategy thought the
        # account was flat when it was holding 100 ABB.
        # Prefer ``openQuantity`` (the held count, excludes
        # already-sold shares) and fall back to
        # ``accumulateQuantity`` minus ``closedQuantity``.
        open_qty = _int(_first_field(row, ("openQuantity", "openQuantity",)))
        if open_qty <= 0:
            acc_qty = _int(_first_field(row, ("accumulateQuantity",)))
            closed_qty = _int(_first_field(row, ("closedQuantity",)))
            open_qty = max(0, acc_qty - closed_qty)
        quantity = open_qty if open_qty > 0 else _int(_first_field(row, ("quantity", "qty", "totalQuantity")))
        if quantity <= 0:
            continue
        avg_price = _float(_first_field(row, (
            # 2026-10-14: prefer ``breakEvenPrice`` over
            # ``costPrice`` so the P&L matches the DNSE app.
            # ``costPrice`` is the raw average buy price; the
            # buyer fee (~15 bps on Vietnamese exchanges) is
            # NOT in it. ``breakEvenPrice`` = cost + accumulated
            # fees = the price at which the position would
            # actually break even. The DNSE app and the
            # positions API both report this. Using
            # ``costPrice`` understated the P&L by 24-50 VND
            # per share (e.g. ABB 100 @ 17,700 cost vs.
            # 17,724.4 break-even → P&L off by 2,400 VND per
            # 100 shares). The user spotted the discrepancy
            # on 2026-10-14 14:00 ICT: DNSE app showed ABB
            # avg=17,724, bot showed 17,700 → -12,426 VND on
            # the app vs -10,000 VND on the bot. The fix:
            # read breakEvenPrice first, fall back to the
            # legacy ``costPrice``/``avgPrice`` aliases for
            # older payloads.
            "breakEvenPrice", "avgPrice", "averagePrice",
            "costPrice", "costBasisPrice", "costBasis", "avgCost",
        )))
        # 2026-10-13: market price resolution (units + source).
        #
        # DNSE returns prices in DIFFERENT UNITS across endpoints:
        #   - /accounts/{acc}/positions returns ``marketPrice`` /
        #     ``costPrice`` / ``breakEvenPrice`` in absolute VND
        #     (e.g. ``31050`` = 31,050 VND).
        #   - /price/{sym}/quotes/latest and /price/{sym}/close
        #     return prices in THOUSANDS OF VND (kVND), so
        #     ``31.1`` = 31,100 VND.
        #
        # The previous portfolio code used ``get_latest_quote``
        # directly without the ``× 1000`` conversion, so the
        # reported market price was off by 1000× (e.g. BAF mkt
        # showed as ``31`` instead of ``31,050``), which made
        # the unrealized P&L look like ``-99.90%`` on every
        # position in the EOD report (2026-10-13 21:25 ICT
        # incident).
        #
        # 2026-11-10 fix: do not blindly trust the positions endpoint's
        # ``marketPrice``. DNSE only refreshes that field when a position
        # is touched, so an untouched position can retain the previous
        # session's close (AAS reported 9,900 instead of the real 10,700).
        # Prefer the authoritative current-session close, then use the
        # positions field, then a G1-preferred live quote as fallbacks.
        market_price: float | None = _session_close_vnd(client, symbol)
        if not market_price or market_price <= 0:
            positions_mkt = _float(_first_field(row, ("marketPrice", "market_price", "lastPrice")))
            if positions_mkt and positions_mkt > 0:
                market_price = positions_mkt
        if not market_price or market_price <= 0:
            market_price = _quote_mid_vnd(client, symbol)
        # Last-resort fallback: if neither source returned a
        # market price, use the cost basis (so unrealized P&L
        # is 0 instead of misleadingly negative).
        if not market_price or market_price <= 0:
            market_price = avg_price if avg_price and avg_price > 0 else 0.0
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
