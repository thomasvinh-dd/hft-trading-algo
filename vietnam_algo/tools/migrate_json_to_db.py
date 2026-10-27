"""One-shot migration script: read the JSON files under
``vietnam_algo/data/`` and bulk-load them into the Postgres container
described by ``docker-compose.yml``.

Run from the project root::

    python -m vietnam_algo.tools.migrate_json_to_db

By default it uses ``PG_DSN`` (or ``DbConfig.from_env()``). Pass
``--dsn`` to override, or ``--dry-run`` to print what would be loaded
without writing.

Schema: see ``scripts/postgres/initdb.d/001_init.sql`` (and
``vietnam_algo.libs.schema_sql`` for the embedded copy the bot code
uses).

Idempotent: every insert is an UPSERT (``ON CONFLICT DO UPDATE``).
Re-running after a partial migration just re-stamps the same rows.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from vietnam_algo.libs.db import DbClient, DbConfig


DEFAULT_DATA_DIR = Path("vietnam_algo/data")
DEFAULT_PRICES_DIR = "prices_dnse"
DEFAULT_CA_FILE = "corporate_action.json"
DEFAULT_SYMBOLS_FILE = "symbols.json"
DEFAULT_HOLIDAYS_FILE = "market_holidays_vn.json"

logger = logging.getLogger("migrate")


def _load_symbols(data_dir: Path) -> list[dict]:
    p = data_dir / DEFAULT_SYMBOLS_FILE
    if not p.exists():
        return []
    raw = json.loads(p.read_text())
    return [s for s in raw.get("items") or raw.get("symbols") or [] if s]


def _load_one_prices_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    return raw.get("items") or []


def _load_prices(data_dir: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    prices_dir = data_dir / DEFAULT_PRICES_DIR
    if not prices_dir.exists():
        return out
    for path in prices_dir.glob("*.json"):
        sym = path.stem
        items = _load_one_prices_file(path)
        if items:
            out[sym] = items
    return out


def _load_corporate_actions(data_dir: Path) -> list[dict]:
    p = data_dir / DEFAULT_CA_FILE
    if not p.exists():
        return []
    raw = json.loads(p.read_text())
    return raw.get("items") or []


def _load_market_holidays(data_dir: Path) -> list[dict]:
    p = data_dir / DEFAULT_HOLIDAYS_FILE
    if not p.exists():
        return []
    raw = json.loads(p.read_text())
    items = raw.get("holidays") or raw.get("items") or []
    return [
        {
            "date": h.get("date") or h.get("holiday"),
            "name": h.get("name") or h.get("holidayName"),
            "type": h.get("holidayType") or h.get("type"),
        }
        for h in items
    ]


def migrate(
    db: DbClient,
    data_dir: Path,
    *,
    dry_run: bool = False,
    skip_symbols: bool = False,
    skip_prices: bool = False,
    skip_corporate_actions: bool = False,
    skip_market_holidays: bool = False,
) -> dict[str, int]:
    summary: dict[str, int] = {}

    # Always init the schema (idempotent).
    if not dry_run:
        db.init_schema_via_pool()

    if not skip_symbols:
        symbols = _load_symbols(data_dir)
        logger.info("symbols: %d rows", len(symbols))
        if symbols and not dry_run:
            n = db.upsert_symbols(symbols)
            summary["symbols"] = n
        else:
            summary["symbols"] = len(symbols)

    if not skip_corporate_actions:
        ca = _load_corporate_actions(data_dir)
        logger.info("corporate_actions: %d rows", len(ca))
        if ca and not dry_run:
            n = db.upsert_corporate_actions(ca)
            summary["corporate_actions"] = n
        else:
            summary["corporate_actions"] = len(ca)

    if not skip_market_holidays:
        holidays = _load_market_holidays(data_dir)
        logger.info("market_holidays: %d rows", len(holidays))
        if holidays and not dry_run:
            n = db.upsert_market_holidays(holidays)
            summary["market_holidays"] = n
        else:
            summary["market_holidays"] = len(holidays)

    if not skip_prices:
        prices = _load_prices(data_dir)
        total_bars = sum(len(b) for b in prices.values())
        logger.info(
            "prices: %d symbols, %d bars total",
            len(prices), total_bars,
        )
        if prices and not dry_run:
            t0 = time.time()
            done = 0
            for i, (sym, bars) in enumerate(prices.items(), 1):
                db.copy_prices_dnse(sym, bars)
                done += len(bars)
                if i % 100 == 0 or i == len(prices):
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    logger.info(
                        "  progress: %d/%d symbols  %d bars  %.0f bars/s",
                        i, len(prices), done, rate,
                    )
            summary["prices_symbols"] = len(prices)
            summary["prices_bars"] = done
        else:
            summary["prices_symbols"] = len(prices)
            summary["prices_bars"] = total_bars

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--dsn", default=os.environ.get("PG_DSN"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-symbols", action="store_true")
    parser.add_argument("--skip-prices", action="store_true")
    parser.add_argument("--skip-corporate-actions", action="store_true")
    parser.add_argument("--skip-market-holidays", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = DbConfig.from_env()
    if args.dsn:
        config = DbConfig(
            dsn=args.dsn, pool_min=config.pool_min, pool_max=config.pool_max,
        )
    db = DbClient(config)

    try:
        summary = migrate(
            db,
            args.data_dir,
            dry_run=args.dry_run,
            skip_symbols=args.skip_symbols,
            skip_prices=args.skip_prices,
            skip_corporate_actions=args.skip_corporate_actions,
            skip_market_holidays=args.skip_market_holidays,
        )
    finally:
        db.close()

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
