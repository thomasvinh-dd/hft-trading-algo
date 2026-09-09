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
from dataclasses import dataclass, replace
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
    min_leftover_fraction: float = 0.05,
) -> list[AutofinSignal]:
    """Pick signals in priority order until cumulative rateForNav <= budget.

    Per user spec: prioritize by risk-adjusted return, NOT recency:
      "the method choose the signal that make total NAV near 100 % is not
       correct, we need to priority the signal that potentially make higher
       profit with lower risk"

    Score: (-risk_reward_ratio, -autofin_rating, -win_rate)

    Partial-budget allocation (user feedback 2026-09-08):
      When the budget has leftover after fitting as many full deals as
      possible, we DO NOT just skip the next deal — we allocate the
      leftover fraction to it (down to ``min_leftover_fraction`` of NAV,
      default 5%) so the portfolio can be fully invested. The
      signal is returned as a shallow copy with ``rate_for_nav``
      adjusted to the leftover amount, so ``_plan_orders_for_new_positions``
      sizes the position correctly without further changes.

      Example:
        NAV=100M, rateForNav=0.30 each, budget=1.0
        Deals A,B,C at 30% → 0.90 cumulative
        Deal D wants 0.30, leftover 0.10 → enter D at 0.10 NAV
        Deal E wants 0.30, leftover 0 → skip (no room)
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
        # Full fit: cumulative + s.rate_for_nav <= budget
        if cumulative + s.rate_for_nav <= nav_budget_pct + 1e-9:
            selected.append(s)
            cumulative += s.rate_for_nav
            continue
        # Overflow — try partial-budget entry on the leftover.
        leftover = nav_budget_pct - cumulative
        if (
            min_leftover_fraction > 0
            and leftover >= min_leftover_fraction
            and s.rate_for_nav > leftover
        ):
            # Allocate the leftover fraction to this deal. Return a
            # shallow copy with the adjusted rate so we don't mutate
            # the caller's signal.
            selected.append(replace(s, rate_for_nav=leftover))
            cumulative += leftover
            continue
        # No room — skip.
        continue
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


def _expand_signals_with_held_unentered(
    signals: list[AutofinSignal],
    holding: list[dict[str, Any]],
    our_deals: dict[str, dict[str, Any]],
    current_prices: dict[str, float],
    above_zone_tolerance_pct: float = 0.05,
) -> list[AutofinSignal]:
    """Augment ``signals`` with held-but-not-entered deals that are
    currently in the buy zone.

    Per user feedback (2026-09-08): autofin's recommendation gives a
    *price range* (e.g. "Vùng mua: 20,650 - 21,300"). The deal can be
    entered at any time while the current market price is within that
    range — NOT only on ``time_Open`` day. The previous logic only
    considered signals from /signal-static-suggest (latest 20 deals),
    so any older deal that autofin kept open but dropped off the
    suggest page was invisible to the entry logic.

    This function scans ``holding`` (autofin's source of truth for
    currently-open deals) and converts any deal we have NOT yet
    entered (``our_deals[deal_id]._our_qty == 0`` or missing) into
    a synthetic AutofinSignal, but ONLY if its current market price
    is within (or up to ``above_zone_tolerance_pct`` above) the
    declared supply zone. De-duplicated against existing ``signals``
    by deal_id so fresh signals win.
    """
    seen_ids: set[str] = {_deal_id(s.raw) for s in signals if s.symbol}
    out: list[AutofinSignal] = list(signals)
    for h in holding:
        did = _deal_id(h)
        if not did or did in seen_ids:
            continue
        # Already entered this deal — partial/full-close logic handles it.
        if (our_deals.get(did) or {}).get("_our_qty", 0) > 0:
            continue
        # Filter out holds that autofin has already closed — keep only
        # genuinely OPEN deals (statusDeal.isClosed != True).
        sd = h.get("statusDeal") or {}
        if sd.get("isClosed"):
            continue
        # Build signal-shaped record from the holding row.
        try:
            sig = parse_signal(h)
        except Exception:
            continue
        if not sig.symbol:
            continue
        cur = current_prices.get(sig.symbol, 0)
        if cur <= 0:
            # No price yet — can't gate on zone. Skip to avoid stale orders.
            continue
        s_lo = float(sig.supply_from or 0)
        s_hi = float(sig.supply_to or 0)
        if s_hi <= 0:
            # No declared buy zone — can't decide. Skip.
            continue
        if cur > s_hi * (1.0 + above_zone_tolerance_pct):
            # Way above the buy zone — wait for pullback.
            continue
        # Also enforce a reasonable lower bound: if current price is
        # more than 30% below the buy zone, the deal is likely
        # already invalid (autofin's zone has moved past). Skip.
        if s_lo > 0 and cur < s_lo * 0.7:
            continue
        out.append(sig)
        seen_ids.add(did)
    return out


def _plan_orders_for_new_positions(
    selected: list[AutofinSignal],
    nav_vnd: float,
    our_positions: dict[str, int],
    board_lot: int = DEFAULT_BOARD_LOT,
    min_order_value_vnd: float = 1_000_000,
    user_sl_pct: float = 0.0,
    current_prices: dict[str, float] | None = None,
    above_zone_tolerance_pct: float = 0.05,
    already_planned_symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Emit BUY + TP + SL bracket orders for **new** positions only.

    Fixed-sizing model (introduced 2026-09-07): a position is
    sized ONCE at entry based on the current NAV × rateForNav.
    We never top up after that — partial-TP signals reduce the
    position, full-close exits. This avoids the NAV-based death
    spiral where daily rebalancing amplifies losses (when NAV
    drops, target shrinks, strategy SELLs, realizes losses,
    NAV drops further, more SELLs).

    Per user feedback (2026-09-09): also accept ``already_planned_symbols`` —
    a set of symbols that are already in the bot's persistent
    ``_our_deals`` with positive ``_our_qty``. The bot was previously
    relying solely on DNSE's positions (via ``our_positions``) to skip
    duplicate entries, which works fine in live mode but causes double
    entries in dry-run (where DNSE returns empty positions even
    though we have "planned" entries from prior cycles). Now the
    bot also skips symbols that are already tracked in its own state
    — so a held deal with no DNSE position still won't be re-bought
    on the next EOD cycle.

    Each emitted order dict carries a ``kind`` field that the
    backtest uses to route orders correctly:
      - ``kind="entry"``: BUY at entry, fills immediately at today's price
      - ``kind="sl"``: stop-loss bracket, conditional (fires only on trigger)
        (TP brackets are NOT emitted here — autofin's TP events are
        partial closes driven by the per-event ``numberOfProfit``
        logic in ``_plan_partial_close_orders``.)

    Concretely:
      - If we already hold a positive qty for this symbol,
        skip entirely. The partial-close / full-close logic
        handles exits.
      - Otherwise, size the new BUY at
        target_qty = NAV * rateForNav / ref_price.
      - Emit the BUY plus TP and SL bracket. SL is the TIGHTER
        of autofin's declared sL_F and the user's `user_sl_pct`
        below entry (default 0 = use autofin's SL only).

    Entry price selection (user feedback 2026-09-08):
      - If ``current_prices[symbol]`` is provided AND the price is
        within (or up to ``above_zone_tolerance_pct`` above) the
        buy zone ``[supply_from, supply_to]``, enter.
      - Entry price = ``min(current_price, supply_to)`` — buys at
        the better of the two, so we never overpay vs the buy zone
        ceiling.
      - If current price is way above the buy zone (above tolerance),
        SKIP — wait for a pullback to the zone.
      - If no current price is available for this symbol (price data
        not yet synced), fall back to the legacy behaviour of entering
        at ``supply_to`` (high end of buy zone).
    """
    rows: list[dict[str, Any]] = []
    for s in selected:
        if not s.symbol:
            continue
        current_qty = our_positions.get(s.symbol, 0)
        if current_qty > 0:
            # Already hold this position — never rebalance.
            continue
        # ALSO skip if this symbol is already in our tracked deals
        # with positive _our_qty. This catches the dry-run case where
        # DNSE returns empty positions but the bot already planned
        # an entry on a previous cycle.
        if already_planned_symbols and s.symbol in already_planned_symbols:
            continue
        if s.rate_for_nav <= 0 or nav_vnd <= 0:
            continue
        target_value = nav_vnd * s.rate_for_nav
        if target_value < min_order_value_vnd:
            continue
        ref_price = _signal_ref_price(s)
        if ref_price <= 0:
            continue
        # ----- Entry-price gate based on current market price -----
        cur = (current_prices or {}).get(s.symbol, 0)
        s_lo = float(s.supply_from or 0)
        s_hi = float(s.supply_to or 0)
        if cur > 0:
            # Way above the buy zone → skip and wait for pullback.
            if s_hi > 0 and cur > s_hi * (1.0 + above_zone_tolerance_pct):
                continue
            # Below or at the top of zone → enter at the better price.
            entry_price = int(round(min(cur, s_hi))) if s_hi > 0 else int(round(cur))
        else:
            # No live price → legacy fallback.
            entry_price = int(round(ref_price))
        if entry_price <= 0:
            continue
        target_qty = _round_to_lots(int(target_value / entry_price), board_lot)
        if target_qty <= 0:
            continue
        did = _deal_id(s.raw)
        rows.append({
            "symbol": s.symbol,
            "side": "NB",
            "price": entry_price,
            "quantity": target_qty,
            "kind": "entry",
            "reason": (
                f"autofin_buy:deal#{did}:"
                f"nav{int(s.rate_for_nav*100)}pct:{s.last_recomment[:60]}"
            ),
        })
        # NO TP bracket on entry — TPs come from autofin's per-event signal
        # (``numberOfProfit`` increment / ``lastRecomment`` partial close).
        # Autofin publishes TPs as PARTIAL closes at varying percentages
        # (e.g. 10%, 30% of REMAINING position), not full closes. The bracket
        # mechanism was emitting a SELL of the full qty at s.tp_from, which
        # both (a) duplicated the per-event logic (causing 2× the intended
        # sell when both fire) and (b) closed the position when autofin
        # only wanted a partial trim. The correct primary mechanism is
        # ``_plan_partial_close_orders``, which reads autofin's actual %
        # from ``lastRecomment`` and ``numberOfProfit`` deltas.
        # SL bracket only — autofin's stop-loss exits are always full-close
        # ("Đóng DEAL chạm điểm cắt lỗ"). Keeping this as a safety net
        # means that if autofin's API hiccups during a fast drop, our
        # _our_qty > 0 + 5%-below-entry bracket catches the loss BEFORE
        # the per-event logic fires on the next cycle.
        autofin_sl = int(round(s.sl_from)) if s.sl_from > 0 else 0
        user_sl = int(round(entry_price * (1 - user_sl_pct))) if user_sl_pct > 0 else 0
        effective_sl = max(autofin_sl, user_sl)
        if effective_sl > 0 and effective_sl < entry_price:
            # Identify which source the SL came from for the Telegram message
            if user_sl > autofin_sl:
                sl_reason = f"autofin_user_sl:deal#{did}:{user_sl_pct:.0%}:{effective_sl}"
            else:
                sl_reason = f"autofin_sl:deal#{did}:{int(s.sl_from)}"
            rows.append({
                "symbol": s.symbol,
                "side": "NS",
                "price": effective_sl,
                "quantity": target_qty,
                "kind": "sl",
                "reason": sl_reason,
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
            "kind": "full_close",
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
            # Reduce events often include a price on the next line as
            # "Giá: X - Nắm giữ: Y ngày". Try to extract it.
            price_m = re.search(r"Giá\s*:\s*([\d,\.]+)", rec)
            price = _parse_vnd(price_m.group(1)) if price_m else 0.0
            return PartialCloseAction(action=action, lap=int(m.group(1)), price=price)
        if action == "capital_protect":
            pct = _parse_percent(m.group(1))
            # capital_protect also includes a price: "tại P"
            price_m = re.search(r"tại\s*([\d,\.]+)", rec)
            price = _parse_vnd(price_m.group(1)) if price_m else 0.0
            return PartialCloseAction(action=action, percent=pct, price=price)
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
            "kind": "partial_close",
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


def _event_to_partial_orders(
    event_title: str,
    event_body: str,
    our_deals: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert a single autofin notification event into one or more SELL orders.

    The event title + body has the same structured text that we already parse
    via ``_parse_partial_close`` (e.g. "💰 Chốt lãi lần 1 mức giá 15,000",
    "⚠️ Hạ 50 % tỉ trọng lần 2", "🛡️ Chốt lãi 12.50 % bảo toàn vốn tại 10,150").
    We reuse that parser and translate it into SELL orders that the
    bot can submit.

    The notification body doesn't include the deal_id explicitly (it's
    not in the standard autofin notification schema), so we match the
    SYMBOL in the body (e.g. "[HII] 💰 Chốt lãi lần 1 ...") against
    the symbol of each deal in ``our_deals``. If we have HII in our
    deals and the event says "[HII] 💰 Chốt lãi ...", we attribute
    the partial-close to HII.

    Returns a list of SELL order dicts (same shape as
    ``_plan_partial_close_orders``). Empty list if the event doesn't
    parse or we don't have a matching tracked position.
    """
    import re
    # Extract the symbol from the body — usually wrapped in [SYMBOL].
    sym_match = re.search(r"\[([A-Z0-9]{2,5})\]", event_body or "")
    if not sym_match:
        return []
    symbol = sym_match.group(1).upper()

    # Find the matching tracked deal for this symbol.
    our_match: dict[str, Any] | None = None
    for did, sig in our_deals.items():
        if str(sig.get("symbol") or "").upper() == symbol and int(sig.get("_our_qty", 0)) > 0:
            our_match = sig
            our_match_id = did
            break
    if our_match is None:
        return []

    # Reuse the existing partial-close parser.
    action = _parse_partial_close(event_body)
    if action.action in ("unknown", "full_close"):
        return []

    our_qty = int(our_match.get("_our_qty", 0))
    if our_qty <= 0:
        return []

    if action.action in ("take_profit_lap", "reduce_lap"):
        delta = our_qty // 3
    elif action.action == "capital_protect":
        delta = int(our_qty * action.percent / 100)
    else:
        return []

    delta = _round_to_lots(delta)
    if delta < DEFAULT_BOARD_LOT:
        return []

    price = action.price if action.price > 0 else 0
    if price <= 0:
        return []

    return [{
        "symbol": symbol,
        "side": "NS",
        "price": price,
        "quantity": delta,
        "kind": "partial_close",
        "reason": (
            f"autofin_partial_event:{our_match_id}:{action.action}:"
            f"lap{action.lap}:pct{int(round(action.percent * 100))}:"
            f"{(event_title or '')[:60]}"
        ),
    }]


def _is_trading_event(tags: tuple[str, ...]) -> bool:
    """True if a notification has tags indicating a TP/SL/stop-event.

    We use this as a pre-filter so we don't waste cycles parsing
    daily-report / reminder notifications. The actual action type
    (TP / reduce / capital_protect / stop / close) is then
    determined by parsing the BODY text via ``_parse_partial_close``.
    """
    trade_tags = {"TAKEPROFIT", "STOPORDER", "TRAILINGSTOP"}
    return any(t in tags for t in trade_tags)


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
    """Stable key for an order row, used to de-dupe Telegram notifications.

    Per user feedback (2026-09-09): the key MUST be stable across cycles
    — same deal_id + action should always hash to the same key even if
    the entry price or partial-budget qty changes between days. Otherwise
    we'd re-notify "new entry" messages every day the price moves a tick,
    which is confusing for users.
    """
    reason = str(o.get("reason") or "")
    did = _extract_deal_id(reason) or ""
    side = str(o.get("side") or "")
    symbol = str(o.get("symbol") or "")
    kind = str(o.get("kind") or "")
    # Group by deal + action. Price/qty deliberately excluded so the
    # same deal's bracket arms dedupe cleanly across re-plans.
    return f"{did}|{side}|{symbol}|{kind}|{_arm(reason)}"


def _arm(reason: str) -> str:
    """Extract the order-arm from a reason string.

    Examples (input → output):
      autofin_buy:deal#1234:HII:nav15pct:...   → "entry"
      autofin_tp:deal#1234:HI:10000            → "tp:10000"
      autofin_user_sl:deal#1234:HII:5%:8550   → "usersl"
      autofin_partial:deal#1234:HII           → "partial"
      autofin_closed:deal#1234:HII            → "closed"
    """
    r = reason.strip()
    if r.startswith("autofin_buy:"):
        return "entry"
    if r.startswith("autofin_tp:"):
        return "tp:" + r.split(":", 3)[-1]  # include the trigger level
    if r.startswith("autofin_user_sl:"):
        return "usersl"
    if r.startswith("autofin_sl:"):
        return "sl"
    if r.startswith("autofin_partial:"):
        return "partial"
    if r.startswith("autofin_closed:"):
        return "closed"
    return r[:32]


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


def decide_orders(
    *,
    holding: list[dict[str, Any]],
    signals: list[AutofinSignal],
    our_deals: dict[str, dict[str, Any]],
    our_positions: dict[str, int],
    nav: float,
    config: StrategyConfig | None = None,
    board_lot: int = DEFAULT_BOARD_LOT,
    user_sl_pct: float = 0.0,
    current_prices: dict[str, float] | None = None,
    already_planned_symbols: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Pure-functional decision function for the autofin mirror strategy.

    Given a snapshot of autofin state + our tracked state, return
    the orders to place and the new state to persist. No I/O, no
    time-of-day, no DNSE calls — so this function can be replayed
    against any historical snapshot, which is what the daily
    backtest does.

    Parameters
    ----------
    user_sl_pct : fraction (e.g. 0.05 = 5%) — sets a user-defined
        stop-loss at this fraction below the entry price. The actual
        SL bracket is the TIGHTER of this and autofin's declared
        sL_F. Default 0 = use autofin's SL only.
    current_prices : optional ``symbol → last_close`` map. When
        provided, the function augments ``signals`` with any HELD
        autofin deals we haven't entered yet, as long as their
        current price is within the buy zone (per
        ``_expand_signals_with_held_unentered``). BUY orders are
        also gated on price within zone + sized at the better of
        current_price vs supply_to.

    Returns
    -------
    (orders, new_our_deals, deals_actioned)
        ``orders``: list of DNSE order rows (close → partial → new).
        ``new_our_deals``: the updated ``our_deals`` state (deal_id →
            signal dict, with ``_our_qty`` and ``_last_numberOfProfit``
            updated to reflect the orders issued).
        ``deals_actioned``: per-deal grouping of orders, suitable for
        Telegram formatting.
    """
    config = config or StrategyConfig()

    # 1. Build lookup maps from holding snapshot
    autofin_holding_by_id: dict[str, dict[str, Any]] = {}
    for d in holding:
        did = _deal_id(d)
        if did:
            autofin_holding_by_id[did] = dict(d)

    # 2. Augment signals with HELD-but-not-entered deals that are
    # currently in the buy zone. Per user feedback (2026-09-08): an
    # autofin recommendation gives a price range, not a one-day
    # entry window — we can enter any day while current price is
    # within the zone. This also picks up older deals that dropped
    # off /signal-static-suggest but are still active in autofin's
    # /signal-static-holding.
    augmented_signals = signals
    if current_prices:
        augmented_signals = _expand_signals_with_held_unentered(
            signals, holding, our_deals, current_prices,
        )

    # 3. Select new-entry signals (top risk-adjusted, NAV-budgeted).
    # NOTE (2026-09-07, fixed-sizing model): we use the SUGGEST
    # endpoint only. The previous logic added HELD deals back to
    # `selected` so the strategy could "rebalance" them — but that
    # caused a death spiral when NAV dropped (target shrinks →
    # SELL → realized loss → NAV drops further → more SELLs).
    # Under fixed sizing, existing positions are NEVER re-evaluated
    # for entry sizing; partial-close / full-close logic handles exits.
    selected = _select_signals_within_nav_budget(
        augmented_signals, nav_budget_pct=1.0,
    )

    # 4. NO rebalance loop. Existing positions stay in `our_deals`
    # for partial-close / full-close detection only.

    # 5. Compute orders
    new_orders = _plan_orders_for_new_positions(
        selected, nav, our_positions,
        board_lot=board_lot, user_sl_pct=user_sl_pct,
        current_prices=current_prices,
        already_planned_symbols=already_planned_symbols,
    )
    partial_orders = _plan_partial_close_orders(our_deals, holding, board_lot=board_lot)
    our_real_deals = {
        did: sig for did, sig in our_deals.items()
        if sig.get("_our_qty", 0) > 0
    }
    suggest_ids = {_deal_id(s.raw) for s in signals if s.raw.get("_id")}
    close_orders = _plan_close_orders(our_real_deals, holding, suggest_ids, board_lot=board_lot)

    # 4b. CRITICAL FIX (2026-09-07): don't re-enter deals that are being
    # closed in this same iteration. The previous logic added held
    # deals to `selected` for "rebalance", which caused a death spiral
    # when NAV had just dropped: we issue a close SELL (selling the
    # whole position), then immediately re-buy because the deal is
    # still in `selected` and `our_positions` is now zero. We need
    # to filter out symbols being closed from new entries.
    closing_symbols: set[str] = {
        o.get("symbol", "") for o in close_orders if o.get("symbol")
    }
    if closing_symbols:
        new_orders = [
            o for o in new_orders
            if o.get("symbol") not in closing_symbols
        ]
        selected = [
            s for s in selected if s.symbol not in closing_symbols
        ]

    # 5. Update our_deals state to reflect the orders we're issuing.
    # Build a deep-copy so callers can mutate our_deals without
    # affecting the in-memory state of the bot.
    new_our_deals: dict[str, dict[str, Any]] = {
        did: dict(sig) for did, sig in our_deals.items()
    }
    # for each new BUY we issue, add the deal + track qty
    for s in selected:
        did = _deal_id(s.raw)
        if not did:
            continue
        if did not in new_our_deals:
            new_our_deals[did] = dict(s.raw)
        new_our_deals[did].setdefault("_our_qty", 0)
        for o in new_orders:
            if (
                o["symbol"] == s.symbol
                and o["side"] == "NB"
                and "tp:" not in o["reason"]
                and "sl:" not in o["reason"]
            ):
                new_our_deals[did]["_our_qty"] = o["quantity"]
    # for each close, drop the deal
    for o in close_orders:
        closed_did = _extract_deal_id(o.get("reason", ""))
        if closed_did:
            new_our_deals.pop(closed_did, None)

    all_orders = close_orders + partial_orders + new_orders
    deals_actioned = _build_deals_actioned(
        new_orders, partial_orders, close_orders, autofin_holding_by_id
    )
    return all_orders, new_our_deals, deals_actioned


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
        # Last notification id we've seen (from /notification/get-notify-by-topics).
        # Used to fetch only NEW events on each poll (id > this). Empty string
        # means we haven't seen any yet (next poll fetches everything).
        self._last_notification_id: str = ""
        # Last time we fetched notifications. Throttles the mid-day poll.
        self._last_notification_poll_at: str | None = None
        # Notification-ids already converted into SELL orders — used to
        # de-dupe so the same event doesn't fire a SELL twice across cycles.
        self._notified_event_ids: set[str] = set()

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
            self._last_notification_id = str(payload.get("last_notification_id") or "")
            self._last_notification_poll_at = payload.get("last_notification_poll_at")
            self._notified_event_ids = set(
                str(x) for x in (payload.get("notified_event_ids") or [])
            )
        except Exception:
            self._our_deals = {}
            self._notified_order_keys = set()
            self._last_poll_at = None
            self._last_notification_id = ""
            self._last_notification_poll_at = None
            self._notified_event_ids = set()

    def _save_state(self, data_dir: Path) -> None:
        path = data_dir / "live" / "autofin_signal_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {
                "our_deals": self._our_deals,
                "notified_order_keys": sorted(self._notified_order_keys),
                "last_poll_at": datetime.now(timezone.utc).isoformat(),
                "last_notification_id": self._last_notification_id,
                "last_notification_poll_at": self._last_notification_poll_at,
                "notified_event_ids": sorted(self._notified_event_ids),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ))

    def last_poll_at(self) -> str | None:
        return self._last_poll_at

    def _poll_notifications_for_events(
        self,
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        """Fetch new autofin notification events and emit SELL orders for TP/SL.

        Per user feedback (2026-09-09): the ``/notification/get-notify-by-topics``
        endpoint gives us a fast event stream for TAKEPROFIT/STOPORDER/CLOSEDEAL
        — instead of waiting until the next EOD cycle to detect
        ``numberOfProfit`` deltas, we can read the event itself and
        issue a SELL order within minutes.

        Pagination: read up to ``max_pages * number`` events. We only
        process events with ``id > self._last_notification_id`` (so we
        don't re-fire orders for events we've already handled).

        The returned orders are in the same shape as
        ``_plan_partial_close_orders`` so the existing
        ``_notify_new_orders`` / ``format_cycle_summary`` paths work
        unchanged.

        Returns the list of partial-close orders derived from the new
        events. The caller should also call ``_notify_new_orders`` if it
        wants Telegram alerts.
        """
        try:
            client = self._client_lazy()
        except Exception as exc:
            print(f"[autofin_signal] notification poll: no client ({exc})")
            return []

        all_events = []
        try:
            for skip in range(0, max_pages * 20, 20):
                page = client.fetch_notifications(topics="SIGNAL", number=20, skip=skip)
                if not page:
                    break
                all_events.extend(page)
                # If a page came back short, we've hit the end of available
                # events — stop paginating.
                if len(page) < 20:
                    break
        except Exception as exc:
            print(f"[autofin_signal] notification poll failed: {exc}")
            return []

        # Sort events newest-first by id, then filter to NEW only.
        # Events arrive newest-first from the API (id is Unix-ns timestamp).
        new_events = []
        max_id_seen = int(self._last_notification_id) if self._last_notification_id else 0
        overall_max_id = max_id_seen
        for ev in all_events:
            if ev.id > overall_max_id:
                overall_max_id = ev.id
            if ev.id > max_id_seen and str(ev.id) not in self._notified_event_ids:
                new_events.append(ev)

        # Update last_notification_id so the next poll skips these.
        if overall_max_id > max_id_seen:
            self._last_notification_id = str(overall_max_id)
        self._last_notification_poll_at = datetime.now(timezone.utc).isoformat()

        # Convert TP/SL events into partial-close orders. The body parser
        # is the source of truth for the action type — the tag is just a
        # filter so we don't waste cycles parsing daily-report events.
        new_orders: list[dict[str, Any]] = []
        for ev in new_events:
            if not _is_trading_event(ev.tags):
                continue
            partials = _event_to_partial_orders(ev.title, ev.body, self._our_deals)
            for o in partials:
                # Mark this notification id as notified so we don't reprocess.
                self._notified_event_ids.add(str(ev.id))
                new_orders.append(o)

        # For CLOSEDEAL events on tracked positions, also queue a full-close SELL.
        for ev in new_events:
            if not ev.is_close_deal:
                continue
            if str(ev.id) in self._notified_event_ids:
                continue
            # Reuse _event_to_partial_orders with action="full_close" by
            # re-parsing — but our parser returns "unknown" for full close.
            # Instead, build a full-close order directly: find matching symbol,
            # set qty to remaining, price from body if available.
            import re
            sym_match = re.search(r"\[([A-Z0-9]{2,5})\]", ev.body or "")
            if not sym_match:
                continue
            symbol = sym_match.group(1).upper()
            our_match_id = None
            our_match = None
            for did, sig in self._our_deals.items():
                if (
                    str(sig.get("symbol") or "").upper() == symbol
                    and int(sig.get("_our_qty", 0)) > 0
                ):
                    our_match_id = did
                    our_match = sig
                    break
            if our_match is None:
                continue
            # Extract close price from body (e.g. "📦 HSL chốt 9,950, T+: ...").
            price_match = re.search(r"ch[oố]t\s*([\d,\.]+)", ev.body or "")
            close_price = int(price_match.group(1).replace(".", "").replace(",", "")) if price_match else 0
            qty = int(our_match.get("_our_qty", 0))
            if qty <= 0:
                continue
            self._notified_event_ids.add(str(ev.id))
            new_orders.append({
                "symbol": symbol,
                "side": "NS",
                "price": close_price,
                "quantity": qty,
                "kind": "close_event",
                "reason": (
                    f"autofin_close_event:{our_match_id}:{symbol}:"
                    f"price{close_price}:{(ev.title or '')[:60]}"
                ),
            })

        return new_orders

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
        # 2. /signal-static-suggest — latest recommended signals
        try:
            signals = client.fetch_signals(page_size=20)
        except Exception as exc:
            notify(f"⚠️ autofin fetch_signals failed: {exc}")
            signals = []
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
                positions = dict(positions)
            except ValueError:
                pass

        # 5c. User-defined stop-loss (overrides autofin's loose SL with
        # a tighter one). Read from env so it can be tuned per-environment.
        # 0 = disabled (use autofin's sL_F only). Default 10% matches
        # typical Vietnamese stock intraday volatility (~7%); 5% was
        # too tight and got stopped out by noise on entry-day gaps.
        try:
            user_sl_pct = float(os.environ.get("AUTOFIN_USER_SL_PCT", "0.10") or "0.10")
        except ValueError:
            user_sl_pct = 0.10

        # 5d. Build a symbol → last_close price map from local price data.
        # Used to (a) gate entry on price-within-buy-zone and (b) include
        # HELD-but-not-entered deals in the candidate list. Per user
        # feedback 2026-09-08, autofin deals have a price range, so the
        # bot should be able to enter at any time the current price is
        # within that range — not only on the day time_Open was set.
        try:
            from ..universe import load_price_bars
        except ImportError:
            load_price_bars = None  # type: ignore[assignment]
        current_prices: dict[str, float] = {}
        seen_symbols: set[str] = set()
        for s in signals:
            if s.symbol and s.symbol not in seen_symbols:
                seen_symbols.add(s.symbol)
        for h in holding:
            sym = str(h.get("symbol") or "").strip().upper()
            if sym and sym not in seen_symbols:
                seen_symbols.add(sym)
        if load_price_bars is not None:
            for sym in seen_symbols:
                try:
                    bars = load_price_bars(sym, config.dataDir)
                except Exception:
                    continue
                if bars:
                    last_close = float(bars[-1].close or 0)
                    if last_close > 0:
                        current_prices[sym] = last_close

        # 5e. Compute the set of symbols we already have planned entries
        # for (used to skip duplicate BUY emissions on consecutive EOD
        # cycles, especially in dry-run mode where DNSE returns empty
        # positions even though we've "planned" entries from prior cycles).
        # Per user feedback (2026-09-09): previously the bot double-bought
        # the same deal on consecutive days because it only consulted DNSE
        # for "do I already have this position" — and DNSE is empty in
        # dry-run mode.
        already_planned_symbols: set[str] = {
            str(sig.get("symbol") or "").strip().upper()
            for sig in self._our_deals.values()
            if int(sig.get("_our_qty", 0) or 0) > 0
        }

        # 6-9. Delegate to the pure decision function. This is the same
        # function the daily-replay backtest calls.
        all_orders, new_our_deals, deals_actioned = decide_orders(
            holding=holding,
            signals=signals,
            our_deals=self._our_deals,
            our_positions=positions,
            nav=nav,
            config=strategy_config,
            user_sl_pct=user_sl_pct,
            current_prices=current_prices,
            already_planned_symbols=already_planned_symbols,
        )

        # 9b. Notification-driven mid-day TP/SL detection. Per user feedback
        # 2026-09-09: pull the latest TAKEPROFIT/STOPORDER/CLOSEDEAL events
        # from autofin's notification feed and convert them into SELL orders
        # so we capture intraday events without waiting for EOD reconciliation
        # via numberOfProfit deltas. The new orders merge with the dedupe
        # flow so the same event never fires twice across cycles.
        # Build a holding-by-id map from the holding snapshot so the
        # notification parser can attribute events to deals (by symbol).
        autofin_holding_by_id: dict[str, dict[str, Any]] = {}
        for h in holding:
            did = _deal_id(h)
            if did:
                autofin_holding_by_id[did] = dict(h)
        notification_orders = self._poll_notifications_for_events()
        if notification_orders:
            all_orders.extend(notification_orders)
            deals_actioned.extend(_build_deals_actioned(
                new_orders=[],  # no new entries from events
                partial_orders=[o for o in notification_orders if o.get("kind") == "partial_close"],
                close_orders=[o for o in notification_orders if o.get("kind") == "close_event"],
                autofin_holding_by_id=autofin_holding_by_id,
            ))

        # 10. Update our persistent state. Note: decide_orders() returns
        # a deep-copied new_our_deals; we copy it back into our adapter's
        # state so subsequent calls see the new positions.
        self._our_deals = new_our_deals
        new_orders = [o for o in all_orders if o.get("side") == "NB" and "tp:" not in o.get("reason", "") and "sl:" not in o.get("reason", "")]
        partial_orders = [
            o for o in all_orders
            if "autofin_partial:" in o.get("reason", "")
            or o.get("kind") == "partial_close"
            or o.get("kind") == "close_event"
        ]
        close_orders = [o for o in all_orders if "autofin_closed:" in o.get("reason", "")]

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
