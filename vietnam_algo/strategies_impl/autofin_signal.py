"""Autofin signal strategy — mirror autofin.vn signals to DNSE orders via deal-ID tracking.

Per user spec (2026-09-04):
  - track our own positions using deal_id (autofin's unique deal identifier)
  - the order may not be listed in /signal-static-suggest anymore once
    taken; use /signal-static-holding as the source of truth
  - use /signal-static-risk for risk-flagged positions
  - use /closing-signals to detect exits and SELL to mirror autofin

Per cycle:
  1. Login to autofin.vn (cached, refreshes ~weekly)
  2. Fetch /signal-static-holding  → current open deals (autofin source of truth)
  3. Fetch /signal-static-suggest → new recommended entries (for ranking)
  4. Fetch /closing-signals      → recently closed (detect new exits)
  5. Fetch /signal-static-risk    → risk-flagged positions (for alerts)
  6. Compute plan:
     a. for each deal in holding NOT in our_state → BUY + TP + SL
        (with target size = NAV * rateForNav)
     b. for each deal in our_state NOT in holding → SELL (autofin exited)
     c. for each deal in risk      → notify telegram with risk signal
  7. Write orders + state diff
"""
from __future__ import annotations

import csv
import json
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
    if quantity <= 0:
        return 0
    return (quantity // board_lot) * board_lot


def _signal_ref_price(s: AutofinSignal) -> float:
    return s.supply_to or s.tp_from or 0.0


def _select_signals_within_nav_budget(
    signals: list[AutofinSignal],
    nav_budget_pct: float = 1.0,
) -> list[AutofinSignal]:
    """Pick signals in priority order until cumulative rateForNav <= budget.

    Per user spec: prioritize by risk-adjusted return, NOT recency:
      "the method choose the signal that make total NAV near 100 % is not
       correct, we need to priority the signal that potentially make higher
       profit with lower risk"

    Score: (-risk_reward_ratio, -autofin_rating, -win_rate)
    """
    def _score(s: AutofinSignal) -> tuple[float, float, float]:
        entry = (s.supply_from + s.supply_to) / 2 if (s.supply_from and s.supply_to) else 0
        target = (s.tp_from + s.tp_to) / 2 if (s.tp_from and s.tp_to) else 0
        stop = max(s.sl_from or 0, s.sl_to or 0)
        rating = float(s.raw.get("rS_Rating_IDB") or 0)
        win_rate = float(s.raw.get("rateWinByPrice") or 0)
        if entry <= 0 or target <= 0 or stop <= 0 or entry <= stop:
            rr = 0.0
        else:
            rr = (target - entry) / (entry - stop)
        return (-rr, -rating, -win_rate)

    sorted_signals = sorted(signals, key=_score)
    selected: list[AutofinSignal] = []
    cumulative = 0.0
    for s in sorted_signals:
        if s.rate_for_nav <= 0:
            continue
        if cumulative + s.rate_for_nav > nav_budget_pct + 1e-9:
            continue
        selected.append(s)
        cumulative += s.rate_for_nav
    return selected


def _deal_id(raw: dict[str, Any]) -> str:
    """Autofin deal identifier (stable across pages)."""
    return str(raw.get("_id") or raw.get("code") or "")


def _plan_orders_for_new_positions(
    selected: list[AutofinSignal],
    nav_vnd: float,
    our_positions: dict[str, int],
    board_lot: int = DEFAULT_BOARD_LOT,
    min_order_value_vnd: float = 1_000_000,
) -> list[dict[str, Any]]:
    """Emit BUY + TP + SL bracket orders for selected new positions."""
    rows: list[dict[str, Any]] = []
    for s in selected:
        if not s.symbol:
            continue
        current_qty = our_positions.get(s.symbol, 0)
        if s.rate_for_nav <= 0 or nav_vnd <= 0:
            continue
        target_value = nav_vnd * s.rate_for_nav
        if target_value < min_order_value_vnd:
            continue
        ref_price = _signal_ref_price(s)
        if ref_price <= 0:
            continue
        target_qty = _round_to_lots(int(target_value / ref_price), board_lot)
        if target_qty <= 0:
            continue
        delta = target_qty - current_qty
        if abs(delta) < board_lot:
            continue
        side = "NB" if delta > 0 else "NS"
        rows.append({
            "symbol": s.symbol,
            "side": side,
            "price": int(round(ref_price)),
            "quantity": abs(delta),
            "reason": (
                f"autofin_buy:deal#{s.raw.get('_id', '?')}:"
                f"nav{int(s.rate_for_nav*100)}pct:{s.last_recomment[:60]}"
            ),
        })
        if side == "NB" and delta > 0:
            tp_price = int(round(s.tp_from)) if s.tp_from > 0 else 0
            sl_price = int(round(s.sl_from)) if s.sl_from > 0 else 0
            if tp_price > 0:
                rows.append({
                    "symbol": s.symbol,
                    "side": "NS",
                    "price": tp_price,
                    "quantity": abs(delta),
                    "reason": f"autofin_tp:deal#{s.raw.get('_id', '?')}:{int(s.tp_from)}",
                })
            if sl_price > 0:
                rows.append({
                    "symbol": s.symbol,
                    "side": "NS",
                    "price": sl_price,
                    "quantity": abs(delta),
                    "reason": f"autofin_sl:deal#{s.raw.get('_id', '?')}:{int(s.sl_from)}",
                })
    return rows


def _plan_close_orders(
    our_held_deal_ids: dict[str, dict[str, Any]],
    autofin_holding: list[dict[str, Any]],
    board_lot: int = DEFAULT_BOARD_LOT,
) -> list[dict[str, Any]]:
    """Emit SELL orders for deals we hold that autofin no longer holds.

    `our_held_deal_ids` is a map: deal_id → original signal (so we know
    the symbol + qty + the deal we opened).
    """
    rows: list[dict[str, Any]] = []
    autofin_open_ids = {_deal_id(d) for d in autofin_holding if _deal_id(d)}
    for did, sig in our_held_deal_ids.items():
        if did in autofin_open_ids:
            continue
        symbol = sig.get("symbol", "?")
        qty = sig.get("_our_qty", 0)
        if qty <= 0:
            continue
        rows.append({
            "symbol": symbol,
            "side": "NS",
            "price": 0,  # market order to close
            "quantity": _round_to_lots(qty, board_lot),
            "reason": f"autofin_closed:deal#{did}",
        })
    return rows


class AutofinSignalAdapter(StrategyAdapter):
    """Mirror autofin.vn signals to DNSE orders, tracked by autofin deal_id."""

    name = "autofin_signal"
    description = "Mirror autofin.vn signals to DNSE (deal-id tracked, NAV-budgeted)"
    env_filename = ".env.autofin-signal"

    def __init__(self) -> None:
        self._client: AutofinClient | None = None
        # Persistent state (loaded by the bot each cycle)
        self._our_deals: dict[str, dict[str, Any]] = {}  # deal_id → original signal

    def _client_lazy(self) -> AutofinClient:
        if self._client is None:
            email, password = _load_env_credentials()
            if not email or not password:
                raise RuntimeError(
                    "AUTOFIN_EMAIL and AUTOFIN_PASSWORD must be set in env (.env.autofin-signal)"
                )
            self._client = AutofinClient(email=email, password=password)
        return self._client

    def _load_state(self, data_dir: Path) -> None:
        path = data_dir / "live" / "autofin_signal_state.json"
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text())
            self._our_deals = {str(k): dict(v) for k, v in payload.get("our_deals", {}).items()}
        except Exception:
            self._our_deals = {}

    def _save_state(self, data_dir: Path) -> None:
        path = data_dir / "live" / "autofin_signal_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"our_deals": self._our_deals, "updatedAt": datetime.now(timezone.utc).isoformat()},
            indent=2,
        ))

    def generate_orders(
        self,
        config: EngineConfig,
        strategy_config: StrategyConfig,
        trade_date: str | None = None,
    ) -> dict[str, Any]:
        client = self._client_lazy()
        self._load_state(config.dataDir)

        # 1. /signal-static-holding — autofin currently open positions
        try:
            holding = client.fetch_holding(page_size=50)
        except Exception as exc:
            notify(f"⚠️ autofin fetch_holding failed: {exc}")
            holding = []
        autofin_open_ids = {_deal_id(d) for d in holding if _deal_id(d)}
        # Build a map: deal_id → signal details (for the TP/SL etc.)
        autofin_holding_by_id: dict[str, dict[str, Any]] = {}
        for d in holding:
            did = _deal_id(d)
            if did:
                d["_our_qty"] = 0  # placeholder; set when we hold it
                autofin_holding_by_id[did] = d

        # 2. /signal-static-suggest — latest recommended signals
        try:
            suggest_raw = client.fetch_signals(page_size=20)
        except Exception as exc:
            notify(f"⚠️ autofin fetch_signals failed: {exc}")
            suggest_raw = []
        signals = [parse_signal(s) for s in suggest_raw if isinstance(s, dict)]
        suggest_by_id: dict[str, AutofinSignal] = {s.raw.get("_id", ""): s for s in signals if s.raw.get("_id")}

        # 3. /closing-signals — recently closed
        try:
            closing = client.fetch_closing(page_size=50)
        except Exception as exc:
            notify(f"⚠️ autofin fetch_closing failed: {exc}")
            closing = []

        # 4. /signal-static-risk — risk positions (for alerts)
        try:
            risk = client.fetch_risk(page_size=50)
        except Exception as exc:
            notify(f"⚠️ autofin fetch_risk failed: {exc}")
            risk = []

        # 5. fetch our current DNSE positions for sizing
        try:
            from ..dnse_client import DnseClient
            dnse = DnseClient(
                os.environ.get("DNSE_API_KEY", ""),
                os.environ.get("DNSE_API_SECRET", ""),
            )
            snap = build_portfolio_snapshot(dnse, account_no=os.environ.get("DNSE_ACCOUNT_NO", ""))
            positions = {p["symbol"]: int(p.get("quantity", 0)) for p in (snap.get("positions") or [])}
            nav = float(snap.get("totals", {}).get("portfolioValue", 0))
            account = snap.get("account", "?")
        except Exception as exc:
            notify(f"⚠️ autofin needs DNSE creds; portfolio fetch failed: {exc}")
            positions = {}
            nav = 0.0
            account = "?"

        # 6. select signals (top risk-adjusted, NAV-budgeted)
        selected = _select_signals_within_nav_budget(signals, nav_budget_pct=1.0)
        # filter to only selected deals that are actually open in autofin
        selected = [s for s in selected if (s.raw.get("_id") in autofin_open_ids)]
        # also include any deal we hold that autofin still holds (for rebalance)
        for did, sig in self._our_deals.items():
            if did in autofin_open_ids and sig not in selected:
                selected.append(parse_signal(sig))

        # 7. new entries (BUY + TP + SL)
        new_orders = _plan_orders_for_new_positions(selected, nav, positions)

        # 8. close positions autofin no longer holds
        # Filter out deals we never actually opened (NAV=0 caused zero orders)
        our_real_deals = {
            did: sig for did, sig in self._our_deals.items()
            if sig.get("_our_qty", 0) > 0
        }
        close_orders = _plan_close_orders(our_real_deals, holding)
        # merge — close orders first so any new BUY on same symbol replaces

        # 9. update our tracked deals
        # for each new selected deal that we now hold, add to our_deals
        for s in selected:
            did = s.raw.get("_id")
            if not did:
                continue
            if did not in self._our_deals:
                self._our_deals[did] = dict(s.raw)
            self._our_deals[did]["_our_qty"] = self._our_deals[did].get("_our_qty", 0)
            # also derive _our_qty from new order if BUY
            for o in new_orders:
                if o["symbol"] == s.symbol and o["side"] == "NB" and "tp:" not in o["reason"] and "sl:" not in o["reason"]:
                    self._our_deals[did]["_our_qty"] = o["quantity"]
        # for each closed deal, remove
        for o in close_orders:
            for did, sig in list(self._our_deals.items()):
                if sig.get("symbol") == o["symbol"]:
                    self._our_deals.pop(did, None)
                    break

        # 10. assemble final order list (close first, then new)
        all_orders = close_orders + new_orders

        # 11. write outputs
        today = trade_date or datetime.now(timezone.utc).date().isoformat()
        out_dir = config.dataDir / config.liveDirName
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / self.orders_csv_filename
        json_path = out_dir / self.orders_json_filename

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["orderId", "date", "symbol", "side", "price", "quantity", "value", "fee", "reason"],
            )
            writer.writeheader()
            for i, r in enumerate(all_orders, start=1):
                writer.writerow({
                    "orderId": i,
                    "date": today,
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "price": r["price"],
                    "quantity": r["quantity"],
                    "value": r["price"] * r["quantity"],
                    "fee": 0,
                    "reason": r["reason"],
                })

        json_path.write_text(json.dumps({
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "tradeDate": today,
            "source": "autofin.vn",
            "account": account,
            "nav": nav,
            "autofinHoldingCount": len(holding),
            "autofinRiskCount": len(risk),
            "autofinClosingCount": len(closing),
            "signalsFetched": len(signals),
            "dealsTracked": len(self._our_deals),
            "newOrders": len(new_orders),
            "closeOrders": len(close_orders),
            "items": [{
                "orderId": i + 1,
                "date": today,
                "symbol": r["symbol"],
                "side": r["side"],
                "price": r["price"],
                "quantity": r["quantity"],
                "reason": r["reason"],
            } for i, r in enumerate(all_orders)],
        }, indent=2, ensure_ascii=False))

        # 12. save state for next cycle
        self._save_state(config.dataDir)

        # 13. notify
        risk_alerts = [d.get("symbol") for d in risk if isinstance(d, dict)]
        notify_lines = [
            "📡 Vietnam Algo: AUTOFIN signal cycle",
            f"account: {account}",
            f"NAV: {nav:,.0f} VND",
            f"autofin holding: {len(holding)}  risk: {len(risk)}  closing: {len(closing)}",
            f"signals fetched: {len(signals)}  deals tracked: {len(self._our_deals)}",
            f"new entries: {len(new_orders)}  closes: {len(close_orders)}",
            f"ordersFile: {csv_path}",
        ]
        if risk_alerts:
            notify_lines.append(f"⚠️ RISK FLAGGED: {', '.join(risk_alerts[:10])}")
        notify("\n".join(notify_lines))

        return {
            "status": "generated",
            "tradeDate": today,
            "count": len(all_orders),
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
                "autofin_holding": len(holding),
                "autofin_closing": len(closing),
                "autofin_risk": len(risk),
                "deals_tracked": len(self._our_deals),
                "new_count": len(new_orders),
                "close_count": len(close_orders),
                "new_symbols": [r["symbol"] for r in new_orders],
                "close_symbols": [r["symbol"] for r in close_orders],
            },
        }

    def format_cycle_summary(self, result: dict[str, Any]) -> str:
        sig = result.get("signals", {}) or {}
        return "\n".join(
            [
                "📡 Vietnam Algo: AUTOFIN signal cycle",
                f"tradeDate: {result.get('tradeDate')}",
                f"autofin holding: {sig.get('autofin_holding', 0)}  risk: {sig.get('autofin_risk', 0)}  closing: {sig.get('autofin_closing', 0)}",
                f"signals fetched: {sig.get('fetched', 0)}",
                f"deals tracked: {sig.get('deals_tracked', 0)}",
                f"orderCount: {result.get('count')}",
                f"new entries:  {sig.get('new_count', 0)} → {', '.join(sig.get('new_symbols', []) or []) or '(none)'}",
                f"closes:        {sig.get('close_count', 0)} → {', '.join(sig.get('close_symbols', []) or []) or '(none)'}",
                f"ordersFile: {result.get('latestOrdersCsv')}",
            ]
        )
