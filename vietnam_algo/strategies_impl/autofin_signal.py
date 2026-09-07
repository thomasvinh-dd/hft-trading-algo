"""Autofin signal strategy — mirror autofin.vn signals to DNSE orders via deal-ID tracking.

Per user spec (2026-09-04 / 2026-09-06):
  - track our own positions using deal_id (autofin's unique deal identifier)
  - the order may not be listed in /signal-static-suggest anymore once
    taken; use /signal-static-holding as the source of truth
  - use /signal-static-risk for risk-flagged positions
  - use /closing-signals to detect exits and SELL to mirror autofin
  - mirror partial close signals (`lastRecomment`) by issuing a partial SELL
    when `numberOfProfit` increases since the previous cycle

Per cycle:
  1. Login to autofin.vn (cached, refreshes ~weekly)
  2. Fetch /signal-static-holding  → current open deals (autofin source of truth)
  3. Fetch /signal-static-suggest → new recommended entries (for ranking)
  4. Fetch /closing-signals      → recently closed (detect new exits)
  5. Fetch /signal-static-risk    → risk-flagged positions (for alerts)
  6. Compute plan:
     a. for each deal in holding NOT in our_state → BUY + TP + SL
        (with target size = NAV * rateForNav)
     b. for each deal we hold that autofin still holds AND whose
        numberOfProfit has increased → partial SELL (mirror trim / TP step)
     c. for each deal in our_state NOT in holding → SELL (autofin exited)
     d. for each deal in risk      → notify telegram with risk signal
  7. Write orders + state diff
"""
from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass
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
    """Autofin deal identifier (stable across pages).

    Note: the raw ``_id`` field is a Unix timestamp of the deal's open
    time, which is **not unique** — multiple deals opened on the same
    day share the same ``_id`` (e.g. HCM and MZG both have
    ``_id=1785888000`` because they were both opened on 2026-08-05).
    We therefore combine ``_id`` with ``symbol`` to form a unique
    composite key.
    """
    _id = raw.get("_id") or raw.get("code") or ""
    sym = raw.get("symbol") or ""
    if not sym:
        return str(_id)
    return f"{_id}:{sym}"


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
        did = _deal_id(s.raw)
        side = "NB" if delta > 0 else "NS"
        rows.append({
            "symbol": s.symbol,
            "side": side,
            "price": int(round(ref_price)),
            "quantity": abs(delta),
            "reason": (
                f"autofin_buy:deal#{did}:"
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
                    "reason": f"autofin_tp:deal#{did}:{int(s.tp_from)}",
                })
            if sl_price > 0:
                rows.append({
                    "symbol": s.symbol,
                    "side": "NS",
                    "price": sl_price,
                    "quantity": abs(delta),
                    "reason": f"autofin_sl:deal#{did}:{int(s.sl_from)}",
                })
    return rows


def _plan_close_orders(
    our_held_deal_ids: dict[str, dict[str, Any]],
    autofin_holding: list[dict[str, Any]],
    autofin_suggest_ids: set[str] | None = None,
    board_lot: int = DEFAULT_BOARD_LOT,
) -> list[dict[str, Any]]:
    """Emit SELL orders for deals we hold that autofin no longer holds.

    A deal is considered "still active in autofin" if it's in either
    the holding list (currently held by autofin) or the suggest list
    (still a recommended open position). Only when the deal is in
    neither do we mirror-autofin by closing it.

    `our_held_deal_ids` is a map: deal_id → original signal (so we know
    the symbol + qty + the deal we opened).
    """
    rows: list[dict[str, Any]] = []
    autofin_open_ids = {_deal_id(d) for d in autofin_holding if _deal_id(d)}
    if autofin_suggest_ids:
        autofin_open_ids |= autofin_suggest_ids
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


@dataclass(frozen=True)
class PartialCloseAction:
    """Parsed partial-close signal from autofin's lastRecomment text."""

    action: str  # "take_profit_lap" | "reduce_lap" | "capital_protect" | "full_close" | "unknown"
    lap: int = 0          # 1, 2, 3 — for lap variants
    price: float = 0.0    # VND; explicit price (e.g. 26900) for take_profit_lap
    percent: float = 0.0  # 0..1; for capital_protect (e.g. 0.2857 for 28.57%)


_PARTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Chốt lãi lần\s*(\d+)(?:\s*mức giá\s*([\d,\.]+))?", re.IGNORECASE), "take_profit_lap"),
    (re.compile(r"Hạ tỷ trọng lần\s*(\d+)", re.IGNORECASE), "reduce_lap"),
    (re.compile(r"Chốt lãi\s*([\d,\.]+)\s*%\s*bảo toàn vốn", re.IGNORECASE), "capital_protect"),
    (re.compile(r"Đóng DEAL", re.IGNORECASE), "full_close"),
)


def _parse_vnd(s: str) -> float:
    """Parse '26,900' → 26900.0 (Vietnamese thousands separator).

    Strips dots and commas to recover the raw number; e.g. "1,234,500"
    becomes 1234500.0.
    """
    return float(s.replace(".", "").replace(",", "").strip() or "0")


def _parse_percent(s: str) -> float:
    """Parse '28.57' → 28.57 (Vietnamese percent uses '.' as decimal)."""
    return float(s.replace(",", "").strip() or "0")


def _parse_partial_close(recomment: str) -> PartialCloseAction:
    """Parse the Vietnamese `lastRecomment` text into a typed action.

    Returns ``PartialCloseAction("unknown")`` when the text does not match
    any known partial-close pattern.
    """
    rec = (recomment or "").strip()
    for pat, action in _PARTIAL_PATTERNS:
        m = pat.search(rec)
        if not m:
            continue
        if action == "take_profit_lap":
            lap = int(m.group(1))
            price = _parse_vnd(m.group(2)) if m.group(2) else 0.0
            return PartialCloseAction(action=action, lap=lap, price=price)
        if action == "reduce_lap":
            return PartialCloseAction(action=action, lap=int(m.group(1)))
        if action == "capital_protect":
            pct = _parse_percent(m.group(1))
            return PartialCloseAction(action=action, percent=pct)
        if action == "full_close":
            return PartialCloseAction(action=action)
    return PartialCloseAction(action="unknown")


def _plan_partial_close_orders(
    our_held_deals: dict[str, dict[str, Any]],
    autofin_holding: list[dict[str, Any]],
    board_lot: int = DEFAULT_BOARD_LOT,
) -> list[dict[str, Any]]:
    """Emit partial SELL orders for deals we hold that autofin still holds
    but whose ``numberOfProfit`` has increased since the previous cycle.

    Per user spec (2026-09-06):
      - Sizing: 1/3 of current qty per ``lần`` step (take_profit_lap /
        reduce_lap), X% of current qty for ``bảo toàn vốn X%`` events
      - Price: explicit price from recomment → else ``tP_F`` → else market
      - Bootstrap: on first cycle (no ``_last_numberOfProfit`` in state),
        record the current value but do NOT issue any partial close

    Mutates the deal dicts in-place to update ``_last_numberOfProfit`` and
    ``_our_qty`` so the next cycle plans from the correct baseline.
    """
    autofin_by_id: dict[str, dict[str, Any]] = {}
    for d in autofin_holding:
        did = _deal_id(d)
        if did:
            autofin_by_id[did] = d

    rows: list[dict[str, Any]] = []
    for did, sig in our_held_deals.items():
        if did not in autofin_by_id:
            continue
        h = autofin_by_id[did]
        rec = str(h.get("lastRecomment") or "")
        action = _parse_partial_close(rec)
        if action.action in ("unknown", "full_close"):
            continue

        cur_nP = int(h.get("numberOfProfit") or 0)
        last_nP = int(sig.get("_last_numberOfProfit", -1))
        if last_nP < 0:
            sig["_last_numberOfProfit"] = cur_nP
            sig["_last_recomment"] = rec
            continue
        if cur_nP <= last_nP:
            continue

        our_qty = int(sig.get("_our_qty", 0))
        if our_qty <= 0:
            continue

        if action.action in ("take_profit_lap", "reduce_lap"):
            delta = our_qty // 3
        elif action.action == "capital_protect":
            # action.percent is in % (e.g. 28.57) → convert to fraction
            delta = int(our_qty * action.percent / 100)
        else:
            continue

        delta = _round_to_lots(delta, board_lot)
        if delta < board_lot:
            continue

        price = action.price if action.price > 0 else 0
        if price <= 0:
            # /signal-static-holding returns raw VND (e.g. 15100), while
            # /signal-static-suggest returns thousands (e.g. 15.1). Detect
            # the scale by magnitude so we get the same number either way.
            tp_f = float(h.get("tP_F") or 0)
            if 0 < tp_f < 1000:
                tp_f *= 1000
            price = int(round(tp_f)) if tp_f > 0 else 0

        rows.append({
            "symbol": sig.get("symbol", "?"),
            "side": "NS",
            "price": price,
            "quantity": delta,
            "reason": (
                f"autofin_partial:deal#{did}:{action.action}:"
                f"lap{action.lap}:pct{int(round(action.percent * 100))}:{rec[:60]}"
            ),
        })
        sig["_last_numberOfProfit"] = cur_nP
        sig["_last_recomment"] = rec
        sig["_our_qty"] = max(0, our_qty - delta)

    return rows


def _extract_deal_id(reason: str) -> str:
    # deal_id format is {digits}:{UPPERCASE} (e.g. "1785888000:HCM").
    # The rest of the reason is colon-separated fields like ":take_profit_lap:lap1".
    m = re.search(r"deal#(\d+:[A-Z]+)", reason or "")
    return m.group(1) if m else ""


def _build_deals_actioned(
    new_orders: list[dict[str, Any]],
    partial_orders: list[dict[str, Any]],
    close_orders: list[dict[str, Any]],
    autofin_holding_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group the planned orders by deal and pair each with the autofin
    signal that triggered it. Used to build a rich Telegram summary.

    For new entries, we extract the signal context from the order
    reason itself (autofin_buy:deal#X:navNpct:<recomment>) so we can
    show it even when the deal isn't in the holding list yet.
    """
    deals: list[dict[str, Any]] = []

    new_by_deal: dict[str, list[dict[str, Any]]] = {}
    for o in new_orders:
        did = _extract_deal_id(o.get("reason", ""))
        if did:
            new_by_deal.setdefault(did, []).append(o)
    for did, orders in new_by_deal.items():
        # Prefer holding data; fall back to parsing the BUY order reason.
        h = autofin_holding_by_id.get(did, {})
        rec = str(h.get("lastRecomment") or "")
        rate = float(h.get("rateForNav") or 0)
        nP = int(h.get("numberOfProfit") or 0)
        if not rec:
            # extract from first order's reason: "autofin_buy:deal#X:navNpct:<rec>"
            m = re.search(r"autofin_buy:[^:]+:[^:]+:nav\d+pct:(.+)$", orders[0].get("reason", ""))
            if m:
                rec = m.group(1).strip()
                # also pull rate from the reason
                rm = re.search(r"nav(\d+)pct", orders[0].get("reason", ""))
                if rm:
                    rate = int(rm.group(1)) / 100.0
        deals.append({
            "action": "new_entry",
            "symbol": h.get("symbol", orders[0].get("symbol", "?")),
            "deal_id": did,
            "lastRecomment": rec,
            "rateForNav": rate,
            "numberOfProfit": nP,
            "dnse_orders": orders,
        })

    for o in partial_orders:
        did = _extract_deal_id(o.get("reason", ""))
        h = autofin_holding_by_id.get(did, {})
        deals.append({
            "action": "partial_close",
            "symbol": o.get("symbol", "?"),
            "deal_id": did,
            "lastRecomment": str(h.get("lastRecomment") or ""),
            "rateForNav": float(h.get("rateForNav") or 0),
            "numberOfProfit": int(h.get("numberOfProfit") or 0),
            "dnse_orders": [o],
        })

    for o in close_orders:
        did = _extract_deal_id(o.get("reason", ""))
        deals.append({
            "action": "full_close",
            "symbol": o.get("symbol", "?"),
            "deal_id": did,
            "lastRecomment": "(autofin exited — not in holding)",
            "rateForNav": 0.0,
            "numberOfProfit": 0,
            "dnse_orders": [o],
        })

    return deals


def _order_key(o: dict[str, Any]) -> str:
    """Stable key for an order row, used to de-dupe Telegram notifications."""
    return (
        f"{o.get('reason', '')}|{o.get('side', '')}|"
        f"{o.get('symbol', '')}|{o.get('price', 0)}|{o.get('quantity', 0)}"
    )


def _format_per_action_message(deal: dict[str, Any], ts: str) -> str:
    """Short per-action Telegram message (one per new order)."""
    action = deal["action"]
    title = {
        "new_entry": "🤖 autofin_signal: new entry",
        "partial_close": "🤖 autofin_signal: new partial close",
        "full_close": "🤖 autofin_signal: full close",
    }.get(action, "🤖 autofin_signal: order")

    sym = deal["symbol"]
    did = deal["deal_id"]
    rec = deal.get("lastRecomment") or "(no recomment)"

    lines: list[str] = [title, f"📅 {ts}"]
    if action == "new_entry":
        rate = deal.get("rateForNav") or 0
        lines.append(f"• {sym}  [deal#{did}, rateForNav={rate:.0%}]")
    elif action == "partial_close":
        nP = deal.get("numberOfProfit", 0)
        lines.append(f"• {sym}  [deal#{did}, nP={nP}]")
    else:
        lines.append(f"• {sym}  [deal#{did}]")
    lines.append(f"  signal: {rec}")
    for o in deal["dnse_orders"]:
        side_lbl = "BUY" if o.get("side") == "NB" else "SELL"
        price = o.get("price", 0)
        price_s = f"{int(price):,}" if price else "market"
        lines.append(
            f"  → DNSE: {side_lbl} {o.get('quantity')} "
            f"{o.get('symbol')} @ {price_s} VND"
        )
    return "\n".join(lines)
    deals: list[dict[str, Any]] = []

    new_by_deal: dict[str, list[dict[str, Any]]] = {}
    for o in new_orders:
        did = _extract_deal_id(o.get("reason", ""))
        if did:
            new_by_deal.setdefault(did, []).append(o)
    for did, orders in new_by_deal.items():
        # Prefer holding data; fall back to parsing the BUY order reason.
        h = autofin_holding_by_id.get(did, {})
        rec = str(h.get("lastRecomment") or "")
        rate = float(h.get("rateForNav") or 0)
        nP = int(h.get("numberOfProfit") or 0)
        if not rec:
            # extract from first order's reason: "autofin_buy:deal#X:navNpct:<rec>"
            m = re.search(r"autofin_buy:[^:]+:[^:]+:nav\d+pct:(.+)$", orders[0].get("reason", ""))
            if m:
                rec = m.group(1).strip()
                # also pull rate from the reason
                rm = re.search(r"nav(\d+)pct", orders[0].get("reason", ""))
                if rm:
                    rate = int(rm.group(1)) / 100.0
        deals.append({
            "action": "new_entry",
            "symbol": h.get("symbol", orders[0].get("symbol", "?")),
            "deal_id": did,
            "lastRecomment": rec,
            "rateForNav": rate,
            "numberOfProfit": nP,
            "dnse_orders": orders,
        })

    for o in partial_orders:
        did = _extract_deal_id(o.get("reason", ""))
        h = autofin_holding_by_id.get(did, {})
        deals.append({
            "action": "partial_close",
            "symbol": o.get("symbol", "?"),
            "deal_id": did,
            "lastRecomment": str(h.get("lastRecomment") or ""),
            "rateForNav": float(h.get("rateForNav") or 0),
            "numberOfProfit": int(h.get("numberOfProfit") or 0),
            "dnse_orders": [o],
        })

    for o in close_orders:
        did = _extract_deal_id(o.get("reason", ""))
        deals.append({
            "action": "full_close",
            "symbol": o.get("symbol", "?"),
            "deal_id": did,
            "lastRecomment": "(autofin exited — not in holding)",
            "rateForNav": 0.0,
            "numberOfProfit": 0,
            "dnse_orders": [o],
        })

    return deals


class AutofinSignalAdapter(StrategyAdapter):
    """Mirror autofin.vn signals to DNSE orders, tracked by autofin deal_id."""

    name = "autofin_signal"
    description = "Mirror autofin.vn signals to DNSE (deal-id tracked, NAV-budgeted)"
    env_filename = ".env.autofin-signal"

    def __init__(self) -> None:
        self._client: AutofinClient | None = None
        # Persistent state (loaded by the bot each cycle)
        self._our_deals: dict[str, dict[str, Any]] = {}  # deal_id → original signal
        # Order-key set already sent to Telegram — used to de-dupe per-order
        # notifications so we don't spam the chat every poll.
        self._notified_order_keys: set[str] = set()
        # Last time the strategy ran (UTC ISO). Used by the bot to throttle
        # mid-day polls to once per hour.
        self._last_poll_at: str | None = None

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
            self._notified_order_keys = set(payload.get("notified_order_keys", []) or [])
            self._last_poll_at = payload.get("last_poll_at")
        except Exception:
            self._our_deals = {}
            self._notified_order_keys = set()
            self._last_poll_at = None

    def _save_state(self, data_dir: Path) -> None:
        path = data_dir / "live" / "autofin_signal_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {
                "our_deals": self._our_deals,
                "notified_order_keys": sorted(self._notified_order_keys),
                "last_poll_at": datetime.now(timezone.utc).isoformat(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ))

    def last_poll_at(self) -> str | None:
        return self._last_poll_at

    def _notify_new_orders(self, deals_actioned: list[dict[str, Any]]) -> None:
        """Send a short Telegram message for any deal-actioned entry that
        hasn't been notified yet. Updates ``self._notified_order_keys`` so
        the next cycle won't re-notify the same orders.

        Each ``deal`` in ``deals_actioned`` becomes one Telegram message
        (showing all DNSE legs for that deal — BUY+TP+SL bracket for
        entries, single SELL for closes).
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        for deal in deals_actioned:
            # build a per-deal key so multiple legs collapse to one message
            keys = {_order_key(o) for o in deal.get("dnse_orders", [])}
            if not keys:
                continue
            if keys.issubset(self._notified_order_keys):
                continue  # already notified
            msg = _format_per_action_message(deal, ts)
            try:
                notify(msg)
            except Exception as exc:
                print(f"[autofin_signal] per-order notify failed: {exc}")
            self._notified_order_keys |= keys

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
            signals = client.fetch_signals(page_size=20)
        except Exception as exc:
            notify(f"⚠️ autofin fetch_signals failed: {exc}")
            signals = []
        suggest_by_id: dict[str, AutofinSignal] = {
            _deal_id(s.raw): s for s in signals if s.raw.get("_id")
        }

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

        # 5b. MOCK_NAV override — for dry-run testing with funded account
        # but zero balance. Lets us see the order plan without real money.
        mock_nav = os.environ.get("MOCK_NAV", "").strip()
        if mock_nav:
            try:
                nav = float(mock_nav)
                positions = dict(positions)  # copy
            except ValueError:
                pass

        # 6. select signals (top risk-adjusted, NAV-budgeted)
        # suggest-endpoint signals are NEW entries; holding-endpoint deals
        # are existing positions to monitor (handled by partial / full close).
        selected = _select_signals_within_nav_budget(signals, nav_budget_pct=1.0)
        # also include any deal we already hold (for rebalance when our
        # tracked qty drifts from autofin's target). Use the original
        # holding data as the source so the composite deal_id is preserved.
        for did, sig in self._our_deals.items():
            if did in autofin_open_ids:
                already = any(_deal_id(s.raw) == did for s in selected)
                if not already:
                    h = autofin_holding_by_id.get(did)
                    if h is not None:
                        selected.append(parse_signal(h))
                    # If the deal is in suggest but not holding, fall back to
                    # the stored sig — we lose the composite _id but the
                    # partial-close path doesn't need it (it keys off nP).

        # 7. new entries (BUY + TP + SL)
        new_orders = _plan_orders_for_new_positions(selected, nav, positions)

        # 8. partial close (mirror autofin's trim / take-profit step)
        # Only for deals we actually hold; reads + mutates self._our_deals
        # to update _last_numberOfProfit and decrement _our_qty.
        partial_orders = _plan_partial_close_orders(self._our_deals, holding)

        # 9. close positions autofin no longer holds
        # Filter out deals we never actually opened (NAV=0 caused zero orders)
        our_real_deals = {
            did: sig for did, sig in self._our_deals.items()
            if sig.get("_our_qty", 0) > 0
        }
        suggest_ids = {_deal_id(s.raw) for s in signals if s.raw.get("_id")}
        close_orders = _plan_close_orders(our_real_deals, holding, suggest_ids)
        # merge — close orders first so any new BUY on same symbol replaces

        # 10. update our tracked deals
        # for each new selected deal that we now hold, add to our_deals
        for s in selected:
            did = _deal_id(s.raw)
            if not did:
                continue
            if did not in self._our_deals:
                self._our_deals[did] = dict(s.raw)
            self._our_deals[did]["_our_qty"] = self._our_deals[did].get("_our_qty", 0)
            # also derive _our_qty from new order if BUY
            for o in new_orders:
                if o["symbol"] == s.symbol and o["side"] == "NB" and "tp:" not in o["reason"] and "sl:" not in o["reason"]:
                    self._our_deals[did]["_our_qty"] = o["quantity"]
        # for each closed deal, remove by deal_id (not symbol — symbols can
        # collide across deals on the same day)
        for o in close_orders:
            closed_did = _extract_deal_id(o.get("reason", ""))
            if closed_did:
                self._our_deals.pop(closed_did, None)

        # 11. assemble final order list (close → partial → new)
        all_orders = close_orders + partial_orders + new_orders

        # 11b. group orders by deal so the EOD summary can show signal ↔
        # DNSE-order mapping per deal.
        deals_actioned = _build_deals_actioned(
            new_orders, partial_orders, close_orders, autofin_holding_by_id
        )

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
            "partialOrders": len(partial_orders),
            "dealsActioned": deals_actioned,
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

        # 12. send per-order Telegram notifications for any newly-detected
        # action since the last cycle. The EOD summary will still bundle
        # everything in one message at 15:15 ICT.
        self._notify_new_orders(deals_actioned)

        # 13. save state for next cycle
        self._save_state(config.dataDir)

        # Note: the EOD daily-summary Telegram message is sent separately
        # via format_cycle_summary() by the auto_bot's EOD path.

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
                "partial_count": len(partial_orders),
                "new_symbols": [r["symbol"] for r in new_orders],
                "close_symbols": [r["symbol"] for r in close_orders],
                "partial_symbols": [r["symbol"] for r in partial_orders],
                "account": account,
                "nav": nav,
            },
            "deals_actioned": deals_actioned,
        }

    def format_cycle_summary(
        self,
        result: dict[str, Any],
        portfolio: dict[str, Any] | None = None,
    ) -> str:
        """Rich per-deal Telegram summary.

        Shows:
          - which bot is sending (autofin_signal)
          - what autofin signal triggered each order (lastRecomment, nP)
          - the actual DNSE order we'll send (side, symbol, qty, price)
          - current portfolio: cash, NAV, positions, unrealized P&L
        """
        from ..notifier import portfolio_section_lines
        sig = result.get("signals", {}) or {}
        deals = result.get("deals_actioned", []) or []
        nav = float(sig.get("nav") or result.get("metrics", {}).get("finalEquity") or 0)
        account = sig.get("account") or "?"

        lines: list[str] = []
        lines.append("🤖 Vietnam Algo bot — [autofin_signal]")
        lines.append(f"📅 Cycle: {result.get('tradeDate', '?')}")
        lines.append(f"💼 Account: {account}")
        lines.append(f"💰 MOCK NAV (autofin target): {nav:,.0f} VND")
        lines.append("")
        # current portfolio: cash, NAV, positions, unrealized P&L
        lines.extend(portfolio_section_lines(portfolio))
        lines.append("")
        lines.append("📊 Autofin state")
        lines.append(
            f"  • holding={sig.get('autofin_holding', 0)}  "
            f"risk={sig.get('autofin_risk', 0)}  "
            f"closing={sig.get('autofin_closing', 0)}"
        )
        lines.append(
            f"  • signals fetched={sig.get('fetched', 0)}  "
            f"our tracked deals={sig.get('deals_tracked', 0)}"
        )
        lines.append("")

        def _fmt_price(p: int | float) -> str:
            return f"{int(p):,}" if p else "market"

        def _fmt_order_line(o: dict[str, Any]) -> str:
            side_lbl = "BUY" if o.get("side") == "NB" else "SELL"
            return (
                f"    → DNSE: {side_lbl} {o.get('quantity')} "
                f"{o.get('symbol')} @ {_fmt_price(o.get('price', 0))} VND"
            )

        new_entries = [d for d in deals if d["action"] == "new_entry"]
        partials = [d for d in deals if d["action"] == "partial_close"]
        fulls = [d for d in deals if d["action"] == "full_close"]

        if new_entries:
            lines.append(f"📥 New entries ({len(new_entries)})")
            for d in new_entries:
                rate = d.get("rateForNav") or 0
                lines.append(
                    f"  • {d['symbol']}  [deal#{d['deal_id']}, rateForNav={rate:.0%}]"
                )
                lines.append(f"    signal: {d['lastRecomment'] or '(no recomment)'}")
                for o in d["dnse_orders"]:
                    lines.append(_fmt_order_line(o))
            lines.append("")

        if partials:
            lines.append(f"📤 Partial closes ({len(partials)})")
            for d in partials:
                nP = d.get("numberOfProfit", 0)
                lines.append(f"  • {d['symbol']}  [deal#{d['deal_id']}, nP={nP}]")
                lines.append(f"    signal: {d['lastRecomment'] or '(no recomment)'}")
                for o in d["dnse_orders"]:
                    lines.append(_fmt_order_line(o))
            lines.append("")

        if fulls:
            lines.append(f"🚪 Full closes ({len(fulls)})")
            for d in fulls:
                lines.append(f"  • {d['symbol']}  [deal#{d['deal_id']}]")
                lines.append(f"    signal: {d['lastRecomment']}")
                for o in d["dnse_orders"]:
                    lines.append(_fmt_order_line(o))
            lines.append("")

        if not deals:
            lines.append("ℹ️  No orders planned this cycle")
            if nav == 0:
                lines.append("   (account NAV=0 — orders will plan once funded)")
            lines.append("")

        if sig.get("autofin_risk"):
            lines.append(
                f"⚠️  {sig['autofin_risk']} positions risk-flagged (see risk endpoint)"
            )
            lines.append("")

        lines.append(f"📁 Orders file: {result.get('latestOrdersCsv', '?')}")
        lines.append(f"📊 JSON output:  {result.get('latestOrdersJson', '?')}")
        return "\n".join(lines)
