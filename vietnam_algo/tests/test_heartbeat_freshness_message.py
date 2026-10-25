"""Regression tests for the heartbeat data-freshness line.

2026-10-25: ``heartbeat_message`` raised ``UnboundLocalError`` when
``fresh is False`` and ``median_latest_bar_age_days`` was None.  The
``cal_age`` variable was assigned only inside the
``if median_age is not None`` branch but was read unconditionally on
the lines below it.

That is not a cosmetic bug.  The heartbeat is sent from inside the
bot's main loop, so the exception unwound the whole cycle and killed
the bot — and it triggered precisely when the data-sync cron had
failed and the sentinel was missing, i.e. exactly the situation the
"market data STALE" alert exists to report.  A missing sentinel gives
``sentinel_exists is False`` and no median age, which is the crashing
combination.

The same shape is duplicated in ``vietnam_algo/bots/runner.py`` (the
EOD/dry-run alert), where the ``cal_age`` assignment IS correctly
scoped inside the branch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vietnam_algo.libs.notifier import heartbeat_message  # noqa: E402

# The repo carries a second, near-identical copy of the notifier at
# ``vietnam_algo/notifier.py``.  It had the identical scoping bug and
# is imported by the EOD tooling under scripts/, so it needs the same
# coverage.
from vietnam_algo import notifier as _sibling_notifier  # noqa: E402


def _msg(freshness: dict) -> str:
    return heartbeat_message(
        account_no="0003937868",
        live_submit=False,
        state={},
        proxy_url=None,
        proxy_check=None,
        pid=1234,
        uptime_seconds=3600,
        freshness=freshness,
    )


def _sibling_msg(freshness: dict) -> str:
    return _sibling_notifier.heartbeat_message(
        account_no="0003937868",
        live_submit=False,
        state={},
        proxy_url=None,
        proxy_check=None,
        pid=1234,
        uptime_seconds=3600,
        freshness=freshness,
    )


def test_stale_with_missing_sentinel_does_not_raise() -> None:
    """The crashing case: fresh=False, sentinel missing, no median age.

    This is the shape produced when the data-sync cron fails, so it is
    the most important one to keep working.
    """
    msg = _msg({
        "fresh": False,
        "sentinel_age_hours": None,
        "sentinel_mtime": None,
        "sentinel_exists": False,
        "median_latest_bar_age_days": None,
    })
    assert "data: STALE" in msg
    assert "sentinel=MISSING" in msg


def test_stale_with_median_age_reports_trading_days() -> None:
    msg = _msg({
        "fresh": False,
        "sentinel_age_hours": 2.0,
        "median_latest_bar_age_days": 3.0,
    })
    assert "data: STALE" in msg
    assert "sample-bar-median=3.0d" in msg
    # No calendar-days key -> no calendar annotation.
    assert "d-cal" not in msg


def test_stale_with_median_and_calendar_age_reports_both() -> None:
    msg = _msg({
        "fresh": False,
        "sentinel_age_hours": 2.0,
        "median_latest_bar_age_days": 3.0,
        "median_latest_bar_age_calendar_days": 5.0,
    })
    assert "sample-bar-median=3.0d-trading (5.0d-cal)" in msg


def test_fresh_reports_fresh() -> None:
    msg = _msg({
        "fresh": True,
        "sentinel_age_hours": 1.0,
        "median_latest_bar_age_days": 1.0,
    })
    assert "data: fresh" in msg


def test_unknown_freshness_emits_no_data_line() -> None:
    msg = _msg({"fresh": None})
    assert "data:" not in msg


def test_freshness_none_is_tolerated() -> None:
    msg = heartbeat_message(
        account_no="0003937868",
        live_submit=False,
        state={},
        proxy_url=None,
        proxy_check=None,
        pid=1234,
        uptime_seconds=3600,
        freshness=None,
    )
    assert "data:" not in msg


@pytest.mark.parametrize("freshness", [
    {"fresh": False, "sentinel_exists": False},
    {"fresh": False, "sentinel_age_hours": None, "median_latest_bar_age_days": None},
    {"fresh": False, "median_latest_bar_age_days": 0.0},
    {"fresh": False, "median_latest_bar_age_days": 3.0},
    {"fresh": False, "median_latest_bar_age_days": 3.0,
     "median_latest_bar_age_calendar_days": 3.0},
])
def test_heartbeat_never_raises_on_any_freshness_shape(freshness: dict) -> None:
    """No freshness payload may take the bot down."""
    assert _msg(freshness)


@pytest.mark.parametrize("freshness", [
    {"fresh": False, "sentinel_exists": False},
    {"fresh": False, "sentinel_age_hours": None, "median_latest_bar_age_days": None},
    {"fresh": False, "median_latest_bar_age_days": 3.0},
    {"fresh": False, "median_latest_bar_age_days": 3.0,
     "median_latest_bar_age_calendar_days": 5.0},
])
def test_sibling_notifier_copy_never_raises(freshness: dict) -> None:
    """``vietnam_algo/notifier.py`` carried the same bug — cover it too."""
    assert _sibling_msg(freshness)


def test_both_notifier_copies_render_the_same_freshness_line() -> None:
    """The two copies must not drift apart again."""
    for freshness in (
        {"fresh": False, "sentinel_exists": False},
        {"fresh": False, "sentinel_age_hours": 2.0,
         "median_latest_bar_age_days": 3.0,
         "median_latest_bar_age_calendar_days": 5.0},
        {"fresh": True, "median_latest_bar_age_days": 1.0},
    ):
        a = [ln for ln in _msg(freshness).splitlines() if ln.startswith("data:")]
        b = [ln for ln in _sibling_msg(freshness).splitlines() if ln.startswith("data:")]
        assert a == b, f"copies diverged for {freshness}: {a!r} != {b!r}"
