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
        )


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _signal_to_dict(s) -> Dict[str, Any]:
    """Convert a Signal dataclass to a JSON-safe dict."""
    return {
        "ticker": s.ticker,
        "entry_price": float(s.entry_price),
        "sl_F": float(s.sl_F),
        "sl_T": float(s.sl_T),
        "size_pct": float(s.size_pct),
        "as_of_date": s.entry_date.isoformat()
            if hasattr(s.entry_date, "isoformat") else str(s.entry_date),
        "atr": float(s.atr_value) if s.atr_value is not None else None,
        "high_conviction": bool(s.size_pct > 0.30),
    }


def _open_position_to_dict(t) -> Dict[str, Any]:
    """Build a display dict for a trade that's still open at the latest bar."""
    sig = t.signal
    last_close = float(t.highest_close) if t.highest_close else float(sig.entry_price)
    pnl_pct = (last_close / float(sig.entry_price) - 1.0) * 100.0
    pnl_vnd = (last_close - float(sig.entry_price)) * int(sig.shares)
    return {
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
        path = PRICES_DIR / f"{ticker}.json"
        if not path.exists():
            continue
        bars = _load_bars_safe(path)
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


def get_open_positions(cfg=None) -> List[Dict[str, Any]]:
    """Read the open positions from the current cache (no recomputation).

    The full computation is in refresh_cache(). This is a fast read-only
    helper for the API endpoint.
    """
    return load_cache().open_positions


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
            "ticker": t.signal.ticker,
            "entry_date": t.signal.entry_date.isoformat()
                if hasattr(t.signal.entry_date, "isoformat") else str(t.signal.entry_date),
            "exit_date": t.exit_date.isoformat()
                if hasattr(t.exit_date, "isoformat") else str(t.exit_date),
            "pnl_pct": round(t.return_pct * 100, 2),
            "exit_reason": t.exit_reason,
            "bars_held": t.bars_held,
        }
        for t in sorted_by_pnl[:5] if t.realized_pnl > 0
    ]
    top_losers = [
        {
            "ticker": t.signal.ticker,
            "entry_date": t.signal.entry_date.isoformat()
                if hasattr(t.signal.entry_date, "isoformat") else str(t.signal.entry_date),
            "exit_date": t.exit_date.isoformat()
                if hasattr(t.exit_date, "isoformat") else str(t.exit_date),
            "pnl_pct": round(t.return_pct * 100, 2),
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


def _load_bars_safe(path: Path):
    """Wrapper to delay-import the loader (avoid circular)."""
    from darvas_box_strategy.darvas.data_loader import load_price_bars
    return load_price_bars(path)


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

    # 3) Find currently-open positions from the trade list.
    #    A trade is "still open" if exit_date > last available bar date
    #    OR exit_reason == "EXHAUST" (last available bar was the entry).
    last_bar_date_by_ticker: Dict[str, Any] = {}
    for ticker in filtered:
        path = PRICES_DIR / f"{ticker}.json"
        if not path.exists():
            continue
        bars = _load_bars_safe(path)
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
    open_trades.sort(key=lambda t: t.signal.entry_date, reverse=True)
    open_positions = [_open_position_to_dict(t) for t in open_trades[:20]]
    log_msg(f"open positions: {len(open_positions)}")

    # 4) Find "today's signals" by scanning the latest bar.
    signals = get_current_signals(lookback=lookback, cfg=cfg)
    log_msg(f"signals today: {len(signals)}")

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
    )
    save_cache(cache)
    log_msg(f"refresh_cache done in {time.time() - t0:.1f}s")
    return cache


def log_msg(msg: str) -> None:
    """Print a timestamped log line (used during cache refresh)."""
    import logging
    logging.getLogger("darvas.web").info(msg)
