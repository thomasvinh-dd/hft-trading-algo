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

from .telegram import TelegramConfig, load_telegram_config, send, send_telegram
from .report import (
    chunk_message,
    format_backtest_report,
    format_recent_trades,
    format_ticker_breakdown,
)


PRICES_DIR = ROOT / "vietnam_algo" / "data" / "prices"
DEFAULT_BLUE_CHIPS = ROOT / "darvas_box_strategy" / "data" / "hose_blue_chips.json"


def load_blue_chips(path: Path = DEFAULT_BLUE_CHIPS) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(f"HOSE blue chips file not found: {path}")
    data = json.loads(path.read_text())
    return [t.upper() for t in data["tickers"]]


def build_default_config() -> StrategyConfig:
    """Return the OOS-validated default config.

    OOS validation on 70 HOSE blue chips (TRAIN 2019-2022, TEST 2023-2026):
    - vol_target=0% (off) — fixed 2x sizing beats vol-targeting
    - trail=12% — let winners compound (vs 7% which cut them short)
    - size 0.20/0.40 — 2x conviction sizing
    """
    return StrategyConfig(
        base=BaseConfig(min_days=5, max_days=30, max_range_pct=0.030),
        breakout=BreakoutConfig(
            volume_mult=2.0, high_conviction_mult=3.0,
            require_ma200=True, min_history_days=200,
            require_close_above_ma=20,
        ),
        exits=ExitsConfig(),
        trailing=TrailingConfig(trail_mode="pct", trail_from_peak_pct=0.12),
        universe=UniverseConfig(min_avg_value_20d=1.0e9, min_price=5_000.0, max_price=200_000.0),
        backtest=BacktestConfig(
            start_nav=1_000_000_000.0,
            max_concurrent_positions=10,
            cooldown_bars_after_stop=5,
            cost_bps=25.0,
            size_low=0.20, size_high=0.40,
            vol_target_pct=0.0,  # OFF: fixed sizing beats vol-targeting
            vol_target_floor=0.05, vol_target_ceiling=0.30,
        ),
    )


def filter_universe(tickers: List[str], cfg: StrategyConfig,
                     prices_dir: Path = PRICES_DIR) -> List[str]:
    """Filter tickers that have sufficient data for the config."""
    out = []
    for t in tickers:
        path = prices_dir / f"{t}.json"
        if not path.exists():
            continue
        bars = load_price_bars(path)
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
        path = PRICES_DIR / f"{ticker}.json"
        bars = load_price_bars(path)
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

    if not signals:
        lines = [f"📭 *Darvas Box: No breakout signals* on last bar"]
    else:
        lines = [f"🚨 *Darvas Box: {len(signals)} breakout signal(s)*"]
        lines.append(f"_Universe_: HOSE blue chips ({len(filtered)} tickers)")
        lines.append("")
        for s in sorted(signals, key=lambda x: -x.size_pct):
            lines.append(
                f"• *{s.ticker}* @ {s.entry_price:,.0f}\n"
                f"  size {s.size_pct * 100:.0f}% NAV, "
                f"sl_F={s.sl_F:,.0f}, sl_T={s.sl_T:,.0f}"
            )

    text = "\n".join(lines)

    if args.dry_run:
        print("DRY RUN — would send:")
        print(text)
        return 0

    result = send_telegram(config, text)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


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

    p_show = sub.add_parser("show-config", help="Print default config")
    p_show.set_defaults(func=cmd_show_config)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
