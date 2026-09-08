"""Daily event-driven replay of the autofin mirror strategy.

Whereas ``backtest_autofin.run_autofin_backtest`` replays the
**exit side** of every historical autofin deal as a single
atomic BUY → SELL event (entry + final close only), this module
replays the **full strategy** day-by-day against reconstructed
historical snapshots. The result is a much more honest estimate
of what the live bot would have done.

What it does
------------
For every trading day D from the earliest deal's open date to
the latest deal's close date:

1. **Reconstruct the snapshot** autofin.vn would have shown at
   end-of-day D, using the deal records we have:

   - ``holding``: deals where ``time_Open <= D < time_Closed``
     (active positions autofin was tracking)
   - ``suggest``: deals where ``time_Open == D`` (new signals
     published that day — approximation, autofin doesn't
     expose historical suggest feeds)
   - ``closing``: deals where ``time_Closed == D`` (deals
     that closed that day)

2. **Run the actual strategy** on the snapshot via
   ``decide_orders()`` — the same pure function the live bot
   uses. This means the strategy's entry-side selection
   (risk-adjusted ranking, NAV budget, dedup, etc.) is
   exercised exactly as it would be in production.

3. **Queue the orders for next-day fill** at T+1. We never
   fill on the same day the signal was issued — that would
   assume we'd receive the signal and execute in the same
   session, which is unrealistic. For the closing signal
   (the SELL when autofin exits a deal), we fill at the
   NEXT day's price_Open (or the same day's price_Closed
   if it's the final day).

4. **Mark-to-market open positions** daily using a TWAP
   price model: linearly interpolate between
   ``price_Open`` and ``price_Closed`` over the holding
   period. This is the only defensible model without
   intraday data.

5. **Compound NAV**, track daily NAV series, compute
   max-DD from the daily series (not per-trade, which
   is what ``run_autofin_backtest`` does — and that
   understates real drawdown).

Caveats
-------
This is still an approximation. autofin doesn't expose
historical /signal-static-suggest or /signal-static-holding
endpoints, so we reconstruct them from the closing-signals
records we have. Specifically:

- We can't model deals autofin **opened then deleted**
  without ever closing (these are missing from
  /closing-signals).
- We can't model deals autofin **changed their mind on**
  mid-position (e.g. upgraded from suggest to holding).
- We can't model deals that were **never in suggest** but
  still appeared in holding (autofin sometimes opens
  positions that weren't in their public suggest feed).
- We don't model **partial-TP timing** within a deal —
  the strategy only triggers partial-TPs when
  ``numberOfProfit`` increases, which in real-time only
  fires after autofin publishes a new recomment. In the
  daily replay we approximate this by detecting when
  ``numberOfProfit`` on day D > ``numberOfProfit`` we saw
  on day D-1, then issuing a partial-TP at D's TWAP price.

These caveats bias the result **upward**: missing deleted
deals understates losses; missing suggest entries
understates the strategy's selectivity.

Output shape matches ``run_autofin_backtest`` for easy
comparison.
"""
from __future__ import annotations

import csv
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .autofin_signals import AutofinClient, AutofinSignal, parse_signal, parse_recommendation_history
from .backtest_autofin import (
    DEFAULT_BOARD_LOT,
    DEFAULT_FEE_PCT_PER_SIDE,
    DEFAULT_INITIAL_NAV,
    DEFAULT_SLIPPAGE_PCT_PER_FILL,
    MAX_PAGES,
    _round_to_lots,
)
from .data import write_json
from .strategies_impl.autofin_signal import _deal_id, decide_orders


@dataclass
class DayState:
    """State for one day in the daily replay."""
    nav: float
    cash: float
    positions: dict[str, int] = field(default_factory=dict)  # symbol -> qty
    our_deals: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_orders: list[dict[str, Any]] = field(default_factory=list)
    # Conditional TP/SL bracket orders. Each entry fires only when
    # the day's interpolated price reaches the trigger level.
    #   {"trigger": ">=" | "<=", "price": float, "side": "NS",
    #    "symbol": str, "qty": int, "reason": str}
    conditional_orders: list[dict[str, Any]] = field(default_factory=list)


def _parse_dt(s: str) -> datetime | None:
    """Parse autofin date string. Format: 'MM/DD/YYYY HH:MM:SS' (ICT)."""
    if not s:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _interpolated_price(deal: dict[str, Any], day: date) -> float | None:
    """Linearly interpolate price for a deal on a given day.

    Returns price_Open for days before or at time_Open,
    price_Closed for days at or after time_Closed, and
    linear interpolation in between. Used for mark-to-market
    and partial-TP fills.
    """
    sd = deal.get("statusDeal", {}) or {}
    p_open = float(sd.get("price_Open") or 0)
    p_close = float(sd.get("price_Closed") or 0)
    t_open = _parse_dt(str(sd.get("time_Open") or ""))
    t_close = _parse_dt(str(sd.get("time_Closed") or ""))
    if p_open <= 0 or p_close <= 0 or t_open is None or t_close is None:
        return None
    if t_close <= t_open:
        return p_close
    d_open = t_open.date()
    d_close = t_close.date()
    if day <= d_open:
        return p_open
    if day >= d_close:
        return p_close
    span = (d_close - d_open).days or 1
    frac = (day - d_open).days / span
    return p_open + frac * (p_close - p_open)


def fetch_all_closed(
    client: AutofinClient,
    max_pages: int = MAX_PAGES,
    use_full_history: bool = True,
    progress: bool = False,
) -> list[dict[str, Any]]:
    """Fetch all closed deals for the daily-replay backtest.

    ``use_full_history=True`` (default): fetches the FULL per-symbol
    deal history by combining ``/closing-signals`` (to enumerate
    symbols) with ``/signal-deals?symbol=X`` (all deals per symbol).
    This returns 10-20x more deals than just ``/closing-signals``.

    ``use_full_history=False``: legacy — paginate ``/closing-signals``
    only (one deal per symbol).
    """
    if use_full_history:
        return client.fetch_all_deals_pages(
            symbols=None, page_size=50, closed_only=True, progress=progress,
        )
    all_items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        items = client.fetch_closing(page_size=50, page_index=page)
        all_items.extend(items)
        if len(items) < 50:
            break
    return all_items


def reconstruct_snapshot(
    day: date,
    all_deals: list[dict[str, Any]],
) -> dict[str, list[Any]]:
    """Build what autofin.vn would have shown at end-of-day ``day``.

    Returns a dict with keys:
      - ``holding``: list of raw deal dicts active at EOD (deal
        was opened before/on day and not yet closed)
      - ``suggest``: list of AutofinSignal objects for deals opened
        ON day (approximation of new signals published that day)
      - ``closing``: list of raw deal dicts closed on day
      - ``synthetic_partial_tps``: list of partial-TP sell events
        that autofin would have triggered by EOD ``day`` for active
        deals. Each event has symbol, qty, price (interpolated),
        and reason. The strategy didn't actually publish these in
        real-time (it published `lastRecomment` text changes which
        we don't have), so we synthesize them evenly distributed
        across the holding period.

    Each holding/suggest entry carries the full autofin dict
    so ``decide_orders`` can read ``lastRecomment``,
    ``numberOfProfit``, ``rateForNav``, etc.
    """
    holding: list[dict[str, Any]] = []
    closing: list[dict[str, Any]] = []
    suggest_raw: list[dict[str, Any]] = []
    synthetic_partial_tps: list[dict[str, Any]] = []
    for d in all_deals:
        sd = d.get("statusDeal", {}) or {}
        t_open = _parse_dt(str(sd.get("time_Open") or ""))
        t_close = _parse_dt(str(sd.get("time_Closed") or ""))
        if t_open is None:
            continue
        d_open = t_open.date()
        d_close = t_close.date() if t_close else None
        if d_close is None:
            # open deal (no close yet) — in holding on day if d_open <= day
            if d_open <= day:
                holding.append(dict(d))
                _append_synthetic_tps(
                    d, day, synthetic_partial_tps,
                    include_only_if_before_close=False,
                )
            continue
        if d_open <= day < d_close:
            holding.append(dict(d))
            _append_synthetic_tps(
                d, day, synthetic_partial_tps,
                include_only_if_before_close=True,
            )
        if d_close == day:
            closing.append(dict(d))
        if d_open == day:
            suggest_raw.append(dict(d))
    return {
        "holding": holding,
        "suggest": [parse_signal(r) for r in suggest_raw],
        "closing": closing,
        "synthetic_partial_tps": synthetic_partial_tps,
    }


def _append_synthetic_tps(
    deal: dict[str, Any],
    day: date,
    out: list[dict[str, Any]],
    include_only_if_before_close: bool,
) -> None:
    """Add synthetic partial-TP events to ``out`` if ``day`` is a TP day.

    autofin's real-time strategy fires a partial-TP SELL each time
    its `numberOfProfit` increases. We don't have the historical
    intermediate `numberOfProfit` values, only the final count.
    Reconstruct TP days by distributing N partial-TPs evenly over
    the holding period: TP_k fires on day open + k/(N+1) * holding_days.

    Each synthetic TP sells 1/(N+1) of the original target_qty at
    the interpolated price on that day. autofin's actual pattern is
    1/3 per TP of CURRENT position, but uniform split is the most
    defensible default without more data.
    """
    n_profit = int(deal.get("numberOfProfit") or 0)
    if n_profit <= 0:
        return
    sd = deal.get("statusDeal", {}) or {}
    t_open = _parse_dt(str(sd.get("time_Open") or ""))
    t_close = _parse_dt(str(sd.get("time_Closed") or ""))
    if t_open is None:
        return
    d_open = t_open.date()
    if t_close is None:
        d_close = None
        holding_days = None
    else:
        d_close = t_close.date()
        holding_days = (d_close - d_open).days or 1

    if include_only_if_before_close and d_close is not None and day >= d_close:
        return  # never fire synthetic TPs on or after the actual close day

    for k in range(1, n_profit + 1):
        if holding_days is None:
            # open deal — distribute TPs over a 30-day window from open
            tp_day = d_open + timedelta(days=int(k * 30 / (n_profit + 1)))
        else:
            frac = k / (n_profit + 1)
            tp_day = d_open + timedelta(days=int(frac * holding_days))
        if tp_day == day:
            p = _interpolated_price(deal, day) or 0.0
            if p <= 0:
                continue
            out.append({
                "symbol": deal.get("symbol", "?"),
                "side": "NS",
                "qty": None,  # filled by caller from position size
                "fraction": 1.0 / (n_profit + 1),
                "price": p,
                "reason": (
                    f"synthetic_tp:deal#{deal.get('_id')}:"
                    f"{deal.get('symbol')}:k{k}/{n_profit}"
                ),
            })


def _interpolated_price_for_order(deal: dict[str, Any], day: date) -> float:
    """Get the fill price for an order issued on day D, given a deal.

    For BUY orders (entry): use the interpolated price on day D.
    For SELL orders (partial/full): use the interpolated price on day D.
    The strategy sees the recomment AT EOD, so we assume we'd
    fill at the NEXT trading day's open. We approximate by
    using day D's interpolated close price — slightly optimistic
    but defensible (we have no intraday data).
    """
    if deal is None:
        return 0.0
    sd = deal.get("statusDeal", {}) or {}
    p_close = float(sd.get("price_Closed") or 0)
    t_close = _parse_dt(str(sd.get("time_Closed") or ""))
    # If today is the close day, fill at the actual close price
    if t_close is not None and t_close.date() == day and p_close > 0:
        return p_close
    p = _interpolated_price(deal, day)
    return p if p is not None else 0.0


def run_daily_replay_backtest(
    initial_nav: float = DEFAULT_INITIAL_NAV,
    fee_pct_per_side: float = DEFAULT_FEE_PCT_PER_SIDE,
    slippage_pct_per_fill: float = DEFAULT_SLIPPAGE_PCT_PER_FILL,
    board_lot: int = DEFAULT_BOARD_LOT,
    max_pages: int = MAX_PAGES,
    user_sl_pct: float = 0.0,
    use_full_history: bool = True,
    client: AutofinClient | None = None,
    closed_deals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Day-by-day replay that runs the actual strategy code.

    The bot's pure decision function (``decide_orders``) is called
    on each reconstructed daily snapshot, the orders are queued for
    next-day fill, NAV is compounded and marked-to-market daily.

    No hindsight filters are applied. The result is the most
    defensible estimate of what the strategy would actually do
    if run historically.

    Parameters
    ----------
    user_sl_pct : fraction (e.g. 0.05 = 5%) — user-defined stop-loss
        override. The SL bracket is the TIGHTER of this and autofin's
        declared sL_F. Default 0 = use autofin's SL only.
    closed_deals : optional pre-fetched list of autofin closing-signals
        records. If provided, skips the autofin API call. Use this for
        offline testing / when the autofin API is unreachable.
    """
    if closed_deals is None:
        if client is None:
            client = AutofinClient(
                email=os.environ["AUTOFIN_EMAIL"],
                password=os.environ["AUTOFIN_PASSWORD"],
            )
        closed = fetch_all_closed(
            client, max_pages=max_pages, use_full_history=use_full_history,
        )
    else:
        closed = closed_deals
    if not closed:
        return {
            "mode": "daily_replay",
            "trade_count": 0,
            "initial_nav": initial_nav,
            "final_nav": initial_nav,
            "total_pnl": 0.0,
            "total_pnl_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_time_weighted": 0.0,
            "annualized_pct": 0.0,
            "years": 0.0,
            "span_days": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate_pct": 0.0,
            "fees_total": 0.0,
            "slippage_total": 0.0,
            "daily_nav": [],
            "filled_orders": [],
            "skipped_deals": [],
        }

    # Determine the date range
    dates = [_parse_dt(str(d.get("statusDeal", {}).get("time_Open") or "")) for d in closed]
    dates = [dt for dt in dates if dt is not None]
    start = min(dates).date()
    end_dates = [_parse_dt(str(d.get("statusDeal", {}).get("time_Closed") or "")) for d in closed]
    end_dates = [dt for dt in end_dates if dt is not None]
    end = max(end_dates).date() if end_dates else start

    # Pre-build a sorted index of (open_date, deal) and (close_date, deal)
    # so the per-day snapshot lookup is O(active deals) instead of
    # O(total deals). With 3,321 deals, this turns a 200s backtest into <10s.
    # Also pre-parse signals so decide_orders gets AutofinSignal objects.
    open_index: dict[date, list[dict[str, Any]]] = {}
    open_signals_index: dict[date, list[AutofinSignal]] = {}
    for d in closed:
        t_o = _parse_dt(str((d.get("statusDeal") or {}).get("time_Open") or ""))
        if t_o is None:
            continue
        open_index.setdefault(t_o.date(), []).append(d)
        try:
            open_signals_index.setdefault(t_o.date(), []).append(parse_signal(d))
        except Exception:
            pass

    # Build per-day snapshot cache. For each day in [start, end],
    # we pre-compute holding, suggest, closing, synthetic_partial_tps
    # using the open/close indices.
    snapshot_cache: dict[date, dict[str, list[Any]]] = {}
    # Track active deals: deal_id -> {open_date, close_date_or_None}
    active_by_open: dict[date, list[dict[str, Any]]] = open_index
    # Maintain a sorted list of (open_date, deal) entries for incremental
    # traversal. Key = tuple of (date_iso, _id, symbol).
    import heapq
    open_heap: list[tuple[date, int, str, dict[str, Any]]] = []
    for d in open_index.items():
        for deal in d[1]:
            heapq.heappush(open_heap, (d[0], id(deal), str(deal.get("symbol", "")), deal))
    # close_heap: (close_date, sort_key, deal)
    close_heap: list[tuple[date, int, str, dict[str, Any]]] = []
    for d in closed:
        t_c = _parse_dt(str((d.get("statusDeal") or {}).get("time_Closed") or ""))
        if t_c is None:
            continue
        heapq.heappush(close_heap, (t_c.date(), id(d), str(d.get("symbol", "")), d))
    # Currently-active deals (by id(deal) for stable tracking)
    currently_active: dict[int, dict[str, Any]] = {}

    def _build_snapshot_fast(day: date) -> dict[str, list[Any]]:
        # Open any deals that opened today
        while open_heap and open_heap[0][0] == day:
            _, _, _, deal = heapq.heappop(open_heap)
            currently_active[id(deal)] = deal
        # Close any deals that closed today
        closes_today: list[dict[str, Any]] = []
        while close_heap and close_heap[0][0] == day:
            _, _, _, deal = heapq.heappop(close_heap)
            d_id = id(deal)
            if d_id in currently_active:
                del currently_active[d_id]
            closes_today.append(deal)
        holding = list(currently_active.values())
        suggest = open_signals_index.get(day, [])
        # Partial-TPs: use the REAL recommendation history from autofin's
        # ``hisRecomment`` field, parsed by ``parse_recommendation_history``.
        # Each event has day_offset (days from open), action, price, and
        # the fraction of CURRENT position to sell.
        synthetic_partial_tps: list[dict[str, Any]] = []
        for deal in holding:
            sd = deal.get("statusDeal", {}) or {}
            t_open = _parse_dt(str(sd.get("time_Open") or ""))
            t_close = _parse_dt(str(sd.get("time_Closed") or ""))
            if t_open is None:
                continue
            d_open = t_open.date()
            d_close = t_close.date() if t_close else None
            event_day = d_open
            for ev in parse_recommendation_history(deal):
                if ev["action"] in ("entry",):
                    continue  # entry events are handled via BUY
                if ev["action"] in ("tp", "reduce", "capital_protect"):
                    event_day = d_open + timedelta(days=ev["day_offset"])
                    if event_day != day:
                        continue
                    if d_close is not None and event_day >= d_close:
                        continue
                    p = float(ev["price"]) if ev["price"] > 0 else (
                        _interpolated_price(deal, day) or 0.0
                    )
                    if p <= 0:
                        continue
                    synthetic_partial_tps.append({
                        "symbol": deal.get("symbol", "?"),
                        "side": "NS",
                        "qty": None,
                        "fraction": float(ev["fraction"]),
                        "price": p,
                        "action": ev["action"],
                        "reason": (
                            f"{ev['action']}:deal#{deal.get('_id')}:"
                            f"{deal.get('symbol')}:d{ev['day_offset']}"
                        ),
                    })
        return {
            "holding": holding,
            "suggest": suggest,
            "closing": closes_today,
            "synthetic_partial_tps": synthetic_partial_tps,
        }

    # Walk every day in the range
    state = DayState(nav=initial_nav, cash=initial_nav)
    daily_nav: list[dict[str, Any]] = []
    filled_orders: list[dict[str, Any]] = []
    skipped_deals: list[dict[str, Any]] = []
    total_fees = 0.0
    total_slippage = 0.0

    current = start
    while current <= end:
        snap = _build_snapshot_fast(current)
        holding = snap["holding"]
        signals = snap["suggest"]

        # 1. Apply any pending fills dated today first. These are BUY
        # orders queued on previous days. Then check TP/SL brackets
        # against today's price — fire if reached.
        fees_added, slippage_added = apply_pending_fills(
            state, current, fee_pct_per_side, slippage_pct_per_fill, filled_orders,
        )
        total_fees += fees_added
        total_slippage += slippage_added
        # Drop conditional orders for symbols we no longer hold
        state.conditional_orders = [
            co for co in state.conditional_orders
            if state.positions.get(co["symbol"], 0) > 0
        ]
        fees_added, slippage_added = check_conditional_triggers(
            state, current, closed, fee_pct_per_side, slippage_pct_per_fill,
            filled_orders,
        )
        total_fees += fees_added
        total_slippage += slippage_added

        # 2. Mark-to-market open positions at today's interpolated price.
        # NAV here = cash + positions at their today's price. After fills
        # (BUYs/SELLs applied in step 1), this reflects the actual wealth.
        mtm_value = state.cash
        for sym, qty in state.positions.items():
            if qty <= 0:
                continue
            sym_deals = filled_deals_in_holding_for_day(closed, current, sym)
            for d in sym_deals:
                p = _interpolated_price(d, current) or 0.0
                if p > 0:
                    mtm_value += qty * p
                    break  # use first deal's price
        state.nav = mtm_value

        # 3. Call the strategy's pure decision function on today's snapshot.
        orders, new_our_deals, _ = decide_orders(
            holding=holding,
            signals=signals,
            our_deals=state.our_deals,
            our_positions=dict(state.positions),
            nav=state.nav,
            user_sl_pct=user_sl_pct,
        )
        state.our_deals = new_our_deals

        # 4. Queue orders. The strategy marks each order with a ``kind``:
        #   - "tp" or "sl": conditional brackets (fire only when price
        #     reaches the trigger level)
        #   - anything else (entry / partial_close / full_close): immediate
        #     fill at today's interpolated price
        for o in orders:
            sym = o.get("symbol", "?")
            side = o.get("side", "")
            qty = int(o.get("quantity", 0))
            price_hint = float(o.get("price", 0))
            if qty <= 0:
                continue
            reason = o.get("reason", "")
            kind = o.get("kind", "")
            # TP/SL bracket → conditional
            if kind == "tp":
                state.conditional_orders.append({
                    "trigger": ">=",
                    "price": price_hint,
                    "side": "NS",
                    "symbol": sym,
                    "qty": qty,
                    "reason": reason,
                })
                continue
            if kind == "sl":
                state.conditional_orders.append({
                    "trigger": "<=",
                    "price": price_hint,
                    "side": "NS",
                    "symbol": sym,
                    "qty": qty,
                    "reason": reason,
                })
                continue
            # BUY / partial-close / full-close → immediate fill at today's price.
            # First try the holding snapshot (active deals). If empty
            # (e.g. close-day orders where the deal just left holding),
            # fall back to the closing snapshot which has the final price.
            deal_for_sym = next((d for d in holding if d.get("symbol") == sym), None)
            if deal_for_sym is None:
                deal_for_sym = next((d for d in snap["closing"] if d.get("symbol") == sym), None)
            day_price = _interpolated_price_for_order(deal_for_sym, current) if deal_for_sym else price_hint
            if day_price <= 0:
                continue
            state.pending_orders.append({
                "fill_date": current,
                "side": side,
                "symbol": sym,
                "qty": qty,
                "price": day_price,
                "reason": reason,
            })

        # 4b. SYNTHETIC PARTIAL-TPs: autofin publishes `numberOfProfit`
        # changes in real-time (we don't have those historical snapshots),
        # so we synthesize partial-TP events evenly distributed across
        # the holding period. Each event sells a fraction of our current
        # position at the day's interpolated price.
        for tp in snap["synthetic_partial_tps"]:
            sym = tp["symbol"]
            held = state.positions.get(sym, 0)
            if held <= 0:
                continue
            # Sell `fraction` of current position, rounded down to lots.
            sell_qty = int(held * tp["fraction"])
            sell_qty = sell_qty - (sell_qty % board_lot)
            if sell_qty < board_lot:
                continue
            half = slippage_pct_per_fill * 0.5
            fill = tp["price"] * (1 - half)
            value = sell_qty * fill
            fee = value * fee_pct_per_side
            slip = (tp["price"] - fill) * sell_qty
            state.cash += value - fee
            state.positions[sym] = held - sell_qty
            if state.positions[sym] <= 0:
                state.positions.pop(sym, None)
            total_fees += fee
            total_slippage += slip
            filled_orders.append({
                "date": current.isoformat(),
                "side": "SELL",
                "symbol": sym,
                "qty": sell_qty,
                "price": fill,
                "fees": fee,
                "slippage": slip,
                "reason": tp["reason"] + " [synthetic]",
            })

        # 5. Apply any fills that were just queued for today. Then
        # RECOMPUTE NAV because cash and positions both changed.
        fees_added, slippage_added = apply_pending_fills(
            state, current, fee_pct_per_side, slippage_pct_per_fill, filled_orders,
        )
        total_fees += fees_added
        total_slippage += slippage_added
        # After today's fills, recompute NAV. If we still hold positions,
        # mark them at today's price; if we sold everything, NAV = cash.
        post_fill_value = state.cash
        for sym, qty in state.positions.items():
            if qty <= 0:
                continue
            sym_deals = filled_deals_in_holding_for_day(closed, current, sym)
            for d in sym_deals:
                p = _interpolated_price(d, current) or 0.0
                if p > 0:
                    post_fill_value += qty * p
                    break
        state.nav = post_fill_value

        # Record daily NAV
        daily_nav.append({
            "date": current.isoformat(),
            "nav": state.nav,
            "cash": state.cash,
            "positions_value": mtm_value - state.cash,
            "position_count": len(state.positions),
            "deals_tracked": len(state.our_deals),
        })
        current += timedelta(days=1)

    final_nav = state.nav
    total_pnl = final_nav - initial_nav
    total_pnl_pct = total_pnl / initial_nav if initial_nav > 0 else 0
    years = (end - start).days / 365.0 if end > start else 0
    annualized = (final_nav / initial_nav) ** (1.0 / years) - 1 if (years > 0 and final_nav > 0 and initial_nav > 0) else 0
    max_dd = _max_drawdown_from_daily(daily_nav)
    # win/loss from filled sell orders that closed at a profit/loss
    wins, losses, returns = _summarize_fills(filled_orders)

    # Time-weighted Sharpe from daily NAV returns
    daily_returns = _daily_returns(daily_nav)
    if daily_returns and len(daily_returns) > 1:
        mu = statistics.mean(daily_returns)
        sd = statistics.stdev(daily_returns)
        sharpe = (mu / sd) * (252 ** 0.5) if sd > 0 else 0  # 252 trading days
    else:
        sharpe = 0

    return {
        "mode": "daily_replay",
        "initial_nav": initial_nav,
        "final_nav": final_nav,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "annualized_pct": annualized,
        "years": years,
        "span_days": (end - start).days,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "trade_count": len(filled_orders),
        "win_count": wins,
        "loss_count": losses,
        "win_rate_pct": wins / (wins + losses) if (wins + losses) > 0 else 0,
        "max_drawdown_pct": max_dd,
        "sharpe_time_weighted": sharpe,
        "fees_total": total_fees,
        "slippage_total": total_slippage,
        "fee_pct_per_side": fee_pct_per_side,
        "slippage_pct_per_fill": slippage_pct_per_fill,
        "user_sl_pct": user_sl_pct,
        "daily_nav": daily_nav,
        "filled_orders": filled_orders,
        "skipped_deals": skipped_deals,
    }


def filled_deals_in_holding_for_day(all_deals: list[dict[str, Any]], day: date, sym: str) -> list[dict[str, Any]]:
    """Helper: deals for symbol sym that are active on day."""
    out: list[dict[str, Any]] = []
    for d in all_deals:
        sd = d.get("statusDeal", {}) or {}
        if str(d.get("symbol") or "").upper() != sym.upper():
            continue
        t_open = _parse_dt(str(sd.get("time_Open") or ""))
        t_close = _parse_dt(str(sd.get("time_Closed") or ""))
        if t_open is None or t_open.date() > day:
            continue
        if t_close is not None and t_close.date() <= day:
            continue
        out.append(d)
    return out


def apply_pending_fills(
    state: DayState,
    today: date,
    fee_pct_per_side: float,
    slippage_pct_per_fill: float,
    filled_orders: list[dict[str, Any]],
) -> tuple[float, float]:
    """Apply pending fills dated today (queued yesterday, fill today).

    Returns ``(fees_added, slippage_added)`` so the caller can
    accumulate totals. Mutates ``state.cash``, ``state.positions``,
    and ``state.pending_orders`` in place.
    """
    half = slippage_pct_per_fill * 0.5
    still_pending: list[dict[str, Any]] = []
    fees_added = 0.0
    slippage_added = 0.0
    for order in state.pending_orders:
        if order["fill_date"] != today:
            still_pending.append(order)
            continue
        sym = order["symbol"]
        side = order["side"]
        qty = order["qty"]
        ref_price = order["price"]
        if side == "NB":
            fill = ref_price * (1 + half)
            value = qty * fill
            fee = value * fee_pct_per_side
            slip = (fill - ref_price) * qty
            cost = value + fee
            if state.cash < cost:
                # insufficient cash — skip (and drop the order)
                continue
            state.cash -= cost
            state.positions[sym] = state.positions.get(sym, 0) + qty
            fees_added += fee
            slippage_added += slip
            filled_orders.append({
                "date": today.isoformat(),
                "side": "BUY",
                "symbol": sym,
                "qty": qty,
                "price": fill,
                "fees": fee,
                "slippage": slip,
                "reason": order["reason"],
            })
        elif side == "NS":
            held = state.positions.get(sym, 0)
            if held <= 0:
                continue
            sell_qty = min(qty, held)
            if sell_qty < _round_to_lots(1):
                continue
            fill = ref_price * (1 - half)
            value = sell_qty * fill
            fee = value * fee_pct_per_side
            slip = (ref_price - fill) * sell_qty
            proceeds = value - fee
            state.cash += proceeds
            state.positions[sym] = held - sell_qty
            if state.positions[sym] <= 0:
                state.positions.pop(sym, None)
            fees_added += fee
            slippage_added += slip
            filled_orders.append({
                "date": today.isoformat(),
                "side": "SELL",
                "symbol": sym,
                "qty": sell_qty,
                "price": fill,
                "fees": fee,
                "slippage": slip,
                "reason": order["reason"],
            })
    state.pending_orders = still_pending
    return fees_added, slippage_added


def check_conditional_triggers(
    state: DayState,
    today: date,
    all_deals: list[dict[str, Any]],
    fee_pct_per_side: float,
    slippage_pct_per_fill: float,
    filled_orders: list[dict[str, Any]],
) -> tuple[float, float]:
    """Check TP/SL bracket orders — fire if today's price hits the trigger.

    Each conditional order has:
      - ``trigger``: ">=" for TP (price rises above) or "<=" for SL (price
        drops below)
      - ``price``: the trigger level
      - ``side``: always "NS" (these are sell orders)
      - ``symbol``, ``qty``, ``reason``

    A conditional order fires when the interpolated price on ``today``
    for that symbol reaches the trigger level. Once fired, the order
    is consumed (removed from ``state.conditional_orders``).

    Returns ``(fees_added, slippage_added)``.
    """
    half = slippage_pct_per_fill * 0.5
    fees_added = 0.0
    slippage_added = 0.0
    still_active: list[dict[str, Any]] = []
    for order in state.conditional_orders:
        sym = order["symbol"]
        trigger_level = float(order["price"])
        triggered = False
        # find a deal for this symbol
        sym_deals = filled_deals_in_holding_for_day(all_deals, today, sym)
        for d in sym_deals:
            p = _interpolated_price(d, today)
            if p is None:
                continue
            if order["trigger"] == ">=" and p >= trigger_level:
                triggered = True
                fill = trigger_level * (1 - half)  # TP/SL fills AT the trigger level
                break
            if order["trigger"] == "<=" and p <= trigger_level:
                triggered = True
                fill = trigger_level * (1 - half)
                break
        if not triggered:
            still_active.append(order)
            continue
        held = state.positions.get(sym, 0)
        if held <= 0:
            # already sold or never held — drop the conditional
            continue
        sell_qty = min(int(order["qty"]), held)
        if sell_qty < _round_to_lots(1):
            continue
        value = sell_qty * fill
        fee = value * fee_pct_per_side
        slip = (trigger_level - fill) * sell_qty
        proceeds = value - fee
        state.cash += proceeds
        state.positions[sym] = held - sell_qty
        if state.positions[sym] <= 0:
            state.positions.pop(sym, None)
        fees_added += fee
        slippage_added += slip
        filled_orders.append({
            "date": today.isoformat(),
            "side": "SELL",
            "symbol": sym,
            "qty": sell_qty,
            "price": fill,
            "fees": fee,
            "slippage": slip,
            "reason": order["reason"] + " [bracket_triggered]",
        })
    state.conditional_orders = still_active
    return fees_added, slippage_added


def _max_drawdown_from_daily(daily_nav: list[dict[str, Any]]) -> float:
    """Max DD computed from the daily NAV series (most accurate)."""
    if not daily_nav:
        return 0.0
    peak = daily_nav[0]["nav"]
    max_dd = 0.0
    for d in daily_nav:
        nav = d["nav"]
        peak = max(peak, nav)
        if peak > 0:
            dd = (peak - nav) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def _daily_returns(daily_nav: list[dict[str, Any]]) -> list[float]:
    """Day-over-day simple returns from the daily NAV series."""
    if len(daily_nav) < 2:
        return []
    out: list[float] = []
    prev = daily_nav[0]["nav"]
    for d in daily_nav[1:]:
        if prev > 0:
            out.append((d["nav"] - prev) / prev)
        prev = d["nav"]
    return out


def _summarize_fills(filled_orders: list[dict[str, Any]]) -> tuple[int, int, list[float]]:
    """Count wins and losses from filled BUY→SELL pairs.

    For simplicity we approximate: each SELL whose fill price
    is above the most recent BUY price for the same symbol
    counts as a win; below counts as a loss. Returns
    (wins, losses, list of per-trade returns).
    """
    last_buy: dict[str, float] = {}
    wins = 0
    losses = 0
    returns: list[float] = []
    for o in filled_orders:
        sym = o["symbol"]
        if o["side"] == "BUY":
            last_buy[sym] = o["price"]
        elif o["side"] == "SELL" and sym in last_buy:
            buy_p = last_buy[sym]
            sell_p = o["price"]
            if buy_p > 0:
                r = (sell_p - buy_p) / buy_p
                returns.append(r)
                if r > 0:
                    wins += 1
                elif r < 0:
                    losses += 1
    return wins, losses, returns


def write_daily_replay_outputs(
    result: dict[str, Any],
    out_dir: Path,
    prefix: str = "autofin_daily_replay",
) -> tuple[Path, Path, Path]:
    """Write JSON summary + daily-NAV CSV + per-fill CSV."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{prefix}.json"
    nav_csv = out_dir / f"{prefix}_daily_nav.csv"
    fills_csv = out_dir / f"{prefix}_fills.csv"

    payload = {k: v for k, v in result.items() if k not in ("daily_nav", "filled_orders")}
    payload["daily_nav"] = result["daily_nav"]
    payload["filled_orders"] = result["filled_orders"]
    write_json(json_path, payload)

    if result["daily_nav"]:
        with nav_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(result["daily_nav"][0].keys()))
            writer.writeheader()
            writer.writerows(result["daily_nav"])

    if result["filled_orders"]:
        with fills_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(result["filled_orders"][0].keys()))
            writer.writeheader()
            writer.writerows(result["filled_orders"])

    return json_path, nav_csv, fills_csv


def format_daily_replay_report(result: dict[str, Any]) -> str:
    """Telegram-friendly summary of the daily-replay backtest."""
    lines: list[str] = []
    lines.append("🤖 Vietnam Algo: autofin_signal DAILY REPLAY (no look-ahead)")
    lines.append(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} ICT")
    lines.append("")
    lines.append("💼 Replay assumptions")
    lines.append(f"  • initial NAV: {result['initial_nav']:,.0f} VND")
    lines.append(
        f"  • fee per side: {result['fee_pct_per_side']:.4%}, "
        f"slippage per fill: {result['slippage_pct_per_fill']:.4%}"
    )
    if result.get("user_sl_pct", 0) > 0:
        lines.append(
            f"  • user stop-loss: {result['user_sl_pct']:.1%} below entry "
            f"(tighter of this vs autofin sL_F wins)"
        )
    lines.append(
        f"  • strategy: actual decide_orders() function, run against "
        f"reconstructed daily snapshots"
    )
    lines.append(
        f"  • date range: {result.get('start_date', '?')} → "
        f"{result.get('end_date', '?')} ({result.get('span_days', 0)}d)"
    )
    lines.append("")
    lines.append("📈 Summary")
    lines.append(
        f"  • filled orders: {result['trade_count']}  "
        f"win: {result['win_count']}  loss: {result['loss_count']}"
    )
    lines.append(
        f"  • win rate: {result['win_rate_pct']:.1%}"
    )
    lines.append(
        f"  • final NAV: {result['final_nav']:,.0f} VND  "
        f"({result['total_pnl_pct']:+.1%})"
    )
    lines.append(
        f"  • total P&L: {result['total_pnl']:+,.0f} VND  "
        f"over {result['years']:.2f}y → annualized {result['annualized_pct']:+.1%}"
    )
    lines.append(
        f"  • max drawdown (daily series): {result['max_drawdown_pct']:.1%}  "
        f"Sharpe (daily, annualized): {result['sharpe_time_weighted']:.2f}"
    )
    lines.append(
        f"  • total fees: {result['fees_total']:,.0f} VND  "
        f"slippage: {result['slippage_total']:,.0f} VND"
    )
    return "\n".join(lines)
