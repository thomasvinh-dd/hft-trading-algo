"""Tests for the cron entry installed by ``scripts/install_cron.sh``.

2026-09-29 incident: the autofin bot died on 2026-09-25 evening
during my testing (``pkill -9 -f 'auto-trading-bot.*--strategy
autofin_signal'``), but the cron-installed watchdog never
detected the death and never relaunched. Reason: the watchdog
pattern in the cron entry was the OLD pattern WITHOUT the
``[a]`` bracket trick:

    pgrep -f "auto-trading-bot.*--strategy autofin_signal"

That pattern matched the watchdog's own ``sh -c`` invocation
because the literal pattern string appears in the watchdog's
argv. pgrep returned 0 (process found), the ``||`` branch never
fired, and the autofin bot stayed dead for ~4 days until I
investigated.

The fix was already in ``scripts/install_cron.sh`` (commit
fe72f2d) — but the live cron entry on the VPS was never updated
because ``bash scripts/install_cron.sh`` was never re-run after
the fix. Net downtime: 4 days (no SOD/EOD/heartbeat messages from
autofin bot).

These tests pin the fix:
  - The cron entry in ``install_cron.sh`` MUST use the ``[a]`` trick
    (so re-running the script installs the correct pattern).
  - After running ``bash scripts/install_cron.sh``, the live
    crontab MUST contain the ``[a]`` pattern (live-state check).

The second check is environment-specific (needs a real VPS or
local cron), so it skips on dev machines. The first check is
unit-testable.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def test_install_cron_sh_uses_bracket_trick():
    """``scripts/install_cron.sh`` MUST use ``[a]uto-trading-bot``,
    not ``auto-trading-bot``, in the watchdog pattern. Otherwise
    the watchdog's own ``sh -c`` invocation matches the pattern
    and the watchdog never fires the ``||`` relaunch branch.

    See test_bot_watchdog_pattern.py for the underlying regex
    semantics — this test is the cron-file-level pin.
    """
    install_script = (
        Path(__file__).parent.parent.parent / "scripts" / "install_cron.sh"
    )
    assert install_script.exists(), (
        f"install_cron.sh not found at {install_script}"
    )
    src = install_script.read_text()
    # The new pattern must appear in the watchdog definition.
    assert 'pgrep -f "[a]uto-trading-bot' in src, (
        "install_cron.sh must use the [a] bracket-class trick in the "
        "watchdog pgrep pattern. Without it the watchdog self-matches "
        "its own sh -c invocation and never relaunches a dead bot. "
        "See 2026-09-29 incident."
    )
    # The OLD pattern (no brackets) must NOT appear in the watchdog line.
    # We use a regex to scope the check to the _watchdog= assignment so
    # we don't false-positive on the docstring (which mentions both
    # old and new patterns for educational purposes).
    watchdog_line = re.search(
        r"_watchdog=['\"](.+)['\"]", src, re.DOTALL,
    )
    assert watchdog_line is not None, (
        "_watchdog=... assignment not found in install_cron.sh"
    )
    watchdog_value = watchdog_line.group(1)
    assert '"[a]uto-trading-bot' in watchdog_value, (
        f"_watchdog value must contain '[a]uto-trading-bot' bracket "
        f"trick; got: {watchdog_value!r}"
    )
    # Old pattern would be 'pgrep -f "auto-trading-bot' WITHOUT
    # the [a]. Make sure no such form exists in the _watchdog value.
    assert 'pgrep -f "auto-trading-bot' not in watchdog_value, (
        "_watchdog still uses the unbracketed old pattern. "
        "Re-running install_cron.sh would install the broken "
        "self-matching watchdog. See 2026-09-29 incident."
    )


def test_install_cron_sh_filter_removes_old_pattern_entry():
    """The ``install_cron.sh`` filter that strips existing vietnam-algo
    cron entries must remove BOTH the new (``[a]uto``) AND the old
    (``auto-trading-bot``) watchdog patterns, otherwise re-running
    the script accumulates stale entries.

    We check the grep filter pattern at line ~96 covers all three
    watchdog-marker substrings (``bot_watchdog``, ``launch_autofin``,
    ``launch_momentum``). Any of these substrings in an old cron
    line should match the filter.
    """
    install_script = (
        Path(__file__).parent.parent.parent / "scripts" / "install_cron.sh"
    )
    src = install_script.read_text()
    # The filter line uses grep -v with multiple patterns. Check
    # all three watchdog markers are present.
    filter_line = re.search(
        r"grep -v ['\"]([^'\"]+)['\"]", src,
    )
    assert filter_line is not None, "grep -v filter not found"
    filter_patterns = filter_line.group(1)
    for marker in ("bot_watchdog", "launch_autofin", "launch_momentum"):
        assert marker in filter_patterns, (
            f"Filter must include {marker!r} so re-running "
            f"install_cron.sh removes old cron entries. "
            f"Got: {filter_patterns!r}"
        )


def test_live_crontab_uses_bracket_trick():
    """End-to-end check: when ``bash scripts/install_cron.sh`` has
    been run on this machine, the live crontab must contain the
    ``[a]uto-trading-bot`` pattern.

    Skips if cron isn't installed (e.g. on CI without crontab).
    On the VPS this catches the 2026-09-29 incident class of bug:
    developer updates ``install_cron.sh`` but never re-runs it on
    the VPS, leaving the stale OLD pattern in production.
    """
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return  # no crontab binary — skip
    if out.returncode != 0:
        return  # no crontab installed — skip
    crontab_text = out.stdout
    # If there's a watchdog entry, it MUST use [a]. If there's
    # no watchdog entry, install_cron.sh hasn't been run yet —
    # we can't fail the test, but log a note.
    if "auto-trading-bot" not in crontab_text:
        return
    assert "[a]uto-trading-bot" in crontab_text, (
        "Live crontab has the watchdog cron entry but uses the OLD "
        "self-matching pattern. Re-run `bash scripts/install_cron.sh` "
        "to apply the fix. See 2026-09-29 incident where the autofin "
        "bot stayed dead for 4 days because the cron pattern was stale."
    )
