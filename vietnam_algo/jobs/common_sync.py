"""Standalone job: refresh the shared data sources every bot reads.

Outputs:
  - data/symbols.json                       (universe of symbols)
  - data/prices/<SYMBOL>.json               (per-symbol price series)
  - data/corporate_actions/<YYYY-MM-DD>.json (per-day CA events)
  - data/live/data_ready_for_eod            (sentinel — bots gate on this)

Idempotent within a single UTC trading day: if ``last_synced_date``
in the trade-state file matches today, the job short-circuits and
returns 0 immediately. The sentinel is re-stamped with the current
mtime either way so stale sentinel lookups are robust.

Run on cron:
  - 08:30 ICT  : pre-market refresh (futures *shouldn't* yet be open)
  - 15:15 ICT  : post-close refresh (CA events publish at 17:00)

Why this is its own job
-----------------------

Both autofin and momentum used to call ``update_dataset`` (vnindex +
vietnamfinance + Vietstock) inside their bot loops. With two bots on
the same VPS, that doubled every HTTP call to those upstreams and ate
through DM/email quotas. Now exactly one job hits each upstream per
day; both bots loop on a 60s heart-beat and wait for the sentinel.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vietnam_algo.libs.corporate_actions import sync_corporate_actions_incremental
from vietnam_algo.libs.data import DEFAULT_DATA_DIR, DEFAULT_PRICE_LIMIT, load_saved_symbols, sync_prices, sync_prices_incremental, sync_symbols
from vietnam_algo.libs.schedule import is_trading_day, local_date, vietnam_now


DATA_DIR = DEFAULT_DATA_DIR
LIVE_DIR = DATA_DIR / "live"
SYMBOLS_PATH = DATA_DIR / "symbols.json"
PRICES_DIR = DATA_DIR / "prices"
CA_DIR = DATA_DIR / "corporate_actions"
SENTINEL_PATH = LIVE_DIR / "data_ready_for_eod"
TRADE_STATE = LIVE_DIR / "common_sync_state.json"


@dataclass
class CommonSyncResult:
    ran_at: str  # ISO 8601 UTC
    date: str  # VN trading date
    symbols_updated: int
    prices_refreshed: int
    ca_events: int
    sentinel: str  # path to the sentinel file


def already_done_today(date: str) -> bool:
    """True when common_sync_state.json's date matches today."""
    if not TRADE_STATE.exists():
        return False
    try:
        payload = json.loads(TRADE_STATE.read_text())
    except Exception:
        return False
    return payload.get("date") == date


def _load_ca_index() -> list[dict]:
    """Load the latest corporate-action index so we can decide whether
    a re-sync is needed today.
    """
    files = sorted(CA_DIR.glob("*.json")) if CA_DIR.exists() else []
    for path in reversed(files):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        events = payload.get("events") if isinstance(payload, dict) else None
        if events:
            return events
    return []


def run() -> CommonSyncResult:
    """Do the day's common sync. Skips already-done work.

    Steps:
      1. Refresh symbols.json (sync_symbols).
      2. Refresh the price series for each symbol (sync_prices with
         ``skip_existing=True`` so we only fill what's missing).
      3. Run the CA sync (incremental).
      4. Stamp ``data_ready_for_eod`` with current mtime.

    Returns a CommonSyncResult summary. Exits non-zero only on a hard
    failure (e.g., empty universe, network down).
    """
    now = vietnam_now()
    today = local_date(now)
    if already_done_today(today):
        print(f"[common_sync] already done for {today}; re-stamping sentinel")
        SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        SENTINEL_PATH.write_text(datetime.now(tz=timezone.utc).isoformat())
        return CommonSyncResult(
            ran_at=datetime.now(tz=timezone.utc).isoformat(),
            date=today,
            symbols_updated=0,
            prices_refreshed=0,
            ca_events=len(_load_ca_index()),
            sentinel=str(SENTINEL_PATH),
        )

    if not is_trading_day(now):
        # On weekends / holidays we still want the sentinel present
        # (Friday's plan should be readable on Monday morning), but
        # skipping the heavy work saves cost.
        print(f"[common_sync] {today} is non-trading — refreshing sentinel only")
        SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        SENTINEL_PATH.write_text(datetime.now(tz=timezone.utc).isoformat())
        return CommonSyncResult(
            ran_at=datetime.now(tz=timezone.utc).isoformat(),
            date=today,
            symbols_updated=0,
            prices_refreshed=0,
            ca_events=0,
            sentinel=str(SENTINEL_PATH),
        )

    print(f"[common_sync] refreshing symbols + prices + CA for {today}")
    sync_symbols(DATA_DIR)
    symbols = load_saved_symbols(DATA_DIR)
    print(f"[common_sync] symbols: {len(symbols)}")
    if not symbols:
        raise RuntimeError("sync_symbols returned 0 symbols — aborting")

    sleep_seconds = float(os.environ.get("COMMON_SYNC_SLEEP_SECONDS", "0.2"))
    incremental_limit = int(os.environ.get("COMMON_SYNC_INCREMENTAL_LIMIT", "10"))
    paths = sync_prices_incremental(
        symbols,
        DATA_DIR,
        timeout=30,
        sleep_seconds=sleep_seconds,
        incremental_limit=incremental_limit,
    )
    print(f"[common_sync] prices refreshed: {len(paths)} (incremental)")

    # CA: ``sync_corporate_actions_incremental`` is itself idempotent —
    # it tracks a ``fromDate`` cursor in process memory; we wrap a
    # thin "load index from disk, sync from last known date" loop here
    # so multiple invocations on the same day don't re-fetch.
    index = _load_ca_index()
    from ._helpers import run_ca_incremental  # local helper to avoid
                                             # creating a separate module
    ca_events = run_ca_incremental(index_path=CA_DIR, existing=index)

    # Stamp the sentinel file with the current UTC instant.
    SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENTINEL_PATH.write_text(datetime.now(tz=timezone.utc).isoformat())
    print(f"[common_sync] sentinel: {SENTINEL_PATH}")

    # Persist a small state file so we don't re-run heavy work twice.
    summary = {
        "date": today,
        "ran_at": datetime.now(tz=timezone.utc).isoformat(),
        "symbols_updated": len(symbols),
        "prices_refreshed": len(paths),
        "ca_events": len(ca_events),
    }
    TRADE_STATE.parent.mkdir(parents=True, exist_ok=True)
    TRADE_STATE.write_text(json.dumps(summary, indent=2))

    return CommonSyncResult(
        ran_at=summary["ran_at"],
        date=today,
        symbols_updated=len(symbols),
        prices_refreshed=len(paths),
        ca_events=len(ca_events),
        sentinel=str(SENTINEL_PATH),
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Refresh shared price + CA data and stamp the EOD sentinel")
    parser.add_argument("--force", action="store_true", help="Re-run even if already done today (does NOT skip sentinel)")
    args = parser.parse_args(argv)
    if args.force:
        TRADE_STATE.unlink(missing_ok=True)
    try:
        result = run()
    except Exception as exc:
        print(f"[common_sync] FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
