"""Autofin signal strategy — mirror autofin.vn BUY/SELL signals to DNSE orders.

Spec from user:
  - "the total nav of that list will be over 100%" → need to PRIORITIZE which
    signals to act on (NAV-budget-aware selection)
  - "track when we will down the nav, close the position, etc"
    → use signal-deals API for autofin's open positions and their lifecycle
  - "signal-deals gives the history of opening position"
    → we use this to mirror autofin's current state

Per cycle:
  1. Login to autofin.vn (cached, refreshes ~weekly)
  2. Fetch signal-deals (autofin's currently OPEN positions)
  3. Fetch signal-static-suggest (latest recommendations)
  4. Compute NAV-budget allocation:
     - Sort signals by priority (newest first, then by rateForNav desc)
     - Take signals in order until cumulative rateForNav <= 1.0
  5. Compare with our DNSE positions, generate BUY/SELL deltas
  6. Write orders to data/live/latest_orders.autofin_signal.csv
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

    Per user spec: prioritize by risk-adjusted return, NOT by recency:
      "the method choose the signal that make total NAV near 100 % is not
       correct, we need to priority the signal that potentially make higher
       profit with lower risk"

    Score per signal combines:
      - Risk/reward ratio:  (target_mid - entry_mid) / (entry_mid - stop_max)
        higher is better (bigger upside vs tighter stop)
      - Rating:             autofin's internal rS_Rating_IDB (0..10)
      - Win rate:           autofin's rateWinByPrice (historical performance)
      - Tiny tie-breaker on   rateForNav (larger allocation first when scores tie)
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


def _extract_open_deal_signal(deal: dict[str, Any]) -> AutofinSignal:
    """Convert a signal-deals record (with nested detail) into AutofinSignal."""
    detail = deal.get("detail") or {}
    if detail:
        return parse_signal(detail)
    return parse_signal(deal)


def _compute_order_plan(
    selected_signals: list[AutofinSignal],
    autofin_open_symbols: set[str],
    our_positions: dict[str, int],
    nav_vnd: float,
    board_lot: int = DEFAULT_BOARD_LOT,
    min_order_value_vnd: float = 1_000_000,  # skip if target < 1M VND
) -> list[dict[str, Any]]:
    """Build BUY/SELL orders that mirror autofin's NAV-budget selection.

    Real portfolio management — per user spec:
      "how we are managing our portfolio? it's only signal so will now
       manage the portfolio for us"

    Decision tree per symbol:
      1. autofin holds it AND we don't   → BUY at supply zone
      2. autofin holds it AND we do hold  → leave as-is (or top up if delta)
      3. autofin doesn't hold it AND we do → SELL to close the position
      4. autofin doesn't hold it AND we don't → ignore (out of NAV budget)

    For each BUY/SELL, we also place a paired exit-order (TP/SL bracket)
    so the position auto-exits if price moves favorably or unfavorably.
    TP at tp_F zone low, SL at sl_F zone low.

    Sub-threshold: skip the symbol if target_value < min_order_value_vnd
    (so the strategy doesn't try to liquidate everything on a tiny NAV).
    """
    rows: list[dict[str, Any]] = []
    closes = autofin_open_symbols - {s.symbol for s in selected_signals if s.symbol}

    # Index selected signals by symbol for quick lookup of SL/TP
    sig_by_sym: dict[str, AutofinSignal] = {s.symbol: s for s in selected_signals if s.symbol}

    for s in selected_signals:
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
            "reason": f"autofin_mirror:nav{int(s.rate_for_nav*100)}pct:{s.last_recomment[:60]}",
        })
        # For new BUY positions, also queue paired TP/SL exit orders
        if side == "NB" and delta > 0:
            tp_price = int(round(s.tp_from)) if s.tp_from > 0 else 0
            sl_price = int(round(s.sl_from)) if s.sl_from > 0 else 0
            if tp_price > 0:
                rows.append({
                    "symbol": s.symbol,
                    "side": "NS",
                    "price": tp_price,
                    "quantity": abs(delta),
                    "reason": f"autofin_take_profit:{int(s.tp_from)}",
                })
            if sl_price > 0:
                rows.append({
                    "symbol": s.symbol,
                    "side": "NS",
                    "price": sl_price,
                    "quantity": abs(delta),
                    "reason": f"autofin_stop_loss:{int(s.sl_from)}",
                })

    for sym in closes:
        current_qty = our_positions.get(sym, 0)
        if current_qty <= 0:
            continue
        # market sell to close the position
        rows.append({
            "symbol": sym,
            "side": "NS",
            "price": 0,
            "quantity": _round_to_lots(current_qty, board_lot),
            "reason": "autofin_no_longer_holds:close_position",
        })

    return rows


class AutofinSignalAdapter(StrategyAdapter):
    """Mirror autofin.vn signals to DNSE orders with NAV-budget selection."""

    name = "autofin_signal"
    description = "Mirror autofin.vn BUY/SELL signals to DNSE orders (NAV-budgeted)"
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

        # 1. latest recommended signals (each carries statusDeal with the open
        #    lifecycle info: price_Open, time_Open, isClosed, SL/TP, etc.)
        try:
            signals = client.fetch_signals(page_size=20)
        except Exception as exc:
            notify(f"⚠️ autofin fetch_signals failed: {exc}")
            signals = []

        # 2. autofin's currently OPEN symbols — derived from statusDeal.isClosed
        autofin_open_symbols = {
            s.symbol for s in signals
            if not (s.raw.get("statusDeal", {}) or {}).get("isClosed")
        }

        # 3. NAV-budget-aware selection (newest first, then largest NAV)
        selected = _select_signals_within_nav_budget(signals, nav_budget_pct=1.0)
        excluded = [s for s in signals if s not in selected]

        # 4. our DNSE portfolio snapshot (for NAV + current positions)
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

        # 5. compute order plan
        plan = _compute_order_plan(selected, autofin_open_symbols, positions, nav)

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

        # Persist plan + context for the next cycle to diff against
        import json as _json
        json_path.write_text(
            _json.dumps(
                {
                    "generatedAt": datetime.now(timezone.utc).isoformat(),
                    "tradeDate": today,
                    "source": "autofin.vn",
                    "account": account,
                    "nav": nav,
                    "autofinOpenSymbols": sorted(autofin_open_symbols),
                    "signalsFetched": len(signals),
                    "signalsSelected": [
                        {"symbol": s.symbol, "rateForNav": s.rate_for_nav, "lastIndex": s.raw.get("lastIndex")}
                        for s in selected
                    ],
                    "signalsExcludedForNav": [
                        {"symbol": s.symbol, "rateForNav": s.rate_for_nav}
                        for s in excluded
                    ],
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
        selected_pct = sum(s.rate_for_nav for s in selected) * 100
        notify(
            "\n".join(
                [
                    "📡 Vietnam Algo: AUTOFIN signal cycle",
                    f"account: {account}",
                    f"NAV: {nav:,.0f} VND",
                    f"autofin open positions: {len(autofin_open_symbols)} ({', '.join(sorted(autofin_open_symbols)) or '(none)'})",
                    f"signals fetched: {len(signals)}",
                    f"signals selected (NAV budget): {len(selected)}  using {selected_pct:.0f}% of NAV",
                    f"signals excluded (over budget): {len(excluded)}",
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
                "selected": len(selected),
                "excluded_over_budget": len(excluded),
                "autofin_open_count": len(autofin_open_symbols),
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
                f"signals selected: {sig.get('selected', 0)} (over-budget excluded: {sig.get('excluded_over_budget', 0)})",
                f"autofin open positions: {sig.get('autofin_open_count', 0)}",
                f"orderCount: {result.get('count')}",
                f"BUY intents:  {sig.get('buy_count', 0)} → {', '.join(sig.get('buy_symbols', []) or []) or '(none)'}",
                f"SELL intents: {sig.get('sell_count', 0)} → {', '.join(sig.get('sell_symbols', []) or []) or '(none)'}",
                f"ordersFile: {result.get('latestOrdersCsv')}",
            ]
        )
