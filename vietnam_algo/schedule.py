from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo


VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True)
class TradingSchedule:
    tokenTime: time = time(8, 30)
    orderSubmitTime: time = time(9, 16)
    eodUpdateTime: time = time(15, 15)
    corporateActionSyncTime: time = time(15, 10)
    heartbeatTime: time = time(10, 30)
    morningStart: time = time(9, 15)
    morningEnd: time = time(11, 30)
    afternoonStart: time = time(13, 0)
    afternoonEnd: time = time(14, 45)


def vietnam_now() -> datetime:
    return datetime.now(VIETNAM_TZ)


def is_weekday(moment: datetime) -> bool:
    return moment.weekday() < 5


def is_trading_day(moment: datetime, holidays: set[str] | None = None) -> bool:
    return is_weekday(moment) and local_date(moment) not in (holidays or set())


def local_time(moment: datetime) -> time:
    return moment.astimezone(VIETNAM_TZ).time()


def local_date(moment: datetime) -> str:
    return moment.astimezone(VIETNAM_TZ).date().isoformat()


def is_main_trading_phase(moment: datetime, schedule: TradingSchedule = TradingSchedule(), holidays: set[str] | None = None) -> bool:
    if not is_trading_day(moment, holidays):
        return False
    current = local_time(moment)
    return (schedule.morningStart <= current < schedule.morningEnd) or (
        schedule.afternoonStart <= current < schedule.afternoonEnd
    )


def should_refresh_token(moment: datetime, done_for_date: str | None, schedule: TradingSchedule = TradingSchedule(), holidays: set[str] | None = None) -> bool:
    today = local_date(moment)
    return is_trading_day(moment, holidays) and done_for_date != today and local_time(moment) >= schedule.tokenTime


def should_submit_orders(moment: datetime, done_for_date: str | None, schedule: TradingSchedule = TradingSchedule(), holidays: set[str] | None = None) -> bool:
    today = local_date(moment)
    return (
        is_trading_day(moment, holidays)
        and done_for_date != today
        and local_time(moment) >= schedule.orderSubmitTime
        and is_main_trading_phase(moment, schedule, holidays)
    )


def should_run_eod(moment: datetime, done_for_date: str | None, schedule: TradingSchedule = TradingSchedule(), holidays: set[str] | None = None) -> bool:
    today = local_date(moment)
    return is_trading_day(moment, holidays) and done_for_date != today and local_time(moment) >= schedule.eodUpdateTime


def should_sync_corporate_actions(moment: datetime, done_for_date: str | None, schedule: TradingSchedule = TradingSchedule(), holidays: set[str] | None = None) -> bool:
    today = local_date(moment)
    return is_trading_day(moment, holidays) and done_for_date != today and local_time(moment) >= schedule.corporateActionSyncTime


def should_send_heartbeat(moment: datetime, last_date: str | None, schedule: TradingSchedule = TradingSchedule(), holidays: set[str] | None = None) -> bool:
    """Send a daily heartbeat when we cross the configured heartbeatTime and
    haven't sent one today yet.

    Note: heartbeat fires even on non-trading days so we always get a "still
    alive" signal regardless of the market calendar.
    """
    today = local_date(moment)
    return today != last_date and local_time(moment) >= schedule.heartbeatTime


def should_poll_strategy(
    moment: datetime,
    last_poll_time: datetime | str | None,
    min_interval_seconds: int = 3600,
    schedule: TradingSchedule = TradingSchedule(),
    holidays: set[str] | None = None,
) -> bool:
    """Return True if we should re-run the strategy now.

    Fires during main trading phases only, with at least ``min_interval_seconds``
    between polls (default 1 hour). Use this to detect mid-day signal changes
    (e.g. autofin partial closes) without spamming Telegram on every loop.

    ``last_poll_time`` may be a ``datetime`` or an ISO-8601 string (the
    latter is what we persist in the strategy state file).
    """
    if not is_main_trading_phase(moment, schedule, holidays):
        return False
    if last_poll_time is None:
        return True
    if isinstance(last_poll_time, str):
        try:
            last_poll_time = datetime.fromisoformat(last_poll_time.replace("Z", "+00:00"))
        except ValueError:
            return True
    if last_poll_time.tzinfo is None:
        last_poll_time = last_poll_time.replace(tzinfo=VIETNAM_TZ)
    elapsed = (moment - last_poll_time).total_seconds()
    return elapsed >= min_interval_seconds
