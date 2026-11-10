"""Data layer for the Darvas Box web dashboard.

Wraps the bot's signal scanner + backtest engine, caches results in a JSON
file (default `data/web_cache.json`), and exposes typed helpers that the
Flask app uses.

The cache is intentionally small and dumb — a single JSON file. The flask
routes re-read it on every request, but the underlying computations only
re-run when:
  - the cache is missing/stale
  - someone calls `POST /api/refresh`
  - the daily refresh cron runs (separate concern)
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "darvas_box_strategy"))

from darvas_box_strategy.bot.cli import (
    PRICES_DIR,
    DEFAULT_BLUE_CHIPS,
    build_default_config,
    filter_universe,
    load_blue_chips,
)
from darvas_box_strategy.darvas import (
    run_backtest,
    summary_report,
    ticker_breakdown,
)
from darvas_box_strategy.darvas.base_detector import BaseScanner
from darvas_box_strategy.darvas.data_loader import derive_meta, passes_universe
from darvas_box_strategy.darvas.signal_generator import generate_signal
from darvas_box_strategy.darvas.executor import simulate_trade
from darvas_box_strategy.darvas.report import recent_trades

CACHE_PATH = ROOT / "darvas_box_strategy" / "data" / "web_cache.json"
CACHE_MAX_AGE_SECONDS = 6 * 3600  # 6 hours — backtest is heavy


@dataclass
class WebCache:
    """JSON-serializable dashboard cache."""

    last_updated: str = ""
    scan_lookback: int = 0  # 0 = today, 1 = yesterday, etc.

    signals: List[Dict[str, Any]] = field(default_factory=list)
    open_positions: List[Dict[str, Any]] = field(default_factory=list)
    backtest: Dict[str, Any] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)
    # Tickers that have a pre-built history in `ticker_history`.
    # Lazy-loaded: if a user clicks a ticker not in this set, we build
    # on demand and add it.
    ticker_history: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WebCache":
        return cls(
            last_updated=d.get("last_updated", ""),
            scan_lookback=d.get("scan_lookback", 0),
            signals=d.get("signals", []),
            open_positions=d.get("open_positions", []),
            backtest=d.get("backtest", {}),
            config=d.get("config", {}),
            ticker_history=d.get("ticker_history", {}),
        )


def _make_deal_id(ticker: str, entry_date, seq: int = 0) -> str:
    """Build a deal_id from ticker + entry date.

    Format: ``{TICKER}_{YYYY-MM-DD}[_{seq}]``

    The sequence suffix is appended only if multiple deals share the same
    entry date (rare; only on same-day re-entry edge cases).
    """
    if hasattr(entry_date, "isoformat"):
        date_part = entry_date.isoformat()
    else:
        date_part = str(entry_date)
    base = f"{ticker}_{date_part}"
    return base if seq == 0 else f"{base}_{seq}"


def load_cache(path: Path = CACHE_PATH) -> WebCache:
    if not path.exists():
        return WebCache()
    try:
        return WebCache.from_dict(json.loads(path.read_text()))
    except Exception:
        return WebCache()


def save_cache(cache: WebCache, path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache.to_dict(), indent=2, default=str))


def cache_age_hours(last_updated: str) -> Optional[float]:
    """Age of a cache timestamp in hours, or None if it cannot be parsed.

    Used to decide whether the cache needs refreshing and to age out the
    stale-cache alert. An empty or unparseable timestamp means "unknown
    age" — the caller should treat that as stale.
    """
    if not last_updated:
        return None
    try:
        ts = datetime.fromisoformat(last_updated)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    return delta.total_seconds() / 3600.0


def is_cache_stale(last_updated: str, max_age_seconds: int = CACHE_MAX_AGE_SECONDS) -> bool:
    """Whether the cache is older than ``max_age_seconds``.

    A missing or unparseable timestamp counts as stale — that is the
    dangerous case, because it means the cache was never written or the
    write failed.
    """
    age = cache_age_hours(last_updated)
    if age is None:
        return True
    return age * 3600.0 > max_age_seconds


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _signal_to_dict(s, deal_seq: int = 0) -> Dict[str, Any]:
    """Convert a Signal dataclass to a JSON-safe dict.

    ``deal_seq`` is the sequence number for this ticker+entry_date combo
    (0 unless multiple signals share the same entry date, which is rare).
    """
    return {
        "deal_id": _make_deal_id(s.ticker, s.entry_date, deal_seq),
        "ticker": s.ticker,
        "entry_price": float(s.entry_price),
        "sl_F": float(s.sl_F),
        "sl_T": float(s.sl_T),
        "size_pct": float(s.size_pct),
        "shares": int(s.shares),
        "as_of_date": s.entry_date.isoformat()
            if hasattr(s.entry_date, "isoformat") else str(s.entry_date),
        "signal_date": s.signal_date.isoformat()
            if hasattr(s.signal_date, "isoformat") else str(s.signal_date),
        "entry_date": s.entry_date.isoformat()
            if hasattr(s.entry_date, "isoformat") else str(s.entry_date),
        "atr": float(s.atr_value) if s.atr_value is not None else None,
        "high_conviction": bool(s.size_pct > 0.30),
    }


def _open_position_to_dict(t, deal_seq: int = 0) -> Dict[str, Any]:
    """Build a display dict for a trade that's still open at the latest bar.

    Uses the actual last observed close (not the peak) so the displayed
    PnL matches what a live broker would show right now.
    """
    sig = t.signal
    # Prefer the executor's last_close (latest bar, post-limit). Fall back
    # to highest_close for trades simulated before this field was added.
    last_close = float(getattr(t, "last_close", 0.0) or 0.0) or float(
        t.highest_close if t.highest_close else sig.entry_price
    )
    pnl_pct = (last_close / float(sig.entry_price) - 1.0) * 100.0
    pnl_vnd = (last_close - float(sig.entry_price)) * int(sig.shares)
    return {
        "deal_id": _make_deal_id(sig.ticker, sig.entry_date, deal_seq),
        "ticker": sig.ticker,
        "entry_price": float(sig.entry_price),
        "last_close": last_close,
        "shares": int(sig.shares),
        "size_pct": float(sig.size_pct),
        "sl_F": float(sig.sl_F),
        "sl_T": float(sig.sl_T),
        "pnl_pct": pnl_pct,
        "pnl_vnd": pnl_vnd,
        "entry_date": sig.entry_date.isoformat()
            if hasattr(sig.entry_date, "isoformat") else str(sig.entry_date),
        "signal_date": sig.signal_date.isoformat()
            if hasattr(sig.signal_date, "isoformat") else str(sig.signal_date),
        "bars_held": int(t.bars_held) if hasattr(t, "bars_held") else 0,
    }


def get_current_signals(lookback: int = 0,
                          cfg=None) -> List[Dict[str, Any]]:
    """Scan the latest bar(s) for breakout signals across HOSE blue chips.

    Same logic as bot/cli.py::cmd_scan_signals — kept here so the
    web layer doesn't need to import the bot CLI.
    """
    if cfg is None:
        cfg = build_default_config()
    tickers = load_blue_chips()
    filtered = filter_universe(tickers, cfg)
    signals: List[Dict[str, Any]] = []
    for ticker in filtered:
        bars = _load_bars_safe(ticker)
        if len(bars) < cfg.breakout.min_history_days:
            continue
        idx = len(bars) - 1 - lookback
        if idx < cfg.breakout.min_history_days:
            continue
        scanner = BaseScanner(bars, cfg.base, ticker=ticker)
        base = scanner.find_base(as_of_idx=idx)
        if base is None or base.end_idx >= idx:
            continue
        sig = generate_signal(
            bars, base, nav=cfg.backtest.start_nav, cfg=cfg, as_of_idx=idx,
        )
        if sig is None:
            continue
        signals.append(_signal_to_dict(sig))
    signals.sort(key=lambda x: (-x["size_pct"], x["ticker"]))
    return signals


# Module-level cache for get_recent_signals — the scan takes 5-10s
# across 70 tickers, but the result changes only when new bar data
# arrives (typically once a day). 5-minute cache is plenty.
_RECENT_SIGNALS_CACHE: Dict[str, Any] = {"data": None, "expires": 0.0}
_RECENT_SIGNALS_TTL = 300  # 5 minutes


def get_recent_signals(days: int = 7, cfg=None) -> List[Dict[str, Any]]:
    """Scan the last ``days`` trading days for any breakout signals.

    Used by the web dashboard's Signals tab to show "recent signals"
    even when the latest bar has none. Returns signals sorted by
    signal_date descending, then by size_pct descending.

    Optimized: scans bars[i-1], bars[i-2], ... for the latest BASE
    end_idx, then only generates a signal for that one bar (the day
    after the base ends). This is much faster than scanning every
    possible day.

    Result is cached for 5 minutes (the scan takes 5-10s across
    70 tickers).
    """
    import time
    now = time.time()
    if _RECENT_SIGNALS_CACHE["data"] is not None and now < _RECENT_SIGNALS_CACHE["expires"]:
        return _RECENT_SIGNALS_CACHE["data"]
    if cfg is None:
        cfg = build_default_config()
    tickers = load_blue_chips()
    filtered = filter_universe(tickers, cfg)
    out: List[Dict[str, Any]] = []
    for ticker in filtered:
        bars = _load_bars_safe(ticker)
        if len(bars) < cfg.breakout.min_history_days:
            continue
        # Find the most recent base that ended within the last `days`
        # bars. The signal is at base.end_idx + 1.
        scanner = BaseScanner(bars, cfg.base, ticker=ticker)
        last_idx = len(bars) - 1
        min_idx = max(cfg.breakout.min_history_days, last_idx - days)
        # Search backwards from the latest bar for a valid base.
        # The base.end_idx is fixed once found; the breakout is at
        # end_idx + 1. We check up to `days` bars back.
        sig = None
        for check_idx in range(last_idx, min_idx - 1, -1):
            base = scanner.find_base(as_of_idx=check_idx)
            if base is None:
                continue
            breakout_idx = base.end_idx + 1
            if breakout_idx >= len(bars):
                continue
            # Try to generate a signal at breakout_idx
            sig = generate_signal(
                bars, base, nav=cfg.backtest.start_nav, cfg=cfg, as_of_idx=breakout_idx,
            )
            if sig is not None:
                break  # found the most recent one
        if sig is not None:
            out.append(_signal_to_dict(sig))
    out.sort(key=lambda x: (x.get("signal_date", ""), -x["size_pct"]), reverse=True)
    _RECENT_SIGNALS_CACHE["data"] = out
    _RECENT_SIGNALS_CACHE["expires"] = now + _RECENT_SIGNALS_TTL
    return out


def get_open_positions(cfg=None) -> List[Dict[str, Any]]:
    """Read the open positions from the current cache (no recomputation).

    The full computation is in refresh_cache(). This is a fast read-only
    helper for the API endpoint.
    """
    return load_cache().open_positions


def _fills_to_dicts(fills, signal=None, *, is_open: bool = False) -> List[Dict[str, Any]]:
    """Convert a list of FillEvent (or TradeFill) into JSON-safe dicts.

    The fill objects come from the executor (FillEvent) or the new
    Portfolio (TradeFill). They have a slightly different shape, so
    we normalize them here. The deal-detail modal uses the dict
    format below.

    The old executor's simulate_trade does not record an ENTRY fill —
    the fills list starts with the first exit event. We synthesize
    a proper ENTRY here using the signal's entry_price and shares.
    We also compute cumulative_pnl correctly: sum the per-fill
    realized_pnl values (not the gross proceeds).

    ``is_open=True`` means the deal is still held (the backtest ran
    out of bars). In that case the executor's terminal EXHAUST fill
    is NOT a real sale — it is a mark-to-market snapshot taken at the
    last available bar. We rewrite it so it reads as an open-position
    marker (0 shares, no sale) instead of "sold everything", which
    used to contradict the deal's own "OPEN" status.
    """
    out: List[Dict[str, Any]] = []
    # Synthesize ENTRY if the fills list doesn't have one
    has_entry = any(getattr(f, "kind", "") == "ENTRY" for f in fills)
    if not has_entry and signal is not None:
        out.append({
            "date": signal.entry_date.isoformat()
                if hasattr(signal.entry_date, "isoformat") else str(signal.entry_date),
            "kind": "ENTRY",
            "price": float(signal.entry_price),
            "shares": int(signal.shares),
            "pnl_delta": 0.0,
            "cumulative_pnl": 0.0,
            "remaining": int(signal.shares),
            "note": f"Buy {int(signal.shares):,} @ {signal.entry_price:,.0f}",
        })
    running_pnl = 0.0
    for f in fills:
        # FillEvent: date, kind, level, shares, proceeds, fraction_of_pos,
        #            cumulative_proceeds, realized_pnl
        # TradeFill: date, kind, price, shares, pnl_delta,
        #            cumulative_pnl, remaining, note
        d = {}
        if hasattr(f, "to_dict"):
            # New TradeFill
            d = f.to_dict()
        else:
            # Old FillEvent — compute running PnL from realized_pnl
            pnl_delta = float(getattr(f, "realized_pnl", 0.0))
            running_pnl += pnl_delta
            d = {
                "date": f.date.isoformat() if hasattr(f.date, "isoformat") else str(f.date),
                "kind": f.kind,
                "price": float(f.level),
                "shares": int(f.shares) if f.kind != "DIVIDEND" else 0,
                "pnl_delta": pnl_delta,
                "cumulative_pnl": running_pnl,
                # Remaining is now tracked on the FillEvent itself
                # (was hardcoded to 0 before the executor fix).
                "remaining": int(getattr(f, "remaining", 0)),
                "note": "",
            }
        # An open deal's terminal EXHAUST fill is a mark-to-market
        # snapshot, not a sale. Rewrite it so the timeline does not
        # claim "sold all shares" on a position that is still held.
        if is_open and d.get("kind") == "EXHAUST":
            # The EXHAUST fill records shares=0 remaining because the
            # executor closed the position to settle the accounting.
            # The shares actually still held are the ones it sold, so
            # restore them for display.
            held = abs(int(d.get("shares") or 0))
            d["kind"] = "MARK"
            d["shares"] = 0
            d["remaining"] = held
            d["pnl_delta"] = 0.0
            d["cumulative_pnl"] = running_pnl
            d["note"] = f"Position still open — {held:,} sh marked at last close"
        out.append(d)
    return out


def get_ticker_history(ticker: str, cfg=None) -> Dict[str, Any]:
    """Return all deals (open + closed) for a single ticker.

    This runs the per-ticker backtest logic in isolation — same as
    ``_scan_ticker`` does inside ``run_backtest``, but exposed so the
    web UI can deep-dive into one ticker. Takes ~10-15s per ticker on
    the first call; subsequent calls hit the in-memory cache.
    """
    if cfg is None:
        cfg = build_default_config()
    ticker = ticker.upper()

    # Read-through cache: return pre-built history if we have it
    cache = load_cache()
    if ticker in cache.ticker_history:
        return cache.ticker_history[ticker]

    bars = _load_bars_safe(ticker)
    if len(bars) < cfg.breakout.min_history_days:
        return {"ticker": ticker, "summary": _empty_summary(), "deals": []}

    last_bar_date = bars[-1].date
    scanner = BaseScanner(bars, cfg.base, ticker=ticker)
    deals: List[Dict[str, Any]] = []

    # Track sequence numbers per (ticker, entry_date) to keep deal_id
    # unique when the strategy somehow opens twice on the same day.
    seq_by_entry: Dict[Any, int] = {}

    # Track the most recent exit date for this ticker. If a new
    # signal fires while a deal is still open, the rule "1 ticker
    # = 1 open position at a time" says skip it. This mirrors
    # the Portfolio class's behaviour (portfolio.py:298-300) and
    # prevents fictional concurrent deals on the same ticker.
    last_exit_date = None  # None = no open position yet
    skipped_overlap = 0  # for logging

    for i in range(cfg.breakout.min_history_days, len(bars)):
        base = scanner.find_base(as_of_idx=i)
        if base is None or base.end_idx >= i:
            continue
        sig = generate_signal(
            bars, base, nav=cfg.backtest.start_nav, cfg=cfg, as_of_idx=i,
        )
        if sig is None:
            continue
        # Enforce "1 ticker = 1 open position at a time".
        # If a previous deal on this ticker is still open at the
        # new signal's entry_date, skip the new signal — the live
        # portfolio would have rejected it.
        ed = sig.entry_date
        if last_exit_date is not None and ed <= last_exit_date:
            skipped_overlap += 1
            continue
        t = simulate_trade(sig, bars[i + 1:], cfg, atr_value=sig.atr_value)

        is_open = (
            t.exit_reason == "EXHAUST"
            or t.exit_date > last_bar_date
        )
        ed = sig.entry_date
        seq = seq_by_entry.get(ed, 0)
        seq_by_entry[ed] = seq + 1

        # For closed trades, derive exit_price from the realized pnl
        exit_price = None
        if not is_open and sig.shares:
            exit_price = round(
                sig.entry_price + (t.realized_pnl / sig.shares), 0
            )

        deals.append({
            "deal_id": _make_deal_id(ticker, ed, seq),
            "ticker": ticker,
            "entry_date": ed.isoformat() if hasattr(ed, "isoformat") else str(ed),
            "signal_date": sig.signal_date.isoformat()
                if hasattr(sig.signal_date, "isoformat") else str(sig.signal_date),
            "exit_date": t.exit_date.isoformat() if hasattr(t.exit_date, "isoformat") else str(t.exit_date),
            "entry_price": float(sig.entry_price),
            "exit_price": exit_price,
            # last_close is the actual latest bar (not the peak). For closed
            # trades, the exit_price is more meaningful; for open trades,
            # last_close is what a live broker would show right now.
            "last_close": round(float(
                getattr(t, "last_close", 0.0) or t.highest_close or sig.entry_price
            ), 0),
            "highest_close": round(float(getattr(t, "highest_close", 0.0) or sig.entry_price), 0),
            "pnl_pct": round(t.return_pct * 100, 2),
            "pnl_vnd": round(t.realized_pnl, 0) if not is_open else None,
            "exit_reason": t.exit_reason if not is_open else "OPEN",
            "bars_held": int(t.bars_held),
            "is_open": is_open,
            "shares": int(sig.shares),
            "size_pct": float(sig.size_pct),
            "sl_F": float(sig.sl_F),
            "sl_T": float(sig.sl_T),
            # The bar-by-bar event log — used by the deal-detail modal
            "fills": _fills_to_dicts(getattr(t, "fills", ()), signal=sig,
                                     is_open=is_open),
        })

        # Update the "last exit date" tracker so the next signal
        # for this ticker is rejected if it falls inside this
        # trade's holding period.
        #
        # Open deals must ALSO advance the tracker: a still-open deal
        # occupies the ticker until it exits, so any later signal has
        # to be rejected. (This used to advance the tracker only for
        # closed deals, which let concurrent open deals pile up —
        # ABB ended up with 5 open deals at once, GEE with 5.)
        #
        # An open deal's exit_date is the last available bar, so it
        # blocks every subsequent signal on that ticker.
        last_exit_date = t.exit_date

    # Sort by entry date descending — newest first
    deals.sort(key=lambda d: d["entry_date"], reverse=True)

    closed = [d for d in deals if not d["is_open"]]
    winners = [d for d in closed if d["pnl_pct"] > 0]
    losers = [d for d in closed if d["pnl_pct"] <= 0]
    total_pnl = sum(d["pnl_vnd"] or 0 for d in closed)

    summary = {
        "total_deals": len(deals),
        "open_deals": sum(1 for d in deals if d["is_open"]),
        "closed_deals": len(closed),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate_pct": round(len(winners) / len(closed) * 100, 1) if closed else 0.0,
        "total_pnl_vnd": round(total_pnl, 0),
        "avg_pnl_pct": round(
            sum(d["pnl_pct"] for d in closed) / len(closed), 2
        ) if closed else 0.0,
    }

    history = {"ticker": ticker, "summary": summary, "deals": deals}

    # Persist back into the cache so subsequent reads are O(1)
    cache.ticker_history[ticker] = history
    save_cache(cache)
    return history


def _empty_summary() -> Dict[str, Any]:
    return {
        "total_deals": 0, "open_deals": 0, "closed_deals": 0,
        "wins": 0, "losses": 0, "win_rate_pct": 0.0,
        "total_pnl_vnd": 0, "avg_pnl_pct": 0.0,
    }


def _summarize_backtest(result, cfg, filtered) -> Dict[str, Any]:
    """Compute the backtest summary fields from a BacktestResult.

    Pulled out of refresh_cache() so it can be tested in isolation.
    """
    closed = [t for t in result.trades if t.exit_reason != "EXHAUST"]
    winners = [t for t in closed if t.realized_pnl > 0]
    losers = [t for t in closed if t.realized_pnl <= 0]

    equity = [v for _, v in result.nav_curve]
    if equity and equity[0] > 0:
        total_return_pct = (equity[-1] / equity[0] - 1.0) * 100.0
    else:
        total_return_pct = 0.0

    years = ((result.end - result.start).days / 365.25) if result.end and result.start else 0.0
    cagr_pct = (((1 + total_return_pct / 100.0) ** (1.0 / years) - 1) * 100.0) if years > 0 else 0.0

    win_rate = (len(winners) / len(closed) * 100.0) if closed else 0.0
    avg_win = (sum(t.realized_pnl for t in winners) / len(winners)) if winners else 0.0
    avg_loss = (sum(t.realized_pnl for t in losers) / len(losers)) if losers else 0.0
    profit_factor = abs(avg_win * len(winners) / (avg_loss * len(losers))) if (avg_loss and losers) else 0.0

    max_dd_pct = 0.0
    peak = equity[0] if equity else cfg.backtest.start_nav
    for nav in equity:
        if nav > peak:
            peak = nav
        dd = (nav - peak) / peak * 100.0
        if dd < max_dd_pct:
            max_dd_pct = dd

    from darvas_box_strategy.darvas.metrics import sharpe_ratio
    sharpe = sharpe_ratio(result.daily_returns) if result.daily_returns else None

    curve = result.nav_curve
    if len(curve) > 1000:
        step = max(1, len(curve) // 1000)
        curve = curve[::step]

    exit_reasons: Dict[str, int] = {}
    for t in result.trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    sorted_by_pnl = sorted(result.trades, key=lambda t: t.realized_pnl, reverse=True)
    top_winners = [
        {
            "deal_id": _make_deal_id(t.signal.ticker, t.signal.entry_date),
            "ticker": t.signal.ticker,
            "entry_date": t.signal.entry_date.isoformat()
                if hasattr(t.signal.entry_date, "isoformat") else str(t.signal.entry_date),
            "signal_date": t.signal.signal_date.isoformat()
                if hasattr(t.signal.signal_date, "isoformat") else str(t.signal.signal_date),
            "exit_date": t.exit_date.isoformat()
                if hasattr(t.exit_date, "isoformat") else str(t.exit_date),
            "pnl_pct": round(t.return_pct * 100, 2),
            "shares": t.signal.shares,
            "size_pct": round(t.signal.size_pct * 100, 0),
            "exit_reason": t.exit_reason,
            "bars_held": t.bars_held,
        }
        for t in sorted_by_pnl[:5] if t.realized_pnl > 0
    ]
    top_losers = [
        {
            "deal_id": _make_deal_id(t.signal.ticker, t.signal.entry_date),
            "ticker": t.signal.ticker,
            "entry_date": t.signal.entry_date.isoformat()
                if hasattr(t.signal.entry_date, "isoformat") else str(t.signal.entry_date),
            "signal_date": t.signal.signal_date.isoformat()
                if hasattr(t.signal.signal_date, "isoformat") else str(t.signal.signal_date),
            "exit_date": t.exit_date.isoformat()
                if hasattr(t.exit_date, "isoformat") else str(t.exit_date),
            "pnl_pct": round(t.return_pct * 100, 2),
            "shares": t.signal.shares,
            "size_pct": round(t.signal.size_pct * 100, 0),
            "exit_reason": t.exit_reason,
            "bars_held": t.bars_held,
        }
        for t in sorted_by_pnl[-5:] if t.realized_pnl < 0
    ]

    from darvas_box_strategy.bot.report import format_ticker_breakdown
    breakdown = format_ticker_breakdown(result, top=10)

    return {
        "period": {
            "start": result.start.isoformat() if result.start else None,
            "end": result.end.isoformat() if result.end else None,
            "years": round(years, 2),
        },
        "universe_size": len(filtered),
        "total_trades": len(result.trades),
        "closed_trades": len(closed),
        "open_trades": len(result.trades) - len(closed),
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": round(cagr_pct, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_win_vnd": round(avg_win, 0),
        "avg_loss_vnd": round(avg_loss, 0),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "exit_reasons": exit_reasons,
        "equity_curve": [
            {"date": d.isoformat() if hasattr(d, "isoformat") else str(d),
             "nav": round(nav, 0)}
            for d, nav in curve
        ],
        "top_winners": top_winners,
        "top_losers": top_losers,
        "ticker_breakdown": breakdown,
    }


def get_backtest_summary(cfg=None) -> Dict[str, Any]:
    """Run the backtest and return a summary dict for the dashboard.

    Note: this re-runs the full backtest (~5 min). Prefer refresh_cache()
    which computes the cache once and reuses it across API calls.
    """
    if cfg is None:
        cfg = build_default_config()
    tickers = load_blue_chips()
    filtered = filter_universe(tickers, cfg)
    result = run_backtest(
        cfg, prices_dir=PRICES_DIR, tickers=filtered,
        progress=False, portfolio=True, mtm_mode="close",
    )
    return _summarize_backtest(result, cfg, filtered)


def _load_bars_safe(ticker: str):
    """Load a ticker's bars, preferring Postgres over the JSON files.

    The VPS keeps market data in both places. Postgres is the intended
    source of truth; the JSON files are the fallback for when Postgres
    is down (and, historically, the only source — which is how a stale
    JSON copy could mark the dashboard's open deals against an
    outdated price).
    """
    from darvas_box_strategy.darvas.db_prices import load_bars
    return load_bars(ticker)


def refresh_cache(lookback: int = 0) -> WebCache:
    """Re-run scan + backtest and persist to CACHE_PATH.

    Optimization: we do this in ONE pass by reusing the backtest's trade
    list. The previous implementation ran three separate scans (one for
    signals, one for open positions, one for backtest) which took 8+
    minutes. This version takes ~5 minutes.
    """
    import time
    t0 = time.time()
    cfg = build_default_config()
    tickers = load_blue_chips()
    filtered = filter_universe(tickers, cfg)
    log_extra = f"({len(filtered)} tickers)"

    # 1) Run the backtest once. This is the expensive part.
    result = run_backtest(
        cfg, prices_dir=PRICES_DIR, tickers=filtered,
        progress=False, portfolio=True, mtm_mode="close",
    )
    log_msg(f"backtest done in {time.time() - t0:.1f}s {log_extra}")

    # 2) Derive the backtest summary from the result.
    backtest = _summarize_backtest(result, cfg, filtered)

    # 3) Find currently-open positions from the trade list. A trade
    #    is "logically open" if exit_reason == "EXHAUST" (backtest
    #    ran out of bars before the trade exited — in live trading
    #    the strategy would still be holding it) OR exit_date >
    #    last available bar date.
    #
    #    The trade list may have MULTIPLE signals for the same ticker
    #    at different entry prices (the simulator doesn't know about
    #    portfolio constraints). We dedupe by ticker, keeping the
    #    most recent entry — this matches what a live broker would
    #    show as currently held. The portfolio_open_positions field
    #    is also populated by the backtest but is empty in practice
    #    because all EXHAUST positions get closed at the end of the
    #    portfolio's day-by-day loop.
    last_bar_date_by_ticker: Dict[str, Any] = {}
    for ticker in filtered:
        bars = _load_bars_safe(ticker)
        if bars:
            last_bar_date_by_ticker[ticker] = bars[-1].date

    open_trades = []
    for t in result.trades:
        sig = t.signal
        last_bar_date = last_bar_date_by_ticker.get(sig.ticker)
        if last_bar_date is None:
            continue
        if t.exit_reason == "EXHAUST":
            open_trades.append(t)
            continue
        try:
            if t.exit_date > last_bar_date:
                open_trades.append(t)
        except TypeError:
            continue

    # Dedupe by ticker, keeping the most recent entry (the one
    # that would be currently held in a live portfolio). This
    # fixes the bug where MSB appeared 3x with different entry
    # prices because the simulator generated multiple valid signals.
    open_trades.sort(
        key=lambda t: (
            t.signal.ticker,
            t.signal.entry_date if hasattr(t.signal.entry_date, "isoformat")
            else str(t.signal.entry_date),
        ),
        reverse=True,
    )
    seen_tickers: set = set()
    deduped_open: List[Any] = []
    for t in open_trades:
        if t.signal.ticker in seen_tickers:
            continue
        seen_tickers.add(t.signal.ticker)
        deduped_open.append(t)
    open_positions = [_open_position_to_dict(t) for t in deduped_open[:20]]
    log_msg(f"open positions (deduped by ticker): {len(open_positions)}")

    # 4) Find "today's signals" by scanning the latest bar.
    signals = get_current_signals(lookback=lookback, cfg=cfg)
    log_msg(f"signals today: {len(signals)}")

    # 5) Pre-build ticker history for the tickers the user is most
    #    likely to click: those in current signals, open positions, or
    #    the top winners/losers. The remaining tickers will be built
    #    on demand when the user clicks them.
    priority_tickers: set = set()
    for s in signals:
        priority_tickers.add(s["ticker"])
    for p in open_positions:
        priority_tickers.add(p["ticker"])
    for w in backtest.get("top_winners", []):
        priority_tickers.add(w["ticker"])
    for l in backtest.get("top_losers", []):
        priority_tickers.add(l["ticker"])
    log_msg(f"pre-building history for {len(priority_tickers)} tickers")

    ticker_history: Dict[str, Any] = {}
    for ticker in priority_tickers:
        ticker_history[ticker] = get_ticker_history(ticker, cfg=cfg)
    log_msg(f"ticker history built in {time.time() - t0:.1f}s")

    cache = WebCache(
        last_updated=_now_iso(),
        scan_lookback=lookback,
        signals=signals,
        open_positions=open_positions,
        backtest=backtest,
        config={
            "universe": "HOSE blue chips (70 tickers)",
            "size_low": cfg.backtest.size_low,
            "size_high": cfg.backtest.size_high,
            "vol_target_pct": cfg.backtest.vol_target_pct,
            "trail_pct": cfg.trailing.trail_from_peak_pct,
            "sl_T_pct": cfg.exits.sl_T_pct,
            "sl_F_pct": cfg.exits.sl_F_pct,
        },
        ticker_history=ticker_history,
    )
    save_cache(cache)
    log_msg(f"refresh_cache done in {time.time() - t0:.1f}s")
    return cache


def log_msg(msg: str) -> None:
    """Print a timestamped log line (used during cache refresh)."""
    import logging
    logging.getLogger("darvas.web").info(msg)
