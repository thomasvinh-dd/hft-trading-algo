"""Darvas Box Telegram bot CLI commands.

Subcommands:
  send-report   Run backtest on HOSE blue chips and send report to Telegram
  send-test     Send a test message
  scan-signals  Scan current bars for breakout signals and send alerts
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "darvas_box_strategy"))

from darvas_box_strategy.darvas import (
    StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig,
    TrailingConfig, UniverseConfig, ExitsConfig,
    run_backtest, load_price_bars, summary_report,
)
from darvas_box_strategy.darvas.data_loader import derive_meta, passes_universe
from darvas_box_strategy.darvas.db_prices import load_bars

from .telegram import TelegramConfig, load_telegram_config, send, send_telegram


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
from .report import (
    chunk_message,
    format_backtest_report,
    format_recent_trades,
    format_ticker_breakdown,
)
from .positions import (
    STATE_PATH,
    LivePosition,
    find_position_for_ticker,
    load_state,
    mark_notified,
    position_from_signal,
    remove_position,
    save_state,
    upsert_position,
)
from .exits import ExitEvent, evaluate_exits
from .messages import (
    format_entry_alert,
    format_exit_event,
    format_exit_events,
    format_no_exits_summary,
    format_positions_summary,
)


PRICES_DIR = ROOT / "vietnam_algo" / "data" / "prices"
DEFAULT_BLUE_CHIPS = ROOT / "darvas_box_strategy" / "data" / "hose_blue_chips.json"
DEFAULT_MARKET_INDEX = ROOT / "darvas_box_strategy" / "data" / "market_index.json"


def load_blue_chips(path: Path = DEFAULT_BLUE_CHIPS) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(f"HOSE blue chips file not found: {path}")
    data = json.loads(path.read_text())
    return [t.upper() for t in data["tickers"]]


def build_default_config() -> StrategyConfig:
    """Return the OOS-validated default config.

    OOS validation on 70 HOSE blue chips (TRAIN 2019-2022, TEST 2023-2026),
    on the corrected NAV accounting (the portfolio used to double-count
    partially-sold shares, which inflated every drawdown by up to 5.2pp
    and made the exposure cap reject trades it should not have):

    - vol_target=0% (off) — fixed 2x sizing beats vol-targeting
    - trail=10% — let winners compound after partial cut
    - size 0.10/0.20 + max_total_exposure_pct=0.75 — caps the drawdown
      on BOTH halves of the split (TRAIN -11.0%, TEST -17.8%)
    - min_market_volatility_pct=1.2 — trade only when the market is
      moving enough for a breakout to work. Raises the return AND cuts
      the drawdown on both halves (see "The market volatility filter")
    - sl_T=10%, sl_F=18% — entry-anchored stops (was base-anchored, buggy)
    - board_lot=100 — HOSE 100-share lots; small signals dropped
    - t_plus_n_bars=3 — T+2.5 settlement, as required for VN
    - daily_price_limit_pct=0 (off) — opt-in: ±7% price cap costs ~16% return
    """
    return StrategyConfig(
        base=BaseConfig(min_days=5, max_days=30, max_range_pct=0.030),
        breakout=BreakoutConfig(
            volume_mult=2.0, high_conviction_mult=3.0,
            require_ma200=True, min_history_days=200,
            require_close_above_ma=20,
            # Market-volatility entry filter. The index is rebuilt by
            # `python3 -m darvas_box_strategy.tools.build_market_index`
            # and must be refreshed regularly — the filter reads the
            # market's recent volatility, so a stale index means it is
            # judging today's market on old data.
            volatility_index_path=str(DEFAULT_MARKET_INDEX),
            min_market_volatility_pct=1.2,
            market_volatility_window=20,
        ),
        exits=ExitsConfig(
            sl_T_pct=0.10,        # 10% partial stop from entry
            sl_F_pct=0.18,        # 18% hard stop from entry
            use_entry_anchored_stops=True,  # CRITICAL: not the ancient base
            t_plus_n_bars=3,      # T+2.5 settlement: skip first 2 bars, exit on bar 3
        ),
        trailing=TrailingConfig(trail_mode="pct", trail_from_peak_pct=0.10),
        universe=UniverseConfig(min_avg_value_20d=1.0e9, min_price=5_000.0, max_price=200_000.0),
        backtest=BacktestConfig(
            start_nav=1_000_000_000.0,
            max_concurrent_positions=10,
            cooldown_bars_after_stop=5,
            cost_bps=25.0,
            # Sizing + total-exposure cap (2026-11-11, re-validated on the
            # corrected NAV accounting 2026-11-13).
            #
            # Capping the NUMBER of open positions does not cap the risk:
            # smaller positions mean more of them fit, so exposure rises
            # exactly when losers cluster. The 15/30 sizing was
            # self-limiting; reducing it without a cap made the drawdown
            # WORSE, not better.
            #
            # With the exposure cap the drawdown falls on both halves of
            # the walk-forward split:
            #   TRAIN 2019-2022  DD -23.22% -> -16.96%
            #   TEST  2023-2026  DD -25.96% -> -18.49%
            # (Those figures are the corrected ones — before the
            # 2026-11-13 NAV fix the drawdowns were 5.2pp worse.)
            #
            # The market-volatility filter (min_market_volatility_pct
            # above) then improves it further, to TRAIN -8.99% /
            # TEST -15.01%, while RAISING the return.
            size_low=0.10, size_high=0.20,
            max_total_exposure_pct=0.75,
            vol_target_pct=0.0,  # OFF: fixed sizing beats vol-targeting
            vol_target_floor=0.05, vol_target_ceiling=0.30,
            board_lot=100,  # HOSE board lot: shares rounded down to 100s
            # daily_price_limit_pct=0.07,  # opt-in: ±7% daily price cap
            # t_plus_n_bars=3,  # opt-in: VN T+2.5 settlement (costs ~25% return)
        ),
    )


def filter_universe(tickers: List[str], cfg: StrategyConfig,
                     prices_dir: Path = PRICES_DIR) -> List[str]:
    """Filter tickers that have sufficient data for the config."""
    from darvas_box_strategy.darvas.db_prices import load_bars

    out = []
    for t in tickers:
        bars = load_bars(t, prices_dir)
        if len(bars) < cfg.breakout.min_history_days:
            continue
        meta = derive_meta(t, bars)
        if not passes_universe(meta, cfg):
            continue
        out.append(t)
    return out


def cmd_send_report(args) -> int:
    """Run backtest on HOSE blue chips and send report to Telegram."""
    config = load_telegram_config(
        env_path=ROOT / "vietnam_algo" / "env" / ".env.telegram"
    )
    if config is None and not args.dry_run:
        print("ERROR: No Telegram config. Set TELEGRAM_BOT_TOKEN/CHAT_ID env vars "
              "or add vietnam_algo/env/.env.telegram", file=sys.stderr)
        return 1

    tickers = load_blue_chips()
    cfg = build_default_config()
    filtered = filter_universe(tickers, cfg)
    if not filtered:
        print("ERROR: No tickers passed the universe filter", file=sys.stderr)
        return 1

    print(f"Universe: HOSE blue chips ({len(filtered)} tickers)")
    print(f"Running backtest...")

    t0 = time.time()
    result = run_backtest(
        cfg,
        prices_dir=PRICES_DIR,
        tickers=filtered,
        progress=False,
        portfolio=True,
        mtm_mode="close",
    )
    print(f"Done in {time.time() - t0:.1f}s")

    period_label = (
        f"{result.start} to {result.end}"
        if result.start and result.end else "full period"
    )
    report_text = format_backtest_report(
        result,
        universe_name=f"HOSE blue chips ({len(filtered)} tickers)",
        period_label=period_label,
    )
    recent_text = format_recent_trades(result, n=10)
    breakdown_text = format_ticker_breakdown(result, top=10)

    full_message = report_text + "\n\n" + recent_text + "\n\n" + breakdown_text

    if args.dry_run:
        print("=" * 60)
        print("DRY RUN — would send to Telegram:")
        print("=" * 60)
        print(full_message)
        return 0

    print(f"Sending {len(full_message)} chars to Telegram...")
    failed = 0
    for i, chunk in enumerate(chunk_message(full_message)):
        result_msg = send_telegram(config, chunk)
        if not result_msg.get("ok"):
            print(f"  chunk {i + 1}: FAILED: {result_msg}", file=sys.stderr)
            failed += 1
        else:
            print(f"  chunk {i + 1}: sent OK")
        time.sleep(0.3)  # avoid rate-limit
    return 0 if failed == 0 else 1


def cmd_send_test(args) -> int:
    """Send a test message."""
    config = load_telegram_config(
        env_path=ROOT / "vietnam_algo" / "env" / ".env.telegram"
    )
    if config is None and not args.dry_run:
        print("ERROR: No Telegram config", file=sys.stderr)
        return 1

    text = (
        "✅ *Darvas Box bot test*\n\n"
        f"Bot is online. Time: `{date.today().isoformat()}`\n"
        f"Universe: HOSE blue chips\n"
    )

    if args.dry_run:
        print(f"DRY RUN — would send: {text}")
        return 0

    result = send_telegram(config, text)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def cmd_scan_signals(args) -> int:
    """Scan the most recent bar for breakout signals and send alerts."""
    config = load_telegram_config(
        env_path=ROOT / "vietnam_algo" / "env" / ".env.telegram"
    )
    if config is None and not args.dry_run:
        print("ERROR: No Telegram config", file=sys.stderr)
        return 1

    tickers = load_blue_chips()
    cfg = build_default_config()
    filtered = filter_universe(tickers, cfg)
    if not filtered:
        print("ERROR: No tickers passed filter", file=sys.stderr)
        return 1

    from darvas_box_strategy.darvas.base_detector import BaseScanner
    from darvas_box_strategy.darvas.signal_generator import generate_signal

    # Scan the last few bars for breakout signals
    as_of_offset = args.lookback  # 0 = today, 1 = yesterday, etc.
    signals = []
    for ticker in filtered:
        bars = load_bars(ticker)
        if len(bars) < cfg.breakout.min_history_days:
            continue
        idx = len(bars) - 1 - as_of_offset
        if idx < cfg.breakout.min_history_days:
            continue
        scanner = BaseScanner(bars, cfg.base, ticker=ticker)
        base = scanner.find_base(as_of_idx=idx)
        if base is None or base.end_idx >= idx:
            continue
        signal = generate_signal(
            bars, base, nav=cfg.backtest.start_nav, cfg=cfg,
            as_of_idx=idx,
        )
        if signal is None:
            continue
        signals.append(signal)

    # Load live position state so we can (a) avoid re-alerting on the
    # same breakout and (b) start tracking new positions for exit events.
    positions = load_state(STATE_PATH)
    existing_tickers = {p.ticker for p in positions.values()}

    new_positions = []
    fresh_signals = []
    for s in signals:
        if s.ticker in existing_tickers:
            # Already have an open position for this ticker — one active
            # deal per ticker. Don't re-alert, don't track a duplicate.
            continue
        fresh_signals.append(s)
        pos = position_from_signal(s)
        upsert_position(positions, pos)
        new_positions.append((s, pos))

    if not fresh_signals:
        lines = ["📭 *Darvas Box: No breakout signals* on last bar"]
    else:
        lines = [f"🚨 *Darvas Box: {len(fresh_signals)} breakout signal(s)*"]
        lines.append(f"_Universe_: HOSE blue chips ({len(filtered)} tickers)")
        lines.append("")
        for s in sorted(fresh_signals, key=lambda x: -x.size_pct):
            notional = s.shares * s.entry_price
            lines.append(
                f"• *{s.ticker}* @ {s.entry_price:,.0f}\n"
                f"  {s.shares:,} sh (~{notional / 1e6:.0f}M VND), "
                f"size {s.size_pct * 100:.0f}% NAV, "
                f"sl_T={s.sl_T:,.0f}, sl_F={s.sl_F:,.0f}"
            )

    text = "\n".join(lines)

    if args.dry_run:
        print("DRY RUN — would send:")
        print(text)
        if new_positions:
            print()
            print("DRY RUN — would track as new position(s):")
            for s, pos in new_positions:
                print(f"  {pos.key}  {pos.shares:,} sh @ {pos.entry_price:,.0f}")
        return 0

    # Persist position state BEFORE sending, so a crash between send and
    # save doesn't cause a duplicate alert on the next run.
    if new_positions:
        save_state(positions, STATE_PATH)

    result = send_telegram(config, text)
    print(json.dumps(result, indent=2))

    # Send a separate, richer message per new position so the exit
    # tracker has the details (TPs, stops) visible on Telegram.
    for s, pos in new_positions:
        msg = format_entry_alert(
            ticker=pos.ticker,
            entry_price=pos.entry_price,
            shares=pos.shares,
            size_pct=pos.size_pct,
            sl_T=pos.sl_T,
            sl_F=pos.sl_F,
            tiers=pos.tiers,
        )
        r = send_telegram(config, msg)
        ok = r.get("ok")
        print(f"  position {pos.key}: {'sent OK' if ok else 'FAILED'}")
        if not ok:
            print(f"    {r}", file=sys.stderr)
        time.sleep(0.3)
    return 0 if result.get("ok") else 1


def _bars_after_entry(ticker: str, entry_date: str) -> list:
    """Load price bars strictly after the entry date for a ticker."""
    from darvas_box_strategy.darvas.db_prices import load_bars

    bars = load_bars(ticker)
    out = []
    for b in bars:
        # PriceBar.time is an ISO date string
        if str(b.time) > str(entry_date):
            out.append(b)
    return out


def cmd_scan_exits(args) -> int:
    """Evaluate the exit ladder for tracked positions and notify Telegram.

    This is the counterpart to ``scan-signals``: it walks each open
    position's price history, applies the TIER / TRAIL / SL_SCALE /
    SL_HARD rules, and sends a Telegram message for every NEW exit
    event (deduped so the same event is never sent twice).
    """
    config = load_telegram_config(
        env_path=ROOT / "vietnam_algo" / "env" / ".env.telegram"
    )
    if config is None and not args.dry_run:
        print("ERROR: No Telegram config", file=sys.stderr)
        return 1

    cfg = build_default_config()
    positions = load_state(STATE_PATH)
    if not positions:
        msg = "📭 *Darvas Box: No open positions tracked*"
        if args.dry_run:
            print("DRY RUN — would send:")
            print(msg)
            return 0
        result = send_telegram(config, msg)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    all_events = []   # list of (position, [ExitEvent])
    closed_keys = []

    for key, pos in list(positions.items()):
        bars = _bars_after_entry(pos.ticker, pos.entry_date)
        if not bars:
            print(f"  {key}: no bars after entry, skipping")
            continue

        # Only evaluate bars we haven't seen yet. The evaluator walks
        # the bars in order and updates the running state (peak close,
        # tier_idx, trailing_active) as it goes, so we must feed it the
        # NEW bars only — replaying old ones would double-count the
        # bar counter and could re-fire a deduped event.
        if pos.last_bar_date:
            eval_bars = [b for b in bars if str(b.time) > str(pos.last_bar_date)]
            if not eval_bars:
                print(f"  {key}: no new bars since {pos.last_bar_date}, skipping")
                continue
        else:
            eval_bars = bars

        already = set(pos.notified)
        events = evaluate_exits(pos, eval_bars, cfg, already_notified=already)
        pos.last_bar_date = str(eval_bars[-1].time)

        if events:
            all_events.append((pos, events))
            for ev in events:
                mark_notified(pos, ev.dedupe_key)
            print(f"  {key}: {len(events)} exit event(s) — "
                  f"{', '.join(e.kind for e in events)}")
            if pos.remaining <= 0:
                closed_keys.append(key)
        else:
            print(f"  {key}: no exit events (bar {pos.last_bar_date}, "
                  f"remaining {pos.remaining:,} sh)")

    # Persist state BEFORE sending so a crash between send and save
    # cannot cause a duplicate alert on the next run.
    # A --dry-run must NOT persist: the events have not been sent yet,
    # so the next real run must still be able to notify them.
    if not args.dry_run:
        for key in closed_keys:
            remove_position(positions, key)
        if all_events or closed_keys:
            save_state(positions, STATE_PATH)

    if not all_events:
        msg = format_no_exits_summary(len(positions))
        if args.dry_run:
            print("DRY RUN — would send:")
            print(msg)
            return 0
        result = send_telegram(config, msg)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    failed = 0
    for pos, events in all_events:
        text = (format_exit_events(pos, events) if len(events) > 1
                else format_exit_event(pos, events[0]))
        if args.dry_run:
            print("=" * 60)
            print("DRY RUN — would send:")
            print("=" * 60)
            print(text)
            continue
        r = send_telegram(config, text)
        if r.get("ok"):
            print(f"  {pos.key}: sent OK ({len(events)} event(s))")
        else:
            failed += 1
            print(f"  {pos.key}: FAILED: {r}", file=sys.stderr)
        time.sleep(0.3)
    return 0 if failed == 0 else 1


def cmd_positions(args) -> int:
    """Print the tracked open positions (and optionally notify Telegram)."""
    config = load_telegram_config(
        env_path=ROOT / "vietnam_algo" / "env" / ".env.telegram"
    )
    if config is None and not args.dry_run:
        print("ERROR: No Telegram config", file=sys.stderr)
        return 1

    positions = load_state(STATE_PATH)
    text = format_positions_summary(list(positions.values()))
    if args.dry_run:
        print("DRY RUN — would send:")
        print(text)
        return 0
    result = send_telegram(config, text)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def cmd_refresh_cache(args) -> int:
    """Rebuild the web dashboard cache (scan + backtest) and persist it.

    This is the scheduled counterpart of the dashboard's "Refresh data"
    button. The cache is what the web UI reads; without this job it only
    changes when somebody clicks the button, and the dashboard silently
    serves stale data.

    The backtest is the expensive part (~5 minutes), so the command is
    idempotent: if the cache is still fresh enough it exits without doing
    the work, unless --force is given.
    """
    from darvas_box_strategy.web.data import (
        CACHE_MAX_AGE_SECONDS,
        CACHE_PATH,
        cache_age_hours,
        load_cache,
        refresh_cache,
    )

    if not args.force:
        cache = load_cache()
        age = cache_age_hours(cache.last_updated)
        if age is not None and age < CACHE_MAX_AGE_SECONDS / 3600:
            print(
                f"cache is fresh ({age:.1f}h old, limit "
                f"{CACHE_MAX_AGE_SECONDS / 3600:.0f}h) — nothing to do"
            )
            return 0
        if age is not None:
            print(f"cache is stale ({age:.1f}h old) — refreshing")

    t0 = time.time()

    # Rebuild the market index FIRST. The market-volatility entry filter
    # reads the index's recent volatility, so a stale index means the
    # filter is judging today's market on old data. The index is cheap
    # to rebuild (a few seconds) and the backtest below depends on it.
    try:
        from darvas_box_strategy.tools.build_market_index import main as build_index
        print("rebuilding the market index...")
        build_index(["--quiet"])
        print("  market index rebuilt")
    except Exception as exc:
        # A failed index rebuild must not stop the refresh: the previous
        # index is still usable, it is just staler than ideal.
        print(f"WARN: market index rebuild failed: {exc}", file=sys.stderr)

    try:
        new_cache = refresh_cache()
    except Exception as exc:
        print(f"ERROR: refresh failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"cache refreshed in {time.time() - t0:.1f}s -> {CACHE_PATH} "
        f"(last_updated={new_cache.last_updated}, "
        f"signals={len(new_cache.signals)}, "
        f"positions={len(new_cache.open_positions)})"
    )
    return 0


def cmd_check_stale(args) -> int:
    """Alert when the web dashboard cache is too old.

    The existing Vietnam Algo staleness alert covers the *price* data, and
    that data can be perfectly fresh while the darvas web cache is days
    old — the cache is only rewritten by the dashboard's refresh button or
    by the scheduled refresh-cache job. A stale cache means the dashboard
    silently shows old signals and an old equity curve, so it needs its own
    alert.

    Exits non-zero when the cache is stale, so a monitoring wrapper can act
    on it.
    """
    from darvas_box_strategy.web.data import (
        CACHE_MAX_AGE_SECONDS,
        CACHE_PATH,
        cache_age_hours,
        is_cache_stale,
        load_cache,
    )

    cache = load_cache()
    age = cache_age_hours(cache.last_updated)
    stale = is_cache_stale(cache.last_updated)
    limit_h = CACHE_MAX_AGE_SECONDS / 3600

    if not stale:
        print(
            f"cache OK: {cache.last_updated} "
            f"({age:.1f}h old, limit {limit_h:.0f}h)"
        )
        return 0

    # Once-per-day gating: the alert would otherwise repeat on every cron
    # run while the cache stays stale.
    today = date.today().isoformat()
    alert_state_path = ROOT / "darvas_box_strategy" / "data" / "cache_alert_state.json"
    alert_state = {}
    if alert_state_path.exists():
        try:
            alert_state = json.loads(alert_state_path.read_text())
        except Exception:
            alert_state = {}

    if alert_state.get("staleAlertSentDate") == today and not args.force:
        print(
            f"cache is stale but the alert was already sent today "
            f"({today}) — not sending again"
        )
        return 1

    if age is not None:
        message = (
            "⚠️ Darvas Box: dashboard cache STALE\n"
            f"cache: {cache.last_updated}\n"
            f"age: {age:.1f}h (threshold {limit_h:.0f}h)"
        )
    else:
        message = (
            "⚠️ Darvas Box: dashboard cache MISSING or unparseable\n"
            f"cache file: {CACHE_PATH}"
        )
    print(message)

    if args.dry_run:
        print("DRY RUN — would send:")
        print(message)
        return 1

    config = load_telegram_config(
        env_path=ROOT / "vietnam_algo" / "env" / ".env.telegram"
    )
    if config is None:
        print("ERROR: No Telegram config — cannot send the stale alert",
              file=sys.stderr)
        return 1

    result = send_telegram(config, message)
    print(json.dumps(result, indent=2))

    if result.get("ok"):
        alert_state["staleAlertSentDate"] = today
        alert_state["lastAlertSentAt"] = _now_iso()
        alert_state_path.parent.mkdir(parents=True, exist_ok=True)
        alert_state_path.write_text(json.dumps(alert_state, indent=2))

    return 1


def cmd_show_config(args) -> int:
    """Print the default config in human-readable form."""
    cfg = build_default_config()
    print("Darvas Box default config:")
    print()
    print(f"  Universe filter:  {cfg.universe}")
    print(f"  Base detector:    {cfg.base}")
    print(f"  Breakout config:  {cfg.breakout}")
    print(f"  Exit tiers:       {cfg.exits}")
    print(f"  Trailing stop:    {cfg.trailing}")
    print(f"  Backtest:         {cfg.backtest}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Darvas Box Telegram bot",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("send-report", help="Run backtest and send report to Telegram")
    p_report.add_argument("--dry-run", action="store_true",
                          help="Print the report instead of sending it")
    p_report.set_defaults(func=cmd_send_report)

    p_test = sub.add_parser("send-test", help="Send a test message")
    p_test.add_argument("--dry-run", action="store_true")
    p_test.set_defaults(func=cmd_send_test)

    p_scan = sub.add_parser("scan-signals", help="Scan for current breakout signals")
    p_scan.add_argument("--dry-run", action="store_true")
    p_scan.add_argument("--lookback", type=int, default=0,
                       help="How many bars back to scan (0=today, 1=yesterday)")
    p_scan.set_defaults(func=cmd_scan_signals)

    p_exits = sub.add_parser(
        "scan-exits",
        help="Evaluate exit ladder (TIER/TRAIL/SL) for tracked positions "
             "and notify Telegram")
    p_exits.add_argument("--dry-run", action="store_true")
    p_exits.set_defaults(func=cmd_scan_exits)

    p_pos = sub.add_parser(
        "positions", help="Show tracked open positions")
    p_pos.add_argument("--dry-run", action="store_true")
    p_pos.set_defaults(func=cmd_positions)

    p_show = sub.add_parser("show-config", help="Print default config")
    p_show.set_defaults(func=cmd_show_config)

    p_refresh = sub.add_parser(
        "refresh-cache",
        help="Rebuild the web dashboard cache (scan + backtest) and persist it",
    )
    p_refresh.add_argument(
        "--force",
        action="store_true",
        help="Refresh even if the cache is still fresh",
    )
    p_refresh.set_defaults(func=cmd_refresh_cache)

    p_stale = sub.add_parser(
        "check-stale",
        help="Alert (via Telegram) when the web dashboard cache is too old",
    )
    p_stale.add_argument("--dry-run", action="store_true")
    p_stale.add_argument(
        "--force",
        action="store_true",
        help="Send the alert even if it was already sent today",
    )
    p_stale.set_defaults(func=cmd_check_stale)

    p_serve = sub.add_parser("serve-web", help="Start the web dashboard (Flask)")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=5001)
    p_serve.add_argument("--no-reload", action="store_true",
                        help="Disable Flask auto-reload (recommended for systemd)")
    p_serve.set_defaults(func=cmd_serve_web)

    args = parser.parse_args(argv)
    return args.func(args)


def cmd_serve_web(args) -> int:
    """Launch the web dashboard."""
    from darvas_box_strategy.web.app import main as serve_main
    # Re-route args into the web app
    import sys
    new_argv = ["serve"]
    new_argv += ["--host", str(args.host)]
    new_argv += ["--port", str(args.port)]
    if args.no_reload:
        new_argv += ["--no-reload"]
    sys.argv = new_argv
    serve_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
