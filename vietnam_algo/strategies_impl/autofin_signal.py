"""Autofin signal strategy — mirror autofin.vn BUY/SELL signals to DNSE orders.

Per the user's spec:
  "the sell, buy, nav of portfolio we will follow the signal"

Each cycle:
  1. Authenticate with autofin.vn (cached, refreshes ~weekly)
  2. Fetch active signals (statusDeal.isClosed=False) + waiting
  3. For each signal:
     - isWaittingBuy=True → generate BUY order (using supply zone mid-price)
     - isWaittingSell=True → generate SELL order (using take-profit zone)
     - statusDeal.isClosed=False (autofin holds position) → mirror:
       target_qty = round(nav × rateForNav / price / 100) × 100   # board lots
       delta = target_qty - current_qty_held
       if delta > 0  → BUY delta
       if delta < 0  → SELL abs(delta)
  4. Write orders to data/live/latest_orders.autofin.csv
  5. The existing DNSE submit pipeline takes over from there
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..autofin_signals import AutofinClient, AutofinSignal, parse_signal
from ..config import StrategyConfig
from ..daily_engine import EngineConfig
from ..notifier import notify
from ..portfolio import build_portfolio_snapshot
from ..strategy_base import StrategyAdapter


DEFAULT_BOARD_LOT = 100


def _load_env_credentials() -> tuple[str, str]:
    email = os.environ.get("AUTOFIN_EMAIL", "").strip()
    password = os.environ.get("AUTOFIN_PASSWORD", "").strip()
    return email, password


def _round_to_lots(quantity: int, board_lot: int = DEFAULT_BOARD_LOT) -> int:
    """Round a quantity to the nearest board lot (down to avoid over-buying)."""
    if quantity <= 0:
        return 0
    return (quantity // board_lot) * board_lot


def _signal_target_price(s: AutofinSignal, side: str) -> float:
    """Pick a sensible limit price for the order.

    BUY  → use supply zone high (we'll buy at the top of their buy range)
    SELL → use take-profit zone low (we'll sell at the bottom of their TP range)
    """
    if side == "NB":
        return s.supply_to or s.supply_from
    if side == "NS":
        return s.tp_from or s.tp_to
    return 0.0


def _compute_order_plan(
    signals: list[AutofinSignal],
    current_positions: dict[str, int],
    nav_vnd: float,
    board_lot: int = DEFAULT_BOARD_LOT,
) -> list[dict[str, Any]]:
    """Translate autofin signals into concrete DNSE order rows."""
    rows: list[dict[str, Any]] = []
    for s in signals:
        if not s.symbol:
            continue
        current_qty = current_positions.get(s.symbol, 0)

        # 1. explicit waiting triggers
        if s.is_waitting_buy:
            rows.append({
                "symbol": s.symbol, "side": "NB",
                "price": int(round(_signal_target_price(s, "NB"))),
                "quantity": board_lot,  # default 1 lot on entry signal
                "reason": f"autofin_wait_buy: {s.last_recomment[:80]}",
            })
            continue
        if s.is_waitting_sell:
            sell_qty = current_qty if current_qty > 0 else board_lot
            rows.append({
                "symbol": s.symbol, "side": "NS",
                "price": int(round(_signal_target_price(s, "NS"))),
                "quantity": _round_to_lots(sell_qty, board_lot),
                "reason": f"autofin_wait_sell: {s.last_recomment[:80]}",
            })
            continue

        # 2. mirror autofin's open position to our NAV × rateForNav
        if s.rate_for_nav <= 0 or nav_vnd <= 0:
            continue
        target_value = nav_vnd * s.rate_for_nav
        ref_price = s.supply_to or s.tp_from or _signal_target_price(s, "NB")
        if ref_price <= 0:
            continue
        target_qty = _round_to_lots(int(target_value / ref_price), board_lot)
        delta = target_qty - current_qty
        if abs(delta) < board_lot:
            continue
        side = "NB" if delta > 0 else "NS"
        rows.append({
            "symbol": s.symbol, "side": side,
            "price": int(round(ref_price)),
            "quantity": abs(delta),
            "reason": f"autofin_mirror: {s.last_recomment[:80]}",
        })
    return rows


class AutofinSignalAdapter(StrategyAdapter):
    """Mirror autofin.vn signals to DNSE orders.

    Cycle behaviour:
      1. Authenticate with autofin.vn (cached, refreshes ~weekly)
      2. Fetch active signal-static-suggest list
      3. For each signal, derive target DNSE position from rateForNav × NAV
      4. Compare with our current DNSE positions, generate BUY/SELL deltas
      5. Write orders to data/live/latest_orders.autofin.csv
    """

    name = "autofin_signal"
    description = "Mirror autofin.vn BUY/SELL signals to DNSE orders"
    env_filename = ".env.autofin-signal"

    def __init__(self) -> None:
        self._client: AutofinClient | None = None

    def _client_lazy(self) -> AutofinClient:
        if self._client is None:
            email, password = _load_env_credentials()
            if not email or not password:
                raise RuntimeError(
                    "AUTOFIN_EMAIL and AUTOFIN_PASSWORD must be set in env (.env.autofin-signal)"
                )
            self._client = AutofinClient(email=email, password=password)
        return self._client

    def generate_orders(
        self,
        config: EngineConfig,
        strategy_config: StrategyConfig,
        trade_date: str | None = None,
    ) -> dict[str, Any]:
        client = self._client_lazy()
        signals = client.fetch_signals(page_size=20)
        if not signals:
            notify("⚠️ Vietnam Algo: autofin returned 0 signals")
            return {
                "status": "no_signals",
                "tradeDate": trade_date,
                "count": 0,
                "latestOrdersCsv": "",
                "latestOrdersJson": "",
                "metrics": {"totalReturnPercent": 0, "maxDrawdownPercent": 0, "finalEquity": 0},
                "strategy": self.name,
            }

        # Fetch current DNSE positions + NAV (requires dnse creds)
        try:
            from ..dnse_client import DnseClient
            dnse = DnseClient(
                os.environ.get("DNSE_API_KEY", ""),
                os.environ.get("DNSE_API_SECRET", ""),
            )
            snap = build_portfolio_snapshot(dnse, account_no=os.environ.get("DNSE_ACCOUNT_NO", ""))
            positions = {}
            for p in snap.get("positions", []) or []:
                positions[p["symbol"]] = int(p.get("quantity", 0))
            nav = float(snap.get("totals", {}).get("portfolioValue", 0))
            account = snap.get("account", "?")
        except Exception as exc:
            notify(
                "⚠️ Vietnam Algo: autofin strategy needs DNSE creds; "
                f"failed to fetch portfolio: {exc}"
            )
            positions = {}
            nav = 0
            account = "?"

        plan = _compute_order_plan(signals, positions, nav)
        today = trade_date or datetime.now(timezone.utc).date().isoformat()

        out_dir = config.dataDir / config.liveDirName
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / self.orders_csv_filename
        json_path = out_dir / self.orders_json_filename
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["orderId", "date", "symbol", "side", "price", "quantity", "value", "fee", "reason"],
            )
            writer.writeheader()
            for i, r in enumerate(plan, start=1):
                row = {
                    "orderId": i,
                    "date": today,
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "price": r["price"],
                    "quantity": r["quantity"],
                    "value": r["price"] * r["quantity"],
                    "fee": 0,
                    "reason": r["reason"],
                }
                writer.writerow(row)

        import json as _json
        json_path.write_text(
            _json.dumps(
                {
                    "generatedAt": datetime.now(timezone.utc).isoformat(),
                    "tradeDate": today,
                    "source": "autofin.vn",
                    "account": account,
                    "nav": nav,
                    "signalsFetched": len(signals),
                    "currentPositions": positions,
                    "ordersPlanned": len(plan),
                    "items": [
                        {
                            "orderId": i + 1,
                            "date": today,
                            "symbol": r["symbol"],
                            "side": r["side"],
                            "price": r["price"],
                            "quantity": r["quantity"],
                            "value": r["price"] * r["quantity"],
                            "reason": r["reason"],
                        }
                        for i, r in enumerate(plan)
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )

        buy_n = sum(1 for r in plan if r["side"] == "NB")
        sell_n = sum(1 for r in plan if r["side"] == "NS")
        notify(
            "\n".join(
                [
                    "📡 Vietnam Algo: AUTOFIN signal cycle",
                    f"account: {account}",
                    f"fetched: {len(signals)} signals  (open={sum(1 for s in signals if not s.raw.get('statusDeal',{}).get('isClosed'))}, waiting_buy={sum(1 for s in signals if s.is_waitting_buy)}, waiting_sell={sum(1 for s in signals if s.is_waitting_sell)})",
                    f"NAV: {nav:,.0f} VND",
                    f"plan: BUY={buy_n}  SELL={sell_n}",
                    f"ordersFile: {csv_path}",
                ]
            )
        )

        return {
            "status": "generated",
            "tradeDate": today,
            "count": len(plan),
            "latestOrdersCsv": str(csv_path),
            "latestOrdersJson": str(json_path),
            "metrics": {
                "totalReturnPercent": 0.0,
                "maxDrawdownPercent": 0.0,
                "finalEquity": int(nav),
            },
            "strategy": self.name,
            "signals": {
                "fetched": len(signals),
                "buy_count": buy_n,
                "sell_count": sell_n,
                "buy_symbols": [r["symbol"] for r in plan if r["side"] == "NB"],
                "sell_symbols": [r["symbol"] for r in plan if r["side"] == "NS"],
            },
        }

    def format_cycle_summary(self, result: dict[str, Any]) -> str:
        sig = result.get("signals", {}) or {}
        return "\n".join(
            [
                "📡 Vietnam Algo: AUTOFIN signal cycle",
                f"tradeDate: {result.get('tradeDate')}",
                f"signals fetched: {sig.get('fetched', 0)}",
                f"orderCount: {result.get('count')}",
                f"BUY intents:  {sig.get('buy_count', 0)} → {', '.join(sig.get('buy_symbols', []) or []) or '(none)'}",
                f"SELL intents: {sig.get('sell_count', 0)} → {', '.join(sig.get('sell_symbols', []) or []) or '(none)'}",
                f"ordersFile: {result.get('latestOrdersCsv')}",
            ]
        )
