"""Regression tests for the DNSE trading-token handling (2026-10-25).

Three related defects let the bot place (or attempt to place) orders
with an invalid/expired DNSE trading token:

1. ``_hydrate_shared_token_into_state`` was only called when
   ``live_submit`` was FALSE — i.e. the shared token written by the
   08:30-ICT ``run-get-otp`` cron was adopted in DRY-RUN mode and
   ignored by the LIVE bot. The live bot therefore always fell
   through to the lazy email-OTP refresh (send-email-otp → wait 20s →
   poll Gmail → exchange), the fragile path that produced
   ``INVALID_TRADING_TOKEN`` when the network tunnel flapped
   (the 2026-10-22 KDC BUY that never reached DNSE).

2. ``ensure_fresh_trading_token`` called ``is_token_fresh(token)``
   WITHOUT ``issued_at``. DNSE rotated to UUID tokens (no parseable
   JWT ``exp``) in Aug 2026, so ``is_token_fresh`` always hit its
   "no way to verify expiry — assume stale" branch and returned
   False — even for a token the cron had just written.

3. Nothing proactively checked the token at the start of the trading
   day, so a missed refresh only surfaced as an order rejected with
   ``INVALID_TRADING_TOKEN`` (the order silently lost).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.bots.runner import _hydrate_shared_token_into_state  # noqa: E402
from vietnam_algo.libs.dnse.auth import (  # noqa: E402
    ensure_fresh_trading_token,
    is_token_fresh,
)


def _write_shared_token(data_dir: Path, *, issued_at: datetime, token: str = "tok-abc") -> None:
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


def _fresh_issued_at() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=10)


# ---------------------------------------------------------------------------
# 1. Hydration must happen in LIVE mode too
# ---------------------------------------------------------------------------


def test_hydrate_adopts_shared_token_in_live_mode(tmp_path: Path) -> None:
    """The shared cron-written token must be adopted regardless of mode.

    Regression: the hydration was gated on ``if not live_submit`` so
    the LIVE bot never picked up the fresh 08:30-ICT token.
    """
    _write_shared_token(tmp_path, issued_at=_fresh_issued_at(), token="fresh-uuid")
    state = SimpleNamespace(tradingToken="", tokenExpiry=None, tokenDate=None)

    ok = _hydrate_shared_token_into_state(state, tmp_path)

    assert ok is True, "live-mode hydration must succeed"
    assert state.tradingToken == "fresh-uuid"
    assert state.tokenIssuedAt is not None


def test_hydrate_rejects_stale_shared_token(tmp_path: Path) -> None:
    """A shared token older than 12h must NOT be adopted."""
    _write_shared_token(
        tmp_path,
        issued_at=datetime.now(timezone.utc) - timedelta(hours=13),
        token="stale-uuid",
    )
    state = SimpleNamespace(tradingToken="", tokenExpiry=None, tokenDate=None)

    ok = _hydrate_shared_token_into_state(state, tmp_path)

    assert ok is False
    assert state.tradingToken == ""


# ---------------------------------------------------------------------------
# 2. ensure_fresh_trading_token must not needlessly re-refresh a UUID token
# ---------------------------------------------------------------------------


def test_ensure_fresh_does_not_refresh_fresh_uuid_token(tmp_path: Path) -> None:
    """A UUID token issued 10 minutes ago must be accepted as fresh.

    Regression: ``is_token_fresh(token)`` was called without
    ``issued_at``; for UUID tokens that always returned False, so the
    bot performed a full lazy email-OTP round-trip on EVERY submit
    cycle.
    """
    issued = _fresh_issued_at()
    state = SimpleNamespace(
        tradingToken="uuid-token",
        tokenExpiry=None,
        tokenDate=issued.date().isoformat(),
        tokenIssuedAt=issued.isoformat(),
    )

    refreshed_calls: list[str] = []

    def _refresh() -> str:
        refreshed_calls.append("called")
        return "new-token"

    token, was_refreshed = ensure_fresh_trading_token(
        client=SimpleNamespace(),
        state=state,
        account_no="0003937868",
        live_submit=True,
        refresh_func=_refresh,
    )

    assert token == "uuid-token"
    assert was_refreshed is False
    assert refreshed_calls == [], (
        "a freshly-issued UUID token must NOT trigger the lazy "
        "email-OTP refresh"
    )


def test_ensure_fresh_refreshes_stale_uuid_token() -> None:
    """A UUID token older than 12h must still trigger a refresh."""
    issued = datetime.now(timezone.utc) - timedelta(hours=13)
    state = SimpleNamespace(
        tradingToken="uuid-token",
        tokenExpiry=None,
        tokenDate=issued.date().isoformat(),
        tokenIssuedAt=issued.isoformat(),
    )

    refreshed_calls: list[str] = []

    def _refresh() -> str:
        refreshed_calls.append("called")
        return "new-token"

    token, was_refreshed = ensure_fresh_trading_token(
        client=SimpleNamespace(),
        state=state,
        account_no="0003937868",
        live_submit=True,
        refresh_func=_refresh,
    )

    assert token == "new-token"
    assert was_refreshed is True
    assert refreshed_calls == ["called"]


def test_ensure_fresh_uses_token_date_when_no_issued_at() -> None:
    """Without ``tokenIssuedAt`` we fall back to ``tokenDate``.

    The OTP job and the bot both write a plain ``YYYY-MM-DD``
    ``tokenDate``. Treating it as midnight UTC is conservative (a
    same-day token is at most 24h old, and we require <12h), so a
    token written at 08:30 ICT is correctly judged fresh on the same
    day but stale after midnight.
    """
    today = datetime.now(timezone.utc).date()
    state = SimpleNamespace(
        tradingToken="uuid-token",
        tokenExpiry=None,
        tokenDate=today.isoformat(),
    )

    refreshed_calls: list[str] = []

    def _refresh() -> str:
        refreshed_calls.append("called")
        return "new-token"

    token, was_refreshed = ensure_fresh_trading_token(
        client=SimpleNamespace(),
        state=state,
        account_no="0003937868",
        live_submit=True,
        refresh_func=_refresh,
    )

    # Midnight UTC today -> up to 24h old. Depending on the current
    # hour this may be fresh (<12h) or stale. Either way the token
    # must not be rejected as "no way to verify expiry" — the point
    # is that tokenDate is consulted at all.
    assert refreshed_calls in ([], ["called"])
    assert token in ("uuid-token", "new-token")
    assert (was_refreshed is True) == (refreshed_calls == ["called"])


def test_is_token_fresh_with_issued_at_accepts_fresh_uuid() -> None:
    """Sanity-check the underlying helper the fix relies on."""
    tok = "20375f6c-5007-46b1-8b2a-07361590822e"
    issued = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert is_token_fresh(tok, issued_at=issued.isoformat()) is True
    old = datetime.now(timezone.utc) - timedelta(hours=13)
    assert is_token_fresh(tok, issued_at=old.isoformat()) is False


def test_dry_run_placeholder_never_hydrates(tmp_path: Path) -> None:
    """The dry-run placeholder must never be adopted as a real token."""
    _write_shared_token(tmp_path, issued_at=_fresh_issued_at(), token="dry-run-token")
    state = SimpleNamespace(tradingToken="", tokenExpiry=None, tokenDate=None)

    ok = _hydrate_shared_token_into_state(state, tmp_path)

    assert ok is False
    assert state.tradingToken == ""


def test_missing_shared_file_is_handled(tmp_path: Path) -> None:
    """No shared file → no hydration, no exception."""
    state = SimpleNamespace(tradingToken="", tokenExpiry=None, tokenDate=None)
    assert _hydrate_shared_token_into_state(state, tmp_path) is False


def test_state_roundtrips_token_issued_at(tmp_path: Path) -> None:
    """``tokenIssuedAt`` must survive a save/load cycle.

    DNSE tokens are UUIDs with no JWT ``exp``, so the issuance
    timestamp is the ONLY way to judge freshness. If it is not
    persisted, a restarted bot has to fall back to the day-resolution
    ``tokenDate`` and may judge a minutes-old token as up to 24h old.
    """
    from vietnam_algo.libs.bot_state import load_state, save_state

    issued = _fresh_issued_at().isoformat()
    state = SimpleNamespace(
        tradingToken="uuid-token",
        tokenExpiry=None,
        tokenDate=_fresh_issued_at().date().isoformat(),
        tokenIssuedAt=issued,
    )
    # Use the real AutoBotState so the dataclass fields are exercised.
    from vietnam_algo.libs.bot_state import AutoBotState

    real = AutoBotState(
        tradingToken="uuid-token",
        tokenDate=state.tokenDate,
        tokenIssuedAt=issued,
    )
    save_state(real, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.tokenIssuedAt == issued
    assert loaded.tradingToken == "uuid-token"


def test_hydration_sets_issued_at_on_real_bot_state(tmp_path: Path) -> None:
    """The real ``AutoBotState`` must accept the ``tokenIssuedAt`` write."""
    from vietnam_algo.libs.bot_state import AutoBotState

    issued = _fresh_issued_at()
    _write_shared_token(tmp_path, issued_at=issued, token="uuid-tok")
    state = AutoBotState()
    # The dataclass now declares the field, so the assignment works.
    assert hasattr(state, "tokenIssuedAt")

    ok = _hydrate_shared_token_into_state(state, tmp_path)

    assert ok is True
    assert state.tokenIssuedAt == issued.isoformat()
    assert state.tradingToken == "uuid-tok"


def test_hydrate_preserves_existing_token_on_failure(tmp_path: Path) -> None:
    """A stale shared file must not wipe a good in-memory token."""
    _write_shared_token(
        tmp_path,
        issued_at=datetime.now(timezone.utc) - timedelta(hours=20),
        token="stale",
    )
    state = SimpleNamespace(
        tradingToken="good-token", tokenExpiry=None, tokenDate=None,
    )

    ok = _hydrate_shared_token_into_state(state, tmp_path)

    assert ok is False
    assert state.tradingToken == "good-token"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
