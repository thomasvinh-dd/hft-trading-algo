from __future__ import annotations

import re
import http.client
import html
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

from .data import DEFAULT_DATA_DIR, read_json, write_json


MARKET_HOLIDAY_HOSE_URL = "https://market-holiday.com/markets/hose"
MONTHS = {
    "Jan": "01",
    "Feb": "02",
    "Mar": "03",
    "Apr": "04",
    "May": "05",
    "Jun": "06",
    "Jul": "07",
    "Aug": "08",
    "Sep": "09",
    "Oct": "10",
    "Nov": "11",
    "Dec": "12",
}


@dataclass(frozen=True)
class MarketHoliday:
    date: str
    name: str
    type: str


def market_holiday_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir / "market_holidays_vn.json"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_market_holiday_page(timeout: float = 30.0) -> str:
    request = urllib.request.Request(
        MARKET_HOLIDAY_HOSE_URL,
        headers={"User-Agent": "Mozilla/5.0 market-bot-vietnam-algo/0.1", "Accept": "text/html,text/markdown,*/*"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        try:
            body = response.read()
        except http.client.IncompleteRead as exc:
            body = exc.partial
        return body.decode("utf-8", errors="replace")


def parse_market_holidays(text: str) -> list[MarketHoliday]:
    clean_text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    clean_text = " ".join(clean_text.split())
    pattern = re.compile(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})\s+"
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
        r"(.+?)\s+Full Close\s+Closed",
    )
    holidays: dict[str, MarketHoliday] = {}
    for month, day, year, name in pattern.findall(clean_text):
        clean_name = " ".join(name.split())
        holiday_date = f"{year}-{MONTHS[month]}-{int(day):02d}"
        holidays[holiday_date] = MarketHoliday(holiday_date, clean_name, "Full Close")
    return sorted(holidays.values(), key=lambda item: item.date)


def save_market_holidays(holidays: list[MarketHoliday], data_dir: Path = DEFAULT_DATA_DIR, source: str = MARKET_HOLIDAY_HOSE_URL) -> Path:
    """Persist the holiday list.

    The file carries an explicit ``syncedAt`` (UTC ISO8601) timestamp
    so the next-sync decision can be made without consulting the bot
    state file (which is per-strategy, ephemeral, and gets cleared on
    state-file migrations). The bot's ``marketHolidaySyncDate`` field
    is updated separately as a convenience for the heartbeat.
    """
    path = market_holiday_path(data_dir)
    write_json(
        path,
        {
            "source": source,
            "market": "Vietnam",
            "exchangeReference": "HOSE",
            "timezone": "Asia/Ho_Chi_Minh",
            "syncedAt": utc_now(),
            "updatedAt": utc_now(),
            "count": len(holidays),
            "holidays": [asdict(holiday) for holiday in holidays],
        },
    )
    return path


def load_market_holidays(data_dir: Path = DEFAULT_DATA_DIR) -> set[str]:
    path = market_holiday_path(data_dir)
    if not path.exists():
        return set()
    payload = read_json(path)
    rows = payload.get("holidays", []) if isinstance(payload, dict) else []
    return {str(row.get("date")) for row in rows if isinstance(row, dict) and row.get("date")}


def _last_holiday_sync_at(data_dir: Path = DEFAULT_DATA_DIR) -> str | None:
    """Return the ``syncedAt`` ISO timestamp from the holidays file.

    Returns ``None`` if the file is missing or has no timestamp (e.g.
    older format before this field existed).
    """
    path = market_holiday_path(data_dir)
    if not path.exists():
        return None
    try:
        payload = read_json(path)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    ts = payload.get("syncedAt") or payload.get("updatedAt")
    if isinstance(ts, str) and ts:
        return ts
    return None


def sync_market_holidays(data_dir: Path = DEFAULT_DATA_DIR, timeout: float = 30.0) -> dict[str, Any]:
    text = fetch_market_holiday_page(timeout=timeout)
    holidays = parse_market_holidays(text)
    path = save_market_holidays(holidays, data_dir)
    return {"path": str(path), "count": len(holidays), "dates": [holiday.date for holiday in holidays]}


def should_sync_market_holidays(
    last_sync_date: str | None,
    today: str,
    interval_days: int = 30,
    data_dir: Path | None = None,
) -> bool:
    """Decide whether to re-fetch the market-holidays calendar.

    Vietnam's HOSE publishes its holiday calendar once a year (typically
    in December for the following year). Daily re-syncs are wasteful and
    mislead operators reading the heartbeat — a fresh ``syncedAt``
    looks like "new holidays were added" when in practice nothing
    changed. The previous default of 7 days was too aggressive.

    Returns ``True`` (sync now) when:
      - We've never synced (``last_sync_date`` is None/empty), OR
      - ``interval_days`` or more have elapsed since ``last_sync_date``,
        AND
      - The on-disk holidays file is missing OR also older than the
        interval (defense in depth: catches the case where the bot
        state field was cleared by a manual reset but the holidays
        file itself is still fresh).

    Returns ``False`` (skip) otherwise — the cached holidays are
    authoritative until the interval elapses.
    """
    # Never synced in this bot's state → defer to the data-file check.
    if not last_sync_date:
        if data_dir is None:
            return True
        last_sync_iso = _last_holiday_sync_at(data_dir)
        if last_sync_iso is None:
            return True  # no file at all → fetch
        # File exists and has a syncedAt → use it instead of state.
        last_sync_date = last_sync_iso[:10]
    try:
        last_dt = date.fromisoformat(last_sync_date[:10])
        today_dt = date.fromisoformat(today)
    except ValueError:
        # Unparseable → be conservative and fetch.
        return True
    if (today_dt - last_dt).days < interval_days:
        return False
    # Belt-and-suspenders: if the data file itself is also stale, sync;
    # otherwise skip (the bot state is what's authoritative for the
    # cron-style cadence — operators can clear it to force a refresh).
    if data_dir is not None:
        file_sync_iso = _last_holiday_sync_at(data_dir)
        if file_sync_iso is None:
            return True  # file missing → fetch
        try:
            file_dt = date.fromisoformat(file_sync_iso[:10])
        except ValueError:
            return True
        if (today_dt - file_dt).days < interval_days:
            return False
    return True


def trading_dates_between(start_date: str, end_date: str, holidays: set[str] | None = None) -> list[str]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        return []
    holiday_set = holidays or set()
    dates: list[str] = []
    current = start
    while current <= end:
        value = current.isoformat()
        if current.weekday() < 5 and value not in holiday_set:
            dates.append(value)
        current += timedelta(days=1)
    return dates


def live_trading_dates(data_dir: Path, start_date: str, end_date: str) -> list[str]:
    return trading_dates_between(start_date, end_date, load_market_holidays(data_dir))
