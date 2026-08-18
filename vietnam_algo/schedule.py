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
    morningStart: time = time(9, 15)
    morningEnd: time = time(11, 30)
    afternoonStart: time = time(13, 0)
    afternoonEnd: time = time(14, 45)


def vietnam_now() -> datetime:
    return datetime.now(VIETNAM_TZ)


def is_weekday(moment: datetime) -> bool:
    return moment.weekday() < 5


def local_time(moment: datetime) -> time:
    return moment.astimezone(VIETNAM_TZ).time()


def local_date(moment: datetime) -> str:
    return moment.astimezone(VIETNAM_TZ).date().isoformat()


def is_main_trading_phase(moment: datetime, schedule: TradingSchedule = TradingSchedule()) -> bool:
    if not is_weekday(moment):
        return False
    current = local_time(moment)
    return (schedule.morningStart <= current < schedule.morningEnd) or (
        schedule.afternoonStart <= current < schedule.afternoonEnd
    )


def should_refresh_token(moment: datetime, done_for_date: str | None, schedule: TradingSchedule = TradingSchedule()) -> bool:
    today = local_date(moment)
    return is_weekday(moment) and done_for_date != today and local_time(moment) >= schedule.tokenTime


def should_submit_orders(moment: datetime, done_for_date: str | None, schedule: TradingSchedule = TradingSchedule()) -> bool:
    today = local_date(moment)
    return (
        is_weekday(moment)
        and done_for_date != today
        and local_time(moment) >= schedule.orderSubmitTime
        and is_main_trading_phase(moment, schedule)
    )


def should_run_eod(moment: datetime, done_for_date: str | None, schedule: TradingSchedule = TradingSchedule()) -> bool:
    today = local_date(moment)
    return is_weekday(moment) and done_for_date != today and local_time(moment) >= schedule.eodUpdateTime


def should_sync_corporate_actions(moment: datetime, done_for_date: str | None, schedule: TradingSchedule = TradingSchedule()) -> bool:
    today = local_date(moment)
    return is_weekday(moment) and done_for_date != today and local_time(moment) >= schedule.corporateActionSyncTime
