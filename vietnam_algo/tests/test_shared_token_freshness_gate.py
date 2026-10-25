"""Regression tests for the mid-day shared-token freshness gate (2026-10-25).

The mid-day poll path hydrates the DNSE trading token from the shared
cron-written file. Before the fix it adopted whatever was in the file
without checking freshness — so on a day when the 08:30-ICT
``run-get-otp`` cron missed its window (DST fall-back on 2026-10-25,
or a tunnel ``407``), the mid-day path would happily adopt the STALE
token and every submit would be rejected with
``INVALID_TRADING_TOKEN``.

Adopting a stale token is worse than having no token: with no token
the submit gate skips the order loudly; with a stale token the order
is attempted and fails opaquely.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.libs.dnse.auth import is_token_fresh  # noqa: E402


def _write_token(data_dir: Path, issued_at: datetime, token: str) -> None:
    live = data_dir / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "dnse_trading_token.json").write_text(
        json.dumps({
            "token": token,
            "account_no": "0003937868",
            "issued_at": issued_at.isoformat(),
            "expires_at": None,
            "date": issued_at.date().isoformat(),
        }),
        encoding="utf-8",
    )


def test_fresh_shared_token_is_adopted(tmp_path: Path) -> None:
    """A token written by the cron minutes ago must be usable."""
    _write_token(
        tmp_path,
        datetime.now(timezone.utc) - timedelta(minutes=5),
        "fresh-token",
    )
    assert is_token_fresh(
        "fresh-token",
        issued_at=(
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat(),
    ) is True


def test_stale_shared_token_is_rejected(tmp_path: Path) -> None:
    """A token from a missed cron window must NOT be adopted."""
    issued = datetime.now(timezone.utc) - timedelta(hours=30)
    _write_token(tmp_path, issued, "stale-token")
    assert is_token_fresh(
        "stale-token", issued_at=issued.isoformat(),
    ) is False


def test_token_exactly_12h_old_is_still_accepted(tmp_path: Path) -> None:
    """The freshness window is 12h; just inside it must pass."""
    issued = datetime.now(timezone.utc) - timedelta(hours=11, minutes=50)
    _write_token(tmp_path, issued, "borderline")
    assert is_token_fresh(
        "borderline", issued_at=issued.isoformat(),
    ) is True


def test_missing_issued_at_is_treated_as_stale(tmp_path: Path) -> None:
    """Without an issuance time we cannot verify the token → stale."""
    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "dnse_trading_token.json").write_text(
        json.dumps({"token": "no-issued-at", "account_no": "0003937868"}),
        encoding="utf-8",
    )
    payload = json.loads(
        (live / "dnse_trading_token.json").read_text(encoding="utf-8")
    )
    assert is_token_fresh(
        payload["token"], issued_at=payload.get("issued_at"),
    ) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
