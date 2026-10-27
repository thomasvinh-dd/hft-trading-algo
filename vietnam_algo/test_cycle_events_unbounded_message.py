"""Tests for 2026-09-25 review issue #4: stop truncating ``message`` at
``cycle_events`` insert.

Before this fix, ``runner.py`` truncated the SOD and EOD messages
to 500 chars before storing them in Postgres ``cycle_events.message``:

    message=msg[:500]    # SOD  (line 875, pre-fix)
    message=eod_msg[:500] # EOD  (line 1166, pre-fix)

The ``cycle_events.message`` column is ``TEXT`` (unbounded per
``libs/schema_sql.py:147``). The operator-visible Telegram alert
that ``notify(...)`` sends just above is also unbounded. Truncating
at the DB insert step silently corrupted the audit trail — anyone
querying ``cycle_events`` for the full EOD summary would get a
clipped copy.

The fix passes the full message to ``_emit_cycle`` for both SOD
and EOD. The full message is *also* mirrored into
``daily_reports.raw_message`` (also TEXT), so the audit trail is
consistent across both tables.

These tests pin the new behaviour.
"""
from __future__ import annotations

from pathlib import Path


def test_sod_emit_cycle_passes_full_message_not_truncated():
    """``runner.py`` SOD path passes the full ``msg`` to
    ``_emit_cycle`` — no ``[:500]`` truncation. The pre-fix
    expression was ``message=msg[:500]``."""
    runner_src = (
        Path(__file__).parent.parent / "bots" / "runner.py"
    ).read_text()
    # The truncated form must NOT appear anywhere in the SOD emit
    # call. The EOD emit call has the same constraint (checked
    # below).
    assert "message=msg[:500]" not in runner_src, (
        "SOD _emit_cycle call must not truncate msg[:500] — "
        "cycle_events.message is TEXT (unbounded). "
        "See 2026-09-25 review issue #4."
    )


def test_eod_emit_cycle_passes_full_message_not_truncated():
    """``runner.py`` EOD path passes the full ``eod_msg`` to
    ``_emit_cycle`` — no ``[:500]`` truncation."""
    runner_src = (
        Path(__file__).parent.parent / "bots" / "runner.py"
    ).read_text()
    assert "message=eod_msg[:500]" not in runner_src, (
        "EOD _emit_cycle call must not truncate eod_msg[:500] — "
        "cycle_events.message is TEXT (unbounded). "
        "See 2026-09-25 review issue #4."
    )


def test_cycle_events_message_column_is_unbounded_text():
    """The schema column ``cycle_events.message`` is ``TEXT`` —
    not ``VARCHAR(N)`` — so it can hold the full operator-visible
    message without truncation."""
    schema_src = (
        Path(__file__).parent.parent / "libs" / "schema_sql.py"
    ).read_text()
    # Find the cycle_events CREATE TABLE block.
    import re
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS cycle_events \((.*?)\);",
        schema_src, re.DOTALL,
    )
    assert m is not None, "cycle_events CREATE TABLE not found"
    block = m.group(1)
    # The message column must be declared as TEXT, not VARCHAR(N).
    msg_match = re.search(r"message\s+(\w+(?:\([^)]+\))?)", block)
    assert msg_match is not None, "message column not declared"
    declared = msg_match.group(1).upper()
    assert declared == "TEXT", (
        f"cycle_events.message must be TEXT (unbounded); got {declared!r}. "
        f"Truncating to 500 chars at insert time would lose data."
    )


def test_emit_cycle_passes_message_through_unchanged():
    """End-to-end: ``_emit_cycle`` forwards the ``message`` arg
    verbatim to ``emit_cycle_event`` (no [:N] slicing inside the
    helper itself)."""
    from vietnam_algo.bots.runner import _emit_cycle

    captured: dict = {}

    def fake_emit(db, **kwargs):
        captured.update(kwargs)
        return 1

    _emit_cycle(
        db=None,  # early return — see below
        strategy="autofin_signal",
        event_type="eod",
        message="A" * 5000,
        payload={"date": "2026-09-25"},
        local_date="2026-09-25",
    )

    # When db is None, _emit_cycle returns early without calling
    # emit_cycle_event. To exercise the path we need a fake db.
    class _FakeDb:
        def insert_cycle_event(self, **kwargs):
            captured.update(kwargs)
            return 1

    _emit_cycle(
        db=_FakeDb(),
        strategy="autofin_signal",
        event_type="eod",
        message="A" * 5000,
        payload={"date": "2026-09-25"},
        local_date="2026-09-25",
    )

    # The full 5000-char message is forwarded — no [:500] inside
    # the helper.
    assert captured["message"] == "A" * 5000, (
        "_emit_cycle must forward the full message to "
        "emit_cycle_event without slicing. Got "
        f"{len(captured['message'])} chars."
    )
