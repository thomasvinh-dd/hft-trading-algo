from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo


VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True)
class TradingSchedule:
    tokenTime: time = time(8, 30)
    startOfDayTime: time = time(9, 0)
    orderSubmitTime: time = time(9, 16)
    # 2026-10-08 fix: eodUpdateTime was 15:15, but the data-sync owner
    # (autofin bot) takes ~26 minutes for ``sync_prices_incremental``
    # across 1500+ symbols. At 15:15 the autofin sync was still in
    # progress when the momentum bot fired its EOD, so the momentum
    # mark-to-market read yesterday's close (file last written by the
    # 08:30 common_sync). Pushed the EOD trigger to 15:45 — by that time
    # autofin's sync has finished and the daily prices are on disk. The
    # momentum bot also gained a stale-sentinel check in
    # ``bots/runner.py`` (see commit msg), so even at 15:45 it waits
    # for ``data_ready_for_eod`` to be fresh before using prices.
    eodUpdateTime: time = time(15, 45)
    corporateActionSyncTime: time = time(15, 10)
    heartbeatTime: time = time(10, 30)
    # 2026-10-08 extension: morningStart 09:00 (was 09:15) and
    # afternoonEnd 15:00 (was 14:45) so the bot can place orders
    # during the ATO opening auction (09:00-09:15) and the post-close
    # / PLO window (14:45-15:00) on HOSE/HNX. The orderType chosen
    # for the new sub-windows is decided in ``current_session()`` —
    # LO works in continuous, ATO/ATC are required for the auction
    # windows (with price=0). See ``bots/runner.py:_submit_*`` for
    # the wiring.
    morningStart: time = time(9, 0)
    morningEnd: time = time(11, 30)
    afternoonStart: time = time(13, 0)
    afternoonEnd: time = time(15, 0)
    # Sub-window boundaries (HOSE / HNX).
    # - ATO (At-The-Open): 09:00-09:15. Only ATO orders accepted.
    # - Continuous: 09:15-11:30 and 13:00-14:30. LO / MTL.
    # - ATC (At-The-Close): 14:30-14:45 on HOSE. Only ATC orders accepted.
    # - Post-close (PLO on HOSE, MAK/MOK/PLO on HNX): 14:45-15:00.
    atoStart: time = time(9, 0)
    atoEnd: time = time(9, 15)
    atcStart: time = time(14, 30)
    atcEnd: time = time(14, 45)


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


def is_auction_session(session: "TradingSession") -> bool:
    """True for the ATO / ATC auction windows.

    2026-10-31: the bot must NOT trade in an auction.

    Two reasons, both of which showed up in the live ledger:

    1. An auction order is submitted with ``price=0`` — the auction
       price is decided at the close of the window, not by us. We
       lose all control over the fill price. The VOS BUY on
       2026-10-29 09:01:17 was submitted that way and filled at
       11,800 (the ATO price) while the continuous-market bid was
       ~10,800.

    2. ``price=0`` on a BUY means "buy at ANY price" to DNSE. That
       is an unbounded risk, not a bounded one.

    The user asked for this explicitly: no orders in ATO or ATC.
    Protective STOPs are included — the decision is to block ALL
    orders, not just trades. A stop planned in the window is
    deferred to the next continuous session.

    Post-close (14:45-15:00) is NOT an auction — it is a normal LO
    session on HOSE (PLO) with a real price — so it stays tradeable.
    """
    return session in (TradingSession.ATO, TradingSession.ATC)


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


def should_run_start_of_day(
    moment: datetime,
    last_date: str | None,
    schedule: TradingSchedule = TradingSchedule(),
    holidays: set[str] | None = None,
) -> bool:
    """Send the morning briefing when we cross startOfDayTime and haven't
    sent one today. Skips weekends + Vietnam market holidays.
    """
    if not is_trading_day(moment, holidays):
        return False
    today = local_date(moment)
    return today != last_date and local_time(moment) >= schedule.startOfDayTime


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


class TradingSession(str, Enum):
    """The HOSE/HNX/UPCOM trading-session taxonomy the bot needs to know
    about when picking an order type for a submission.

    Mapping (per DNSE OpenAPI ``place-order`` spec):

    - ``OFF_HOURS``  — outside the trading day (pre-open or after close).
    - ``ATO``        — HOSE opening auction 09:00-09:15. Only ATO orders
                       accepted; price=0 (auction determines fill).
    - ``CONTINUOUS`` — Continuous trading 09:15-11:30 and 13:00-14:30.
                       LO / MTL accepted.
    - ``ATC``        — HOSE closing auction 14:30-14:45. Only ATC orders
                       accepted; price=0.
    - ``POST_CLOSE`` — Post-close price-ladder session 14:45-15:00.
                       On HOSE this is PLO; on HNX MAK/MOK/PLO; on
                       UPCOM the session is closed. The bot sends LO
                       here and lets DNSE route by board.
    """

    OFF_HOURS = "off_hours"
    ATO = "ATO"
    CONTINUOUS = "continuous"
    ATC = "ATC"
    POST_CLOSE = "post_close"


def order_type_for_session(session: TradingSession) -> str:
    """Return the DNSE orderType the bot should use in a given session.

    For auctions (ATO/ATC) the fill price is determined by the auction,
    so the bot must send ``price=0`` AND the matching orderType
    (per the DNSE spec, the server rejects LO with a non-zero price
    during ATO and LO/ATO/ATC with a non-zero price during ATC).

    For continuous the bot sends LO with the signal price.

    For post-close we send LO with the signal price; on HOSE/HNX DNSE
    routes by board, on UPCOM this window is closed and DNSE rejects.
    """
    if session == TradingSession.ATO:
        return "ATO"
    if session == TradingSession.ATC:
        return "ATC"
    if session in (TradingSession.CONTINUOUS, TradingSession.POST_CLOSE):
        return "LO"
    raise ValueError(f"No orderType for {session} (off-hours — bot should not be submitting)")


def current_session(
    moment: datetime,
    schedule: TradingSchedule = TradingSchedule(),
    holidays: set[str] | None = None,
) -> TradingSession:
    """Classify the current Vietnam time into a trading session.

    Returns ``OFF_HOURS`` on weekends, holidays, and outside the
    morning+afternoon window. The ATO/ATC sub-windows take precedence
    over the coarse morning/afternoon boundaries so the bot can
    distinguish an ATO auction from a continuous-morning moment at
    09:10.
    """
    if not is_trading_day(moment, holidays):
        return TradingSession.OFF_HOURS
    current = local_time(moment)
    # ATO (HOSE opening auction): 09:00-09:15
    if schedule.atoStart <= current < schedule.atoEnd:
        return TradingSession.ATO
    # Continuous morning: 09:15-11:30
    if schedule.atoEnd <= current < schedule.morningEnd:
        return TradingSession.CONTINUOUS
    # ATC (HOSE closing auction): 14:30-14:45
    if schedule.atcStart <= current < schedule.atcEnd:
        return TradingSession.ATC
    # Post-close: 14:45-15:00
    if schedule.atcEnd <= current < schedule.afternoonEnd:
        return TradingSession.POST_CLOSE
    # Continuous afternoon: 13:00-14:30
    if schedule.afternoonStart <= current < schedule.atcStart:
        return TradingSession.CONTINUOUS
    return TradingSession.OFF_HOURS
