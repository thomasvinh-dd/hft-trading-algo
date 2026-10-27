"""Tests for the 2026-10-21 EOD-when-stale-held-position alert.

Background
----------
Pre-fix, the bot silently emitted ``count: 0`` orders on days when
the momentum backtest couldn't reach ``trade_date`` (e.g., because
the held symbol's price file was 67+ calendar days stale). The
Telegram EOD summary reported ``count: 0`` as if everything were
healthy — meanwhile the dry-run state held a 9,500-share MST
position it couldn't reprice.

The fix adds a "0 orders but stale + held" alert that fires in the
EOD path. To stay robust against future refactors, these tests
scan ``bots/runner.py`` for the new alert's contract markers and
use a source-text regex approach so we don't need to spin up the
full bot loop.
"""

from __future__ import annotations

from pathlib import Path


def _runner_text() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "bots"
        / "runner.py"
    ).read_text(encoding="utf-8")


def test_eod_block_contains_stale_held_alert_header() -> None:
    """The EOD alert must mention 'EOD emitted 0 orders' so an
    operator glancing at Telegram immediately recognises it.
    """
    src = _runner_text()
    needle = "EOD emitted 0 orders"
    assert needle in src, (
        f"EOD stale-held alert header missing — the 2026-10-21 MST "
        f"scenario where the bot emitted 0 orders but held a "
        f"stale-priced position will again be silent. "
        f"Expected to find {needle!r} in bots/runner.py"
    )


def test_alert_includes_operator_actionable_hint() -> None:
    """Operators can't act on the alert unless they know what to
    do. The message must include a ``sync_prices_incremental``
    hint, mirroring the 2026-10-21 incident response.
    """
    src = _runner_text()
    assert "sync_prices_incremental" in src, (
        "EOD stale-held alert must reference "
        "sync_prices_incremental so the operator can refresh the "
        "price file in one command."
    )


def test_alert_computes_intersection_of_held_and_stale() -> None:
    """Only symbols that are BOTH in ``stale_held_positions`` AND
    in the dry-run state ``positions`` are actionable. The alert
    must compute this intersection explicitly.
    """
    src = _runner_text()
    # Search for the intersection logic. The variable name from the
    # fix branch is ``held_stale_now``.
    assert "held_stale_now" in src, (
        "EOD stale-held alert must compute the intersection of "
        "stale_held_positions and dry-run state positions."
    )


def test_alert_caps_listing_at_10_with_more_marker() -> None:
    """Cap the bullet list at 10 held symbols with a ``+ N more``
    continuation line, mirroring the rest of the operator alerts.
    """
    src = _runner_text()
    # Look for the iteration-with-cap pattern.
    assert "held_stale_now[:10]" in src, (
        "EOD stale-held alert must cap iteration at 10."
    )
    assert "+ {len(held_stale_now) - 10} more" in src or "+ {len(held_stale_now) - 10} more".format() in src, (
        "EOD stale-held alert must include a +N more continuation line."
    )


def test_alert_gated_on_dry_run_mode() -> None:
    """Live submit mode reads positions from DNSE; the
    dry-run-state heuristic doesn't apply. The alert must be
    gated on ``not live_submit``.
    """
    src = _runner_text()
    # The alert must reference ``not live_submit``.
    assert "not live_submit" in src, (
        "EOD stale-held alert must be gated on the dry-run mode flag."
    )


def test_freshness_alert_extended_with_stale_held_section() -> None:
    """The pre-existing 'market data STALE' heartbeat alert (sent
    when ``freshness['fresh']`` is False) is the operator's primary
    on-call channel. We pin that it now contains BOTH the legacy
    median-age text AND a new 'stale HELD POSITIONS' section so the
    operator can act on named symbols.
    """
    src = _runner_text()
    assert "stale HELD POSITIONS" in src, (
        "freshness alert must include the 'stale HELD POSITIONS' "
        "section that lists the actionable symbols."
    )
    assert "no_price_file" in src, (
        "freshness alert must surface the 'no_price_file' reason so "
        "operators know when a held symbol has no price data at all."
    )
