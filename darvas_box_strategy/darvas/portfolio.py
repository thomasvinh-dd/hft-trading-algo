"""Portfolio: separate from the signal layer.

A Portfolio takes a stream of Signals and decides:
  1. WHETHER to act on each signal (cash, position limits, cooldown)
  2. HOW MUCH to allocate (sizing: fixed % of NAV, vol-targeted, etc.)
  3. WHEN to close (TP ladder, trailing stop, hard stop, time stop)

The portfolio owns:
  - Cash + NAV accounting
  - Open positions and their running state
  - Closed trades (history)
  - Sizing rules
  - Order generation (the output)

This is a clean re-implementation of the backtest's _portfolio_layer
as a proper class. The old code mixed the day-by-day loop with
trade simulation; here they're separated:
  - The Portfolio handles bar-by-bar accounting
  - The signal-level exit logic (TP/SL/trail) lives in executor.py
    as a pure function that operates on a Position
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .config import StrategyConfig
from .signals import Signal

log = logging.getLogger("darvas.portfolio")


# ---------- Data types ----------

@dataclass(frozen=True)
class Order:
    """A decision by the portfolio to buy or sell.

    The portfolio emits Orders; the broker / simulator fills them.
    This is the "what we want to do" — not necessarily what happened.
    """
    ticker: str
    side: str           # "buy" | "sell"
    shares: int
    price: float        # expected fill price (entry_price or stop/TP level)
    kind: str           # "ENTRY" | "TIER" | "TRAIL" | "SL_HARD" | "SL_SCALE" | "TIME_STOP"
    reason: str = ""

    def __str__(self) -> str:
        return (
            f"Order({self.side} {self.shares} {self.ticker} @ {self.price:,.0f} "
            f"[{self.kind}{(': ' + self.reason) if self.reason else ''}])"
        )


@dataclass
class TradeFill:
    """A single fill event in a trade's history.

    Used to render the deal-detail timeline: every entry, partial exit,
    full exit, and dividend is recorded. The list of TradeFills on a
    Position/Trade shows the full process from signal to final exit.
    """
    date: object           # when the fill happened
    kind: str              # ENTRY | TIER | TRAIL | SL_HARD | SL_SCALE | TIME_STOP | DIVIDEND
    price: float           # fill price (or dividend per share for DIVIDEND)
    shares: int            # shares bought (+) or sold (-). 0 for DIVIDEND.
    pnl_delta: float       # realized PnL change from this fill (0 for ENTRY)
    cumulative_pnl: float  # running PnL after this fill
    remaining: int         # shares still held after this fill
    note: str = ""         # extra context (e.g. "TP hit", "Hard stop")

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat() if hasattr(self.date, "isoformat") else str(self.date),
            "kind": self.kind,
            "price": self.price,
            "shares": self.shares,
            "pnl_delta": self.pnl_delta,
            "cumulative_pnl": self.cumulative_pnl,
            "remaining": self.remaining,
            "note": self.note,
        }


@dataclass
class Position:
    """An open position held by the portfolio.

    Mutable: the portfolio updates last_close, peak_close, remaining,
    cumulative_pnl, etc. as new bars come in. The `fills` list is the
    bar-by-bar history used to render the deal-detail timeline.
    """
    ticker: str
    signal: Signal
    shares: int
    entry_date: object
    entry_price: float
    cost_basis: float               # shares * entry_price (no commission yet)
    # Running state
    remaining: int
    peak_close: float
    last_close: float
    trailing_active: bool = False
    trail_floor: float = 0.0
    tier_idx: int = 0
    bars_held: int = 0
    cumulative_proceeds: float = 0.0
    cumulative_pnl: float = 0.0
    realized_fees: float = 0.0
    dividend_income: float = 0.0
    # For trade history reconstruction
    exit_date: Optional[object] = None
    exit_reason: Optional[str] = None
    # Bar-by-bar event history (for the deal-detail timeline)
    fills: list = field(default_factory=list)


@dataclass
class Trade:
    """A closed position in the portfolio's history.

    The `fills` list is the bar-by-bar event log (entry, tier exits,
    SL hits, trail, dividends). Renders the deal-detail timeline.
    """
    ticker: str
    signal: Signal
    entry_date: object
    entry_price: float
    shares: int
    exit_date: object
    exit_price: float
    exit_reason: str
    realized_pnl: float
    return_pct: float
    cost_paid: float
    bars_held: int
    highest_close: float
    last_close: float
    dividend_income: float = 0.0
    # Bar-by-bar timeline (list of TradeFill dicts)
    fills: list = field(default_factory=list)

    @property
    def is_winner(self) -> bool:
        return self.realized_pnl > 0

    @property
    def deal_id(self) -> str:
        """Stable identifier for this trade."""
        ed = self.entry_date
        if hasattr(ed, "isoformat"):
            ed = ed.isoformat()
        return f"{self.ticker}_{ed}"


# ---------- Sizing ----------

def fixed_pct_size_pct(conviction: str, cfg: StrategyConfig) -> float:
    """Conviction-based sizing: size_high for high-volume breakouts,
    size_low for normal ones. The "2x" in the comments."""
    if conviction == "high":
        return cfg.backtest.size_high
    return cfg.backtest.size_low


def size_position(
    signal: Signal, nav: float, cfg: StrategyConfig,
) -> Tuple[int, float]:
    """Compute (shares, size_pct) for a signal.

    Returns (0, 0.0) if the position is too small for one board lot.
    Applies:
      - Fixed-pct sizing by conviction (default)
      - Vol-targeted sizing if cfg.backtest.vol_target_pct > 0
      - Board-lot rounding (HOSE = 100 shares)
      - Max-volume cap (5% of avg daily volume)
    """
    if cfg.backtest.vol_target_pct > 0:
        size_pct = _vol_targeted_size_pct(signal, cfg)
    else:
        size_pct = fixed_pct_size_pct(signal.conviction, cfg)

    position_value = nav * size_pct
    entry_price = signal.entry_price
    raw_shares = int(position_value // entry_price)
    if raw_shares <= 0:
        return 0, 0.0

    # Round down to a board lot. HOSE = 100 shares.
    lot = cfg.backtest.board_lot if cfg.backtest.board_lot > 0 else 1
    shares = (raw_shares // lot) * lot
    if shares <= 0:
        return 0, 0.0

    # Max-volume cap (5% of average daily volume)
    avg_vol = signal.extra.get("avg_vol", 0) if signal.extra else 0
    max_volume = int(avg_vol * 0.05) if avg_vol else 0
    if max_volume > 0 and shares > max_volume:
        shares = (max_volume // lot) * lot
        if shares <= 0:
            return 0, 0.0

    return shares, size_pct


def _vol_targeted_size_pct(signal: Signal, cfg: StrategyConfig) -> float:
    """Volatility-targeted sizing: scale to a target portfolio volatility.

    Higher ATR → smaller position; lower ATR → larger position. Clamped
    between vol_target_floor and vol_target_ceiling.
    """
    if signal.atr_value is None or signal.entry_price <= 0:
        return fixed_pct_size_pct(signal.conviction, cfg)
    # Annualized position vol = (ATR / price) * sqrt(252)
    # Position size such that (size_pct * pos_vol) = vol_target_pct
    pos_vol = (signal.atr_value / signal.entry_price) * (252 ** 0.5)
    if pos_vol <= 0:
        return fixed_pct_size_pct(signal.conviction, cfg)
    raw = cfg.backtest.vol_target_pct / pos_vol
    return max(
        cfg.backtest.vol_target_floor,
        min(cfg.backtest.vol_target_ceiling, raw),
    )


# ---------- Portfolio ----------

class Portfolio:
    """A trading portfolio that takes signals and manages positions.

    Lifecycle:
        portfolio = Portfolio(cfg, start_nav=1e9)
        for signal in signal_stream:
            order = portfolio.process_signal(signal)
            if order:
                print(f"  → {order}")
        for date, prices in bar_stream:
            orders = portfolio.process_bar(date, prices)
            for order in orders:
                print(f"  → {order}")
        portfolio.close()  # force-close any remaining positions
    """

    def __init__(self, cfg: StrategyConfig, start_nav: float = 1e9):
        self.cfg = cfg
        self.start_nav = start_nav
        self.cash = float(start_nav)
        self.open_positions: Dict[str, Position] = {}
        self.history: List[Trade] = []
        self._cooldown_until: Dict[str, date] = {}
        # Stats
        self.peak_concurrent = 0
        self.skipped_signals = 0
        self._daily_nav: List[Tuple[date, float]] = []
        self._current_date: Optional[date] = None
        self._current_prices: Dict[str, float] = {}

    # ----- Read-only accessors -----

    def get_nav(self) -> float:
        return self.cash + sum(
            p.shares * p.last_close for p in self.open_positions.values()
        )

    def get_open_positions(self) -> List[Position]:
        return list(self.open_positions.values())

    def get_history(self) -> List[Trade]:
        return list(self.history)

    def get_daily_nav(self) -> List[Tuple[date, float]]:
        return list(self._daily_nav)

    def get_daily_returns(self) -> List[float]:
        returns: List[float] = []
        prev = self.start_nav
        for _, nav in self._daily_nav:
            if prev > 0:
                returns.append(nav / prev - 1.0)
            prev = nav
        return returns

    # ----- Signal handling -----

    def process_signal(self, signal: Signal) -> Optional[Order]:
        """Evaluate a signal and return an Order to buy, or None.

        Skips if:
          - Ticker is already open
          - Ticker is in cooldown (after a stop-out)
          - Max concurrent positions reached
          - Insufficient cash
        """
        ticker = signal.ticker
        if ticker in self.open_positions:
            self.skipped_signals += 1
            return None
        if self._cooldown_until.get(ticker, date(1970, 1, 1)) > (
            signal.entry_date if hasattr(signal.entry_date, "isoformat")
            else date.today()
        ):
            self.skipped_signals += 1
            return None
        if len(self.open_positions) >= self.cfg.backtest.max_concurrent_positions:
            self.skipped_signals += 1
            return None

        nav = self.get_nav()
        shares, size_pct = size_position(signal, nav, self.cfg)
        if shares <= 0:
            self.skipped_signals += 1
            return None

        cost = shares * signal.entry_price
        if cost > self.cash:
            self.skipped_signals += 1
            return None

        # Open the position
        self.cash -= cost
        pos = Position(
            ticker=ticker,
            signal=signal,
            shares=shares,
            entry_date=signal.entry_date,
            entry_price=signal.entry_price,
            cost_basis=cost,
            remaining=shares,
            peak_close=signal.entry_price,
            last_close=signal.entry_price,
        )
        # Record the ENTRY event for the deal-detail timeline
        pos.fills.append(TradeFill(
            date=signal.entry_date,
            kind="ENTRY",
            price=signal.entry_price,
            shares=shares,
            pnl_delta=0.0,
            cumulative_pnl=0.0,
            remaining=shares,
            note=f"Buy {shares:,} @ {signal.entry_price:,.0f} (size={size_pct:.0%})",
        ))
        self.open_positions[ticker] = pos
        self.peak_concurrent = max(self.peak_concurrent, len(self.open_positions))

        return Order(
            ticker=ticker,
            side="buy",
            shares=shares,
            price=signal.entry_price,
            kind="ENTRY",
            reason=f"signal @ {signal.entry_price:,.0f} size={size_pct:.0%}",
        )

    # ----- Bar handling -----

    def process_bar(
        self, bar_date, prices_by_ticker: Dict[str, float],
    ) -> List[Order]:
        """Process one bar of market data.

        Updates MTM, runs each open position through the exit logic,
        and returns any sell orders generated. Advances state.
        """
        self._current_date = bar_date
        self._current_prices = dict(prices_by_ticker)
        orders: List[Order] = []

        # 1) Close any positions whose exit_date has been reached.
        #    (The position-tracking layer doesn't enforce exit_date —
        #    that's the exit logic in _apply_exits. But if a Position
        #    was marked with exit_date externally, close it here.)
        for ticker in list(self.open_positions.keys()):
            pos = self.open_positions[ticker]
            if pos.exit_date and bar_date >= pos.exit_date:
                order = self._close_position(pos, exit_price=pos.last_close, reason="EXTERNAL")
                if order:
                    orders.append(order)

        # 2) Apply the exit logic to each open position.
        for ticker in list(self.open_positions.keys()):
            pos = self.open_positions[ticker]
            price = prices_by_ticker.get(ticker)
            if price is None:
                continue
            pos.last_close = price
            pos.peak_close = max(pos.peak_close, price)
            pos.bars_held += 1

            exit_orders = self._apply_exits(pos, price, bar_date)
            orders.extend(exit_orders)

        # 3) Record daily NAV
        self._daily_nav.append((bar_date, self.get_nav()))
        return orders

    def _apply_exits(
        self, pos: Position, price: float, bar_date,
    ) -> List[Order]:
        """Run the signal-level exit logic on one position.

        Returns 0 or more sell Orders. Mutates pos in-place.
        The order of checks: trail > tier > sl_T > sl_F > time stop.
        """
        sig = pos.signal
        cfg = self.cfg
        exits_cfg = cfg.exits
        trail_cfg = cfg.trailing
        cost_rate = cfg.backtest.cost_bps / 10_000.0
        lot = cfg.backtest.board_lot if cfg.backtest.board_lot > 0 else 1
        daily_limit = cfg.backtest.daily_price_limit_pct
        # T+2.5 settlement: lock-up first N-1 bars
        t_plus_n = max(0, exits_cfg.t_plus_n_bars - 1) if exits_cfg.t_plus_n_bars > 0 else 0

        # Cap the close to the daily price limit (modeled separately
        # from the actual price; here we use price directly).
        effective_price = price
        if daily_limit > 0 and pos.last_close > 0:
            upper = pos.last_close * (1.0 + daily_limit)
            lower = pos.last_close * (1.0 - daily_limit)
            if effective_price > upper:
                effective_price = upper
            elif effective_price < lower:
                effective_price = lower

        pos.last_close = effective_price
        orders: List[Order] = []

        # T+2.5 lock-up
        if pos.bars_held <= t_plus_n:
            return orders

        # Time stop
        max_hold = getattr(cfg.backtest, "max_holding_bars", 0) or 0
        if (max_hold > 0 and pos.remaining > 0
                and pos.bars_held >= max_hold
                and effective_price < sig.entry_price):
            order = self._sell_partial(
                pos, sig.entry_price, pos.remaining, "TIME_STOP",
                bar_date, cost_rate, effective_price,
            )
            orders.append(order)
            return orders

        # Trailing stop (only after first TP hit activates it)
        if pos.trailing_active and pos.remaining > 0:
            if trail_cfg.trail_mode == "atr" and sig.atr_value is not None:
                trail_threshold = pos.peak_close - sig.atr_value * trail_cfg.trail_atr_mult
            else:
                trail_threshold = pos.peak_close * (1.0 - trail_cfg.trail_from_peak_pct)
            if trail_cfg.trail_floor_lock_profit:
                trail_threshold = max(trail_threshold, pos.trail_floor)
            if effective_price <= trail_threshold:
                order = self._sell_partial(
                    pos, effective_price, pos.remaining, "TRAIL",
                    bar_date, cost_rate, effective_price,
                )
                orders.append(order)
                return orders

        # Take-profit ladder
        while pos.tier_idx < len(sig.tiers) and pos.remaining > 0:
            mult, pct = sig.tiers[pos.tier_idx]
            tier_price = sig.entry_price * mult
            if effective_price < tier_price:
                break
            tier_shares = max(int(pos.remaining * pct), 0)
            # Don't leave orphan shares < lot
            if pos.remaining - tier_shares < lot:
                tier_shares = pos.remaining
            if tier_shares <= 0:
                pos.tier_idx += 1
                continue
            order = self._sell_partial(
                pos, tier_price, tier_shares, "TIER",
                bar_date, cost_rate, effective_price,
            )
            orders.append(order)
            if pos.tier_idx == trail_cfg.activation_tier_idx:
                pos.trailing_active = True
                if trail_cfg.trail_floor_lock_profit:
                    pos.trail_floor = max(pos.trail_floor, tier_price)
            pos.tier_idx += 1
            if pos.remaining <= 0:
                return orders

        if pos.remaining <= 0:
            return orders

        # Partial-cut stop (sl_T)
        if effective_price <= sig.sl_T:
            cut = max(int(pos.remaining * exits_cfg.scale_out_pct_at_sl_T), 0)
            if cut <= 0:
                cut = pos.remaining
            if cut > 0:
                order = self._sell_partial(
                    pos, sig.sl_T, cut, "SL_SCALE",
                    bar_date, cost_rate, effective_price,
                )
                orders.append(order)
                if pos.remaining <= 0:
                    return orders

        # Midpoint stop
        sl_mid = sig.sl_midpoint
        if effective_price <= sl_mid and pos.remaining > 0:
            cut = max(int(pos.remaining * exits_cfg.scale_out_pct_at_midpoint), 0)
            if cut <= 0:
                cut = pos.remaining
            if cut > 0:
                order = self._sell_partial(
                    pos, sl_mid, cut, "SL_SCALE",
                    bar_date, cost_rate, effective_price,
                )
                orders.append(order)
                if pos.remaining <= 0:
                    return orders

        # Hard stop
        if effective_price <= sig.sl_F and pos.remaining > 0:
            order = self._sell_partial(
                pos, sig.sl_F, pos.remaining, "SL_HARD",
                bar_date, cost_rate, effective_price,
            )
            orders.append(order)

        return orders

    def _sell_partial(
        self, pos: Position, fill_price: float, shares: int, kind: str,
        bar_date, cost_rate: float, effective_close: float,
    ) -> Optional[Order]:
        """Record a partial (or full) sale. Returns the Order, or None.

        Updates position state, cumulative PnL, and (if full close)
        moves the position to history.
        """
        if shares <= 0 or pos.remaining <= 0:
            return None
        shares = min(shares, pos.remaining)
        proceeds = shares * fill_price
        cost = proceeds * cost_rate * 0.5
        pnl = (fill_price - pos.signal.entry_price) * shares
        net_proceeds = proceeds - cost
        pos.cumulative_proceeds += net_proceeds
        pos.cumulative_pnl += pnl - cost
        pos.realized_fees += cost
        pos.remaining -= shares
        order = Order(
            ticker=pos.ticker,
            side="sell",
            shares=shares,
            price=fill_price,
            kind=kind,
            reason=f"close {shares} @ {fill_price:,.0f}",
        )
        # Record this fill in the position's timeline
        pos.fills.append(TradeFill(
            date=bar_date,
            kind=kind,
            price=fill_price,
            shares=-shares,  # negative = sold
            pnl_delta=pnl - cost,
            cumulative_pnl=pos.cumulative_pnl,
            remaining=pos.remaining,
            note=f"Sell {shares:,} @ {fill_price:,.0f}",
        ))
        if pos.remaining <= 0:
            self._archive_position(pos, fill_price, kind, bar_date)
        return order

    def _archive_position(
        self, pos: Position, exit_price: float, exit_reason: str,
        bar_date,
    ) -> None:
        """Move a fully-closed position into history and update cash."""
        self.cash += pos.cumulative_proceeds
        avg_exit_price = (
            pos.signal.entry_price + pos.cumulative_pnl / pos.shares
        )
        trade = Trade(
            ticker=pos.ticker,
            signal=pos.signal,
            entry_date=pos.entry_date,
            entry_price=pos.signal.entry_price,
            shares=pos.shares,
            exit_date=bar_date,
            exit_price=avg_exit_price,
            exit_reason=exit_reason,
            realized_pnl=pos.cumulative_pnl,
            return_pct=pos.cumulative_pnl / pos.cost_basis if pos.cost_basis else 0.0,
            cost_paid=pos.realized_fees,
            bars_held=pos.bars_held,
            highest_close=pos.peak_close,
            last_close=pos.last_close,
            dividend_income=pos.dividend_income,
            fills=list(pos.fills),  # copy the timeline
        )
        self.history.append(trade)

        # Apply cooldown if the exit was a stop
        cooldown_days = self.cfg.backtest.cooldown_bars_after_stop
        if cooldown_days > 0 and exit_reason in ("SL_HARD", "SL_SCALE"):
            from datetime import timedelta
            cd_end = bar_date + timedelta(days=cooldown_days)
            self._cooldown_until[pos.ticker] = cd_end

        # Remove from open positions
        del self.open_positions[pos.ticker]

    def _close_position(
        self, pos: Position, exit_price: float, reason: str,
    ) -> Optional[Order]:
        """Force-close a position (e.g. for backtest end-of-data)."""
        shares = pos.remaining
        if shares <= 0:
            return None
        return self._sell_partial(
            pos, exit_price, shares, reason, exit_date := pos.exit_date or self._current_date,
            0.0, exit_price,
        )

    def close_all(self) -> List[Order]:
        """Force-close all open positions (e.g. at backtest end)."""
        orders: List[Order] = []
        for ticker in list(self.open_positions.keys()):
            pos = self.open_positions[ticker]
            order = self._sell_partial(
                pos, pos.last_close, pos.remaining, "EXHAUST",
                self._current_date or date.today(), 0.0, pos.last_close,
            )
            if order:
                orders.append(order)
        return orders

    def __repr__(self) -> str:
        return (
            f"Portfolio(nav={self.get_nav():,.0f}, "
            f"cash={self.cash:,.0f}, "
            f"open={len(self.open_positions)}, "
            f"history={len(self.history)})"
        )

    def to_backtest_result(self, *, start_date=None, end_date=None):
        """Convert to a BacktestResult for compatibility with the old API.

        This lets callers that expect BacktestResult continue to work
        while the new Portfolio is the source of truth. The trade list
        comes from portfolio.history (closed trades), the nav curve
        from portfolio._daily_nav, and the open positions from
        portfolio.open_positions.
        """
        from .backtest import BacktestResult
        result = BacktestResult(portfolio_mode=True)
        result.trades = list(self.history)
        result.nav_curve = list(self._daily_nav)
        result.daily_returns = self.get_daily_returns()
        result.concurrent_position_peak = self.peak_concurrent
        result.concurrent_skipped = self.skipped_signals
        if self._daily_nav:
            result.start = self._daily_nav[0][0]
            result.end = self._daily_nav[-1][0]
        elif start_date and end_date:
            result.start = start_date
            result.end = end_date
        # Note: portfolio_open_positions remains empty in this
        # serialization — the new Portfolio doesn't need a separate
        # "open" dict because positions are stored in self.open_positions.
        return result


__all__ = [
    "Order",
    "TradeFill",
    "Position",
    "Trade",
    "Portfolio",
    "size_position",
    "fixed_pct_size_pct",
]
