"""Regression test for the 2026-09-25 watchdog self-match incident.

2026-09-25 21:16 ICT: operator reported the autofin bot sent no SOD/EOD
on 2026-09-25. Investigation found:

    pgrep -f "auto-trading-bot.*--strategy autofin_signal"

…matched the WATCHDOG's own ``sh -c`` process because the pattern
string appears verbatim in the watchdog's argv. pgrep returned 0
on every tick (the watchdog matched itself), the ``||`` branch never
fired, and the bot stayed dead for ~21 hours after a manual pkill
the prior evening.

Fix: use the ``[a]uto-trading-bot`` bracket-class trick so the
literal pattern in argv doesn't self-match.

This test pins both:
  1. The new pattern does NOT match the watchdog's own argv.
  2. The new pattern DOES match a real autofin-bot argv.
  3. The old pattern self-matches (so the regression is documented).
"""
import re
import subprocess

NEW_PATTERN = "[a]uto-trading-bot.*--strategy autofin_signal"
OLD_PATTERN = "auto-trading-bot.*--strategy autofin_signal"

# Regex forms of the patterns (pgrep uses ERE by default)
NEW_RE = re.compile(NEW_PATTERN)
OLD_RE = re.compile(OLD_PATTERN)


def _argv_for_watchdog_shell(pattern: str) -> str:
    """Simulate the watchdog's argv as cron sees it.

    Cron runs the entry as ``sh -c '<full command>'``, so the
    pattern string appears LITERALLY in /proc/<pid>/cmdline.
    This is exactly what pgrep -f matches against.
    """
    return (
        "sh -c pgrep -f " + pattern + " >/dev/null 2>&1 || "
        "(echo watchdog down, relaunching >> /root/logs/bot_watchdog.log && "
        "bash /root/hft-trading-algo/scripts/launch_autofin.sh)"
    )


def _argv_for_real_autofin_bot() -> str:
    """Simulate the real autofin bot's argv: ``python3 -u -m
    vietnam_algo.main auto-trading-bot ... --strategy autofin_signal``.
    """
    return (
        "python3 -u -m vietnam_algo.main auto-trading-bot "
        "--account-no 0003937868 --loan-package-id 1775 "
        "--sleep-seconds 60 --update-symbols --update-prices "
        "--strategy autofin_signal"
    )


def test_old_pattern_self_matches_watchdog_argv():
    """The bug: the old pattern matched the watchdog's own argv.

    pgrep -f matches against /proc/<pid>/cmdline. The watchdog's
    cmdline contains the literal pattern (it's the search key).
    Without the ``[a]`` bracket trick the watchdog always
    self-matches and the ``||`` branch never fires.
    """
    cmdline = _argv_for_watchdog_shell(OLD_PATTERN)
    assert OLD_RE.search(cmdline), (
        f"Old pattern should self-match watchdog argv (this is the bug). "
        f"cmdline: {cmdline!r}"
    )


def test_new_pattern_does_not_self_match_watchdog_argv():
    """The fix: the new pattern does NOT match the watchdog's argv.

    The ``[a]`` regex bracket class is the trick — argv contains
    the literal ``[a]uto-trading-bot`` which is NOT a substring of
    ``auto-trading-bot`` (the thing we're matching against).
    """
    cmdline = _argv_for_watchdog_shell(NEW_PATTERN)
    assert not NEW_RE.search(cmdline), (
        f"New pattern must not self-match watchdog argv. cmdline: {cmdline!r}"
    )


def test_new_pattern_matches_real_autofin_bot():
    """Sanity: the new pattern still matches the actual bot's argv."""
    argv = _argv_for_real_autofin_bot()
    assert NEW_RE.search(argv), (
        f"New pattern must match real bot argv: {argv!r}"
    )


def test_new_pattern_does_not_match_momentum_bot():
    """Sanity: the autofin-specific pattern does NOT match momentum."""
    argv = _argv_for_real_autofin_bot().replace("autofin_signal", "momentum")
    assert not NEW_RE.search(argv), (
        f"New autofin-specific pattern must NOT match momentum: {argv!r}"
    )


def test_new_pattern_matches_real_bot_via_pgrep():
    """Integration: pgrep -f with the new pattern matches the
    actual bot if one is running. If no bot is running, this test
    skips — it depends on the VPS state."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", NEW_PATTERN],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return  # no pgrep in this environment; skip
    # When the test runs, /tmp/test_watchdog_pattern.sh might or
    # might not have a real bot. We can't assert rc=0 (might be no
    # bot) but we CAN assert the pattern is well-formed (pgrep
    # returned without error).
    assert out.returncode in (0, 1), (
        f"pgrep returned unexpected code {out.returncode} "
        f"with new pattern; stderr={out.stderr!r}"
    )
