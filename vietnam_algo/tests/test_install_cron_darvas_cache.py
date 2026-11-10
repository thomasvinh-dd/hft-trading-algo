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
