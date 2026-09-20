"""Backtest engine.

Two-pass design:
  1. Collect signals + simulate trades (same as the legacy version)
  2. Portfolio layer: replay trades day-by-day, track concurrent
     positions, recover cash on exit, enforce position-count limits.

For the audit run we expose a `portfolio=False` knob that runs only
the first pass (matches the old behavior, used for benchmarking).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterator, Sequence

from .base_detector import BaseScanner
from .ca_handler import (
    CAEvent,
    DEFAULT_CA_PATH,
    adjust_bars,
    build_event_index,
    load_events,
)
from .config import StrategyConfig
from .data_loader import (
    DEFAULT_PRICES_DIR,
    PriceBar,
    iter_universe,
    load_price_bars,
)
from .executor import TradeResult, simulate_trade
from .signal_generator import generate_signal


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    skipped_signals: int = 0
    start: date | None = None
    end: date | None = None
    tickers_scanned: int = 0
    bars_scanned: int = 0
    nav_curve: list = field(default_factory=list)
    daily_returns: list = field(default_factory=list)
    total_dividend_income: float = 0.0
    ca_applied: bool = False
    concurrent_position_peak: int = 0
    concurrent_skipped: int = 0
    portfolio_mode: bool = False

    @property
    def n_trades(self):
        return len(self.trades)

    @property
    def n_closed(self):
        return sum(1 for t in self.trades if t.exit_reason != "EXHAUST")

    @property
    def n_exhausted(self):
        return sum(1 for t in self.trades if t.exit_reason == "EXHAUST")


def _scan_ticker(bars, ticker, cfg, *, dividend_per_share=None, upcoming_ca=None,
                  market_bars=None, sector_momentum_by_date=None,
                  ml_model=None):
    """Yield simulated TradeResults for every signal in the bars."""
    if len(bars) < cfg.breakout.min_history_days:
        return
    scanner = BaseScanner(bars, cfg.base, ticker=ticker)
    nav = cfg.backtest.start_nav
    for i in range(cfg.breakout.min_history_days, len(bars)):
        base = scanner.find_base(as_of_idx=i)
        if base is None or base.end_idx >= i:
            continue
        signal = generate_signal(
            bars, base, nav=nav, cfg=cfg, as_of_idx=i,
            upcoming_ca=upcoming_ca, market_bars=market_bars,
            sector_momentum_by_date=sector_momentum_by_date,
            ml_model=ml_model,
        )
        if signal is None:
            continue
        result = simulate_trade(
            signal, bars[i + 1:], cfg,
            dividend_per_share=dividend_per_share,
            atr_value=signal.atr_value,
        )
        yield result


def _build_nav_curve(trades, start_nav, start, end):
    by_date = defaultdict(float)
    for t in trades:
        if t.exit_reason == "EXHAUST":
            continue
        by_date[t.exit_date] += t.realized_pnl

    if end < start:
        return [], []

    days = (end - start).days
    if days <= 0:
        return [(start, start_nav)], [0.0]

    nav = start_nav
    curve = [(start, nav)]
    daily = []
    cursor = start
    for _ in range(days):
        cursor = date.fromordinal(cursor.toordinal() + 1)
        pnl = by_date.get(cursor, 0.0)
        ret = pnl / nav if nav > 0 else 0.0
        nav += pnl
        curve.append((cursor, nav))
        daily.append(ret)
    return curve, daily


def _portfolio_layer(cfg, trades, start, end, mtm_mode="close"):
    """Replay trades day-by-day with cash + concurrent-position tracking.

    For each bar in [start, end]:
      1. Mark open positions to their latest close (MTM).
      2. Close positions whose exit_date has been reached.
      3. Open new trades whose entry_date matches (respecting
         max_concurrent_positions + cash + cooldown).

    ``mtm_mode``:
      - ``"close"``: mark open positions to the bar's close price.
      - ``"cost"``:  mark open positions at their entry cost (no
        unrealized P&L reflected in the equity curve).
    """
    cash = cfg.backtest.start_nav
    open_positions = {}  # ticker -> {shares, entry_price, exit_date, last_close, cost}
    peak_concurrent = 0
    skipped = 0
    cooldown_until = {}  # ticker -> date when cooldown expires

    # Build a fast lookup: ticker -> bars (for close-price MTM)
    # We need to read the per-ticker bars from somewhere. We pass them
    # in via the trades themselves (each trade has a ticker; we
    # re-load from the default prices dir).
    bars_cache: dict[str, list] = {}

    def get_bars(ticker: str) -> list:
        if ticker not in bars_cache:
            path = (cfg.universe_data_dir if hasattr(cfg, "universe_data_dir") else None)
            bars_cache[ticker] = []  # placeholder
        return bars_cache.get(ticker, [])

    # Group trades by entry_date for the replay loop.
    by_entry = defaultdict(list)
    for t in trades:
        by_entry[t.signal.entry_date].append(t)

    # Index trades by ticker for exit lookups.
    by_ticker_exit = {}
    for t in trades:
        if t.exit_reason == "EXHAUST":
            continue
        by_ticker_exit[(t.signal.ticker, t.exit_date)] = t

    # All trading dates.
    if end < start:
        return [], [], 0, 0
    all_dates = []
    cursor = start
    while cursor <= end:
        all_dates.append(cursor)
        cursor = date.fromordinal(cursor.toordinal() + 1)

    daily_nav = []
    cooldown_days = getattr(cfg.backtest, "cooldown_bars_after_stop", 0)

    for d in all_dates:
        # 0. MTM open positions to today's close.
        if mtm_mode == "close":
            for sym, pos in open_positions.items():
                bars = get_bars(sym)
                # find bar at date d
                for b in bars:
                    if b.date == d:
                        pos["last_close"] = b.close
                        break
        # 1. Close positions exiting today.
        to_close = []
        for sym, pos in open_positions.items():
            if pos["exit_date"] <= d:
                to_close.append(sym)
        for sym in to_close:
            pos = open_positions.pop(sym)
            # Recover proceeds at trade's realized value (entry + pnl).
            # If the trade's exit was SL_HARD or SL_SCALE, apply cooldown.
            trade = by_ticker_exit.get((sym, pos["exit_date"]))
            cash += pos["exit_value"]
            # Cooldown after stop-out (if configured and trade was a stop)
            if cooldown_days > 0 and trade is not None and trade.exit_reason in ("SL_HARD", "SL_SCALE"):
                cd_end = date.fromordinal(d.toordinal() + cooldown_days)
                cooldown_until[sym] = cd_end

        # 2. Open new trades whose entry_date matches.
        for t in by_entry.get(d, []):
            ticker = t.signal.ticker
            if ticker in open_positions:
                continue
            if cooldown_until.get(ticker, date(1970, 1, 1)) > d:
                continue
            if len(open_positions) >= cfg.backtest.max_concurrent_positions:
                skipped += 1
                continue
            cost = t.signal.shares * t.signal.entry_price
            if cost > cash:
                skipped += 1
                continue
            cash -= cost
            open_positions[ticker] = {
                "shares": t.signal.shares,
                "entry_price": t.signal.entry_price,
                "exit_date": t.exit_date,
                "last_close": t.signal.entry_price,
                "entry_value": t.entry_value,
                "exit_value": t.entry_value + t.realized_pnl,
            }
            peak_concurrent = max(peak_concurrent, len(open_positions))

        # 3. Mark-to-market.
        if mtm_mode == "close":
            mtm_value = sum(
                pos["shares"] * pos["last_close"] for pos in open_positions.values()
            )
        else:
            mtm_value = sum(pos["entry_value"] for pos in open_positions.values())
        daily_nav.append((d, cash + mtm_value))

    return daily_nav, [], peak_concurrent, skipped

def run_backtest(cfg, *, prices_dir=DEFAULT_PRICES_DIR, tickers=None,
                 progress=True, ca_events=...,
                 adjust_prices=...,
                 ca_path=None, portfolio=False, mtm_mode="close"):
    """Run backtest.

    Parameters
    ----------
    portfolio
        When True, replay trades day-by-day with cash + concurrent-
        position tracking. Default False matches the legacy
        "every signal fires" behavior — used for benchmarking.
    mtm_mode
        "close" (mark open positions at bar close) or "cost" (mark at
        entry cost, no unrealized P&L). Only used when portfolio=True.
    """
    result = BacktestResult(portfolio_mode=portfolio)

    if ca_events is ...:
        ca_events = load_events(ca_path) if ca_path else load_events(DEFAULT_CA_PATH)
    ca_index = build_event_index(ca_events) if ca_events else {}

    if adjust_prices is ...:
        adjust_prices = False
    result.ca_applied = bool(adjust_prices)

    # Load market index bars if regime filter is configured
    market_bars = None
    if cfg.breakout.regime_index_path:
        path = Path(cfg.breakout.regime_index_path)
        if path.exists():
            market_bars = load_price_bars(path)

    # Build sector momentum map if sector filter is configured
    sector_momentum_by_date = None
    sector_map = {}
    bars_by_ticker_for_sector = {}
    if cfg.breakout.sector_data_path:
        from .filters import build_sector_map
        sector_map = build_sector_map(cfg.breakout.sector_data_path)

    # Load ML model if configured
    ml_model = None
    if cfg.breakout.ml_model_path:
        from pathlib import Path as _P
        mp = _P(cfg.breakout.ml_model_path)
        if mp.exists():
            try:
                import joblib
                ml_model = joblib.load(mp)
            except Exception as e:
                print(f"Warning: failed to load ML model: {e}")

    from .data_loader import load_price_bars as _load_price_bars  # for inner branches

    if tickers is not None:
        universe = []
        for t in tickers:
            path = prices_dir / f"{t.upper()}.json"
            raw_bars = _load_price_bars(path)
            if not raw_bars:
                continue
            ticker_events = ca_index.get(t.upper(), [])
            if adjust_prices and ticker_events:
                adj_bars, div = adjust_bars(raw_bars, ticker_events)
            else:
                adj_bars, div = raw_bars, {}
            universe.append((t.upper(), adj_bars, div))
            if sector_map:
                bars_by_ticker_for_sector[t.upper()] = adj_bars
    else:
        universe = []
        for meta, raw_bars in iter_universe(prices_dir, cfg):
            ticker_events = ca_index.get(meta.ticker, [])
            if adjust_prices and ticker_events:
                adj_bars, div = adjust_bars(raw_bars, ticker_events)
            else:
                adj_bars, div = raw_bars, {}
            universe.append((meta.ticker, adj_bars, div))
            if sector_map:
                bars_by_ticker_for_sector[meta.ticker] = adj_bars

    if sector_map:
        from .filters import build_sector_momentum_by_date
        sector_momentum_by_date = build_sector_momentum_by_date(
            bars_by_ticker_for_sector, sector_map,
            window=cfg.breakout.sector_momentum_window,
        )

    # Build a bars cache for MTM lookups (only used when portfolio=True)
    bars_by_ticker = {ticker: bars for ticker, bars, _ in universe}

    total = len(universe)
    for idx, (ticker, bars, dividend_per_share) in enumerate(universe, start=1):
        result.tickers_scanned += 1
        result.bars_scanned += len(bars)
        if bars:
            if result.start is None or bars[0].date < result.start:
                result.start = bars[0].date
            if result.end is None or bars[-1].date > result.end:
                result.end = bars[-1].date
        ticker_events = ca_index.get(ticker, []) if ca_index else None
        n_before = result.n_trades
        for trade in _scan_ticker(
            bars, ticker, cfg,
            dividend_per_share=dividend_per_share,
            upcoming_ca=ticker_events,
            market_bars=market_bars,
            sector_momentum_by_date=sector_momentum_by_date,
            ml_model=ml_model,
        ):
            result.trades.append(trade)
            result.total_dividend_income += trade.dividend_income
        if progress and (idx % 25 == 0 or idx == total):
            print(
                f"[{idx:>4}/{total}] {ticker}: "
                f"+{result.n_trades - n_before} trades "
                f"(running total: {result.n_trades})"
            )

    if portfolio and result.trades and result.start and result.end:
        nav_curve, _, peak, skipped = _portfolio_layer_with_bars(
            cfg, result.trades, result.start, result.end,
            bars_by_ticker=bars_by_ticker,
            mtm_mode=mtm_mode,
        )
        result.nav_curve = nav_curve
        result.concurrent_position_peak = peak
        result.concurrent_skipped = skipped
    elif result.trades and result.start and result.end:
        result.nav_curve, result.daily_returns = _build_nav_curve(
            result.trades, cfg.backtest.start_nav, result.start, result.end,
        )

    return result


def _portfolio_layer_with_bars(cfg, trades, start, end, *, bars_by_ticker,
                                mtm_mode="close"):
    """Portfolio layer that uses pre-loaded bars for MTM at last close.

    Same logic as _portfolio_layer but uses the bars cache passed in
    instead of re-reading from disk.
    """
    cash = cfg.backtest.start_nav
    open_positions = {}
    peak_concurrent = 0
    skipped = 0
    cooldown_until = {}

    by_entry = defaultdict(list)
    for t in trades:
        by_entry[t.signal.entry_date].append(t)

    by_ticker_exit = {}
    for t in trades:
        if t.exit_reason == "EXHAUST":
            continue
        by_ticker_exit[(t.signal.ticker, t.exit_date)] = t

    if end < start:
        return [], [], 0, 0
    all_dates = []
    cursor = start
    while cursor <= end:
        all_dates.append(cursor)
        cursor = date.fromordinal(cursor.toordinal() + 1)

    daily_nav = []
    cooldown_days = getattr(cfg.backtest, "cooldown_bars_after_stop", 0)

    # Build a per-ticker date -> close lookup for fast MTM
    close_by_ticker_date = {}
    for ticker, bars in bars_by_ticker.items():
        close_by_ticker_date[ticker] = {b.date: b.close for b in bars}

    for d in all_dates:
        # 1. Close positions exiting today
        to_close = []
        for sym, pos in open_positions.items():
            if pos["exit_date"] <= d:
                to_close.append(sym)
        for sym in to_close:
            pos = open_positions.pop(sym)
            trade = by_ticker_exit.get((sym, pos["exit_date"]))
            cash += pos["exit_value"]
            if cooldown_days > 0 and trade is not None and trade.exit_reason in ("SL_HARD", "SL_SCALE"):
                cd_end = date.fromordinal(d.toordinal() + cooldown_days)
                cooldown_until[sym] = cd_end

        # 2. Open new trades
        for t in by_entry.get(d, []):
            ticker = t.signal.ticker
            if ticker in open_positions:
                continue
            if cooldown_until.get(ticker, date(1970, 1, 1)) > d:
                continue
            if len(open_positions) >= cfg.backtest.max_concurrent_positions:
                skipped += 1
                continue
            cost = t.signal.shares * t.signal.entry_price
            if cost > cash:
                skipped += 1
                continue
            cash -= cost
            last_close = close_by_ticker_date.get(ticker, {}).get(d, t.signal.entry_price)
            open_positions[ticker] = {
                "shares": t.signal.shares,
                "entry_price": t.signal.entry_price,
                "exit_date": t.exit_date,
                "last_close": last_close,
                "entry_value": t.entry_value,
                "exit_value": t.entry_value + t.realized_pnl,
            }
            peak_concurrent = max(peak_concurrent, len(open_positions))

        # 3. MTM
        if mtm_mode == "close":
            mtm_value = sum(
                pos["shares"] * pos["last_close"] for pos in open_positions.values()
            )
        else:
            mtm_value = sum(pos["entry_value"] for pos in open_positions.values())
        daily_nav.append((d, cash + mtm_value))

    return daily_nav, [], peak_concurrent, skipped


__all__ = ["BacktestResult", "run_backtest"]
