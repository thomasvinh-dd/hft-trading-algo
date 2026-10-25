"""Regression tests for the DST-proof cron schedule installed by
``scripts/install_cron.sh``.

2026-10-25 incident (Europe/Berlin CEST -> CET fall-back):

The installer used to hard-code *CEST* hours pre-converted from ICT
(03:30 for the 08:30-ICT token refresh, 04:00 for 09:00 ICT, ...) with a
``CRON_TZ=UTC`` line at the top of the crontab.  That line is inert:
``man 5 crontab`` documents that the cron daemon schedules every task in
the *system* timezone and that ``TZ=``/``CRON_TZ=`` inside a crontab only
affects the commands, not the schedule.  So when Berlin fell back to CET
every ICT-timed job silently moved one hour later in ICT — worst of all
the DNSE token refresh (08:30 ICT) became 09:30 ICT, i.e. *after* the
09:00-ICT start-of-day cycle it exists to support.

The fix makes the schedule offset-independent: the cron trigger fires
every hour at the target minute and a wall-clock guard *inside the
command* accepts the run only when the clock in the job's own timezone
equals the intended time.  Exactly one of the 24 hourly slots satisfies
the guard, whatever the host offset is, before and after either DST
transition.

These tests pin that behaviour:

  - every scheduled job carries a timezone guard (not a bare fixed hour),
  - the guard's target times are the intended ICT/UTC times,
  - the weekday gate is evaluated in the job's timezone,
  - the schedule fires exactly once per eligible day across both the
    2026-10-25 fall-back and the 2027-03-28 spring-forward,
  - the live crontab contains each managed job exactly once.
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = ROOT / "scripts" / "install_cron.sh"

BERLIN = ZoneInfo("Europe/Berlin")
ICT = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = timezone.utc

# job -> (guard timezone, intended wall-clock time, weekday-gated?)
JOBS = {
    "run-get-otp": (ICT, "08:30", True),
    "run-common-sync": (ICT, "15:15", True),
    "scan-signals": (ICT, "09:00", True),
    "send-report": (ICT, "16:00", True),
    "backup_postgres": (UTC, "23:00", False),
}


def _install_cron_output() -> str:
    proc = subprocess.run(
        ["bash", "scripts/install_cron.sh", "--print"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    return proc.stdout


def _live_crontab() -> str | None:
    try:
        proc = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


@pytest.mark.parametrize("marker", sorted(JOBS))
def test_every_scheduled_job_has_a_timezone_guard(marker: str) -> None:
    """Each scheduled job must gate on a wall clock in its own timezone.

    A bare fixed hour (``30 3 * * 1-5``) is exactly what broke on
    2026-10-25: the hour was only correct for one of the two possible
    UTC offsets.
    """
    out = _install_cron_output()
    lines = [ln for ln in out.splitlines() if marker in ln and not ln.startswith("#")]
    assert lines, f"no cron entry for {marker} in install_cron.sh --print"
    for line in lines:
        assert "TZ=" in line, (
            f"{marker} entry has no TZ guard — a fixed hour will drift "
            f"again at the next DST change: {line}"
        )
        assert "date +" in line, (
            f"{marker} entry does not compare the wall clock: {line}"
        )


@pytest.mark.parametrize("marker", sorted(JOBS))
def test_guard_target_time_is_intended(marker: str) -> None:
    """The guard must compare against the intended ICT/UTC wall time."""
    tz, target, _ = JOBS[marker]
    out = _install_cron_output()
    lines = [ln for ln in out.splitlines() if marker in ln and not ln.startswith("#")]
    assert lines, f"no cron entry for {marker}"
    for line in lines:
        assert f'= "{target}"' in line, (
            f"{marker} must guard on {target} (in {tz}), got: {line}"
        )


def test_trading_jobs_gate_weekday_in_their_own_timezone() -> None:
    """The weekday gate must use the job's timezone, not the host's.

    ICT is UTC+7 and Berlin is UTC+1/UTC+2, so a host-local DOW field
    can put a job on the wrong trading day around midnight.
    """
    out = _install_cron_output()
    for marker in ("run-get-otp", "run-common-sync", "scan-signals", "send-report"):
        lines = [
            ln for ln in out.splitlines()
            if marker in ln and not ln.startswith("#")
        ]
        assert lines, f"no cron entry for {marker}"
        for line in lines:
            assert "TZ=Asia/Ho_Chi_Minh date +\\%u" in line, (
                f"{marker} must evaluate the weekday in Asia/Ho_Chi_Minh: {line}"
            )
            assert "-le 5" in line, (
                f"{marker} must skip Saturday/Sunday: {line}"
            )


def test_pg_backup_has_no_weekday_gate() -> None:
    """The Postgres backup is a daily job — it must run every day."""
    out = _install_cron_output()
    lines = [
        ln for ln in out.splitlines()
        if "backup_postgres" in ln and not ln.startswith("#")
    ]
    assert lines, "no cron entry for backup_postgres"
    for line in lines:
        assert "-le 5" not in line, (
            f"the daily backup must not be weekday-gated: {line}"
        )


def _guard_fires(tz: ZoneInfo, target: str, weekday_gated: bool, moment: datetime) -> bool:
    """Emulate the crontab guard at a given UTC instant."""
    local = moment.astimezone(tz)
    if local.hour != int(target[:2]) or local.minute != int(target[3:]):
        return False
    if weekday_gated and local.isoweekday() > 5:
        return False
    return True


@pytest.mark.parametrize("marker", sorted(JOBS))
def test_schedule_fires_exactly_once_across_both_dst_transitions(marker: str) -> None:
    """Walk real UTC instants across both 2026/2027 DST transitions.

    The guard approach only holds if the cron trigger (hourly at the
    target minute) plus the wall-clock guard produce exactly one run per
    eligible day — on the transition days themselves, not just on
    ordinary days.
    """
    tz, target, weekday_gated = JOBS[marker]

    # The cron trigger fires every hour at the target minute.  Reconstruct
    # it from the installer output rather than hard-coding it, so the test
    # fails if someone changes the trigger without fixing the guard.
    out = _install_cron_output()
    line = next(
        ln for ln in out.splitlines()
        if marker in ln and not ln.startswith("#")
    )
    minute = int(line.split()[0])
    hour_field = line.split()[1]
    assert hour_field == "*", (
        f"{marker} must use an hourly trigger ('*' hour) with a wall-clock "
        f"guard; got hour field {hour_field!r}"
    )

    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2027, 6, 1, tzinfo=UTC)
    fires: dict = {}

    moment = start
    while moment < end:
        host_local = moment.astimezone(BERLIN)
        if host_local.minute == minute:
            if _guard_fires(tz, target, weekday_gated, moment):
                key = moment.astimezone(tz).date()
                fires.setdefault(key, []).append(moment)
        moment += timedelta(minutes=1)

    day = start.date()
    eligible = []
    while day <= end.date():
        # Only count a day as eligible if its fire instant actually falls
        # inside the simulated window — otherwise the last day of the
        # window is reported as "missing" even though the schedule is fine.
        candidate = datetime(
            day.year, day.month, day.day,
            int(target[:2]), int(target[3:]),
            tzinfo=tz,
        )
        if start <= candidate.astimezone(UTC) < end:
            if not weekday_gated or day.isoweekday() <= 5:
                eligible.append(day)
        day += timedelta(days=1)

    missing = [d for d in eligible if d not in fires]
    duplicated = {d: v for d, v in fires.items() if len(v) != 1}

    assert not missing, (
        f"{marker} would not run on {len(missing)} eligible day(s), "
        f"e.g. {missing[:5]} — the DST guard is broken"
    )
    assert not duplicated, (
        f"{marker} would run more than once on {len(duplicated)} day(s), "
        f"e.g. {sorted(duplicated)[:5]} — the DST guard is broken"
    )


def test_live_crontab_has_each_managed_job_exactly_once() -> None:
    """The live crontab must not accumulate duplicate job entries.

    2026-10-25: the backup, Darvas scan and Darvas report were each
    scheduled twice because the installer's removal filter did not match
    those lines, so every reinstall appended another copy.
    """
    live = _live_crontab()
    if live is None:
        return  # no crontab binary — skip
    if "auto-trading-bot" not in live and "run-get-otp" not in live:
        return  # installer never run here — skip
    for marker in JOBS:
        count = sum(1 for ln in live.splitlines() if marker in ln and not ln.startswith("#"))
        assert count == 1, (
            f"live crontab has {count} entries for {marker}, expected exactly 1 "
            f"— duplicate cron entries run the job twice"
        )


def test_install_is_idempotent() -> None:
    """Re-running the installer must not append a second copy of a job."""
    live = _live_crontab()
    if live is None:
        return
    before = [
        ln for ln in live.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    proc = subprocess.run(
        ["bash", "scripts/install_cron.sh"],
        capture_output=True, text=True, cwd=ROOT, check=True,
    )
    assert proc.returncode == 0
    after = [
        ln for ln in (_live_crontab() or "").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    assert len(after) == len(before), (
        f"installer is not idempotent: {len(before)} job lines before, "
        f"{len(after)} after — duplicate cron entries accumulate"
    )
