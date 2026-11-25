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

Run on cron (VPS-local CEST; see scripts/install_cron.sh):
  - 08:15 CEST ≈ 13:15 ICT : pre-market refresh (futures *shouldn't* yet be open)
  - 15:15 CEST ≈ 20:15 ICT : post-close refresh (CA events publish at 17:00 ICT)

Sentinel stamp is now gated on sync success (>=50% of DNSE-accepted
universe must have been fetched). When the stamp is skipped, the
shared ``data_ready_for_eod`` is left untouched and a Telegram
warning is sent — the momentum bot will then keep waiting on the
previous (older) sentinel rather than acting on stale prices.

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
from vietnam_algo.libs.data import (
    DEFAULT_DATA_DIR,
    DEFAULT_PRICE_LIMIT,
    is_invalid_symbol_error,
    load_invalid_symbol_denylist,
    load_saved_symbols,
    save_invalid_symbol_denylist,
    sync_prices,
    sync_prices_incremental,
    sync_symbols,
)
from vietnam_algo.libs.notifier import notify
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


def _mirror_symbols_to_db(data_dir: Path) -> int:
    """Mirror ``symbols.json`` into the Postgres ``symbols`` table.

    Same best-effort contract as the price mirror — a Postgres outage
    must not break the sync.
    """
    import os

    if os.environ.get("HFT_MIRROR_TO_DB", "1") == "0":
        return 0
    try:
        from vietnam_algo.libs.db import DbClient
        from vietnam_algo.libs.db_sync import mirror_symbols
        from vietnam_algo.libs.data import read_json
    except ImportError as exc:  # pragma: no cover — psycopg2 missing
        print(f"[common_sync] skipping Postgres symbol mirror: {exc}")
        return 0

    path = data_dir / "symbols.json"
    if not path.exists():
        return 0
    try:
        payload = read_json(path)
    except Exception as exc:
        print(f"[common_sync] symbol mirror read: {exc}")
        return 0
    items = payload.get("items") if isinstance(payload, dict) else None
    if not items:
        return 0

    db = DbClient.from_default()
    count = mirror_symbols(db, items)
    if count:
        print(f"[common_sync] symbols mirrored to Postgres: {count}")
    return count or 0


def _mirror_prices_to_db(
    symbols: list[str],
    data_dir: Path,
    paths: list[Path],
) -> int:
    """Mirror freshly-written price JSON files into the Postgres mirror.

    Postgres is the intended source of truth for market data (see
    ``vietnam_algo/db.py``), but only ``daily_engine.update_dataset()``
    fed it — and the cron drives prices through ``common_sync``, which
    calls ``sync_prices_incremental`` directly. So ``prices_dnse`` sat
    empty while the JSON files were current.

    The ticker is taken from each path's own stem — NOT zipped against
    ``symbols``. ``sync_prices_incremental`` returns only the files it
    actually changed, in its own iteration order, so zipping the two
    lists paired every symbol with some OTHER symbol's price file. That
    silently wrote ticker A's prices under ticker B (visible as e.g.
    MWG priced at 8,300 VND when it trades at 72,300) and wrecked the
    backtest: max drawdown -21% -> -70%.

    Best-effort: a Postgres outage must not break the sync. The
    ``mirror_*`` helpers swallow ``DbError`` and log.

    Set ``HFT_MIRROR_TO_DB=0`` to disable.
    """
    import os

    if os.environ.get("HFT_MIRROR_TO_DB", "1") == "0":
        return 0
    try:
        from vietnam_algo.libs.db import DbClient
        from vietnam_algo.libs.db_sync import mirror_prices
        from vietnam_algo.libs.data import read_json
    except ImportError as exc:  # pragma: no cover — psycopg2 missing
        print(f"[common_sync] skipping Postgres mirror (no psycopg2): {exc}")
        return 0

    db = DbClient.from_default()
    touched = 0
    for path in paths:
        # The file name IS the ticker — never zip against ``symbols``.
        symbol = path.stem.upper()
        try:
            payload = read_json(path)
        except Exception as exc:
            print(f"[common_sync] mirror read {symbol}: {exc}")
            continue
        bars = payload.get("items") if isinstance(payload, dict) else None
        if not bars:
            continue
        if mirror_prices(db, symbol, bars):
            touched += 1
    return touched


def run(force_prices: bool = False) -> CommonSyncResult:
    """Do the day's common sync. Skips already-done work.

    Steps:
      1. Refresh symbols.json (sync_symbols).
      2. Refresh the price series for each symbol (sync_prices with
         ``skip_existing=True`` so we only fill what's missing).
      3. Mirror symbols + prices into Postgres.
      4. Run the CA sync (incremental).
      5. Stamp ``data_ready_for_eod`` with current mtime.

    ``force_prices=True`` bypasses the trading-day guard. The cron only
    runs on weekdays, but a missed Friday (or a deploy that clobbered
    the data) leaves the mirror stale over the weekend, and there is no
    cron slot to repair it until Monday. This flag lets an operator
    backfill out of hours.

    Returns a CommonSyncResult summary. Exits non-zero only on a hard
    failure (e.g., empty universe, network down).
    """
    now = vietnam_now()
    today = local_date(now)
    if already_done_today(today) and not force_prices:
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

    if not is_trading_day(now) and not force_prices:
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

    # Mirror the symbol universe into Postgres too — the ``symbols``
    # table only had a single leftover test row, so a DB-side reader
    # had no idea which tickers exist.
    _mirror_symbols_to_db(DATA_DIR)

    # Surface the denylist size for log observability — when DNSE stops
    # carrying a symbol it lands here and is skipped on future syncs.
    denylist = load_invalid_symbol_denylist(DATA_DIR)
    print(
        f"[common_sync] denylist: {len(denylist)} symbol(s) "
        f"previously rejected by DNSE as 'invalid symbol'"
    )

    sleep_seconds = float(os.environ.get("COMMON_SYNC_SLEEP_SECONDS", "0.2"))
    incremental_limit = int(os.environ.get("COMMON_SYNC_INCREMENTAL_LIMIT", "10"))
    paths, fetch_errors = sync_prices_incremental(
        symbols,
        DATA_DIR,
        timeout=30,
        sleep_seconds=sleep_seconds,
        incremental_limit=incremental_limit,
    )
    # ``sync_prices_incremental`` extends the on-disk denylist itself
    # when it sees new 400 'invalid symbol' errors. Reload so we have
    # the up-to-date set for the success-rate calc below.
    denylist_after = load_invalid_symbol_denylist(DATA_DIR)
    new_invalid = denylist_after - denylist
    if new_invalid:
        print(
            f"[common_sync] added {len(new_invalid)} new symbol(s) "
            f"to the invalid-symbol denylist"
        )
    print(f"[common_sync] prices refreshed: {len(paths)} (incremental)")

    # Mirror the refreshed prices into Postgres.
    #
    # Before this, only ``daily_engine.update_dataset()`` mirrored
    # prices to the DB — but the cron drives prices through
    # ``common_sync`` (which calls ``sync_prices_incremental``
    # directly). So the mirror never ran on the cron path and
    # ``prices_dnse`` sat empty (0 rows) even though the JSON files
    # were current. Postgres is meant to be the source of truth for
    # downstream readers, so it has to be fed here.
    mirrored = _mirror_prices_to_db(symbols, DATA_DIR, paths)
    print(
        f"[common_sync] prices mirrored to Postgres: {mirrored}/{len(paths)}"
    )

    if fetch_errors:
        # Separate 'invalid symbol' rejections (permanent — DNSE will
        # never carry these tickers) from real failures (network, 5xx,
        # timeouts — these matter for operator alerting). The denylist
        # handles invalid symbols silently; only the real failures
        # contribute to the success-rate threshold below.
        real_errors = [
            (sym, err) for sym, err in fetch_errors
            if sym not in denylist_after or not is_invalid_symbol_error(err)
        ]
        # Effective universe size = total symbols minus the denylist.
        # (DNSE-accepted universe, i.e. what success rate should be
        # measured against.)
        effective_universe = max(len(symbols) - len(denylist_after), 1)
        if real_errors:
            failure_pct = (len(real_errors) / effective_universe) * 100.0
            sample = ", ".join(f"{sym}({err[:40]})" for sym, err in real_errors[:5])
            msg = (
                f"⚠️ Vietnam Algo: market-data price fetch errors\n"
                f"date: {today}\n"
                f"failed: {len(real_errors)}/{effective_universe} symbols ({failure_pct:.1f}%)"
                f" — excluding {len(denylist_after)} denylisted\n"
                f"first failures: {sample}"
            )
            if failure_pct > 5.0:
                print(f"[common_sync] {msg}", file=sys.stderr)
                notify(msg)
            else:
                print(
                    f"[common_sync] price fetch errors (below 5% threshold): "
                    f"{len(real_errors)}"
                )
        else:
            print(
                f"[common_sync] {len(fetch_errors)} error(s) were all "
                f"DNSE 'invalid symbol' rejections — already on the "
                f"denylist; ignoring."
            )

    # CA: ``sync_corporate_actions_incremental`` is itself idempotent —
    # it tracks a ``fromDate`` cursor in process memory; we wrap a
    # thin "load index from disk, sync from last known date" loop here
    # so multiple invocations on the same day don't re-fetch.
    index = _load_ca_index()
    from ._helpers import run_ca_incremental  # local helper to avoid
                                             # creating a separate module
    ca_events = run_ca_incremental(index_path=CA_DIR, existing=index)

    # Sentinel stamp — only mark data as "fresh" if the sync was
    # materially successful. The downstream momentum bot gates its
    # start-of-day and EOD briefings on this sentinel; if we stamp
    # on a broken sync, the bot will show stale prices as if they
    # were today's. Threshold: >=50% of the DNSE-accepted universe
    # (total - denylist) must have been fetched without errors.
    # Below that we skip the stamp and alert — the bot will then
    # wait on the sentinel and re-check next cron tick.
    effective_universe = max(len(symbols) - len(denylist_after), 1)
    success_count = effective_universe - len(
        [e for e in fetch_errors if e[0] not in denylist_after or not is_invalid_symbol_error(e[1])]
    )
    success_rate = (success_count / effective_universe) * 100.0
    SENTINEL_MIN_SUCCESS_PCT = float(
        os.environ.get("COMMON_SYNC_SENTINEL_MIN_SUCCESS_PCT", "50.0")
    )
    if success_rate >= SENTINEL_MIN_SUCCESS_PCT:
        SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        SENTINEL_PATH.write_text(datetime.now(tz=timezone.utc).isoformat())
        print(
            f"[common_sync] sentinel: {SENTINEL_PATH} "
            f"(success {success_count}/{effective_universe} = {success_rate:.1f}%)"
        )
    else:
        # Stale data — don't pretend it's fresh. Bot will keep waiting
        # on the (older) previous sentinel and an operator gets a
        # single Telegram warning per cron run.
        warn = (
            f"⚠️ Vietnam Algo: sentinel NOT stamped — sync success "
            f"{success_rate:.1f}% below {SENTINEL_MIN_SUCCESS_PCT}% threshold\n"
            f"date: {today}\n"
            f"failed: {effective_universe - success_count}/{effective_universe} "
            f"symbols (excluding {len(denylist_after)} denylisted)\n"
            f"bot will continue to use the previous sentinel"
        )
        print(f"[common_sync] {warn}", file=sys.stderr)
        notify(warn)

    # Persist a small state file so we don't re-run heavy work twice.
    summary = {
        "date": today,
        "ran_at": datetime.now(tz=timezone.utc).isoformat(),
        "symbols_updated": len(symbols),
        "denylisted": len(denylist_after),
        "prices_refreshed": len(paths),
        "fetch_errors": len(fetch_errors),
        "success_count": success_count,
        "effective_universe": effective_universe,
        "success_rate_pct": round(success_rate, 2),
        "sentinel_stamped": success_rate >= SENTINEL_MIN_SUCCESS_PCT,
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
    parser.add_argument(
        "--force-prices", action="store_true",
        help="Refresh prices even on a non-trading day (weekend/holiday). "
             "Use to backfill a missed Friday or repair the Postgres mirror.",
    )
    args = parser.parse_args(argv)
    if args.force:
        TRADE_STATE.unlink(missing_ok=True)
    try:
        result = run(force_prices=args.force_prices)
    except Exception as exc:
        # Surface hard failures (network down, 0 symbols, etc.) to
        # Telegram so an operator notices even when the cron run is
        # silent. The bot's outer loop has its own alert path for
        # bot-driven runs; this is the cron-job equivalent.
        err = f"🚨 Vietnam Algo: common_sync FAILED\ndate: {local_date(vietnam_now())}\nerror: {type(exc).__name__}: {exc}"
        print(f"[common_sync] FAILED: {exc}", file=sys.stderr)
        notify(err)
        return 1
    print(json.dumps(result.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
