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
    path = market_holiday_path(data_dir)
    write_json(
        path,
        {
            "source": source,
            "market": "Vietnam",
            "exchangeReference": "HOSE",
            "timezone": "Asia/Ho_Chi_Minh",
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


def sync_market_holidays(data_dir: Path = DEFAULT_DATA_DIR, timeout: float = 30.0) -> dict[str, Any]:
    text = fetch_market_holiday_page(timeout=timeout)
    holidays = parse_market_holidays(text)
    path = save_market_holidays(holidays, data_dir)
    return {"path": str(path), "count": len(holidays), "dates": [holiday.date for holiday in holidays]}


def should_sync_market_holidays(last_sync_date: str | None, today: str, interval_days: int = 7) -> bool:
    if not last_sync_date:
        return True
    try:
        return (date.fromisoformat(today) - date.fromisoformat(last_sync_date)).days >= interval_days
    except ValueError:
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
