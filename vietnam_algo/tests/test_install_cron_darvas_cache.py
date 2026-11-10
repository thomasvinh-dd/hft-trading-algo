"""Regression guard for the Darvas Box dashboard cache cron jobs.

The web dashboard's cache was only ever rewritten by somebody clicking the
"Refresh data" button. On 2026-11-10 the cache was found frozen at
2026-11-04 while the underlying price data was current — the dashboard
silently served a 5-day-old equity curve and signals, and no alert fired.

Two jobs close that gap:

  refresh-cache  Rebuilds the cache (scan + backtest) after the EOD price
                 sync, so the dashboard is current every trading day.
  check-stale    Alerts via Telegram when the cache is too old, so a
                 broken refresh pipeline is noticed instead of silently
                 serving stale data.

Both must be present in the generated crontab, and both must be covered by
the installer's removal filter — otherwise re-running install_cron.sh
appends duplicates (the 2026-10-25 incident).
"""
import re
import subprocess
from pathlib import Path

# tests/ → vietnam_algo/ → repo root
ROOT = Path(__file__).resolve().parents[2]


def _print_crontab() -> str:
    proc = subprocess.run(
        ["bash", "scripts/install_cron.sh", "--print"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    return proc.stdout


def test_refresh_cache_job_is_installed() -> None:
    out = _print_crontab()
    assert "refresh-cache" in out, (
        f"install_cron.sh --print is missing the refresh-cache job — "
        f"the dashboard cache would only change when somebody clicks the "
        f"button.\nGot:\n{out}"
    )


def test_check_stale_job_is_installed() -> None:
    out = _print_crontab()
    assert "check-stale" in out, (
        f"install_cron.sh --print is missing the check-stale job — a stale "
        f"dashboard cache would go unnoticed.\nGot:\n{out}"
    )


def test_refresh_cache_runs_after_the_price_sync() -> None:
    """The cache must be rebuilt AFTER the 15:15 price sync, otherwise it
    is built from the previous day's prices."""
    out = _print_crontab()
    sync_line = next(
        (ln for ln in out.splitlines() if "run-common-sync" in ln), ""
    )
    refresh_line = next(
        (ln for ln in out.splitlines() if "refresh-cache" in ln), ""
    )
    assert sync_line, "common_sync entry not found"
    assert refresh_line, "refresh-cache entry not found"

    def _guard_time(line: str) -> str:
        m = re.search(r'date \+\\%H:\\\\\%M\)" = "(\d\d:\d\d)"', line)
        if not m:
            m = re.search(r'= "(\d\d:\d\d)"', line)
        assert m, f"could not parse the wall-clock guard from: {line}"
        return m.group(1)

    assert _guard_time(refresh_line) > _guard_time(sync_line), (
        f"refresh-cache ({_guard_time(refresh_line)}) must run after "
        f"run-common-sync ({_guard_time(sync_line)}) so the cache is built "
        f"from fresh prices"
    )


def test_new_jobs_are_covered_by_the_removal_filter() -> None:
    """The installer's filter must match the new jobs.

    Without this, re-running install_cron.sh appends a second copy of each
    job every time it runs.
    """
    src = (ROOT / "scripts" / "install_cron.sh").read_text()
    filter_line = re.search(r"grep -v ['\"]([^'\"]+)['\"]", src)
    assert filter_line is not None, "grep -v filter not found in install_cron.sh"
    patterns = filter_line.group(1)
    for marker in ("refresh-cache", "check-stale"):
        assert marker in patterns, (
            f"the removal filter must include {marker!r} so re-running "
            f"install_cron.sh removes old entries instead of duplicating "
            f"them. Got: {patterns!r}"
        )


# ---------------------------------------------------------------------------
# Guard-minute regression (2026-11-10 incident)
# ---------------------------------------------------------------------------
# The installer used to emit minute "0" for jobs whose wall-clock guard
# only accepts ":05" / ":20" / ":25".  Cron schedules on the SYSTEM
# timezone and ignores CRON_TZ, so the entry fired at minute 0 while the
# guard demanded minute 5/20/25 -- the two conditions could never both be
# true.  Those jobs were PERMANENTLY DEAD and failed SILENTLY: no log file
# was ever created and no error was ever reported.
#
#   scan-exits  (guard 09:05 ICT) -> cron minute 0  => never ran
#   scan-exits  (guard 15:25 ICT) -> cron minute 0  => never ran
#   check-stale (guard 09:20 ICT) -> cron minute 0  => never ran
#
# Every guarded job must therefore use a cron minute equal to its guard's
# minute.  These tests assert that invariant for the whole schedule, not
# just the three jobs that happened to break.


def _job_lines() -> list[str]:
    out = _print_crontab()
    return [
        ln
        for ln in out.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _cron_minute(line: str) -> str:
    return line.split()[0]


def _guard_time(line: str) -> str | None:
    """Return the HH:MM wall-clock guard of a job, or None if unguarded."""
    m = re.search(r'date \+\%H:\\\%M\)" = "(\d\d:\d\d)"', line)
    if not m:
        m = re.search(r'= "(\d\d:\d\d)"', line)
    return m.group(1) if m else None


def test_every_guarded_job_cron_minute_matches_its_guard_minute() -> None:
    """The cron minute must equal the guard's minute for EVERY guarded job.

    A mismatch makes the job permanently dead without any error: cron only
    fires the entry during the minute named in the schedule, while the
    guard only passes during the minute named in its wall-clock comparison.
    If those two minutes differ, the job can never run.
    """
    offenders = []
    for line in _job_lines():
        guard = _guard_time(line)
        if guard is None:
            continue  # unguarded jobs (e.g. the */5 watchdog) are fine
        cron_min = _cron_minute(line)
        if cron_min.startswith("*/"):
            continue  # step schedule, not a single-minute trigger
        if int(cron_min) != int(guard.split(":")[1]):
            offenders.append(
                f"cron minute {cron_min!r} != guard minute "
                f"{guard.split(':')[1]!r} (guard {guard}): {line}"
            )
    assert not offenders, (
        "guarded cron jobs whose trigger minute does not match their "
        "wall-clock guard minute can NEVER run:\n  " + "\n  ".join(offenders)
    )


def test_darvas_exit_and_stale_jobs_use_their_intended_minutes() -> None:
    """The three jobs that were dead must use their intended minutes."""
    expected = {
        # marker -> (guard HH:MM, expected cron minute)
        ("scan-exits", "09:05"): "5",
        ("scan-exits", "15:25"): "25",
        ("check-stale", "09:20"): "20",
        ("scan-signals", "09:00"): "0",
        ("send-report", "16:00"): "0",
        ("refresh-cache", "16:05"): "5",
    }
    found = {}
    for line in _job_lines():
        guard = _guard_time(line)
        if guard is None:
            continue
        for (marker, g), minute in expected.items():
            if marker in line and guard == g:
                found[(marker, g)] = _cron_minute(line)

    missing = [k for k in expected if k not in found]
    assert not missing, f"expected cron entries not found: {missing}"

    wrong = {
        k: (found[k], expected[k])
        for k in expected
        if int(found[k]) != int(expected[k])
    }
    assert not wrong, (
        "cron trigger minutes do not match the intended schedule: "
        + "; ".join(
            f"{k[0]} (guard {k[1]}) uses minute {got}, expected {want}"
            for k, (got, want) in wrong.items()
        )
    )
