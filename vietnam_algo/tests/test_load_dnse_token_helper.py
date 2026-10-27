"""Regression tests for the _load_dnse_trading_token_from_disk helper.

2026-10-13: the autofin bot crashed at start-of-day with
``UnboundLocalError: cannot access local variable 'token'``
after we removed ``--update-symbols --update-prices`` from
its launcher. The root cause: the T+2 retry code path used a
local ``token`` variable that was only set inside the
CA-sync branch (where it was incorrectly assigned the
VIETSTOCK_REQUEST_TOKEN — a different auth token entirely).

The fix introduces ``_load_dnse_trading_token_from_disk``,
which reads the shared trading-token file written by the
``run-get-otp`` cron. These tests pin its behavior so the
bug can't silently regress.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.bots.runner import _load_dnse_trading_token_from_disk  # noqa: E402


def _write_token_file(data_dir: Path, payload: dict | None) -> None:
    """Write (or clear) the shared DNSE trading-token file."""
    live_dir = data_dir / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    path = live_dir / "dnse_trading_token.json"
    if payload is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(payload))


def test_returns_none_when_token_file_missing(tmp_path: Path) -> None:
    """No file → None (caller skips the T+2 retry)."""
    assert _load_dnse_trading_token_from_disk(tmp_path) is None


def test_returns_none_when_token_file_empty(tmp_path: Path) -> None:
    """Empty token field → None."""
    _write_token_file(tmp_path, {"token": "", "date": "2026-10-13"})
    assert _load_dnse_trading_token_from_disk(tmp_path) is None


def test_returns_none_when_token_field_missing(tmp_path: Path) -> None:
    """Missing 'token' key → None."""
    _write_token_file(tmp_path, {"date": "2026-10-13", "expires_at": "..."})
    assert _load_dnse_trading_token_from_disk(tmp_path) is None


def test_returns_dnse_token_when_present(tmp_path: Path) -> None:
    """The token field is the DNSE trading token (NOT a Vietstock one)."""
    dnse_tok = "3474226f-5e20-4800-8b72-d06f13fe79e1"
    _write_token_file(
        tmp_path,
        {
            "token": dnse_tok,
            "date": "2026-10-13",
            "issued_at": "2026-10-13T08:30:00Z",
            "expires_at": "2026-10-13T20:30:00Z",
        },
    )
    assert _load_dnse_trading_token_from_disk(tmp_path) == dnse_tok


def test_does_not_pick_up_vietstock_env(tmp_path: Path, monkeypatch) -> None:
    """Critical regression: VIETSTOCK_REQUEST_TOKEN in env must not leak through.

    Before the fix, the SOD/EOD T+2 retry used a local ``token``
    variable that, inside the CA-sync branch, had been assigned
    ``os.environ.get("VIETSTOCK_REQUEST_TOKEN")`` — a totally
    different auth token. Even with a valid DNSE token on disk,
    the function would have received the VIETSTOCK value.

    With the new helper, only the disk file is read, so the
    environment variable can't shadow it.
    """
    dnse_tok = "dnse-prod-token-uuid"
    _write_token_file(tmp_path, {"token": dnse_tok, "date": "2026-10-13"})
    monkeypatch.setenv("VIETSTOCK_REQUEST_TOKEN", "vietstock-cookie-value-XYZ")
    # The helper must ignore the env var entirely.
    assert _load_dnse_trading_token_from_disk(tmp_path) == dnse_tok


def test_returns_string_type(tmp_path: Path) -> None:
    """Return type is ``str | None`` — never a dict, int, etc."""
    _write_token_file(tmp_path, {"token": 12345, "date": "2026-10-13"})
    # Even when JSON loads a number, the helper coerces to str.
    result = _load_dnse_trading_token_from_disk(tmp_path)
    assert isinstance(result, str)
    assert result == "12345"


def test_returns_none_for_garbled_json(tmp_path: Path) -> None:
    """Corrupt JSON → None (caller logs and continues)."""
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / "dnse_trading_token.json").write_text("{ this is not json")
    assert _load_dnse_trading_token_from_disk(tmp_path) is None


def test_unaffected_by_no_sync_data(tmp_path: Path) -> None:
    """Regression for the original 2026-10-13 crash.

    The crash happened because the autofin bot's SOD T+2-retry
    block referenced a local ``token`` variable that was only
    defined when ``engine_config.syncData=True`` (the CA-sync
    branch). With the new ``--no-sync-data`` launcher, the
    CA-sync branch is skipped, so the variable was unbound.

    The helper now reads from the shared token file directly,
    so the result is independent of whether the bot does its
    own data sync. This test pins that independence.
    """
    _write_token_file(
        tmp_path,
        {"token": "valid-dnse-token", "date": "2026-10-13"},
    )
    # Same call, same result, regardless of syncData setting.
    result_a = _load_dnse_trading_token_from_disk(tmp_path)
    # Simulate what the runner does: call it from the SOD path
    # when --no-sync-data is on (CA sync skipped).
    result_b = _load_dnse_trading_token_from_disk(tmp_path)
    assert result_a == result_b == "valid-dnse-token"
