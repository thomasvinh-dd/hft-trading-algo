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

from vietnam_algo.libs.autofin_signals import (
    RECOMMENT_KIND_ENTRY,
    AutofinClient,
    AutofinSignal,
    classify_recomment,
    latest_partial_close_fraction,
    parse_signal,
)
from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig
from vietnam_algo.libs.notifier import notify
from vietnam_algo.libs.order_ledger import (
    TERMINAL_LEDGER_STATUSES,
    filled_quantity,
    load_ledger,
)
from vietnam_algo.libs.portfolio import build_portfolio_snapshot

from .base import StrategyAdapter


DEFAULT_BOARD_LOT = 100


def _load_env_credentials() -> tuple[str, str]:
    email = os.environ.get("AUTOFIN_EMAIL", "").strip()
    password = os.environ.get("AUTOFIN_PASSWORD", "").strip()
    return email, password


def _env_partial_close_fraction() -> float | None:
    """Read ``AUTOFIN_PARTIAL_CLOSE_FRACTION`` from env, or None.

    The env var lets the operator force a specific partial-close
    fraction (e.g. ``AUTOFIN_PARTIAL_CLOSE_FRACTION=0.10`` for
    autofin's "Tỷ trọng 10% danh mục nắm giữ" recommendation) when
    autofin's API doesn't expose the value in ``lastRecomment``.
    Must be a float in (0, 1].
    """
    raw = os.environ.get("AUTOFIN_PARTIAL_CLOSE_FRACTION", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if 0 < value <= 1:
        return value
    return None


def _round_to_lots(quantity: int, board_lot: int = DEFAULT_BOARD_LOT) -> int:
    if quantity <= 0:
        return 0
    return (quantity // board_lot) * board_lot


def _round_up_to_lots(quantity: int, board_lot: int = DEFAULT_BOARD_LOT) -> int:
    """Round UP to the next board lot (vs ``_round_to_lots`` which rounds down).

    Partial-close orders need round-up: when autofin says "sell 10% of
    position", ``10% × 600 = 60`` shares but Vietnamese boards trade
    in lots of 100. Rounding down would skip the order entirely;
    rounding up emits the smallest lot that exceeds the desired qty.
    """
    if quantity <= 0:
        return 0
    return ((quantity + board_lot - 1) // board_lot) * board_lot


def _extract_partial_fraction(
    recomment_text: str | None,
    *,
    default: float = 1 / 3,
    env_fraction: float | None = None,
) -> float:
    """Determine the partial-close fraction for a TP / reduce step.

    Resolution order:
      1. If ``recomment_text`` contains ``"Tỷ trọng X %"`` (autofin's
         UI uses this label for the post-TP target weight), parse X
         and use X/100 as the fraction. The pattern is loose on
         whitespace because autofin's HTML stripping produces
         irregular spacing ("Tỷ trọng 10 %" vs "Tỷ trọng  5%").
      2. If ``env_fraction`` is provided (from
         ``AUTOFIN_PARTIAL_CLOSE_FRACTION`` env var), use it.
      3. Fall back to ``default`` (1/3, the historical behavior).

    Returns a fraction in (0, 1]. The caller rounds the resulting
    qty up to the next board lot.
    """
    import re as _re
    if recomment_text:
        # autofin uses both "Tỷ trọng" (capital, in TP-step text) and
        # "tỷ trọng" / "tỉ trọng" (lowercase + U+1EC9 vs U+1EF7, in
        # the reduce-event body). The OLD regex used a capital T and a
        # y-with-dot, so it missed "tỷ trọng" entirely. The new one is
        # case-insensitive and accepts both y-with-dot (U+1EF7) and
        # i-with-dot (U+1EC9). The whitespace between the
        # consonant and "trọng" is optional because autofin emits
        # both "tỷtrọng" and "tỷ trọng" / "tỉ trọng".
        m = _re.search(
            r"t[ỷỉy]\s*trọng\s*([\d\.,]+)\s*%",
            recomment_text,
            _re.IGNORECASE,
        )
        if m:
            try:
                pct = float(m.group(1).replace(",", ""))
                if 0 < pct <= 100:
                    return pct / 100.0
            except (TypeError, ValueError):
                pass
    if env_fraction is not None and 0 < env_fraction <= 1:
        return env_fraction
    return default


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

    Entry-only filter (2026-10-05 fix):
      Only signals whose ``lastRecomment`` classifies as a fresh entry
      ("Khuyến nghị đầu tư") are eligible for new-entry budget allocation.
      Without this filter, ongoing TP/reduce signals with multi-lap
      ``tp_to`` zones (e.g. DXP tp_to=19000 while the deal is only on
      TP-lap 2) earn artificially high risk-reward scores and crowd out
      the real entry candidates, leaving the bot with cash > 5% NAV
      but 0 new orders (2026-10-05 HDB/ABB incident). The TP/reduce
      paths in ``_plan_partial_close_orders`` handle exits for any
      position we already hold, so excluding them here is safe.
    """
    entry_signals = [
        s for s in signals
        if classify_recomment(s.last_recomment) == RECOMMENT_KIND_ENTRY
    ]
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

    sorted_signals = sorted(entry_signals, key=_score)
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


# Default gap threshold for the autofin catch-up scan.
#
# Why this number: autofin's per-step TP/reduce signals typically fire
# over a 1-3 trading-day window from entry. A gap of 24 hours is
# enough to plausibly miss at least one TP step on a fast-mover,
# so we want to recover those signals when the bot comes back up.
#
# Below this threshold, the per-cycle ``numberOfProfit`` delta logic
# already handles missed signals (see ``_plan_partial_close_orders``).
# Above this threshold, we fall back to reading ``hisRecomment`` for
# the FULL history of every tracked deal and replaying any events
# our state hasn't seen yet.
#
# Operators can override via ``AUTOFIN_CATCHUP_THRESHOLD_HOURS`` env
# var (parsed in the adapter's ``_catchup_threshold_seconds`` method).
DEFAULT_CATCHUP_THRESHOLD_SECONDS = 24 * 60 * 60  # 24 trading hours


def _apply_catchup_orders_to_dry_run_state(
    catchup_orders_by_symbol: dict[str, list[dict[str, Any]]],
    dry_state: Any,
    our_deals: dict[str, dict[str, Any]],
    board_lot: int = DEFAULT_BOARD_LOT,
) -> tuple[int, int, int]:
    """Apply the catchup's missed-event orders to ``dry_state`` directly.

    The catchup scan reports events autofin fired while the bot was
    down — for symbols that are no longer in autofin's
    ``/signal-static-holding`` list, the next cycle's
    ``_plan_partial_close_orders`` won't visit them (line 1053:
    ``if did not in autofin_by_id: continue``). The catchup scan must
    therefore apply the missed events itself, otherwise the dry-run
    state stays out of sync with autofin forever (2026-09-30 HNM
    incident: 100 HNM shares held in dry-run, autofin issued 2 REDUCE
    50% steps during the bot's 4-day downtime, but neither step was
    applied because HNM had been manually closed in the autofin UI
    and dropped from autofin's holding list).

    Args:
      ``catchup_orders_by_symbol``: ``symbol → list[order]`` from
        ``_build_catchup_orders_for_deal``. Each order dict has
        ``side``, ``kind``, ``fraction``, ``price``, ``reason``
        and is sorted oldest-first.
      ``dry_state``: ``DryRunState`` instance (mutated in place).
      ``our_deals``: per-deal tracking dict (mutated in place to
        record the latest applied recomment + ``_last_numberOfProfit``
        + ``_last_partial_fraction`` baseline).
      ``board_lot``: for ``_emit_redmitt`` qty rounding.

    Returns:
      ``(applied_count, skipped_count, closed_count)`` — telemetry
      for the operator notification.
    """
    applied = 0
    skipped = 0
    closed = 0
    for sym, orders in catchup_orders_by_symbol.items():
        # Idempotency guard via ``applied_keys``. The catchup orders
        # use a synthetic date prefix (``catchup:<reason>``) so they
        # never collide with the EOD's per-date dedup keys. If a
        # previous catchup cycle already applied an order with the
        # same synthetic key, skip — prevents the HNM 100 → 50 → 25
        # → 12 → 6 chain when the bot runs the catchup multiple
        # times against the same deal (e.g. two ``last_poll_at``
        # resets in a row, or a bot restart that reloaded an
        # empty ``_our_deals`` from disk).
        applied_keys = set(dry_state.applied_keys or [])
        pos = dry_state.positions.get(sym)
        if pos is None and orders and orders[0].get("kind") != "entry":
            # Catchup orders for a symbol we don't hold in dry-run.
            # Without an entry event, we can't reconstruct the
            # initial BUY. Skip the partial closes — operator must
            # add the position to dry-run state manually if needed.
            for o in orders:
                skipped += 1
            continue
        for order in orders:
            kind = order.get("kind")
            side = order.get("side")
            price = int(order.get("price") or 0)
            fraction = float(order.get("fraction") or 0)
            if kind == "entry":
                # The autofin adapter's ``_plan_orders_for_new_positions``
                # will create the BUY the next time the deal becomes
                # active. Catchup skips entry replay because we don't
                # have the historical fill qty / cash-on-hand.
                skipped += 1
                continue
            if side != "NS":
                # BUY from catchup — same caveat as entry above.
                skipped += 1
                continue
            if pos is None or pos.quantity <= 0:
                skipped += 1
                continue
            if kind == "full_close":
                # Idempotency: skip if this exact catchup order was
                # applied in a previous cycle.
                dedup = f"catchup:{sym}:{side}:full:{price}"
                if dedup in applied_keys:
                    skipped += 1
                    continue
                sell_qty = pos.quantity
                sell_price = price or pos.last_price or pos.avg_price
                realized = (sell_price - pos.avg_price) * sell_qty if pos.avg_price > 0 else 0.0
                pos.quantity = 0
                pos.cost_basis = 0
                pos.avg_price = 0
                dry_state.cash += sell_qty * sell_price
                dry_state.realized_pnl += realized
                dry_state.closed_trades.append({
                    "symbol": sym,
                    "avg_cost": pos.avg_price,
                    "sell_price": sell_price,
                    "qty": sell_qty,
                    "realized_pnl": realized,
                })
                applied_keys.add(dedup)
                closed += 1
                applied += 1
                continue
            if kind in ("partial_close", "reduce_lap"):
                # Idempotency: skip if this exact catchup order was
                # applied in a previous cycle. The dedup key uses
                # ``sym + side + price + fraction`` (NOT qty) — qty
                # is derived from the current position size which
                # changes between runs (100 → 25). The fraction
                # uniquely identifies the autofin event: lần 1 vs
                # lần 2 both have fraction=0.5 but different prices,
                # and the price is part of the recomment text.
                dedup = (
                    f"catchup:{sym}:{side}:"
                    f"{price}:{int(round(fraction * 100))}"
                )
                if dedup in applied_keys:
                    skipped += 1
                    continue
                # Vietnamese stocks trade in board lots of 100 —
                # you can't sell a sub-lot quantity. Compute the
                # raw reduce amount and round UP to the nearest
                # board lot. If the rounded-up amount exceeds the
                # current position (e.g. a 50% reduce on a 100-share
                # lot rounds up to 100, which is the entire
                # position), do a full close instead. This is the
                # 2026-10-01 fix: the previous code rounded DOWN,
                # leaving HNM at 2 shares after multiple reduce
                # 50% events on a 100-share entry. The user feedback
                # was explicit: "for ex if origin is 100 share,
                # 50% will be 50 share round to 100 is 100 share,
                # so we will have no shares left and need to close
                # the deal out of our porfolio".
                if fraction > 0:
                    raw_reduce = int(pos.quantity * fraction)
                else:
                    raw_reduce = pos.quantity // 2  # fallback if fraction is 0
                if raw_reduce <= 0:
                    skipped += 1
                    continue
                if board_lot > 1:
                    sell_qty = ((raw_reduce + board_lot - 1) // board_lot) * board_lot
                else:
                    sell_qty = raw_reduce
                # Don't oversell — if the rounded-up amount exceeds
                # the position, do a full close instead. This is
                # the small-position case the user described.
                sell_qty = min(sell_qty, pos.quantity)
                sell_price = price or pos.last_price or pos.avg_price
                realized = (sell_price - pos.avg_price) * sell_qty if pos.avg_price > 0 else 0.0
                # If the reduce fully closes the position, also
                # emit a closed_trades record so the operator's
                # EOD summary reflects the exit.
                closed_this = sell_qty >= pos.quantity
                pos.quantity -= sell_qty
                pos.cost_basis = pos.quantity * pos.avg_price if pos.avg_price > 0 else 0.0
                if closed_this:
                    pos.avg_price = 0
                    pos.last_price = sell_price
                    dry_state.closed_trades.append({
                        "symbol": sym,
                        "avg_cost": 0,  # cleared above
                        "sell_price": sell_price,
                        "qty": sell_qty,
                        "realized_pnl": realized,
                    })
                    # Remove the closed position so the EOD summary
                    # doesn't list it as a 0-share holding. Use
                    # pop with default to avoid KeyError if the
                    # position was already removed (defensive).
                    dry_state.positions.pop(sym, None)
                    closed += 1
                dry_state.cash += sell_qty * sell_price
                dry_state.realized_pnl += realized
                applied_keys.add(dedup)
                applied += 1
                continue
            skipped += 1
        # Update tracking state for the next cycle so the regular
        # ``_plan_partial_close_orders`` won't re-fire on these
        # events. Without this we'd risk double-applying on the
        # next cycle's numberOfProfit delta logic.
        did_key = next(
            (k for k, v in our_deals.items() if v.get("symbol") == sym),
            None,
        )
        # If the deal isn't tracked in our_deals (orphan symbol that
        # autofin's /signal-static-holding dropped), add a minimal
        # entry so future catchup runs can find it via did_key above.
        # The deal_id used here is just a placeholder — the real
        # deal_id (autofin's `time_Open`-derived) isn't available
        # without an autofin fetch, but the symbol-based lookup is
        # enough to deduplicate catchup re-runs.
        if did_key is None and orders and applied > 0:
            did_key = "orphan:{sym}".format(sym=sym)
            our_deals[did_key] = {"symbol": sym, "_our_qty": 0}
        if did_key and orders:
            last_event = orders[-1]
            our_deals[did_key]["_last_recomment"] = (
                (last_event.get("reason") or "").split(":")[-1]
            )
            our_deals[did_key]["_last_partial_fraction"] = (
                float(last_event.get("fraction") or 0) or None
            )
            # numberOfProfit is unknown from the catchup (we don't
            # have autofin's per-deal numberOfProfit for orphan deals).
            # Set a sentinel — the next cycle's bootstrap (last_nP < 0)
            # would re-anchor on whatever autofin reports then.
            if "_last_numberOfProfit" not in our_deals[did_key]:
                our_deals[did_key]["_last_numberOfProfit"] = -1
        # Persist the catchup dedup keys back to the state. This is
        # what prevents the re-fire: a future catchup cycle reads
        # the same ``applied_keys`` set from disk and skips orders
        # whose synthetic key is already present. The ``:orphan:HNM``
        # key would also work but applied_keys is more reliable
        # because it's an unbounded set rather than a per-deal dict.
        dry_state.applied_keys = applied_keys
    return applied, skipped, closed


def _parse_deal_open_date(deal: dict[str, Any]) -> str | None:
    """Extract the deal's open date as ``YYYY-MM-DD`` from autofin's
    ``statusDeal.time_Open`` field.

    autofin formats the field as ``"MM/DD/YYYY HH:MM:SS"`` (Vietnamese
    locale). Returns None if the field is missing or unparseable.
    """
    sd = deal.get("statusDeal") or {}
    raw = sd.get("time_Open") or ""
    if not raw:
        return None
    # Match "MM/DD/YYYY" prefix.
    import re as _re
    m = _re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    return f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"


def _event_already_applied(event: dict[str, Any], last_recomment: str) -> bool:
    """Return True if the given parsed autofin event was already
    captured in our ``_last_recomment`` (so the catch-up scan should
    skip it).

    The match is heuristic — we strip the ``[SYM]`` prefix and the
    trailing body details from both sides and compare the remaining
    prefix. False negatives are safe (the catch-up scan would re-fire
    a SELL — but the per-deal state tracking prevents this); false
    positives are also safe (we'd just replay one extra event, which
    the next cycle's ``numberOfProfit`` check would skip anyway).

    IMPORTANT: ``_last_recomment`` describes the LATEST applied
    event. All events BEFORE it in the history are also applied
    (you can't have applied a TP step without first having entered
    the deal). So when ``_last_recomment`` matches event N, we
    treat events 0..N as applied and only events N+1..end as
    missed.

    The matching is done per-deal via ``_last_applied_event_index``
    rather than per-event to keep the logic O(N) instead of O(N²).
    """
    # This helper is intentionally kept for the per-event check below,
    # but the catch-up builder uses the indexed version
    # ``_last_applied_event_index`` (below) so an earlier event with
    # the same prefix as a later one isn't accidentally filtered.
    if not last_recomment:
        return False
    last = last_recomment.strip()
    raw = (event.get("raw") or "").strip()
    if not raw:
        return False
    import re as _re
    raw_clean = _re.sub(r"^\s*\[[A-Z]+\]\s*", "", raw)
    if last and last in raw_clean:
        return True
    def norm(s):
        return _re.sub(r"\s+", " ", _re.sub(r"[^\w\s%]", "", s)).strip()
    return bool(norm(last) and norm(last) in norm(raw_clean))


def _last_applied_event_index(
    events: list[dict[str, Any]],
    last_recomment: str,
) -> int:
    """Return the index of the latest event in ``events`` whose text
    matches ``last_recomment``, or ``-1`` if no event matches.

    The catch-up scan replays events ``[idx+1, end]`` as missed
    signals. With ``last_recomment=""`` (cold start), ``idx=-1``
    so all events are replayed.
    """
    if not last_recomment or not events:
        return -1
    last = last_recomment.strip()
    last_idx = -1
    for i, event in enumerate(events):
        if _event_already_applied(event, last):
            last_idx = i
    return last_idx


def _build_catchup_orders_for_deal(
    deal: dict[str, Any],
    our_deal_state: dict[str, Any] | None,
    board_lot: int = DEFAULT_BOARD_LOT,
) -> list[dict[str, Any]]:
    """Generate catch-up orders for one autofin deal.

    For each event in ``hisRecomment`` AFTER our last applied
    ``_last_recomment``, emit an order dict in the same shape
    ``_plan_partial_close_orders`` / ``_plan_orders_for_new_positions``
    produce — same ``side`` / ``kind`` / ``reason`` / etc. — so the
    bot's normal apply path can replay them in chronological order.

    Returns a list of order dicts, ordered oldest-first. Empty list
    when nothing was missed.

    Catches up four event types:
      - ``entry`` (kind="entry", side="NB"): emitted when we never
        tracked the deal.
      - ``tp`` (kind="partial_close", side="NS"): partial close at
        the autofin-specified fraction.
      - ``reduce`` (kind="reduce_lap", side="NS"): autofin reduce
        ("Hạ X % tỉ trọng") at the specified fraction.
      - ``capital_protect`` (kind="partial_close", side="NS"):
        treated like a partial close at the capital-protect %.
      - ``close`` (kind="full_close", side="NS"): full close at the
        autofin close price.
    """
    try:
        from vietnam_algo.libs.autofin_signals import parse_recommendation_history
    except ImportError:
        return []

    events = parse_recommendation_history(deal)
    if not events:
        return []

    # Find the index of the last applied event. All events after
    # that index are missed (TP steps fired while the bot was
    # down, etc.). Using an index instead of a per-event check
    # handles the case where multiple events share a prefix
    # (e.g. two TP steps both contain "Chốt lãi" text).
    last_recomment = (our_deal_state or {}).get("_last_recomment") or ""
    last_idx = _last_applied_event_index(events, last_recomment)
    pending = events[last_idx + 1:]
    if not pending:
        return []

    did = _deal_id(deal)
    sym = deal.get("symbol") or "?"
    sd = deal.get("statusDeal") or {}
    p_open = float(sd.get("price_Open") or 0)

    orders: list[dict[str, Any]] = []
    for event in pending:
        action = event.get("action")
        price = int(event.get("price") or 0)
        fraction = float(event.get("fraction") or 0)
        raw_text = (event.get("raw") or "")[:80]

        if action == "entry":
            orders.append({
                "symbol": sym,
                "side": "NB",
                "price": int(p_open) if p_open > 0 else price,
                "quantity": 0,  # caller fills via existing entry plan
                "kind": "entry",
                "reason": f"autofin_catchup:deal#{did}:entry:{raw_text}",
                "_catchup": True,
            })
        elif action in ("tp", "reduce", "capital_protect"):
            kind = (
                "reduce_lap" if action == "reduce"
                else "partial_close"
            )
            orders.append({
                "symbol": sym,
                "side": "NS",
                "price": int(price) if price > 0 else int(p_open),
                "quantity": 0,  # filled by caller from `_our_qty`
                "fraction": fraction,
                "kind": kind,
                "reason": (
                    f"autofin_catchup:deal#{did}:{action}:"
                    f"pct{int(round(fraction*100))}:{raw_text}"
                ),
                "_catchup": True,
            })
        elif action == "close":
            orders.append({
                "symbol": sym,
                "side": "NS",
                "price": int(price) if price > 0 else int(p_open),
                "quantity": 0,
                "kind": "full_close",
                "reason": f"autofin_catchup:deal#{did}:close:{raw_text}",
                "_catchup": True,
            })

    return orders


def _expand_signals_with_held_unentered(
    signals: list[AutofinSignal],
    holding: list[dict[str, Any]],
    our_deals: dict[str, dict[str, Any]],
    current_prices: dict[str, float],
    above_zone_tolerance_pct: float = 0.03,
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
    is within (or within ``above_zone_tolerance_pct`` of either edge
    of) the declared supply zone ``[supply_from, supply_to]``. The
    same ``above_zone_tolerance_pct`` is used symmetrically for both
    the upper and lower bounds — i.e. a price more than 5 % below
    ``supply_from`` is also rejected as out-of-zone. De-duplicated
    against existing ``signals`` by deal_id so fresh signals win.
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
        # 2026-10-07: also enforce a lower bound (5 % below ``supply_F``,
        # matching the upper-bound tolerance above and the buy-zone
        # gate in ``_plan_orders_for_new_positions``). Previously this
        # used a 30 % tolerance — too permissive, it let in deals
        # whose current price was materially below autofin's intended
        # zone. The plan function would then drop them again on the
        # tighter gate, so the asymmetry was harmless but wasteful.
        # Aligning both stages to 5 % keeps the held-deal candidate
        # list in sync with what the plan function will actually
        # accept.
        if s_lo > 0 and cur < s_lo * (1.0 - above_zone_tolerance_pct):
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
    above_zone_tolerance_pct: float = 0.03,
    below_zone_tolerance_pct: float = 0.03,
    already_planned_symbols: set[str] | None = None,
    available_cash_vnd: float | None = None,
    new_entry_min_cash_pct: float = 0.05,
    top_n: int = 1,
) -> list[dict[str, Any]]:
    """Emit BUY + TP + SL bracket orders for **new** positions only.

    Fixed-sizing model (introduced 2026-09-07): a position is
    sized ONCE at entry based on the current NAV × rateForNav.
    We never top up after that — partial-TP signals reduce the
    position, full-close exits. This avoids the NAV-based death
    spiral where daily rebalancing amplifies losses (when NAV
    drops, target shrinks, strategy SELLs, realizes losses,
    NAV drops further, more SELLs).

    Cash cap (added 2026-09-16, user feedback): each BUY's
    ``target_value`` is capped at the **remaining cash budget**
    rather than unbounded NAV × rateForNav. Without this, the
    bot would plan BUY orders of ``nav × rateForNav`` even when
    the portfolio is fully invested in stocks and the cash
    account can't cover them — leaving the apply phase to
    silently skip them or, worse, in live mode the broker would
    reject the order. We track ``remaining_cash`` across the
    signal list so multiple BUYs share the cash budget rather
    than each one independently re-using the full amount.

    If ``available_cash_vnd`` is not provided, the function falls
    back to ``nav_vnd`` (the original behaviour).

    Per user feedback (2026-09-21): the bot's entry-selection rules
    are stricter than just "NAV × rate ≤ cash":
      - Each signal's ``last_recomment`` is classified (via
        :func:`classify_recomment`) and ONLY entry-recommendation
        signals ("Khuyến nghị đầu tư") are eligible. TP / SL /
        reduce / close signals belong to the per-event path.
      - ``new_entry_min_cash_pct`` (default 0.05 = 5% of NAV) gates
        the whole call: if ``available_cash_vnd / nav_vnd < 5%``
        we skip ALL new entries for this cycle. This prevents
        tiny dust entries when the portfolio is already nearly
        fully invested.
      - ``top_n`` (default 1) — only the top-N signals (in the
        order returned by the caller, typically autofin's
        ``/signal-static-suggest`` ordering) can produce entries.
        This enforces "only the top suggestion" per user spec.

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

    Entry price selection (user feedback 2026-09-08, refined 2026-10-07):
      - If ``current_prices[symbol]`` is provided AND the price is
        within (or within ``above_zone_tolerance_pct`` above /
        ``below_zone_tolerance_pct`` below) the buy zone
        ``[supply_from, supply_to]``, enter.
      - Entry price = ``min(current_price, supply_to)`` — buys at
        the better of the two, so we never overpay vs the buy zone
        ceiling.
      - If current price is way above the buy zone (above tolerance),
        SKIP — wait for a pullback to the zone.
      - If current price is way below the buy zone (below tolerance,
        2026-10-07), SKIP — the deal's analysis no longer applies
        in this price regime; avoid catching a falling knife.
      - If no current price is available for this symbol (price data
        not yet synced), fall back to the legacy behaviour of entering
        at ``supply_to`` (high end of buy zone).
    """
    rows: list[dict[str, Any]] = []
    # 2026-10-02 fix: pre-strip signals whose symbols are in
    # ``already_planned_symbols`` (i.e. we've already issued an order
    # for them) BEFORE the top-N loop. The previous logic ran the
    # top-N check at the start of every iteration (line ~854:
    # ``if considered >= top_n: break``), so when autofin's
    # /signal-static-suggest returned an already-actioned deal at
    # position 0, the single ``top_n=1`` slot was consumed by that
    # deal — the loop exited and any real new suggestions lower
    # in the list were never evaluated. The result: cash > 5% NAV
    # gate passed, but zero orders triggered because the top
    # suggestion was an old, actioned deal. Stripping actioned
    # symbols up-front means ``top_n`` now measures "top N
    # candidates we haven't already traded", which is what the
    # user actually wants.
    if already_planned_symbols:
        _before = len(selected)
        selected = [
            s for s in selected if s.symbol not in already_planned_symbols
        ]
        _filtered = _before - len(selected)
        if _filtered > 0:
            print(
                f"[autofin_signal] filtered {_filtered} already-actioned "
                f"signal(s) before top-N selection"
            )
    # Cash budget shared across the whole selected list. Defaults to
    # nav_vnd if no explicit budget was passed (legacy behaviour).
    if available_cash_vnd is None or available_cash_vnd <= 0:
        available_cash_vnd = nav_vnd
    remaining_cash = float(available_cash_vnd)
    # Per user feedback 2026-09-21: skip ALL new entries when
    # available cash is below ``new_entry_min_cash_pct * nav``.
    # Rationale: tiny dust entries (e.g. 1 share) are noise and
    # don't justify the buy+SL bracket ceremony. Gate at the top so
    # we don't even iterate over the signal list.
    if nav_vnd > 0 and remaining_cash < nav_vnd * new_entry_min_cash_pct:
        return rows
    skipped_non_entry = 0
    skipped_no_cash = 0
    considered = 0
    for s in selected:
        if not s.symbol:
            continue
        # Per user feedback 2026-09-21: top-N filter. Only the first
        # ``top_n`` signals (in autofin's /signal-static-suggest order,
        # typically recency-sorted) can produce entries. Stops the
        # bot from chaining 5 entries across 5 signals when only the
        # top suggestion is meant to be acted on per day.
        if considered >= top_n:
            break
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
        considered += 1
        # ----- Recomment classification (2026-09-21) -----
        # Per user feedback: pre-fix the bot fired BUY orders for ANY
        # signal in /signal-static-suggest, including deals where
        # autofin was actually publishing a TP / SL / reduce update
        # (e.g. "💰 Chốt lãi lần 1 mức giá 16,600" — TP step on a
        # deal we don't hold). Classify the recommmend text FIRST
        # and only proceed to plan a BUY if it's a fresh entry
        # recommendation ("Khuyến nghị đầu tư"). Everything else
        # is a partial-close / reduce / close signal that belongs in
        # the per-event logic, not here.
        rec_kind = classify_recomment(s.last_recomment)
        if rec_kind != RECOMMENT_KIND_ENTRY:
            # Not an entry recommendation — skip. The TP/SL/reduce
            # paths in ``_plan_partial_close_orders`` /
            # ``_plan_close_orders`` will pick this up if WE hold
            # the symbol; otherwise there's nothing to act on.
            skipped_non_entry += 1
            continue
        if s.rate_for_nav <= 0 or nav_vnd <= 0:
            continue
        # Cash cap (2026-09-16): each BUY's target_value is capped at
        # the remaining cash budget, so the bot doesn't emit BUY
        # orders it can't actually afford when the portfolio is fully
        # invested in stocks. We track ``remaining_cash`` across the
        # signal list so multiple BUYs share the cash budget.
        if available_cash_vnd is None or available_cash_vnd <= 0:
            available_cash_vnd = nav_vnd
        target_value = min(nav_vnd * s.rate_for_nav, remaining_cash)
        if target_value < min_order_value_vnd:
            skipped_no_cash += 1
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
            # 2026-10-07 fix: also enforce a lower bound. Autofin's
            # ``supply_F..supply_T`` is the recommended buy zone; if
            # the current price has fallen materially below the floor
            # (default 5 % below ``supply_F``) the deal's analysis
            # no longer applies — the price is in a different regime
            # than what autofin was tracking. Previously the bot
            # would happily BUY at any price below the zone ceiling
            # (e.g. ``Vùng mua: 13,800 - 14,100`` → BUY @ 13,000 when
            # price had dropped 8 %), exposing us to catching a
            # falling knife. Skip and wait for price to climb back
            # into the zone (or for autofin to republish).
            if s_lo > 0 and cur < s_lo * (1.0 - below_zone_tolerance_pct):
                continue
            # Inside the zone (or just outside either tolerance) →
            # 2026-10-09: enter at the LIVE market price, not capped
            # at the zone ceiling. The 3% zone-tolerance check above
            # is the gate that filters out-of-zone prices; the entry
            # price is just whatever the market shows. This is more
            # honest (we pay what we'd actually pay) and aligns the
            # planning price with the live-quote gate at submit time
            # (which uses the same DNSE best-ask as the live best-ask
            # fetched at planning time — see generate_orders below).
            entry_price = int(round(cur))
        else:
            # No live price → legacy fallback.
            entry_price = int(round(ref_price))
        if entry_price <= 0:
            continue
        target_qty = _round_to_lots(int(target_value / entry_price), board_lot)
        if target_qty <= 0:
            continue
        # Cap by remaining cash: even if target_value fits at the
        # remaining_cash level, we want to verify the actual round-lot
        # cost fits too. Reduce target_qty until it does, otherwise skip.
        actual_cost = target_qty * entry_price * 1.0015  # include 15bps fees
        while target_qty > 0 and actual_cost > remaining_cash:
            target_qty -= board_lot
            actual_cost = target_qty * entry_price * 1.0015
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
        # Deduct actual planned cost from remaining_cash so the next
        # BUY in this iteration shares the cash budget.
        remaining_cash -= actual_cost
        if remaining_cash <= 0:
            break
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
    our_positions: dict[str, int] | None = None,
    autofin_closing_ids: set[str] | None = None,
    autofin_suggest_ids: set[str] | None = None,
    board_lot: int = DEFAULT_BOARD_LOT,
) -> list[dict[str, Any]]:
    """Emit SELL orders for deals we hold that autofin has closed.

    Two close strategies are supported:

    1. **Live bot (preferred):** ``autofin_closing_ids`` is the
       authoritative close trigger — when autofin's ``/closing-signals``
       explicitly publishes a close for a deal we hold, we mirror with
       a SELL at autofin's suggested price. We do NOT close on
       absence from ``/signal-static-holding`` alone (or holding +
       suggest), because autofin's automation can briefly drop a deal
       from holding while it's still active — closing on absence alone
       caused the 2026-09-14 HDB false-positive.

    2. **Backtest / historical replay:** ``autofin_closing_ids`` is
       ``None``. In that case we fall back to absence from holding +
       suggest: any deal we hold that's no longer in either list was
       closed by autofin (best inference available historically). This
       matches the original logic the test_backtest_fixed_sizing suite
       was written against.

    The SELL price for each row comes from autofin's recomment when
    available (parsed via ``_parse_partial_close``):

      - "[HDB] 🛑 Chạm điểm cắt lỗ ... Giá: 25,400 - Lỗ: -8.66 %"
        → use ``Giá: 25,400``.
      - "Đóng DEAL ..." (no suggested price) → ``price=0``. The apply
        loop falls back to ``last_price`` in dry-run; in live mode
        DNSE fills at market.

    ``our_positions`` is the authoritative source of held qty (DNSE in
    live mode, dry-run state in dry-run mode). When omitted (older
    callers / backtests), falls back to ``_our_qty`` from each deal —
    which works for tests but can be wrong in production (see the
    2026-09-18 incident).

    `our_held_deal_ids` is a map: deal_id → original signal (so we know
    the symbol + qty + the deal we opened).
    """
    rows: list[dict[str, Any]] = []
    if autofin_closing_ids is not None:
        # Live bot: only close deals autofin explicitly said it closed.
        # Empty closing_ids means autofin published no close events
        # today → nothing to mirror.
        if not autofin_closing_ids:
            return rows
        # Emit close for deals IN closing_ids (deals autofin closed).
        deals_to_close = set(autofin_closing_ids)
        for did, sig in our_held_deal_ids.items():
            if did not in deals_to_close:
                continue
            rows.extend(_emit_full_close_row(did, sig, our_positions, board_lot))
    else:
        # Backtest / fallback: absence from holding + suggest means
        # autofin has closed this deal.
        still_open = {_deal_id(d) for d in autofin_holding if _deal_id(d)}
        if autofin_suggest_ids:
            still_open |= autofin_suggest_ids
        # Emit close for deals NOT in still_open (i.e. autofin exited them).
        for did, sig in our_held_deal_ids.items():
            if did in still_open:
                continue
            rows.extend(_emit_full_close_row(did, sig, our_positions, board_lot))
    return rows


def _emit_full_close_row(
    did: str,
    sig: dict[str, Any],
    our_positions: dict[str, int] | None,
    board_lot: int,
) -> list[dict[str, Any]]:
    """Build the full_close SELL row(s) for a single deal.

    Returns an empty list if the deal has no qty to sell. Today this
    always returns 0 or 1 row, but factored out so both live and
    backtest paths stay in sync.
    """
    symbol = sig.get("symbol", "?")
    # Use live qty from our_positions (DNSE / dry-run), falling back
    # to _our_qty only if absent (e.g. symbol not yet in our_positions
    # or older caller without positions).
    positions = our_positions or {}
    qty = int(positions.get(symbol, 0))
    if qty <= 0:
        qty = int(sig.get("_our_qty", 0))
    if qty <= 0:
        return []
    # Repair the tracking state for next cycle.
    sig["_our_qty"] = qty
    # Autofin's lastRecomment (cached in our state when we first
    # observed the deal) often carries the suggested close price
    # for SL triggers ("Giá: 25,400"). Parse it the same way the
    # partial-close parser does. Falls back to 0 (market) when
    # the recomment has no price — the apply loop fills at
    # last_price in that case.
    rec = str(sig.get("_last_recomment") or sig.get("lastRecomment") or "")
    action = _parse_partial_close(rec)
    price = action.price if action.price > 0 else 0
    return [{
        "symbol": symbol,
        "side": "NS",
        "price": price,
        "quantity": _round_to_lots(qty, board_lot),
        "kind": "full_close",
        "reason": f"autofin_closed:deal#{did}",
    }]


@dataclass(frozen=True)
class PartialCloseAction:
    """Parsed partial-close signal from autofin's lastRecomment text."""

    action: str  # "take_profit_lap" | "reduce_lap" | "capital_protect" | "full_close" | "unknown"
    lap: int = 0          # 1, 2, 3 — for lap variants
    price: float = 0.0    # VND; explicit price (e.g. 26900) for take_profit_lap
    percent: float = 0.0  # 0..1; for capital_protect (e.g. 0.2857 for 28.57%)


_PARTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Stop-loss close: autofin publishes this when an SL is hit and
    # recommends closing the deal. The format is something like::
    #   "[HDB] 🛑 Chạm điểm cắt lỗ, cân nhắc hạ 100 % tỷ trọng"
    #   "Giá: 25,400 - Nắm giữ: 8 ngày"
    #   "Lỗ: -8.66 %"
    # We match the first line and extract the suggested close price from
    # the Giá: line. The percent field is informational; the SL signal
    # is always 100% (full close) per autofin convention.
    (re.compile(
        r"Chạm\s*điểm\s*cắt\s*lỗ.*cân\s*nhắc\s*hạ\s*([\d,\.]+)\s*%\s*tỷ\s*trọng",
        re.IGNORECASE,
    ), "stop_loss_close"),
    (re.compile(r"Chốt lãi lần\s*(\d+)(?:\s*mức giá\s*([\d,\.]+))?", re.IGNORECASE), "take_profit_lap"),
    # Reduce event: autofin's format is "[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1"
    # — the % sits BETWEEN "Hạ" and "tỉ/tỷ trọng", with the lap number
    # at the end. Older builds' regex `Hạ tỷ trọng lần\s*(\d+)` missed
    # this format (no `%` between "Hạ" and "tỷ trọng") and the action
    # was silently classified as ``unknown``, so the bot never issued
    # the reduce. autofin's UI uses two spellings:
    #   * "tỷ trọng" (y-with-dot + trọng, often no space)
    #   * "tỉ trọng" (i-with-dot + space + trọng — `tỉ` is one char,
    #     U+1EC9, NOT in the y-with-dot U+1EF7 family)
    # We accept both. Group 1 = %, group 2 = lap.
    (re.compile(
        r"Hạ\s+([\d,\.]+)\s*%\s*t[ỷỉy]?\s*trọng\s+lần\s*(\d+)",
        re.IGNORECASE,
    ), "reduce_lap"),
    # 2026-10-20 incident (BAF): autofin publishes a NEGATIVE P&L
    # percent when the position is underwater (e.g. "🛡️ Chốt lãi
    # -1.75 % bảo toàn vốn tại 30,950" after a small drop from the
    # buy price). The X% here is still the P&L percent, not a qty
    # fraction — accept an optional leading ``-`` so the parser
    # recognises the action type. The action semantics ("sell all
    # at the suggested price") are identical regardless of sign.
    (re.compile(r"Chốt lãi\s*([-]?[\d,\.]+)\s*%\s*bảo toàn vốn", re.IGNORECASE), "capital_protect"),
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

    Autofin embeds the suggested price in the recomment text. We
    extract it for every action so the dry-run state can record a
    realistic fill price. Typical patterns::

      - Take-profit lap:  "Chốt lãi lần 1 mức giá 15,000"
      - Reduce lap:       "Hạ tỷ trọng lần 1" + "Giá: 25,400 - Nắm giữ: 8 ngày"
      - Capital protect:  "Chốt lãi 28.57% bảo toàn vốn tại 27,200"
      - Full close (SL):  "[HDB] 🛑 Chạm điểm cắt lỗ, cân nhắc hạ 100 % tỷ trọng"
                         + "Giá: 25,400 - Lỗ: -8.66 %"
      - Full close (final): "Đóng DEAL chạm điểm cắt lỗ"   (no price)
    """
    rec = (recomment or "").strip()
    for pat, action in _PARTIAL_PATTERNS:
        m = pat.search(rec)
        if not m:
            continue
        if action == "stop_loss_close":
            # "🛑 Chạm điểm cắt lỗ, cân nhắc hạ 100 % tỷ trọng" +
            # "Giá: 25,400 - Nắm giữ: 8 ngày" + "Lỗ: -8.66 %"
            pct = _parse_percent(m.group(1))
            price_m = re.search(r"Giá\s*:\s*([\d,\.]+)", rec)
            price = _parse_vnd(price_m.group(1)) if price_m else 0.0
            return PartialCloseAction(action=action, percent=pct, price=price)
        if action == "take_profit_lap":
            lap = int(m.group(1))
            price = _parse_vnd(m.group(2)) if m.group(2) else 0.0
            return PartialCloseAction(action=action, lap=lap, price=price)
        if action == "reduce_lap":
            # Reduce events often include a price on the next line as
            # "Giá: X - Nắm giữ: Y ngày". Try to extract it.
            # The regex now captures (percent, lap): group 1 is the
            # "X %" sell fraction, group 2 is the lap number.
            price_m = re.search(r"Giá\s*:\s*([\d,\.]+)", rec)
            price = _parse_vnd(price_m.group(1)) if price_m else 0.0
            pct = _parse_percent(m.group(1))
            return PartialCloseAction(
                action=action, lap=int(m.group(2)), price=price,
                percent=pct,
            )
        if action == "capital_protect":
            pct = _parse_percent(m.group(1))
            # capital_protect also includes a price: "tại P"
            price_m = re.search(r"tại\s*([\d,\.]+)", rec)
            price = _parse_vnd(price_m.group(1)) if price_m else 0.0
            return PartialCloseAction(action=action, percent=pct, price=price)
        if action == "full_close":
            # "Đóng DEAL" — final close with no suggested price.
            price_m = re.search(r"Giá\s*:\s*([\d,\.]+)", rec)
            price = _parse_vnd(price_m.group(1)) if price_m else 0.0
            return PartialCloseAction(action=action, price=price)
    return PartialCloseAction(action="unknown")


def _plan_partial_close_orders(
    our_held_deals: dict[str, dict[str, Any]],
    autofin_holding: list[dict[str, Any]],
    our_positions: dict[str, int] | None = None,
    board_lot: int = DEFAULT_BOARD_LOT,
    autofin_fractions: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Emit partial SELL orders for deals we hold that autofin still holds
    but whose ``numberOfProfit`` has increased since the previous cycle.

    Per user spec (2026-09-06):
      - Sizing: 1/3 of current qty per ``lần`` step (take_profit_lap /
        reduce_lap), X% of current qty for ``bảo toàn vốn X%`` events
      - Price: explicit price from recomment → else ``tP_F`` → else market
      - Bootstrap: on first cycle (no ``_last_numberOfProfit`` in state),
        record the current value but do NOT issue any partial close

    Sizing source priority for take_profit_lap / reduce_lap steps:
      1. ``autofin_fractions[did]`` — the per-step "Tỷ trọng X %" fraction
         pulled from autofin's ``/signal-deals`` history. This is
         the SOURCE OF TRUTH (per user feedback 2026-09-18: autofin
         tells us the fraction; don't override with a fixed env value).
      2. "Tỷ trọng X %" embedded in the recomment text (some history
         entries include the multi-line text).
      3. ``AUTOFIN_PARTIAL_CLOSE_FRACTION`` env var (operator
         override for cases where autofin's API doesn't expose the
         fraction).
      4. Default 1/3 (historical behavior).

    The actual position qty comes from ``our_positions`` (built from the
    dry-run state in dry-run mode or from DNSE in live mode) — NOT from
    ``sig.get("_our_qty", 0)``, which can drift out of sync with the
    actual fills after a cash-clipped BUY. See the 2026-09-18 incident
    where every tracked deal had ``_our_qty=0`` even though the dry-run
    state held 6 positions. The fix uses the source of truth.

    When ``our_positions`` is None/empty (older callers / backtests that
    don't pass it), fall back to ``_our_qty`` from the deal dict so the
    historical test suite keeps working.

    Mutates the deal dicts in-place to update ``_last_numberOfProfit``
    so the next cycle plans from the correct baseline.
    """
    autofin_by_id: dict[str, dict[str, Any]] = {}
    for d in autofin_holding:
        did = _deal_id(d)
        if did:
            autofin_by_id[did] = d

    positions = our_positions or {}

    rows: list[dict[str, Any]] = []
    for did, sig in our_held_deals.items():
        # Always refresh the cached recomment from the LATEST autofin
        # payload, even when the numberOfProfit hasn't incremented.
        # This ensures that when autofin emits an SL alert
        # ("🛑 Chạm điểm cắt lỗ, cân nhắc hạ 100 % tỷ trọng Giá: 25,400")
        # right before auto-closing the deal, _plan_close_orders picks
        # up the suggested price on the next cycle (after autofin
        # removed the deal from holding).
        if did in autofin_by_id:
            latest_rec = str(autofin_by_id[did].get("lastRecomment") or "")
            if latest_rec:
                sig["lastRecomment"] = latest_rec
        if did not in autofin_by_id:
            continue
        h = autofin_by_id[did]
        rec = str(h.get("lastRecomment") or "")
        action = _parse_partial_close(rec)
        # "stop_loss_close" → 100% sell at autofin's suggested price;
        # handled by ``_plan_close_orders`` because it doesn't depend
        # on ``numberOfProfit``. "full_close" (Đóng DEAL) → handled
        # there too.
        if action.action in ("unknown", "full_close", "stop_loss_close"):
            continue

        cur_nP = int(h.get("numberOfProfit") or 0)
        last_nP = int(sig.get("_last_numberOfProfit", -1))
        last_rec = str(sig.get("_last_recomment") or "")
        # 2026-10-20 BAF incident: when an orphan deal is freshly
        # injected (``_orphan_injected=True``) we have NEVER seen
        # this recomment before — bypass the bootstrap baseline-
        # building path AND the ``nP_changed / recomment_changed``
        # guard below. Without this the bot records
        # ``_last_numberOfProfit=cur_nP`` + ``_last_recomment`` on
        # the first cycle and then never re-fires because
        # subsequent cycles see the same ``cur_nP`` and the same
        # recomment text.
        #
        # IMPORTANT: ``is_orphan_fresh`` is also gated on the
        # recomment being ACTUALLY DIFFERENT from what the bot
        # last fired on. The orphan-injection fires on every cycle
        # (the deal is still missing from autofin /signal-static-
        # holding), so without the ``rec != last_rec`` check we'd
        # re-fire the same partial-close every 60s until autofin
        # returns the deal to its holding list. ``_last_recomment``
        # is set after each fire (line 1575-1576) so ``last_rec``
        # reflects the most recent FIRED recomment, not the most
        # recent seen recomment.
        is_orphan_fresh = bool(h.get("_orphan_injected")) and \
            action.action in (
                "capital_protect", "take_profit_lap", "reduce_lap",
            ) and \
            rec != last_rec
        if last_nP < 0 and not is_orphan_fresh:
            sig["_last_numberOfProfit"] = cur_nP
            sig["_last_recomment"] = rec
            continue
        # Fire when EITHER:
        #   (a) ``numberOfProfit`` incremented (TP1 → TP2 → TP3 steps), OR
        #   (b) the recomment text changed — catches the case where
        #       autofin re-announces the same step (e.g. after a
        #       state-desync bug wiped our _last_recomment), OR where
        #       autofin switches between TP / reduce / capital_protect
        #       actions for the same nP. Without (b), a bot that
        #       tracked an nP but never had valid qty (so couldn't
        #       act) would silently never re-fire on the same step.
        #   (c) — 2026-09-22 ASP incident: autofin publishes the
        #       ``lastRecomment`` BEFORE updating the /signal-deals
        #       history with the matching "Tỷ trọng X %" line. The
        #       first poll after the suggestion therefore sees
        #       recomment text but a STALE fraction (the previous
        #       step's %), undersells the deal. On a later cycle
        #       autofin catches up the history, but the recomment
        #       is now unchanged so (a) and (b) don't fire. Force
        #       a re-fire when the recomment matches a previously-
        #       seen step but the latest fraction differs from the
        #       one we last sold at.
        nP_changed = cur_nP > last_nP
        recomment_changed = bool(rec) and rec != last_rec
        # Orphan-fresh deals: force nP_changed=True so the
        # ``nP_changed or recomment_changed or fraction_changed``
        # gate below is satisfied. The fresh recomment is itself
        # a fire trigger even when nP and recomment match a prior
        # cycle (because for the orphan there WAS no prior cycle).
        # See the bootstrap bypass above for the full rationale.
        if is_orphan_fresh:
            nP_changed = True
        fraction_changed = False
        if (
            not (nP_changed or recomment_changed)
            and last_nP >= 0
            and action.action in ("take_profit_lap", "reduce_lap")
            and autofin_fractions is not None
        ):
            last_partial_fraction = sig.get("_last_partial_fraction")
            if last_partial_fraction is not None:
                cur_fraction = autofin_fractions.get(did)
                if (
                    cur_fraction is not None
                    and abs(cur_fraction - float(last_partial_fraction)) > 1e-6
                ):
                    fraction_changed = True
                    print(
                        f"[autofin_signal] {sig.get('symbol','?')}: "
                        f"recomment + nP match but fraction changed "
                        f"{last_partial_fraction:.2%} → "
                        f"{cur_fraction:.2%} (autofin published step "
                        f"before updating history) — re-firing partial "
                        f"close"
                    )
        if not (nP_changed or recomment_changed or fraction_changed):
            continue

        # Use the live position qty, NOT the stale _our_qty. This is
        # the root-cause fix for the 2026-09-18 incident where ASP
        # (600 shares held) was missed because _our_qty=0 after a
        # cash-clipped BUY never reconciled back to tracking. Fall
        # back to _our_qty only when no positions map was supplied.
        symbol = str(sig.get("symbol", "") or "")
        our_qty = int(positions.get(symbol, sig.get("_our_qty", 0)))
        if our_qty <= 0:
            continue
        # Repair the tracking state for next cycle so the new logic
        # has fresh data too.
        sig["_our_qty"] = our_qty

        if action.action in ("take_profit_lap", "reduce_lap"):
            # Fraction resolution (priority order):
            #   1. autofin_fractions[did] — the per-step "Tỷ trọng X %"
            #      pulled from autofin's /signal-deals history. SOURCE
            #      OF TRUTH per user feedback 2026-09-18 — autofin
            #      tells us the fraction; don't override with a fixed
            #      env value.
            #   2. "Tỷ trọng X %" embedded in the recomment text (some
            #      history entries include the multi-line text).
            #   3. AUTOFIN_PARTIAL_CLOSE_FRACTION env var (operator
            #      override for cases where autofin's API doesn't expose
            #      the fraction).
            #   4. Default 1/3 (historical behavior).
            autofin_frac = (autofin_fractions or {}).get(did)
            env_frac = _env_partial_close_fraction()
            if autofin_frac is not None:
                fraction = autofin_frac
            else:
                fraction = _extract_partial_fraction(
                    rec,
                    default=1 / 3,
                    env_fraction=env_frac,
                )
            delta = int(our_qty * fraction)
        elif action.action == "capital_protect":
            # IMPORTANT (2026-10-18 user feedback): the "X %" in
            # ``Chốt lãi X % bảo toàn vốn tại P`` is the P&L
            # percentage, NOT the qty fraction. autofin is saying:
            # "the position is currently up X% — sell at price P
            # to lock in that X% profit and protect capital."
            # The previous implementation sold X% of the qty,
            # which is the wrong read.
            #
            # Correct semantics: SELL ALL REMAINING QTY at the
            # suggested ``tại P`` price. The action is a full
            # close. (Compare with take_profit_lap / reduce_lap
            # where the X% IS a qty fraction — different action
            # types use different semantics.)
            #
            # Sanity check: implied_pnl_pct from buy_price should
            # roughly match action.percent. If they diverge by
            # more than 5% the price is stale (autofin's quote
            # moved since the recomment was published); log a
            # warning but still issue the order at the suggested
            # price (operator can override via
            # AUTOFIN_CAPITAL_PROTECT_REQUIRE_MATCH=1 if they
            # want a strict guard).
            buy_price = float(
                sig.get("buyPrice")
                or sig.get("entryPrice")
                or sig.get("avgPrice")
                or sig.get("costPrice")
                or 0
            )
            if buy_price > 0 and action.price > 0:
                implied_pnl_pct = (action.price - buy_price) / buy_price * 100.0
                if abs(implied_pnl_pct - action.percent) > 5.0:
                    print(
                        f"[autofin_signal] {symbol}: capital_protect "
                        f"sanity check diverges — autofin claims "
                        f"{action.percent:.2f}% P&L but suggested "
                        f"price {action.price:,.0f} implies "
                        f"{implied_pnl_pct:.2f}% from buy price "
                        f"{buy_price:,.0f}. Recomment text: {rec[:80]!r}"
                    )
            # Full close at the suggested price. fraction=1.0 keeps
            # the _last_partial_fraction dedup consistent (a re-fire
            # would only happen if a NEW recomment is published).
            delta = our_qty
            fraction = 1.0
        else:
            continue

        # Partial closes ROUND UP to the next board lot — when autofin
        # says "sell 10%" and we hold 600, the math is 60 shares, but
        # Vietnamese boards trade in lots of 100. Rounding down would
        # skip the order entirely (60 < 100) and we'd miss the partial
        # close. Rounding up emits the smallest valid lot (100).
        delta = _round_up_to_lots(delta, board_lot)
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

        # ``capital_protect`` is a full close (sell all remaining),
        # so its ``kind`` differs from a regular partial_close. The
        # downstream apply / order-ledger code path treats both the
        # same way (SELL at the suggested price), but the Telegram
        # summary and operator logs read better when the action
        # type matches the semantics.
        order_kind = "full_close" if action.action == "capital_protect" else "partial_close"
        rows.append({
            "symbol": sig.get("symbol", "?"),
            "side": "NS",
            "price": price,
            "quantity": delta,
            "kind": order_kind,
            "reason": (
                f"autofin_partial:deal#{did}:{action.action}:"
                f"lap{action.lap}:pct{int(round(action.percent * 100))}:{rec[:60]}"
            ),
        })
        sig["_last_numberOfProfit"] = cur_nP
        sig["_last_recomment"] = rec
        sig["_our_qty"] = max(0, our_qty - delta)
        # Track the fraction we sold at so the next cycle can detect
        # an autofin-side lag where the recomment text was published
        # before the /signal-deals history was updated. See the
        # 2026-09-22 ASP incident.
        sig["_last_partial_fraction"] = fraction

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
    our_positions: dict[str, int] | None = None,
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

    ``our_positions`` is the authoritative held-qty source. We use it
    to size the partial close (and to filter out symbols we don't
    actually hold), with ``_our_qty`` as a fallback. See the
    2026-09-18 incident: tracking _our_qty was 0 for every deal even
    though dry-run state had positions, so notifications never fired
    partial closes.

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
    our_match_id: str | None = None
    for did, sig in our_deals.items():
        if str(sig.get("symbol") or "").upper() == symbol:
            # Prefer our_positions (DNSE / dry-run state) over
            # _our_qty for the held check. Fall back to _our_qty
            # when our_positions isn't supplied (older callers).
            positions = our_positions or {}
            held_qty = int(positions.get(symbol, sig.get("_our_qty", 0)))
            if held_qty > 0:
                our_match = sig
                our_match["_our_qty"] = held_qty  # repair tracking
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

    if action.action == "take_profit_lap":
        # TP-step text never includes "Tỷ trọng X %" — autofin only
        # publishes the price on the TP step ("Chốt lãi lần N mức giá P").
        # Use the historical 1/3 default.
        delta = our_qty // 3
    elif action.action == "reduce_lap":
        # Reduce-event text always includes "Hạ X % tỉ/tỷ trọng" — use
        # that percent directly. Fall back to 1/3 when the regex missed
        # (shouldn't happen now that the regex covers both spellings).
        if action.percent > 0:
            delta = int(our_qty * action.percent / 100)
        else:
            delta = our_qty // 3
    elif action.action == "capital_protect":
        # IMPORTANT (2026-10-18 user feedback; re-confirmed 2026-10-20
        # against BAF notification): the "X %" in
        # ``Chốt lãi X % bảo toàn vốn tại P`` is the P&L PERCENTAGE
        # (may be NEGATIVE when the position is underwater), NOT the
        # qty fraction. autofin is telling us the current P&L and
        # suggesting an exit price; the bot must SELL ALL REMAINING
        # QTY at the suggested price to lock in that P&L. This
        # mirrors the fix in ``_plan_partial_close_orders`` and
        # ensures the notification-driven mid-day path agrees with
        # the EOD-driven path. Without this, -1.75 % × 200 shares
        # rounds to 0 lots → empty list → signal silently dropped.
        delta = our_qty
    else:
        return []

    delta = _round_to_lots(delta)
    if delta < DEFAULT_BOARD_LOT:
        return []

    price = action.price if action.price > 0 else 0
    if price <= 0:
        return []

    # ``capital_protect`` is a full close (sell all remaining) — keep
    # the ``kind`` consistent with the EOD-path decision in
    # ``_plan_partial_close_orders`` so the apply loop, dedup table,
    # and Telegram summary all treat it the same way.
    order_kind = "full_close" if action.action == "capital_protect" else "partial_close"

    return [{
        "symbol": symbol,
        "side": "NS",
        "price": price,
        "quantity": delta,
        "kind": order_kind,
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
            # 2026-10-07: include the autofin supply zone so the live-
            # submit path can fetch a DNSE quote and re-check the gate
            # against the LIVE best-ask price at submit time. The
            # planning step uses the local cached close as `cur`, which
            # can be stale by minutes (last sync was hours ago for mid-
            # day polls). A real DNSE quote lets us catch a price that
            # has since moved out of the zone since planning.
            "supply_F": float(h.get("supply_F") or 0) if h else 0,
            "supply_T": float(h.get("supply_T") or 0) if h else 0,
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
            # 2026-10-07: include the autofin supply zone so the live-
            # submit path can fetch a DNSE quote and re-check the gate
            # against the LIVE best-ask price at submit time. The
            # planning step uses the local cached close as `cur`, which
            # can be stale by minutes (last sync was hours ago for mid-
            # day polls). A real DNSE quote lets us catch a price that
            # has since moved out of the zone since planning.
            "supply_F": float(h.get("supply_F") or 0) if h else 0,
            "supply_T": float(h.get("supply_T") or 0) if h else 0,
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


# 2026-10-13: helper for the dry-run pollution fix. The dedup
# ``already_planned_symbols`` is built from held positions in the
# state file, but the planning step no longer pre-populates
# ``_our_qty`` (that caused dry-run pollution — see the comment
# at the planning site). To prevent re-planning an in-flight
# order whose ``_our_qty`` is still 0 in the state, we read the
# ledger and add symbols that have an order in
# {submitting, submitted, open, partial} status. The status
# ``cancelled``, ``rejected``, ``expired`` are NOT included —
# those are re-planable, see the cleanup at _load_state.
_INFLIGHT_LEDGER_STATUSES = frozenset({
    "submitting", "submitted", "open", "partial",
})


def _collect_in_flight_symbols(
    base_set: set[str] | None,
    data_dir: Path,
) -> set[str]:
    """Return ``base_set`` augmented with symbols that have an
    in-flight order in the ledger.

    See module docstring of the 2026-10-13 dry-run pollution
    fix for context. The function is best-effort: a load failure
    is treated as "no in-flight symbols" so the caller doesn't
    fail the cycle.
    """
    result = set(base_set) if base_set else set()
    try:
        from vietnam_algo.libs.order_ledger import load_ledger as _load_ledger
        _inflight_ledger = _load_ledger(data_dir)
    except Exception:
        return result
    for _item in _inflight_ledger.values():
        if _item.status in _INFLIGHT_LEDGER_STATUSES and _item.symbol:
            result.add(_item.symbol.upper())
    return result


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
    actioned_deals: set[str] | None = None,
    data_dir: "Path | None" = None,
    autofin_closing_ids: set[str] | None = None,
    autofin_fractions: dict[str, float] | None = None,
    new_entry_min_cash_pct: float = 0.05,
    new_entry_top_n: int = 1,
    available_cash_vnd_override: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Pure-functional decision function for the autofin mirror strategy.

    Given a snapshot of autofin state + our tracked state, return
    the orders to place and the new state to persist. No I/O, no
    time-of-day, no DNSE calls — so this function can be replayed
    against any historical snapshot, which is what the daily
    backtest does.

    ``autofin_fractions`` maps deal_id → partial-close fraction
    (e.g. ``0.10`` for "Tỷ trọng 10%") read from autofin's
    ``/signal-deals`` history. When a deal has an entry in this
    map, that fraction OVERRIDES the env-var / 1/3 default. When
    missing, the function falls back to
    ``AUTOFIN_PARTIAL_CLOSE_FRACTION`` env var, then to 1/3.

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
    autofin_closing_ids : optional ``{deal_id, ...}`` set pulled from
        ``/closing-signals``. When non-empty, only deals in this set
        get a full_close SELL — see ``_plan_close_orders`` docstring
        for why we don't close on absence from holding/suggest alone.
        ``None`` (the default) keeps the historical absence-based
        behavior so backtests don't change.
    """
    config = config or StrategyConfig()

    # 0. Reconcile _our_deals against the dry-run state (if a path is
    # provided). The autofin tracking state and the dry-run state can
    # drift if the state file gets wiped (e.g. during a migration or
    # manual reset). Without this check, _plan_close_orders() can emit
    # a full-close for a deal whose position was never recorded in the
    # dry-run state — leading to "Today's planned orders" showing a
    # stale SELL for a symbol we don't actually hold. Dry-run is
    # ground truth; tracking follows.
    if data_dir is not None:
        try:
            from vietnam_algo.libs.dry_run_state import init_dry_run_state as _init_dry
            _dry = _init_dry(data_dir, strategy_name="autofin_signal")
            for _did, _sig in list(our_deals.items()):
                _sym = str(_sig.get("symbol") or "").upper()
                if not _sym:
                    continue
                _dry_pos = _dry.positions.get(_sym)
                _dry_qty = int(_dry_pos.quantity) if _dry_pos is not None else 0
                _trk_qty = int(_sig.get("_our_qty", 0) or 0)
                # Bidirectional sync: dry-run state is ground truth.
                # Without the dry→tracking branch, a cash-clipped BUY
                # that left _our_qty=0 in tracking but 600 in dry-run
                # (2026-09-18 ASP incident) never heals — every subsequent
                # cycle would still skip this deal's close logic.
                if _dry_qty != _trk_qty:
                    _sig["_our_qty"] = _dry_qty
        except Exception as exc:
            print(f"[decide_orders] dry-run reconcile skipped: {exc}")

    # 1. Build lookup maps from holding snapshot
    autofin_holding_by_id: dict[str, dict[str, Any]] = {}
    for d in holding:
        did = _deal_id(d)
        if did:
            autofin_holding_by_id[did] = dict(d)

    # 1a. Determine the cash budget for new BUYs.
    #
    # Two paths:
    #
    # (a) DRY-RUN: use the dry-run state's cash as the source of
    #     truth so the bot doesn't emit BUY orders it can't actually
    #     afford in the simulated account. Without this, the user
    #     reported (2026-09-16) the bot was planning
    #     ``NB 300 GAS @ 90K`` (= 27M VND) when the portfolio only had
    #     ~30K cash.
    #
    # (b) LIVE-SUBMIT: use the LIVE cash from the DNSE portfolio
    #     snapshot. Without this, the dry-run state's initial NAV
    #     (default 100M VND) leaks into live sizing — the strategy
    #     would plan ABB at 1,700 shares on a 10M-VND account because
    #     it thinks the available_cash is 100M. The 2026-10-08 04:30
    #     ICT incident was partly this bug. The caller (generate_orders)
    #     passes ``available_cash_vnd_override`` explicitly when
    #     ``live_submit`` is True; here we fall back to the dry-run
    #     state only when the caller didn't pass a value.
    available_cash_vnd: float | None = available_cash_vnd_override
    if available_cash_vnd is None and data_dir is not None:
        try:
            from vietnam_algo.libs.dry_run_state import init_dry_run_state as _init_dry
            _dry = _init_dry(data_dir, strategy_name="autofin_signal")
            available_cash_vnd = float(_dry.cash)
        except Exception:
            pass

    # 1b. Deal-ids the bot has ever issued ANY order for (entry, partial
    # close, or full close). These are tracked so we don't re-enter a
    # deal we previously closed — autofin keeps re-suggesting open deals
    # even after we fully exit them, and ``_our_deals`` doesn't always
    # retain them once _our_qty hits 0. The caller (AutofinSignalAdapter)
    # provides this set from its persistent ``_actioned_deals``.
    if config:
        pass  # placeholder so future per-config plumbing lives here

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

    # 2b. Filter out signals for deals we've ALREADY actioned (entry,
    # partial-close, or full-close) at any point in the past. autofin
    # keeps re-suggesting deals we previously closed because it tracks
    # them as still-open in /signal-static-suggest; without this guard
    # the bot would re-enter the same deal on the next cycle.
    if actioned_deals:
        before = len(augmented_signals)
        augmented_signals = [
            s for s in augmented_signals
            if _deal_id(s.raw) not in actioned_deals
        ]
        if before != len(augmented_signals):
            print(
                f"[decide_orders] filtered {before - len(augmented_signals)} "
                f"signal(s) for already-actioned deals"
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
        available_cash_vnd=available_cash_vnd,
        new_entry_min_cash_pct=new_entry_min_cash_pct,
        top_n=new_entry_top_n,
    )
    partial_orders = _plan_partial_close_orders(
        our_deals,
        holding,
        our_positions=our_positions,
        board_lot=board_lot,
        autofin_fractions=autofin_fractions,
    )
    # Filter to deals we actually hold — use our_positions (DNSE /
    # dry-run state) as ground truth. _our_qty in our_deals can be
    # stale (e.g. after a cash-clipped BUY never reconciled back to
    # tracking, see 2026-09-18 incident); our_positions is always
    # authoritative because the caller built it from the live source.
    our_real_deals = {
        did: sig for did, sig in our_deals.items()
        if our_positions.get(str(sig.get("symbol", "") or ""), 0) > 0
    }
    suggest_ids = {_deal_id(s.raw) for s in signals if s.raw.get("_id")}
    close_orders = _plan_close_orders(
        our_real_deals,
        holding,
        our_positions=our_positions,
        autofin_closing_ids=autofin_closing_ids,
        autofin_suggest_ids=suggest_ids,
        board_lot=board_lot,
    )

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
    # for each new BUY we issue, add the deal but DO NOT pre-populate
    # ``_our_qty`` from the planned order. The 2026-10-13 dry-run
    # pollution fix: the planning step was setting ``_our_qty`` to
    # the planned order quantity even when the submit had not yet
    # happened (or never would, in dry-run mode). This made the
    # state file claim we held N shares that we never received.
    # If the bot crashed between the planning step and the next
    # cycle's cleanup, the polluted state persisted across
    # restarts, causing the cleanup to skip the deal (because
    # ``_our_qty > 0`` looked like a real holding) and the
    # strategy to refuse to re-plan the entry.
    #
    # The cleanup at ``_load_state`` is the source of truth for
    # ``_our_qty``: it reads the order ledger and reconciles the
    # held qty against the latest order's fillQuantity (or
    # resets to 0 if the order is terminal-without-fill). The
    # order monitor updates the ledger on every DNSE poll. So the
    # next call to ``generate_orders`` sees the correct ``_our_qty``
    # — we don't need to set it during planning.
    #
    # For the same-cycle dedup (``already_planned_symbols``), we
    # rely on the *cleanup-correct* ``_our_qty`` (held positions
    # from the ledger) plus the in-flight ledger orders
    # (constructed in the caller). See generate_orders().
    for s in selected:
        did = _deal_id(s.raw)
        if not did:
            continue
        if did not in new_our_deals:
            new_our_deals[did] = dict(s.raw)
        new_our_deals[did].setdefault("_our_qty", 0)
        # Intentionally do NOT set ``_our_qty`` from the planned
        # order quantity. See the comment above.
    # for each close, mark the deal closed (qty=0) but KEEP it in the
    # dict so we can detect re-entry attempts later. Previously we
    # popped it, but autofin's suggest endpoint keeps returning the
    # same deal as an open BUY even after we exit it — that path
    # caused a re-entry bug on 2026-09-10 (TRC re-entered after
    # full_close). Keeping the row with qty=0 is the safety net;
    # ``_actioned_deals`` (separate set, persisted) is the primary
    # guard.
    closed_dids: set[str] = set()
    for o in close_orders:
        closed_did = _extract_deal_id(o.get("reason", ""))
        if closed_did and closed_did in new_our_deals:
            new_our_deals[closed_did]["_our_qty"] = 0
            closed_dids.add(closed_did)

    # 5b. Mark every deal that produced an order as "actioned" so we
    # never re-enter it on a later cycle, even if our_deals is wiped
    # or the suggest endpoint re-surfaces the same open deal.
    all_orders = close_orders + partial_orders + new_orders
    actioned_dids: set[str] = set()
    for o in all_orders:
        did = _extract_deal_id(o.get("reason", ""))
        if did:
            actioned_dids.add(did)
    # Merge the in-cycle actioned set into the caller's persistent set.
    if actioned_deals is not None:
        actioned_deals |= actioned_dids

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
        # deal_id set for deals we've EVER issued any order for (entry,
        # partial-close, or full-close). Persisted across restarts so the
        # bot doesn't re-enter a deal it previously closed — autofin keeps
        # suggesting open deals even after we fully close them.
        self._actioned_deals: set[str] = set()
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
            # _actioned_deals is the union of: every deal_id we've ever
            # held with qty > 0 (from our_deals, in case _actioned_deals
            # wasn't persisted on an older build) plus the explicit set.
            actioned = set(payload.get("actioned_deals", []) or [])
            for did, sig in self._our_deals.items():
                if int(sig.get("_our_qty", 0) or 0) > 0:
                    actioned.add(str(did))
            # 2026-10-13: prune deals whose most recent order is in a
            # terminal state (expired / cancelled / rejected / closed
            # / dry_run / skipped) AND we no longer hold the position.
            # Without this, an order that never filled (e.g. the
            # 2026-10-12 ABB BUY that expired at EOD) would keep its
            # deal_id in actioned_deals forever, preventing the bot
            # from re-attempting the same signal next day. The user
            # audit on 2026-10-13 surfaced this: "previous day we
            # still make the deal of ABB work so we should consider
            # it as fresh on next day, no need to reset manually".
            #
            # The contract: a deal is "actioned" iff we EITHER still
            # hold the position OR have an outstanding (non-terminal)
            # order in the order ledger. When the order is terminal
            # without a fill, the deal is re-actionable next cycle.
            actioned = self._prune_terminal_unfilled_deals(
                actioned, self._our_deals, data_dir,
            )
            self._actioned_deals = actioned
        except Exception:
            self._our_deals = {}
            self._notified_order_keys = set()
            self._last_poll_at = None
            self._last_notification_id = ""
            self._last_notification_poll_at = None
            self._notified_event_ids = set()
            self._actioned_deals = set()

    @staticmethod
    def _prune_terminal_unfilled_deals(
        actioned: set[str],
        our_deals: dict[str, dict[str, Any]],
        data_dir: Path,
    ) -> set[str]:
        """Remove deals from ``actioned`` when the latest order is
        terminal-without-fill and we don't hold the position.

        Returns the (possibly smaller) actioned set. Best-effort: any
        failure (missing ledger, parse error) is treated as "no
        pruning" so we don't accidentally drop a deal that's still
        in flight. The caller should always call this even if the
        ledger doesn't exist yet (first-run) — it's a no-op in that
        case.

        Side effect: when ``our_deals[did]["_our_qty"]`` is > 0 but the
        latest order for that deal is terminal-without-fill, the qty
        is reset to 0 in place. This is the "dry-run pollution" guard
        — the planning step sets ``_our_qty`` from the planned order
        even when the order never actually fills (e.g. cancelled or
        rejected). Without this reset, the cleanup would keep the
        deal actioned and we'd never re-plan the entry.

        Terminal-with-fill detection: a status of ``filled`` means the
        latest order DID fill some quantity — we hold the position.
        The ``_our_qty`` in the state file may be 0 (stale) or > 0,
        but the truth is in the ledger's ``lastDetail.fillQuantity``.
        We always treat a deal with a filled latest order as held.
        """
        if not actioned:
            return actioned
        # 1. Build deal_id -> (latest-order-status, latest filled-qty)
        #    from the order ledger. The latest order is the one with
        #    the most recent updatedAt (or createdAt as fallback).
        #
        # 2026-10-13 fix: also track the latest FILLED BUY's fillQty
        # separately. The "latest order by timestamp" approach was
        # wrong for live-submit mode where the user might
        # accidentally submit a duplicate BUY that gets cancelled
        # (e.g. the 2026-10-13 06:58 ICT ABB BUY 500 incident — the
        # bot placed a duplicate because the openQuantity parser
        # missed the holding; the user manually cancelled the
        # duplicate via DNSE). The latest order for the ABB deal
        # is now the cancelled BUY 500, not the original filled
        # BUY 100 — but the position is real (we still hold 100
        # ABB from the original fill). The cleanup was reading the
        # latest order's status, seeing "cancelled", and resetting
        # ``_our_qty`` to 0 even though we really did fill 100
        # shares from an earlier order. The fix: when the latest
        # order is terminal-without-fill, look at the latest
        # FILLED order for the same deal — if THAT one had a fill,
        # the position is still real.
        latest_status_by_did: dict[str, str] = {}
        latest_filled_by_did: dict[str, int] = {}
        latest_ts_by_did: dict[str, str] = {}
        # 2026-10-13: latest filled BUY for each deal, by timestamp.
        # We track this separately from ``latest_filled_by_did``
        # (which is keyed to the latest ORDER, not the latest
        # FILLED order) so a cancelled re-attempt doesn't overwrite
        # the real fill evidence.
        latest_filled_buy_qty_by_did: dict[str, int] = {}
        latest_filled_buy_ts_by_did: dict[str, str] = {}
        try:
            ledger = load_ledger(data_dir)
        except Exception:
            ledger = {}
        for item in ledger.values():
            reason = item.reason or ""
            m = re.search(r"deal#(\d+:[A-Z]+)", reason)
            if not m:
                continue
            did = m.group(1)
            ts = item.updatedAt or item.createdAt or ""
            # Extract filled quantity from lastDetail. The DNSE
            # wire format uses ``fillQuantity`` (no "-ed" suffix);
            # ``filled_quantity`` falls back through the older
            # names too. Reading the wrong field silently returned
            # 0 for every fill — see the 2026-10-13 fix that
            # added the helper. Without that, ``_our_qty`` was
            # reconciled to 0 even for live held positions.
            this_filled = filled_quantity(item.lastDetail)
            prev_ts = latest_ts_by_did.get(did, "")
            if ts >= prev_ts:
                latest_ts_by_did[did] = ts
                latest_status_by_did[did] = item.status
                latest_filled_by_did[did] = this_filled
            # Track the latest filled BUY separately. A filled
            # SELL closes the position; a filled BUY opens it.
            # We only want the "is there an open position from a
            # previous BUY" signal here, not the latest order's
            # fill count (which might be from a SELL or from a
            # cancelled duplicate BUY).
            if item.status == "filled" and this_filled > 0:
                buy_ts = latest_filled_buy_ts_by_did.get(did, "")
                if ts >= buy_ts:
                    latest_filled_buy_ts_by_did[did] = ts
                    latest_filled_buy_qty_by_did[did] = this_filled
        # 2. Determine which deals we still hold. A deal is "held"
        #    if EITHER ``_our_qty > 0`` (state file says we own) OR
        #    the latest order is ``filled`` with fillQuantity > 0
        #    (the state file may be stale — the ledger is the truth).
        held_dids: set[str] = set()
        for did, sig in our_deals.items():
            qty = int(sig.get("_our_qty", 0) or 0)
            status = latest_status_by_did.get(did, "")
            filled_qty = latest_filled_by_did.get(did, 0)
            latest_filled_buy_qty = latest_filled_buy_qty_by_did.get(did, 0)
            if qty > 0:
                # State says we hold. Cross-check with the ledger.
                if status in {"cancelled", "rejected", "expired", "closed", "dry_run", "skipped"}:
                    # Latest order is terminal-without-fill. But
                    # that's not the whole story: a cancelled
                    # re-attempt (e.g. duplicate BUY that the
                    # operator manually cancelled via DNSE) doesn't
                    # undo a previous fill. If the latest FILLED
                    # BUY for this deal had a fill, the position
                    # is still real — keep _our_qty as-is (or
                    # reconcile upward if the fill was larger
                    # than the state-file claim).
                    if latest_filled_buy_qty > 0:
                        held_dids.add(did)
                        if latest_filled_buy_qty > qty:
                            sig["_our_qty"] = latest_filled_buy_qty
                    else:
                        # No filled BUY on the books — this is the
                        # genuine dry-run pollution case. Reset.
                        sig["_our_qty"] = 0
                else:
                    held_dids.add(did)
                    # Reconcile upward: if the ledger says we filled
                    # more shares than the state file claims, update
                    # the state file to match the truth. This is the
                    # common case after a re-price cycle that
                    # partially filled at a higher price.
                    if status == "filled" and filled_qty > qty:
                        sig["_our_qty"] = filled_qty
                    continue
            # State says we don't hold, but the ledger might say
            # otherwise (state is stale, the order filled but the
            # state file was reset by some other code path).
            if status == "filled" and filled_qty > 0:
                held_dids.add(did)
                # Reconcile the stale state file.
                sig["_our_qty"] = filled_qty
            elif latest_filled_buy_qty > 0:
                # 2026-10-13: the latest order is something other
                # than "filled" (e.g. cancelled duplicate), but a
                # previous BUY for the same deal did fill. Reconcile
                # the state file to that fill and mark the deal
                # as held. This is the case the duplicate-BUY
                # incident surfaced: state says _our_qty=0 because
                # the original code was looking at the latest order
                # (cancelled BUY 500) instead of the latest FILLED
                # order (filled BUY 100). We really do hold those
                # 100 shares — the cancellation only voided the
                # second attempt.
                held_dids.add(did)
                sig["_our_qty"] = latest_filled_buy_qty
        # 3. Prune: keep the deal only if we hold it OR the latest
        #    order is non-terminal (open / partial / submitting /
        #    unknown / prepared) or filled. Terminal-without-fill
        #    states (expired / cancelled / rejected / closed /
        #    dry_run / skipped) release the deal back to the
        #    planning pool.
        pruned: set[str] = set()
        for did in actioned:
            if did in held_dids:
                pruned.add(did)
                continue
            status = latest_status_by_did.get(did, "")
            if not status:
                # No order on the books for this deal — we may be
                # looking at a stale entry from a prior bot version.
                # Keep it conservative: assume still actioned so we
                # don't double-enter.
                pruned.add(did)
                continue
            if status in TERMINAL_LEDGER_STATUSES:
                # Terminal → release. (For "filled" with no qty, the
                # above reconciliation step would have marked the
                # deal as held; if filled_qty==0, that's a weird edge
                # case but releasing is the safer choice.)
                continue
            # Non-terminal: order is still in flight. Keep actioned
            # so we don't issue a duplicate while the original is
            # working through the exchange.
            pruned.add(did)
        return pruned

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
                "actioned_deals": sorted(self._actioned_deals),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ))

    def last_poll_at(self) -> str | None:
        return self._last_poll_at

    def _catchup_threshold_seconds(self) -> int:
        """Read ``AUTOFIN_CATCHUP_THRESHOLD_HOURS`` from env, default 24h."""
        raw = os.environ.get("AUTOFIN_CATCHUP_THRESHOLD_HOURS", "").strip()
        if raw:
            try:
                return int(float(raw) * 3600)
            except ValueError:
                pass
        return DEFAULT_CATCHUP_THRESHOLD_SECONDS

    def _compute_poll_gap_seconds(self) -> float | None:
        """How long since the bot last saved state, in seconds.

        Returns None if ``_last_poll_at`` isn't set (cold start). For
        warm starts the gap is ``(now_utc - last_poll_at_utc)`` in
        seconds. Used by ``_maybe_catchup_missed_signals`` to decide
        whether to trigger the catch-up scan.
        """
        if not self._last_poll_at:
            return None
        try:
            last = datetime.fromisoformat(
                str(self._last_poll_at).replace("Z", "+00:00"),
            )
        except ValueError:
            return None
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        return (now - last).total_seconds()

    def _maybe_catchup_missed_signals(
        self,
        client,
        holding: list[dict[str, Any]],
        closing: list[dict[str, Any]],
        live_submit: bool,
        config: "EngineConfig | None" = None,
    ) -> dict[str, Any] | None:
        """Replay autofin signals that fired during the bot's downtime.

        Triggered when ``now - last_poll_at > catchup_threshold``
        (default 24h). Iterates the union of ``holding`` and
        ``closing`` symbols, fetches each one's full ``hisRecomment``
        history, compares against our per-deal state, and emits
        catch-up orders for events we haven't applied yet.

        ``config`` (EngineConfig) is needed to read the dry-run state
        file (autofin's ``dry_run_state.autofin_signal.json``). The
        file lists positions the dry-run state still holds but the
        autofin adapter doesn't track in ``_positions`` /
        ``_our_deals`` (orphan positions from a previous bot
        configuration that survived a state-file migration).
        Without ``config`` the dry-run state fallback is skipped.

        Live mode (``live_submit=True``): no-op. We don't replay
        orders for past dates — the live DNSE positions already
        reflect whatever the operator did manually. The catch-up
        only fixes the dry-run state.

        Returns a summary dict ``{"deals": [...], "events": N,
        "orders": N, "skipped": N, "gap_hours": float, "live_skip":
        bool}`` or None if no gap / no missed events.
        """
        gap_sec = self._compute_poll_gap_seconds()
        if gap_sec is None:
            return None
        threshold_sec = self._catchup_threshold_seconds()
        if gap_sec < threshold_sec:
            return None

        gap_hours = gap_sec / 3600.0
        if live_submit:
            notify(
                "\n".join([
                    "⚠️ Vietnam Algo: autofin bot missed signals (LIVE mode)",
                    f"gap: {gap_hours:.1f} hours (threshold {threshold_sec/3600:.0f}h)",
                    "skipping catch-up: live mode never replays past events",
                    "verify dry-run state vs DNSE positions manually",
                ])
            )
            return {
                "deals": [],
                "events": 0,
                "orders": 0,
                "skipped": 0,
                "gap_hours": gap_hours,
                "live_skip": True,
            }

        # Collect the set of symbols to scan: currently held + recently
        # closed (one per symbol — closing-signals gives us the latest
        # close per symbol, the right scope for gap recovery) PLUS the
        # symbols we ourselves hold in dry-run state. The latter is
        # critical: autofin's ``/signal-static-holding`` does NOT return
        # positions whose deals have an active ``isClosed=False`` flag
        # in ``/signal-deals`` (likely positions the operator manually
        # closed in the autofin UI but autofin's deal record wasn't
        # updated). For those symbols the catchup scan would otherwise
        # skip them entirely — leaving reduce events unsurfaced. See
        # 2026-09-30 HNM incident where the dry-run state held 100
        # HNM shares but autofin's holding list returned nothing, so
        # the catchup scan never visited HNM and the REDUCE 50% lần 1
        # + lần 2 events stayed unapplied.
        symbols: set[str] = set()
        for h in holding:
            sym = h.get("symbol") or h.get("stockCode")
            if sym:
                symbols.add(sym)
        for c in closing:
            c_raw = c.raw if hasattr(c, "raw") else c
            sym = c_raw.get("symbol") if isinstance(c_raw, dict) else None
            if not sym and hasattr(c, "symbol"):
                sym = c.symbol
            if not sym:
                # closing rows are parseable — try the standard field.
                if hasattr(c, "raw"):
                    sym = (c.raw or {}).get("symbol")
            if sym:
                symbols.add(sym)
        # Also include every symbol we hold in the dry-run state.
        # ``self._positions`` is the live portfolio snapshot (DNSE in
        # live mode, local in dry-run); ``self._our_deals`` is the
        # per-deal state file. Together they cover every position we
        # might still have a stake in.
        if getattr(self, "_positions", None):
            for p in self._positions:
                if p:
                    symbols.add(p)
        for sig in self._our_deals.values():
            sym = (sig or {}).get("symbol")
            if sym:
                symbols.add(sym)
        # Finally, read the dry-run state file directly. The autofin
        # adapter never reads this file itself — ``_apply_dry_run_fills``
        # in runner.py mutates it externally — so ``_positions`` and
        # ``_our_deals`` (which the adapter DOES read) can both be
        # empty for a symbol that we still hold in dry-run state. This
        # happens when the bot was previously wired to a different
        # autofin-account and the dry-run state accumulated positions
        # that no longer match the current ``our_deals`` tracking
        # state. Without this fallback HNM (and other "orphan"
        # positions) would be invisible to the catchup scan — exactly
        # the 2026-09-30 incident.
        try:
            from vietnam_algo.libs.dry_run_state import (
                load_dry_run_state,
            )
            from pathlib import Path as _P
            _data_dir = None
            if config is not None and hasattr(config, "dataDir"):
                _data_dir = config.dataDir
            else:
                _data_dir = _P("vietnam_algo/data")
            _dry = load_dry_run_state(_P(_data_dir), strategy_name="autofin_signal")
            if _dry is not None and _dry.positions:
                for sym in _dry.positions:
                    if sym:
                        symbols.add(sym)
        except Exception as exc:
            print(
                f"[autofin_catchup] dry-run state load failed "
                f"(non-fatal): {exc}"
            )

        deals_scanned: list[dict[str, Any]] = []
        events_emitted = 0
        orders_emitted = 0
        skipped = 0
        # Per-symbol list of catchup orders, oldest-first. Collected
        # during the scan and applied once at the end so multiple
        # events on the same symbol (e.g. lần 1 then lần 2 reduce)
        # compose correctly: the second reduce operates on the
        # post-first-reduce position qty.
        catchup_orders_by_symbol: dict[str, list[dict[str, Any]]] = {}

        for sym in sorted(symbols):
            try:
                deals = client.fetch_deals(symbol=sym, page_size=10)
            except Exception as exc:
                print(f"[autofin_catchup] fetch_deals({sym}) failed: {exc}")
                continue
            for deal in deals:
                if not isinstance(deal, dict):
                    continue
                did = _deal_id(deal)
                sig = self._our_deals.get(did) or {}
                catchup_orders = _build_catchup_orders_for_deal(
                    deal, sig,
                )
                if not catchup_orders:
                    continue
                deals_scanned.append({
                    "deal_id": did,
                    "symbol": sym,
                    "missed_events": len(catchup_orders),
                })
                events_emitted += len(catchup_orders)
                orders_emitted += len(catchup_orders)
                catchup_orders_by_symbol.setdefault(sym, []).extend(
                    catchup_orders
                )
                print(
                    f"[autofin_catchup] deal={did} {sym}: "
                    f"{len(catchup_orders)} missed event(s) — "
                    f"{[o['kind'] for o in catchup_orders]}"
                )

        if not deals_scanned:
            return None

        # Apply the catchup orders to dry-run state. We do this
        # before the notification so the operator sees the post-fill
        # state in the next cycle's Telegram. Live mode is gated
        # above (returns a "live_skip" summary instead) — this block
        # only runs in dry-run.
        try:
            from vietnam_algo.libs.dry_run_state import (
                load_dry_run_state,
                save_dry_run_state,
            )
            from pathlib import Path as _P
            _data_dir = (
                config.dataDir
                if config is not None and hasattr(config, "dataDir")
                else _P("vietnam_algo/data")
            )
            _dry = load_dry_run_state(_P(_data_dir), strategy_name="autofin_signal")
            if _dry is None:
                # No state yet — bot is cold-starting. Skip apply;
                # the next cycle's ``_plan_orders_for_new_positions``
                # will pick up new entries, and any held positions
                # would have already been tracked via the next cycle's
                # ``fetch_holding`` reconcile.
                applied_count = 0
                skipped_count = 0
                closed_count = 0
                _new_closes: list[dict[str, Any]] = []
            else:
                # Snapshot the closed_trades length BEFORE apply so we
                # can attribute any new closes to this catchup run in
                # the Telegram alert (operator feedback 2026-10-05:
                # the summary-only alert didn't show which symbols got
                # closed in dry-run, making the close invisible until
                # the next cycle's SOD/cycle snapshot).
                _closed_before = len(_dry.closed_trades)
                applied_count, skipped_count, closed_count = (
                    _apply_catchup_orders_to_dry_run_state(
                        catchup_orders_by_symbol,
                        _dry,
                        self._our_deals,
                        board_lot=DEFAULT_BOARD_LOT,
                    )
                )
                save_dry_run_state(
                    _dry, _P(_data_dir), strategy_name="autofin_signal",
                )
                print(
                    f"[autofin_catchup] dry-run applied={applied_count} "
                    f"skipped={skipped_count} closed={closed_count}"
                )
                # Slice any new entries appended during this apply.
                _new_closes = list(_dry.closed_trades[_closed_before:])
        except Exception as exc:
            print(
                f"[autofin_catchup] dry-run apply failed (non-fatal): {exc}"
            )
            applied_count = 0
            skipped_count = 0
            closed_count = 0
            _new_closes = []

        # Build the alert body. When the apply closed any positions
        # this run, append a per-deal breakdown so the operator sees
        # *which* symbols were closed (not just the aggregate count).
        # Per user feedback 2026-10-05: STB's weekend catchup close
        # was silent because the previous notify() body only carried
        # the rolled-up "applied=N closed=M" line.
        _notify_lines: list[str] = [
            "⚠️ Vietnam Algo: autofin bot missed signals",
            f"gap: {gap_hours:.1f} hours (threshold {threshold_sec/3600:.0f}h)",
            f"scanned {len(symbols)} symbol(s), "
            f"{len(deals_scanned)} deal(s) had missed events",
            f"missed events: {events_emitted}",
            f"applied to dry-run: {applied_count} SELL "
            f"({closed_count} deals closed)",
        ]
        if _new_closes:
            _notify_lines.append("")
            _notify_lines.append(
                f"📊 *DRY-RUN CLOSES THIS RUN* ({len(_new_closes)} deal"
                f"{'s' if len(_new_closes) != 1 else ''}):"
            )
            # Cap the per-deal list at 10 to keep the alert readable;
            # the cycle/SOD snapshot still has the full portfolio.
            for _close in _new_closes[:10]:
                _sym = _close.get("symbol", "?")
                _qty = _close.get("qty", 0)
                _avg = _close.get("avg_cost", 0)
                _px = _close.get("sell_price", 0)
                _pnl = _close.get("realized_pnl", 0)
                _note = _close.get("note") or ""
                # Express P&L as % of cost basis for quick scanning.
                _cost_total = (_avg or 0) * (_qty or 0)
                if _cost_total > 0:
                    _pct = (_pnl / _cost_total) * 100.0
                    _pct_str = f"{_pct:+.2f}%"
                else:
                    _pct_str = "n/a"
                _pnl_str = (
                    f"+{_pnl:,.0f}" if _pnl > 0
                    else f"{_pnl:,.0f}"
                )
                _line = (
                    f"  • *{_sym}*: {_qty:,} shares, "
                    f"cost {_avg:,.0f} → sold @ {_px:,.0f} "
                    f"({_pct_str}, {_pnl_str} VND)"
                )
                if _note:
                    _line += f"\n    note: {_note}"
                _notify_lines.append(_line)
            if len(_new_closes) > 10:
                _notify_lines.append(
                    f"  …and {len(_new_closes) - 10} more (see dry-run "
                    f"state for full list)"
                )

        notify("\n".join(_notify_lines))
        return {
            "deals": deals_scanned,
            "events": events_emitted,
            "orders": orders_emitted,
            "applied": applied_count,
            "closed": closed_count,
            "skipped": skipped_count,
            "gap_hours": gap_hours,
            "live_skip": False,
        }


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
            partials = _event_to_partial_orders(
                ev.title, ev.body, self._our_deals,
                our_positions=getattr(self, "_positions", None),
            )
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

    def _augment_orphan_deals(
        self,
        client: "AutofinClient",
        holding: list[dict[str, Any]],
    ) -> None:
        """Augment ``holding`` with synthetic entries for deals in
        ``self._our_deals`` whose ``_id`` is missing from autofin's
        /signal-static-holding response.

        Background (2026-10-20 BAF incident): autofin's
        ``/signal-static-holding`` endpoint drops active deals when
        an operator manually closes them in the autofin UI, BUT
        autofin's automation continues to publish recomments for
        those deals via ``/signal-deals``. Without this augmentation,
        ``_plan_partial_close_orders`` does
        ``if did not in autofin_by_id: continue`` for the orphan
        deals, the partial-close path emits zero orders, and the
        bot reports ``0 orders planned`` to Telegram.

        Mechanism: for every ``did`` in ``self._our_deals`` that is
        NOT in the current ``holding`` snapshot, fetch
        ``/signal-deals?symbol=<sym>``, pick the row matching the
        canonical ``_id``, and append a synthetic holding entry
        carrying the fresh ``lastRecomment`` + ``numberOfProfit``.
        The synthetic entry is tagged ``_orphan_injected: True`` so
        downstream code can distinguish it from real
        /signal-static-holding rows.

        Args:
            client: a live ``AutofinClient`` (already authenticated).
            holding: the list of holding dicts fetched from
                /signal-static-holding; this function MUTATES it
                in place by appending synthetic entries. Order
                matters only for dedup / display, not for
                correctness.
        """
        holding_dids = {_deal_id(h) for h in holding if _deal_id(h)}
        orphan_dids = [
            did for did in self._our_deals if did not in holding_dids
        ]
        if not orphan_dids:
            return
        seen_orphan_symbols: set[str] = set()
        for did in orphan_dids:
            sig = self._our_deals.get(did) or {}
            sym = str(sig.get("symbol", "") or "")
            if not sym or sym in seen_orphan_symbols:
                continue
            # The downstream ``_plan_partial_close_orders`` already
            # gates on ``our_positions.get(symbol, 0) > 0`` (line
            # 1429), so stale ``_our_qty`` here is fine — the
            # orphan-injected recomment just gives the partial-
            # close path something to evaluate. If we genuinely
            # don't hold this symbol on DNSE any more, the
            # downstream guard skips it cleanly.
            try:
                deals = client.fetch_deals(symbol=sym, page_size=10)
            except Exception as exc:
                print(
                    f"[autofin_signal] orphan fetch_deals({sym}) "
                    f"failed: {exc}"
                )
                continue
            if not deals:
                continue
            # Match by canonical ``_id`` — the /signal-deals
            # endpoint may return several deals for the same
            # symbol (different windows). ``_id`` is the deal's
            # open-time Unix-ts, so it's stable across pages.
            try:
                want_id = int(str(did).split(":", 1)[0])
            except ValueError:
                want_id = 0
            match = next(
                (
                    d for d in deals
                    if int(d.get("_id", 0) or 0) == want_id
                ),
                deals[0],  # fallback: most recent for this sym
            )
            last_rec = str(
                match.get("lastRecomment", "") or ""
            ).strip()
            if not last_rec:
                continue
            seen_orphan_symbols.add(sym)
            holding.append({
                "_id": want_id,
                "symbol": sym,
                "numberOfProfit": int(
                    match.get("numberOfProfit", 0) or 0
                ),
                "lastRecomment": last_rec,
                "_orphan_injected": True,
            })
            # Repair the cached recomment on our tracked deal
            # too so subsequent cycles see the latest text even
            # if the orphan path doesn't fire again.
            sig["lastRecomment"] = last_rec
            print(
                f"[autofin_signal] orphan deal {did} ({sym}) "
                f"augmented with fresh recomment from "
                f"/signal-deals: {last_rec[:80]!r}"
            )

    def generate_orders(
        self,
        config: EngineConfig,
        strategy_config: StrategyConfig,
        trade_date: str | None = None,
        *,
        skip_backtest: bool = False,
        live_submit: bool = False,
    ) -> dict[str, Any]:
        # ``skip_backtest`` is a no-op for autofin — the adapter runs
        # its own decision pipeline (autofin API → signal classifier
        # → dry-run apply) rather than a backtest, so there is nothing
        # to cache. Accepted for signature compatibility with the
        # base ``StrategyAdapter.generate_orders`` abstract method.
        del skip_backtest
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
            # _request_with_token_refresh already retries transient
            # network errors (SSL handshake timeout, connection
            # refused, etc.) up to AUTOFIN_NETWORK_RETRIES times with
            # exponential backoff before propagating. If we still
            # see this alert, it means every retry failed — likely
            # autofin's API is genuinely down, not just slow. See
            # libs/autofin_signals.py:_request_with_token_refresh.
            notify(f"⚠️ autofin fetch_closing failed: {exc}")
            closing = []
        # 3b. CATCH-UP: if the bot has been down for more than
        # ``_catchup_threshold_seconds`` (default 24h) since its last
        # poll, scan every held+closed deal's ``hisRecomment``
        # history and emit a Telegram alert for any events we
        # missed. The actual replay happens via the regular
        # ``numberOfProfit`` delta logic on the next poll, but
        # this gives the operator visibility into what would have
        # fired while the bot was down. See 2026-09-29 incident
        # where the autofin bot was dead 4 days; this surfaces
        # the gap rather than silently resuming.
        try:
            catchup_summary = self._maybe_catchup_missed_signals(
                client, holding, closing, live_submit, config,
            )
        except Exception as exc:
            print(f"[autofin_signal] catchup scan failed: {exc}")
            catchup_summary = None
        # Build the closing_ids set: deal_id = _id + ":" + symbol.
        # These are the AUTHORITATIVE close trigger — see
        # _plan_close_orders for why we no longer close on absence
        # from /signal-static-holding alone (2026-09-14 HDB
        # false-positive).
        closing_ids: set[str] = set()
        for c in closing:
            cid = _deal_id(c.raw if hasattr(c, "raw") else c)
            if cid:
                closing_ids.add(cid)
        # 4. /signal-static-risk — risk positions (for alerts)
        try:
            risk = client.fetch_risk(page_size=50)
        except Exception as exc:
            notify(f"⚠️ autofin fetch_risk failed: {exc}")
            risk = []
        # 4b. Fetch each holding's full recomment history so partial
        # closes can honour autofin's per-step "Tỷ trọng X %" fraction
        # (the live /signal-static-holding endpoint only returns a
        # short lastRecomment like "💰 Chốt lãi lần 1 mức giá 15,800";
        # the "Tỷ trọng" line lives in hisRecomment from /signal-deals).
        # Without this, partial closes fall back to the 1/3 default
        # which is wrong for ASP today (autofin says 10%).
        autofin_fractions: dict[str, float] = {}
        for h in holding:
            sym = h.get("symbol") or h.get("stockCode")
            if not sym:
                continue
            try:
                deals = client.fetch_deals(symbol=sym, page_size=5)
            except Exception as exc:
                print(f"[autofin_signal] fetch_deals({sym}) failed: {exc}")
                continue
            if not deals:
                continue
            result = latest_partial_close_fraction(deals[0])
            if result is None:
                continue
            frac, _raw_text = result
            did = _deal_id(h)
            if did:
                autofin_fractions[did] = frac
        # 4c. ORPHAN DEAL AUGMENTATION (2026-10-20 BAF incident):
        # /signal-static-holding sometimes drops deals that we still
        # hold on DNSE — typically when an operator manually closes
        # the deal in the autofin UI (autofin's automation re-publishes
        # recomments via /signal-deals but the deal is no longer in
        # the "active holding" list). Without this block the partial-
        # close path silently skips any ``our_deals`` entry whose
        # ``_id`` is missing from ``holding``, because
        # ``_plan_partial_close_orders`` does
        # ``if did not in autofin_by_id: continue``. The BAF example
        # on 2026-10-20: we held 200 BAF, autofin published
        # "🛡️ Chốt lãi -1.75 % bảo toàn vốn tại 30,950" via the
        # notification stream AND /signal-deals?symbol=BAF, but
        # BAF was not in /signal-static-holding → bot reported
        # "0 orders planned" and never placed the SELL. See
        # ``_augment_orphan_deals`` for the full mechanism.
        self._augment_orphan_deals(client, holding)
        # 5. fetch our current DNSE positions for sizing
        try:
            from vietnam_algo.libs.dnse.client import DnseClient
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
        #
        # 2026-10-09 fix: only apply in DRY-RUN mode. When the strategy
        # is running with ``--live-submit`` the MOCK_NAV override is
        # IGNORED so positions are sized against the real NAV from
        # DNSE. The 04:30 ICT incident on 2026-10-08 happened because
        # the production env file had ``MOCK_NAV=100000000`` set, so
        # the strategy sized ABB to 1,700 shares (NAV=100M × 30 % ÷
        # 17,400 VND) on a real-money account that only has 10M VND
        # of withdrawable cash. With the live account, the same
        # signal should size to ~170 shares (NAV=10M × 30 % ÷ 17,400),
        # which fits within the 10M withdrawable.
        mock_nav = os.environ.get("MOCK_NAV", "").strip()
        # Track the source of `nav` so the formatter can label it
        # correctly. In live-submit mode the value is always from
        # DNSE (we never apply MOCK_NAV when live_submit=True). In
        # dry-run mode it can be either MOCK_NAV (override) or
        # DNSE (default when MOCK_NAV is not set).
        nav_source = "dnse"
        if mock_nav and not live_submit:
            try:
                nav = float(mock_nav)
                positions = dict(positions)
                nav_source = "mock"
                print(
                    f"[autofin_signal] MOCK_NAV override active: nav={nav:,.0f} VND "
                    f"(dry-run mode; ignored in live-submit)"
                )
            except ValueError:
                pass
        elif mock_nav and live_submit:
            print(
                f"[autofin_signal] ⚠️ MOCK_NAV={mock_nav} IGNORED — "
                f"live-submit mode uses real NAV from DNSE: {nav:,.0f} VND"
            )
        elif live_submit:
            print(
                f"[autofin_signal] live-submit mode: nav={nav:,.0f} VND, "
                f"positions={list(positions.keys())} (both from DNSE)"
            )

        # Cache the authoritative position map on the adapter so the
        # notification poll (called later in this same cycle) can size
        # partial closes from it. Without this, the poll would fall back
        # to ``_our_qty`` from our_deals — which can be 0 when the
        # autofin tracking state drifted out of sync with the dry-run
        # state (2026-09-18 incident, see _plan_partial_close_orders).
        self._positions = positions

        # 5c. User-defined stop-loss (overrides autofin's loose SL with
        # a tighter one). Read from env so it can be tuned per-environment.
        # 0 = disabled (use autofin's sL_F only). Default 10% matches
        # typical Vietnamese stock intraday volatility (~7%); 5% was
        # too tight and got stopped out by noise on entry-day gaps.
        try:
            user_sl_pct = float(os.environ.get("AUTOFIN_USER_SL_PCT", "0.10") or "0.10")
        except ValueError:
            user_sl_pct = 0.10

        # 5c'. New-entry timing / cash gate (user feedback 2026-09-21):
        # - ``allow_new_entries`` is False outside the start-of-day
        #   window (default). When False, _plan_orders_for_new_positions
        #   returns immediately — partial-close / full-close paths
        #   still run for held deals. This gives the operator one
        #   "new entry attempt per day" surface instead of a chain of
        #   entries every poll cycle.
        # - ``new_entry_min_cash_pct`` (default 0.05 = 5% of NAV)
        #   gates whether the cash available is enough to bother
        #   entering. Tunable via env var.
        try:
            new_entry_min_cash_pct = float(
                os.environ.get("AUTOFIN_NEW_ENTRY_MIN_CASH_PCT", "0.05") or "0.05"
            )
        except ValueError:
            new_entry_min_cash_pct = 0.05
        try:
            new_entry_top_n = int(
                os.environ.get("AUTOFIN_NEW_ENTRY_TOP_N", "1") or "1"
            )
        except ValueError:
            new_entry_top_n = 1
        # allow_new_entries: True only at the start-of-day window
        # (between orderSubmitTime and a configurable cutoff, default
        # 09:45 ICT — gives the bot ~30 min after market open to make
        # its single daily entry). The cutoff is in minutes after the
        # main trading session opens. After the cutoff the bot still
        # runs every cycle but only the partial-close / full-close
        # paths can produce orders.
        try:
            start_of_day_cutoff_min = int(
                os.environ.get("AUTOFIN_NEW_ENTRY_CUTOFF_MIN", "30") or "30"
            )
        except ValueError:
            start_of_day_cutoff_min = 30
        # Local ICT time check — the bot is in ICT and the trading
        # session opens at 09:15. We allow entries from 09:15 to
        # 09:15 + cutoff. Outside this window we suppress entries.
        from vietnam_algo.libs.schedule import (
            TradingSchedule,
            is_main_trading_phase,
            vietnam_now,
        )
        _sched = TradingSchedule()
        _now_ict = vietnam_now()
        # Minutes since market open (09:15 ICT)
        _open_h, _open_m = _sched.morningStart.hour, _sched.morningStart.minute
        _minutes_since_open = (
            (_now_ict.hour - _open_h) * 60
            + (_now_ict.minute - _open_m)
            + (_now_ict.second / 60.0)
        )
        in_morning = (
            is_main_trading_phase(_now_ict, _sched)
            and _minutes_since_open >= 0
            and _minutes_since_open <= start_of_day_cutoff_min
        )
        # 2026-10-09: ``force-run-trading`` CLI sets this env var to
        # bypass the start-of-day cutoff. The normal bot main loop
        # never sets it; the only way to bypass is to invoke the
        # force-run-trading subcommand. The cutoff is the user's
        # policy ("only one entry attempt per day, in the morning
        # window"); force-run is an explicit override for
        # ad-hoc re-runs.
        force_new_entry = os.environ.get("AUTOFIN_FORCE_NEW_ENTRY", "").strip() == "1"
        allow_new_entries = in_morning or force_new_entry

        # 5d. Build a symbol → last_close price map from local price data.
        # Used to (a) gate entry on price-within-buy-zone and (b) include
        # HELD-but-not-entered deals in the candidate list. Per user
        # feedback 2026-09-08, autofin deals have a price range, so the
        # bot should be able to enter at any time the current price is
        # within that range — not only on the day time_Open was set.
        try:
            from vietnam_algo.libs.universe import load_price_bars
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

        # 5d'. 2026-10-09 fix: in LIVE-SUBMIT mode, override
        # ``current_prices`` with the LIVE best-ask from DNSE
        # (``dnse.get_latest_quote``). The local price file
        # ``data/prices/{SYMBOL}.json`` is yesterday's close, which
        # can be hours stale by mid-day — a 1-2 % move between
        # yesterday's close and today's live ask would silently
        # shift the planned entry price and break the zone-tolerance
        # gate. The 04:30 ABB attempt used yesterday's close (17,500
        # VND) to plan a BUY at 17,400 VND, but the live ask during
        # the lunch break may have been materially different. We
        # fetch the live quote here, on every cycle in live mode,
        # and override the local price. Dry-run / backtest mode
        # keeps the local price (no DNSE call) so the daily replay
        # backtest still works without network.
        if live_submit and "dnse" in locals() and dnse is not None:
            for sym in seen_symbols:
                try:
                    quote = dnse.get_latest_quote(sym)
                except Exception as exc:
                    # Graceful degradation: network blip → fall back
                    # to the local price we already loaded.
                    print(
                        f"[autofin_signal] live quote for {sym} failed "
                        f"({type(exc).__name__}: {str(exc)[:100]}); "
                        f"using local price"
                    )
                    continue
                # Get the G1 (HOSE) best offer, fall back to first offer
                # in any board. Convert from kVND to VND (multiply by
                # 1000). The pre-existing ``_extract_best_ask_in_vnd``
                # helper in bots/runner.py does this — inline a
                # minimal copy here to avoid a runner→algos import
                # cycle.
                best_ask_vnd = None
                for q in quote.get("quotes") or []:
                    if (q.get("boardId") or "").upper() in ("G1", "G4", "G3"):
                        for off in q.get("offer") or []:
                            p = off.get("price")
                            try:
                                p_vnd = float(p) * 1000
                            except (TypeError, ValueError):
                                continue
                            if p_vnd > 0 and (best_ask_vnd is None or p_vnd < best_ask_vnd):
                                best_ask_vnd = p_vnd
                        break  # use first matching board
                if best_ask_vnd and best_ask_vnd > 0:
                    local_price = current_prices.get(sym, 0)
                    current_prices[sym] = best_ask_vnd
                    print(
                        f"[autofin_signal] {sym} live best-ask = "
                        f"{best_ask_vnd:,.0f} VND (overrides local price "
                        f"{local_price:,.0f})"
                    )

        # 5e. Compute the set of symbols we already have planned entries
        # for (used to skip duplicate BUY emissions on consecutive EOD
        # Cycles, especially in dry-run mode where DNSE returns empty
        # positions even though we've "planned" entries from prior cycles.
        # Per user feedback (2026-09-09): previously the bot double-bought
        # the same deal on consecutive days because it only consulted DNSE
        # for "do I already have this position" — and DNSE is empty in
        # dry-run mode.
        #
        # 2026-10-13 dry-run-pollution fix: previously this set was
        # built from ``_our_qty > 0`` ONLY, which was set by the
        # planning step from the *planned* order quantity (even
        # before the order was submitted). In dry-run mode there's
        # no submit, so the state file would claim we held N shares
        # that never existed. The dedup still worked (because the
        # pollution was consistent), but a bot crash + restart
        # between the planning step and the cleanup would persist
        # the pollution across restarts and break the cleanup.
        #
        # Now we dedup from three sources:
        #  1. Held positions in the state file (``_our_qty > 0``,
        #     which the cleanup at _load_state just reconciled
        #     against the ledger — so this IS the held count).
        #  2. In-flight orders in the ledger (submitting, submitted,
        #     open, partial) — these are orders we've already sent
        #     to DNSE but haven't filled yet. We don't want to
        #     re-plan a duplicate while the original is working
        #     through the exchange.
        #  3. Symbols marked "closed" via a recent SELL (closed_dids
        #     is populated at the end of the previous cycle, but
        #     those deals are already handled in actioned_deals).
        already_planned_symbols: set[str] = {
            str(sig.get("symbol") or "").strip().upper()
            for sig in self._our_deals.values()
            if int(sig.get("_our_qty", 0) or 0) > 0
        }
        # Source 2: in-flight ledger orders. Best-effort: a load
        # failure here is non-fatal (we'd just re-plan, which the
        # execute_orders_with_ledger dedup would catch via the
        # ledger key).
        try:
            already_planned_symbols = _collect_in_flight_symbols(
                already_planned_symbols, config.dataDir,
            )
        except Exception:
            # Best-effort: a load failure here is non-fatal (we'd
            # just re-plan, which the execute_orders_with_ledger
            # dedup would catch via the ledger key).
            pass

        # 6-9. Delegate to the pure decision function. This is the same
        # function the daily-replay backtest calls.
        #
        # 2026-10-09 fix: pass the LIVE available_cash in live-submit
        # mode so the strategy sizes positions against the real cash
        # (e.g. 10M VND for our test account) rather than the dry-run
        # state's 100M VND. Without this override, the dry-run state
        # was the cash source-of-truth regardless of live_submit, and
        # the strategy emitted ABB at 1,700 shares when the account
        # only has 10M VND of withdrawable cash. See the companion
        # MOCK_NAV guard in the same function.
        live_cash_vnd = None
        if live_submit and isinstance(snap, dict):
            # The portfolio snapshot's cash fields live under
            # ``snap["balance"]`` (set by build_portfolio_snapshot
            # from DNSE's /balances endpoint), not ``snap["cash"]``.
            # Use withdrawableCash as the primary signal — that's
            # what the operator can actually deploy into a new BUY
            # without margin — and fall back to availableCash.
            balance_block = snap.get("balance") or {}
            try:
                live_cash_vnd = float(
                    balance_block.get("withdrawableCash")
                    or balance_block.get("availableCash")
                    or 0
                )
            except (TypeError, ValueError):
                live_cash_vnd = None
            if live_cash_vnd and live_cash_vnd > 0:
                print(
                    f"[autofin_signal] live_cash_vnd={live_cash_vnd:,.0f} VND "
                    f"(from DNSE; overrides dry-run state's "
                    f"100,000,000 VND default)"
                )

        all_orders, new_our_deals, deals_actioned = decide_orders(
            holding=holding,
            signals=signals,
            our_deals=self._our_deals,
            our_positions=positions,
            nav=nav,
            config=strategy_config,
            user_sl_pct=user_sl_pct,
            data_dir=config.dataDir,
            current_prices=current_prices,
            already_planned_symbols=already_planned_symbols,
            actioned_deals=self._actioned_deals,
            autofin_closing_ids=closing_ids,
            autofin_fractions=autofin_fractions,
            # New-entry gating (user spec 2026-09-21):
            # - new_entry_top_n=0 disables new entries entirely
            #   (used outside the start-of-day window)
            # - new_entry_min_cash_pct gates on the available-cash
            #   threshold (default 5% of NAV)
            new_entry_min_cash_pct=new_entry_min_cash_pct,
            new_entry_top_n=new_entry_top_n if allow_new_entries else 0,
            available_cash_vnd_override=live_cash_vnd,
        )
        print(
            f"[autofin_signal] new_entry_top_n={new_entry_top_n if allow_new_entries else 0} "
            f"(allow_new_entries={allow_new_entries}, "
            f"in_morning={in_morning}, force_new_entry={force_new_entry})"
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
            "nav_source": nav_source,
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
        from vietnam_algo.libs.notifier import portfolio_section_lines
        sig = result.get("signals", {}) or {}
        deals = result.get("deals_actioned", []) or []
        nav = float(sig.get("nav") or result.get("metrics", {}).get("finalEquity") or 0)
        account = sig.get("account") or "?"
        # nav_source is set in generate_orders() — tracks whether
        # `nav` is from MOCK_NAV env (dry-run override) or from
        # DNSE (live-submit or default dry-run). The previous
        # "MOCK NAV (autofin target)" label was misleading in
        # live-submit mode where the value is the live DNSE NAV,
        # not a mock.
        nav_source = result.get("nav_source") or "unknown"

        lines: list[str] = []
        lines.append("🤖 Vietnam Algo bot — [autofin_signal]")
        lines.append(f"📅 Cycle: {result.get('tradeDate', '?')}")
        lines.append(f"💼 Account: {account}")
        if nav_source == "dnse":
            # Live-submit (or dry-run without MOCK_NAV override) —
            # the value is the real DNSE portfolio value.
            lines.append(f"💰 Live NAV (DNSE): {nav:,.0f} VND")
        elif nav_source == "mock":
            # Dry-run with MOCK_NAV env var override — explicit
            # simulated NAV, not from DNSE.
            lines.append(f"💰 Mock NAV (dry-run override): {nav:,.0f} VND")
        else:
            # Fallback for legacy result dicts that pre-date the
            # nav_source field. Default to "Live NAV (DNSE)" since
            # every current code path in generate_orders() sets
            # nav_source="dnse" (MOCK_NAV is the only override
            # path, and it sets nav_source="mock"). The "DNSE"
            # default is the safe one — it doesn't lie about a
            # MOCK value being real.
            lines.append(f"💰 Live NAV (DNSE): {nav:,.0f} VND")
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
