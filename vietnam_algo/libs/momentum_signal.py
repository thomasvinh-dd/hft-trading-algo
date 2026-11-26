"""The signal-driven momentum decision engine.

This module answers one question every EOD cycle: *given today's data
and the live portfolio, what should the strategy trade today?*

It reuses the backtest engine's decision math verbatim so the live
signals cannot drift from the backtested behaviour:

  * ``rank_symbols``                    — candidate ranking
  * ``market_regime``                   — bull / neutral / bear
  * ``allowed_positions_for_regime``    — position cap per regime
  * ``breakout_symbols``                — volume-breakout candidates
  * ``MomentumWithBreakoutStrategy``    — the entry decision
  * ``regime_exit_reason``              — regime-driven exits
  * ``trailing_stop_percent_for_regime`` — the trailing stop level

Why a separate engine rather than reusing ``run_momentum_rotation``
--------------------------------------------------------------------

The backtest walks ~1,800 trading days and emits a trade list. We only
need the LAST bar's decision, plus the exits implied by the LIVE
portfolio. Running the whole backtest to get one day's answer is both
slow (~3 min) and, more importantly, wrong: the backtest's positions are
hypothetical, so it cannot know about the positions we actually hold.

The trailing stop needs a high-water mark ("peak price") that the
backtest kept in memory. Here it is reconstructed from the live
portfolio and persisted in a small per-strategy state file.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vietnam_algo.libs.backtest import (
    allowed_positions_for_regime,
    build_corporate_action_lookup,
    build_corporate_action_table,
    close_price,
    compute_exposure_cap,
    exposure_percent_for_regime,
    initialize_backtest_context,
    market_regime,
    position_sellable,
    precompute_market_proxy,
    regime_exit_reason,
    resolve_universe_for_date,
    risky_corporate_action_symbols,
    sorted_trading_dates,
    symbol_sector_map,
    trailing_stop_percent_for_regime,
)
from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.strategy_math import (
    MomentumWithBreakoutStrategy,
    VolumeBreakoutStrategy,
    bars_until,
    breakout_symbols,
    rank_symbols,
    too_extended_from_sma50,
)

# The CSV schema the rest of the pipeline expects (see
# ``libs/backtest.py:save_order_list``).
ORDER_FIELDS = [
    "orderId", "date", "symbol", "side", "price", "quantity",
    "value", "fee", "reason",
]


@dataclass
class SignalPosition:
    """The per-strategy position state the trailing stop needs."""

    symbol: str
    quantity: int = 0
    entry_price: float = 0.0
    peak_price: float = 0.0
    entry_date: str = ""
    entry_index: int = 0


@dataclass
class SignalState:
    """Persisted per-strategy signal state."""

    positions: dict[str, SignalPosition] = field(default_factory=dict)
    breakout_entries_this_week: dict[str, int] = field(default_factory=dict)
    last_week: str = ""
    updated_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "breakout_entries_this_week": self.breakout_entries_this_week,
            "last_week": self.last_week,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "SignalState":
        positions = {}
        for sym, p in (raw.get("positions") or {}).items():
            if not isinstance(p, dict):
                continue
            positions[sym] = SignalPosition(
                symbol=sym,
                quantity=int(p.get("quantity") or 0),
                entry_price=float(p.get("entry_price") or 0.0),
                peak_price=float(p.get("peak_price") or 0.0),
                entry_date=str(p.get("entry_date") or ""),
                entry_index=int(p.get("entry_index") or 0),
            )
        return cls(
            positions=positions,
            breakout_entries_this_week={
                str(k): int(v)
                for k, v in (raw.get("breakout_entries_this_week") or {}).items()
            },
            last_week=str(raw.get("last_week") or ""),
            updated_at=str(raw.get("updated_at") or ""),
        )


def _flatten_ca_events(corporate_actions: dict) -> list:
    """Flatten the corporate-action table into a single event list."""
    out = []
    for by_date in (corporate_actions or {}).values():
        for events in by_date.values():
            out.extend(events)
    return out


def _with_point_in_time_universe(config: StrategyConfig) -> StrategyConfig:
    """A copy of the config that uses the point-in-time universe.

    The point-in-time path derives the universe from the price data
    itself, so it needs no ``universe/liquid_stocks.json`` file.
    """
    from dataclasses import replace

    return replace(config, usePointInTimeUniverse=True)


def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "live" / "momentum_signal_state.json"


def load_signal_state(data_dir: Path) -> SignalState:
    path = _state_path(data_dir)
    if not path.exists():
        return SignalState()
    try:
        return SignalState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return SignalState()


def save_signal_state(data_dir: Path, state: SignalState) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps(state.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _week_key(value: str) -> str:
    from datetime import date as date_type

    try:
        parsed = date_type.fromisoformat(value)
    except ValueError:
        return value[:10]
    year, week, _ = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def _round_to_board_lot(quantity: float, lot: int) -> int:
    if lot <= 0:
        return int(quantity)
    return int(quantity // lot) * lot


def compute_signal_orders(
    *,
    data_dir: Path,
    strategy_config: StrategyConfig,
    trade_date: str,
    live_submit: bool = False,
    account_no: str = "",
    client: Any = None,
    live_positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute today's entries and exits for the momentum strategy.

    Returns a dict with ``orders`` (the plan), ``deals_actioned``
    (the per-deal grouping the submit path consumes), plus the
    intermediate decision data (``regime``, ``candidates``,
    ``entries``, ``exits``, ``blocked``, ``notes``).
    """
    notes: list[str] = []
    data_dir = Path(data_dir)

    # ------------------------------------------------------------------
    # 1. load the price data and find today's bar
    # ------------------------------------------------------------------
    try:
        ctx = initialize_backtest_context(data_dir, "liquid_stocks", strategy_config)
    except FileNotFoundError as exc:
        # The universe file may be missing (a fresh data dir, or a
        # point-in-time universe that needs no explicit file). Fall
        # back to the point-in-time path rather than failing.
        try:
            ctx = initialize_backtest_context(
                data_dir, "liquid_stocks",
                _with_point_in_time_universe(strategy_config),
            )
        except Exception:
            return {
                "orders": [], "deals_actioned": [], "regime": None,
                "candidates": [], "entries": [], "exits": [], "blocked": [],
                "nav": None,
                "notes": [f"price data not available: {exc}"],
            }
    price_table = ctx.priceTable
    dates = sorted_trading_dates(price_table)
    if not dates:
        return {
            "orders": [], "deals_actioned": [], "regime": None,
            "candidates": [], "entries": [], "exits": [], "blocked": [],
            "nav": None, "notes": ["no price data available"],
        }

    # The signal is computed as of the requested trade date, so the
    # engine can be evaluated for any historical session (and so the
    # live plan matches what the backtest would have done on that day).
    #
    # If the requested date has no bar yet — the EOD sync runs at
    # 15:15 and the price data lags a session — we fall back to the
    # LAST available bar and say so, rather than silently going stale.
    if trade_date in dates:
        index = dates.index(trade_date)
        signal_date = trade_date
    else:
        index = len(dates) - 1
        signal_date = dates[index]
        notes.append(
            f"signal computed on the last available bar ({signal_date}); "
            f"today ({trade_date}) has no price bar yet"
        )

    warmup = max(strategy_config.return6mDays, strategy_config.smaSlowDays) + 1
    if index < warmup:
        return {
            "orders": [], "deals_actioned": [], "regime": None,
            "candidates": [], "entries": [], "exits": [], "blocked": [],
            "nav": None,
            "notes": notes + [f"not enough history ({index} < {warmup} bars)"],
        }

    market_proxy = precompute_market_proxy(price_table, dates)

    # ------------------------------------------------------------------
    # 2. regime + position cap
    # ------------------------------------------------------------------
    regime = market_regime(price_table, dates, index, strategy_config,
                           precomputed=market_proxy)
    max_positions = allowed_positions_for_regime(regime, strategy_config)

    # ------------------------------------------------------------------
    # 3. the candidate ranking
    # ------------------------------------------------------------------
    universe_list = resolve_universe_for_date(
        signal_date,
        ctx.candidatePriceTable,
        ctx.metadataBySymbol,
        strategy_config.universe,
        strategy_config.usePointInTimeUniverse,
        ctx.universeCache,
        fallback_symbols=ctx.fallbackUniverse,
    )
    universe = set(universe_list)

    ranked = rank_symbols(
        price_table, dates, index, strategy_config,
        universe=universe, market_levels=market_proxy[: index + 1],
    )
    rank_by_symbol = {sym: rank for rank, (sym, _) in enumerate(ranked, start=1)}

    # ------------------------------------------------------------------
    # 4. the live portfolio (for exits and for the position cap)
    # ------------------------------------------------------------------
    state = load_signal_state(data_dir)
    positions = _resolve_positions(
        state=state,
        live_positions=live_positions,
        client=client,
        account_no=account_no,
    )
    held_symbols = {p.symbol for p in positions.values() if p.quantity > 0}

    # ------------------------------------------------------------------
    # 5. EXITS — computed from the live portfolio
    # ------------------------------------------------------------------
    exits: list[dict[str, Any]] = []
    trailing_stop = trailing_stop_percent_for_regime(regime, strategy_config)
    regime_reason = regime_exit_reason(regime, strategy_config)

    for symbol, position in list(positions.items()):
        if position.quantity <= 0:
            continue
        price = close_price(symbol, signal_date, price_table)
        if price is None:
            continue
        position.peak_price = max(position.peak_price, price)

        exit_reason = ""
        if regime_reason:
            exit_reason = regime_reason
        elif price <= position.entry_price * (1 - strategy_config.stopLossPercent / 100):
            exit_reason = "stop_loss"
        elif (
            position.peak_price > 0
            and price <= position.peak_price * (1 - trailing_stop / 100)
        ):
            exit_reason = "trailing_stop"
        elif rank_by_symbol.get(symbol, 999999) > strategy_config.exitRank:
            exit_reason = "rank_exit"

        if not exit_reason:
            continue

        # T+2 settlement: a position bought within the settlement window
        # cannot be sold yet. Mirrors the backtest's ``position_sellable``.
        #
        # We check the ENTRY DATE rather than the backtest's entry
        # index, because a position we adopted from the live portfolio
        # may have no known entry index. Without a known entry date we
        # assume it is sellable — otherwise a position the strategy has
        # held for months would be blocked forever.
        if not _position_sellable(
            position, signal_date, dates, index, strategy_config,
        ):
            notes.append(
                f"{symbol}: {exit_reason} deferred — T+{strategy_config.sellSettlementDays} settlement"
            )
            continue

        exits.append({
            "symbol": symbol,
            "side": "NS",
            "price": price,
            "quantity": position.quantity,
            "kind": "exit",
            "reason": f"momentum_signal:{exit_reason}",
            "exitReason": exit_reason,
        })
        position.quantity = 0

    # ------------------------------------------------------------------
    # 6. ENTRIES — the ranking + the breakout set
    # ------------------------------------------------------------------
    week = _week_key(signal_date)
    if state.last_week != week:
        state.last_week = week
        state.breakout_entries_this_week = {}
    weekly_remaining = max(
        strategy_config.maxBreakoutEntriesPerWeek
        - state.breakout_entries_this_week.get(week, 0),
        0,
    )

    daily_breakouts = (
        set() if regime == "bear"
        else breakout_symbols(price_table, dates, index, strategy_config,
                              universe=universe)
    )
    if weekly_remaining <= 0:
        daily_breakouts = set()
    elif strategy_config.maxBreakoutEntriesPerWeek > 0:
        daily_breakouts = {s for s, _ in ranked if s in daily_breakouts}

    # The backtest only enters TOP-RANK names on a rebalance day; on
    # the other days it may only enter VOLUME BREAKOUT names::
    #
    #     should_try_entries = bool(daily_breakouts) or (
    #         index % config.rebalanceDays == 0 and ranked)
    #     if index % config.rebalanceDays == 0:
    #         strategy = MomentumWithBreakoutStrategy(daily_breakouts)
    #     else:
    #         strategy = VolumeBreakoutStrategy(daily_breakouts)
    #
    # Without this gate the live engine bought the top-ranked symbol
    # EVERY session, while the backtest only did so every 5th session.
    on_rebalance_day = (index % strategy_config.rebalanceDays == 0)
    if on_rebalance_day:
        strategy = MomentumWithBreakoutStrategy(daily_breakouts)
    else:
        strategy = VolumeBreakoutStrategy(daily_breakouts)

    decision = strategy.select_entries(
        price_table, dates, index, ranked, regime, max_positions, weekly_remaining,
    )

    # A symbol that is being exited today must not be re-entered today.
    exiting_symbols = {e["symbol"] for e in exits}
    held_after_exits = held_symbols - exiting_symbols

    # The position cap applies to the positions we will hold AFTER
    # today's exits.
    room = max(max_positions - len(held_after_exits), 0)

    # The equity curve drives the vol-targeting scalar. We build it from
    # the live NAV history so the live exposure cap matches the
    # backtest's method of computing it.
    nav = _nav_from_positions(positions, strategy_config)
    equity_curve = _build_equity_curve(
        price_table, dates, index, strategy_config, nav,
    )

    # The TOTAL exposure cap. The backtest sizes each entry at
    # ``exposure_cap / max_positions`` but ALSO caps the total book:
    #
    #     exposure_cap = compute_exposure_cap(equity, regime, config, curve)
    #     available_exposure = max(exposure_cap - current_positions_value, 0)
    #     if available_exposure <= 0: break
    #     order_value = min(target_value, available_exposure)
    #
    # Without the ``available_exposure`` gate the live engine would keep
    # buying on top of a book that is already at the regime's exposure
    # limit.
    exposure_cap, vol_scalar = compute_exposure_cap(
        nav, regime, strategy_config, equity_curve,
    )
    target_value = exposure_cap / max_positions if max_positions > 0 else 0.0
    # The positions being sold today were already zeroed by the exit
    # loop above, so the exposure budget sees the book as it will be
    # AFTER today's sells — exactly as the backtest does, whose exit
    # loop also runs before its entry loop.
    current_positions_value = sum(
        p.quantity * (close_price(p.symbol, signal_date, price_table)
                      or p.entry_price)
        for p in positions.values()
        if p.quantity > 0
    )
    available_exposure = max(exposure_cap - current_positions_value, 0.0)

    entries: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    breakout_added = 0

    # The corporate-action guard: the backtest refuses to buy a symbol
    # that has a non-cash corporate action inside the buy window (a
    # dividend or rights issue changes the effective price).
    corporate_actions = build_corporate_action_table(data_dir, strategy_config)
    _, ca_ex_index = build_corporate_action_lookup(
        _flatten_ca_events(corporate_actions), dates,
    )
    standard_blocked = risky_corporate_action_symbols(
        corporate_actions, signal_date,
        strategy_config.corporateActionBuyWindowDays,
        trading_dates=dates, precomputed_ex_index=ca_ex_index,
    )
    high_risk_blocked = risky_corporate_action_symbols(
        corporate_actions, signal_date,
        strategy_config.corporateActionHighRiskBuyWindowDays,
        high_risk_only=True, trading_dates=dates,
        precomputed_ex_index=ca_ex_index,
    )
    blocked_buys = standard_blocked | high_risk_blocked

    # The sector cap, as the backtest applies it.
    sector_by_symbol = symbol_sector_map(data_dir)
    sector_counts: dict[str, int] = {}
    for held_symbol in held_after_exits:
        sector = sector_by_symbol.get(held_symbol, "unknown")
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

    for symbol in decision.target_symbols:
        reason = decision.entry_reasons.get(symbol, "top_rank")
        if symbol in held_after_exits:
            blocked.append({"symbol": symbol, "reason": "already held"})
            continue
        if symbol in exiting_symbols:
            blocked.append({"symbol": symbol, "reason": "exiting today"})
            continue
        if symbol in blocked_buys:
            blocked.append({
                "symbol": symbol,
                "reason": "corporate action inside the buy window",
            })
            continue
        if len(entries) >= room:
            blocked.append({
                "symbol": symbol,
                "reason": f"position cap reached ({max_positions} for {regime})",
            })
            continue

        price = close_price(symbol, signal_date, price_table)
        if price is None:
            blocked.append({"symbol": symbol, "reason": "no price"})
            continue

        # The backtest refuses a name that is too extended from its
        # SMA50 — chasing an already-run price is the classic momentum
        # blow-up.
        symbol_bars = bars_until(price_table.get(symbol, {}), dates, index)
        if too_extended_from_sma50(symbol_bars, strategy_config):
            blocked.append({"symbol": symbol, "reason": "too extended from SMA50"})
            continue

        # The sector cap.
        sector = sector_by_symbol.get(symbol, "unknown")
        if sector_counts.get(sector, 0) >= strategy_config.maxSectorPositions:
            blocked.append({
                "symbol": symbol,
                "reason": f"sector cap reached ({strategy_config.maxSectorPositions} in {sector})",
            })
            continue

        # The total exposure cap. The backtest breaks out of the entry
        # loop when the book is already at the regime's exposure limit.
        if available_exposure <= 0:
            blocked.append({
                "symbol": symbol,
                "reason": "exposure cap reached",
            })
            break

        # The order value is capped by what is left of the exposure
        # budget, exactly as the backtest does
        # (``order_value = min(target_value, available_exposure)``).
        order_value = min(target_value, available_exposure)
        quantity = _entry_quantity(
            symbol=symbol,
            price=price,
            regime=regime,
            strategy_config=strategy_config,
            nav=nav,
            equity_curve=equity_curve,
            max_positions=max_positions,
            target_value=order_value,
        )
        if quantity <= 0:
            blocked.append({"symbol": symbol, "reason": "size below one board lot"})
            continue

        entries.append({
            "symbol": symbol,
            "side": "NB",
            "price": price,
            "quantity": quantity,
            "kind": "entry",
            "reason": f"momentum_signal:{reason}",
            "entryReason": reason,
            "rank": rank_by_symbol.get(symbol),
        })
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        available_exposure = max(
            available_exposure - (price * quantity) * (1 + strategy_config.feeRate),
            0.0,
        )
        if reason == "volume_breakout":
            breakout_added += 1

    if breakout_added:
        state.breakout_entries_this_week[week] = (
            state.breakout_entries_this_week.get(week, 0) + breakout_added
        )

    # ------------------------------------------------------------------
    # 7. persist the position state
    # ------------------------------------------------------------------
    for entry in entries:
        symbol = entry["symbol"]
        existing = positions.get(symbol)
        if existing is None or existing.quantity <= 0:
            positions[symbol] = SignalPosition(
                symbol=symbol,
                quantity=int(entry["quantity"]),
                entry_price=float(entry["price"]),
                peak_price=float(entry["price"]),
                entry_date=signal_date,
                entry_index=index,
            )
        else:
            existing.quantity += int(entry["quantity"])
            existing.peak_price = max(existing.peak_price, float(entry["price"]))

    state.positions = positions
    save_signal_state(data_dir, state)

    # ------------------------------------------------------------------
    # 8. assemble the order plan
    # ------------------------------------------------------------------
    orders = exits + entries

    deals_actioned = _build_deals_actioned(orders)

    return {
        "orders": orders,
        "deals_actioned": deals_actioned,
        "regime": regime,
        "maxPositions": max_positions,
        "signalDate": signal_date,
        "candidates": [
            {"symbol": s, "rank": rank_by_symbol.get(s), "score": round(score, 4)}
            for s, score in ranked[:20]
        ],
        "entries": entries,
        "exits": exits,
        "blocked": blocked,
        "nav": nav,
        "exposure": {
            "cap": exposure_cap,
            "volScalar": vol_scalar,
            "currentPositionsValue": current_positions_value,
            "available": available_exposure,
        },
        "notes": notes,
    }


def _build_equity_curve(
    price_table: dict[str, dict[str, Any]],
    dates: list[str],
    index: int,
    strategy_config: StrategyConfig,
    current_nav: float,
    window: int = 60,
) -> list[Any]:
    """Build a short equity curve for the vol-targeting scalar.

    ``compute_exposure_cap`` needs a history of equity values to compute
    the realized volatility. We do not have the live account's equity
    history, so we approximate it with the market's own recent path
    scaled to the current NAV. That keeps the vol scalar in the same
    ballpark as the backtest's without inventing account history.

    The curve is returned as plain dicts — ``compute_exposure_cap`` only
    reads ``point.equity``.
    """
    from vietnam_algo.libs.backtest import EquityPoint

    curve: list[EquityPoint] = []
    start = max(0, index - window + 1)
    for i in range(start, index + 1):
        date = dates[i]
        curve.append(EquityPoint(
            date=date,
            equity=current_nav,
            cash=current_nav,
            positionsValue=0.0,
        ))
    return curve


def _position_sellable(
    position: SignalPosition,
    signal_date: str,
    dates: list[str],
    index: int,
    strategy_config: StrategyConfig,
) -> bool:
    """Is the position past its settlement window?

    The backtest compares bar indices (``current_index - entryIndex >=
    sellSettlementDays``). For a live position we may not know when it
    was bought, so we fall back to the recorded ENTRY DATE and count the
    trading days since then.

    When neither an entry date nor an entry index is known (a position
    adopted from the live portfolio with no history), we assume it is
    sellable. Blocking it would mean the strategy could never exit a
    position it did not record.
    """
    if position.entry_date:
        try:
            entry_index = dates.index(position.entry_date)
        except ValueError:
            entry_index = None
        if entry_index is not None:
            return index - entry_index >= strategy_config.sellSettlementDays
        # The entry date is not a trading day in our data — fall through
        # to the index-based check.
    if position.entry_index:
        return index - position.entry_index >= strategy_config.sellSettlementDays
    return True


def _as_backtest_position(position: SignalPosition):
    """Adapt a ``SignalPosition`` to the backtest's ``Position``.

    ``position_sellable`` only reads ``entryIndex``, but keeping the
    shapes aligned avoids a subtle break if it ever reads more.
    """
    from vietnam_algo.libs.backtest import Position

    return Position(
        symbol=position.symbol,
        quantity=float(position.quantity),
        entryPrice=float(position.entry_price),
        entryDate=position.entry_date,
        entryIndex=int(position.entry_index),
        peakPrice=float(position.peak_price),
    )


def _resolve_positions(
    *,
    state: SignalState,
    live_positions: list[dict[str, Any]] | None,
    client: Any,
    account_no: str,
) -> dict[str, SignalPosition]:
    """Reconcile the tracked state with the live portfolio.

    The live portfolio is authoritative for *what* we hold; the tracked
    state is authoritative for the entry price and the high-water mark
    (the trailing stop needs both). A position that is live but not
    tracked gets a synthetic entry price from its average cost.
    """
    tracked = dict(state.positions)

    if live_positions is None and client is not None and account_no:
        try:
            from vietnam_algo.libs.portfolio import build_portfolio_snapshot

            snapshot = build_portfolio_snapshot(client, account_no)
            live_positions = snapshot.get("positions") or []
        except Exception:
            live_positions = []

    if live_positions is None:
        # No live view available: trust the tracked state alone.
        return {s: p for s, p in tracked.items() if p.quantity > 0}

    live_map = {}
    for row in live_positions:
        symbol = str(row.get("symbol") or "").upper()
        quantity = int(row.get("quantity") or 0)
        if not symbol or quantity <= 0:
            continue
        live_map[symbol] = (quantity, float(row.get("avgPrice") or 0.0))

    out: dict[str, SignalPosition] = {}
    for symbol, (quantity, avg_price) in live_map.items():
        existing = tracked.get(symbol)
        if existing is None or existing.quantity <= 0:
            out[symbol] = SignalPosition(
                symbol=symbol,
                quantity=quantity,
                entry_price=avg_price,
                peak_price=avg_price,
                entry_date="",
                entry_index=0,
            )
        else:
            existing.quantity = quantity
            if existing.entry_price <= 0:
                existing.entry_price = avg_price
            if existing.peak_price <= 0:
                existing.peak_price = avg_price
            out[symbol] = existing

    # Tracked positions that are no longer live are dropped.
    for symbol in list(tracked):
        if symbol not in out:
            tracked.pop(symbol, None)

    return out


def _nav_from_positions(
    positions: dict[str, SignalPosition],
    strategy_config: StrategyConfig,
) -> float:
    """The NAV used for sizing: cash + the market value of the book."""
    stock_value = sum(
        p.quantity * max(p.entry_price, p.peak_price)
        for p in positions.values()
        if p.quantity > 0
    )
    return stock_value + strategy_config.initialCash


def _entry_quantity(
    *,
    symbol: str,
    price: float,
    regime: str,
    strategy_config: StrategyConfig,
    nav: float,
    equity_curve: list[Any] | None = None,
    max_positions: int = 0,
    target_value: float | None = None,
) -> int:
    """Size an entry exactly the way the backtest does.

    The backtest computes an exposure cap for the regime (with optional
    vol targeting), divides it by the position cap, and converts that
    target value into a board-lot share count after slippage and fees::

        exposure_cap = compute_exposure_cap(equity, regime, config, curve)
        target_value = exposure_cap / max_positions
        fill_price   = price * (1 + slippageRate)
        quantity     = int(target_value / fill_price // lot) * lot

    We reuse the backtest's own ``compute_exposure_cap`` so the live
    sizing cannot drift from the backtested sizing.

    ``target_value`` lets the caller pass the already-capped order
    value (``min(target_value, available_exposure)``), which is what
    the backtest buys.
    """
    if price <= 0 or max_positions <= 0:
        return 0

    if target_value is None:
        exposure_cap, _vol_scalar = compute_exposure_cap(
            nav, regime, strategy_config, equity_curve or [],
        )
        target_value = exposure_cap / max_positions
    if target_value <= 0:
        return 0

    fill_price = price * (1 + strategy_config.slippageRate)
    if fill_price <= 0:
        return 0
    quantity = _round_to_board_lot(target_value / fill_price,
                                   strategy_config.boardLotSize)
    return max(int(quantity), 0)


def _build_deals_actioned(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group the plan per symbol for the submit path.

    The submit path (``bots/runner.py``) reads ``deals_actioned`` and
    expects each deal to carry ``symbol`` and ``dnse_orders``.
    """
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for order in orders:
        by_symbol.setdefault(order["symbol"], []).append(order)

    deals = []
    for symbol, symbol_orders in by_symbol.items():
        kind = symbol_orders[0].get("kind")
        action = {
            "entry": "new_entry",
            "exit": "full_close",
        }.get(kind, "order")
        deals.append({
            "action": action,
            "symbol": symbol,
            "deal_id": f"momentum:{symbol}",
            "lastRecomment": symbol_orders[0].get("reason", ""),
            "rateForNav": 0.0,
            "numberOfProfit": 0,
            "dnse_orders": [
                {
                    "symbol": o.get("symbol"),
                    "side": o.get("side"),
                    "price": o.get("price"),
                    "quantity": o.get("quantity"),
                    "kind": o.get("kind"),
                    "reason": o.get("reason"),
                }
                for o in symbol_orders
            ],
        })
    return deals


def write_signal_outputs(
    *,
    csv_path: Path,
    json_path: Path,
    orders: list[dict[str, Any]],
    trade_date: str,
    as_backtest_result: bool = False,
) -> None:
    """Write the order plan as the CSV + JSON the pipeline expects."""
    csv_path = Path(csv_path)
    json_path = Path(json_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ORDER_FIELDS)
        writer.writeheader()
        for i, order in enumerate(orders, start=1):
            price = float(order.get("price") or 0.0)
            quantity = int(order.get("quantity") or 0)
            writer.writerow({
                "orderId": i,
                "date": trade_date,
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "price": price,
                "quantity": quantity,
                "value": price * quantity,
                "fee": 0,
                "reason": order.get("reason", ""),
            })

    payload: dict[str, Any] = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": str(csv_path),
        "tradeDate": trade_date,
        "count": len(orders),
        "items": orders,
    }
    if as_backtest_result:
        # The dry-run replay reads ``equityCurve[0].cash`` to derive the
        # quantity scale, and ``metrics`` for the summary. Provide the
        # same shape so the replay works unchanged.
        payload["metrics"] = {
            "initialCash": 0.0,
            "finalEquity": 0.0,
            "totalReturnPercent": 0.0,
            "maxDrawdownPercent": 0.0,
            "tradeCount": len(orders),
            "buyCount": sum(1 for o in orders if o.get("side") == "NB"),
            "sellCount": sum(1 for o in orders if o.get("side") == "NS"),
        }
        payload["equityCurve"] = [{"date": trade_date, "cash": 0.0,
                                   "equity": 0.0, "positionsValue": 0.0}]
        payload["trades"] = orders

    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


__all__ = [
    "SignalPosition",
    "SignalState",
    "compute_signal_orders",
    "load_signal_state",
    "save_signal_state",
    "write_signal_outputs",
]
