"""Trading token helpers — fetch + expiry-aware caching for the live cycle.

The DNSE JWT token has a short expiry (8h). Previously the auto-bot
refreshed it every morning regardless of whether we'd actually trade,
which is wasteful: refresh involves sending email OTP, polling Gmail IMAP,
exchanging with DNSE — and we have to depend on email infra on days we
don't trade.

This module provides:
  - parse_jwt_expiry(token): read `exp` from JWT (no signature check)
  - is_token_fresh(token, leeway_seconds=300): True if token is present
    AND not expiring within `leeway_seconds`
  - ensure_fresh_trading_token(client, state, account_no, live_submit,
    notify_fn): if state.tradingToken is missing OR stale, refresh.
    In dry-run mode, sets a placeholder. Returns the resulting token.

The auto-bot's main loop should NOT refresh the token on a schedule; it
should only refresh when a submit cycle is about to run.

Decoupled OTP (2026-09-12)
--------------------------

With two bots on the same VPS for the same DNSE account, we now have a
dedicated ``vietnam_algo.jobs.get_otp`` job that's cron'd at 08:30 ICT.
It writes the fresh token to ``SHARED_TOKEN_PATH`` (default:
``data/live/dnse_trading_token.json``). The bots' submit-cycle refresh
path now prefers the shared file via ``load_shared_trading_token`` —
they only fall back to a self-refresh when the shared file is missing
or stale. This keeps the DNSE OTP flow serialised at exactly one
request per trading day.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


# Shared token file written by ``vietnam_algo.jobs.get_otp.run``.
# Other code paths should always go through ``load_shared_trading_token``
# so the location stays single-sourced.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SHARED_TOKEN_PATH = DEFAULT_DATA_DIR / "live" / "dnse_trading_token.json"


def parse_jwt_expiry(token: str) -> datetime | None:
    """Return the `exp` claim from a JWT (no signature verification).

    Returns None when the token is empty / not a JWT / malformed.
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1]
    # JWT base64url: padded with `=` to multiple of 4, then urlsafe_b64decode
    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8", errors="replace"))
    except Exception:
        return None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        try:
            return datetime.fromtimestamp(float(exp), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def is_token_fresh(
    token: str | None,
    leeway_seconds: int = 300,
    issued_at: str | None = None,
) -> bool:
    """True if token is set AND not expiring within `leeway_seconds`.

    `leeway_seconds` defaults to 5 minutes — a token about to expire
    in <5min is treated as stale so we have time to refresh before use.

    Tokens that aren't JWTs (DNSE rotated to plain UUIDs in
    2026-08) don't carry a parseable ``exp`` claim, so we fall back
    to the ``issued_at`` ISO timestamp: if the caller hands us the
    issuance time we accept the token as fresh when it's <12h old,
    otherwise we treat it as stale.
    """
    if not token or token == "dry-run-token":
        return False
    expiry = parse_jwt_expiry(token)
    if expiry is not None:
        return (expiry - datetime.now(tz=timezone.utc)).total_seconds() > leeway_seconds
    if issued_at:
        try:
            issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        except Exception:
            return False
        age = (datetime.now(tz=timezone.utc) - issued).total_seconds()
        # DNSE UUID tokens are good for the entire trading day;
        # treat anything <12h old as fresh. Older tokens need a refresh.
        return 0 <= age <= 12 * 3600
    # No way to verify expiry — assume stale to force a refresh.
    return False


def load_shared_trading_token(data_dir: Path | None = None) -> dict[str, Any] | None:
    """Read the shared DNSE trading token written by the OTP job.

    Returns None if the file is missing or unreadable. The job writes:
      ``{"token", "account_no", "issued_at", "expires_at", "date"}``

    ``data_dir`` is honoured for tests that want a sandbox path;
    production callers pass ``engine_config.dataDir``.
    """
    path = data_dir / "live" / "dnse_trading_token.json" if data_dir else SHARED_TOKEN_PATH
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("token"):
        return None
    return payload


def ensure_fresh_trading_token(
    client: Any,
    state: Any,
    account_no: str,
    live_submit: bool,
    refresh_func: Callable[[], str] | None = None,
    notify_fn: Callable[[str], Any] | None = None,
    dry_run_placeholder: str = "dry-run-token",
) -> tuple[str, bool]:
    """Return (token, was_refreshed).

    Behaviour:
      - live_submit=False: sets state.tradingToken = dry_run_placeholder if empty
      - live_submit=True: refreshes if token missing OR stale
      - refresh_func: optional override (default = dnse_client.email-otp flow)
      - notify_fn: optional; called with a single message string on refresh
    """
    token = getattr(state, "tradingToken", "") or ""
    if not live_submit:
        if not token:
            state.tradingToken = dry_run_placeholder
            return token, False
        return token, False

    if is_token_fresh(token):
        return token, False

    # Need to refresh.
    if refresh_func is None:
        # 2026-10-07: absolute import. ``auth.py`` lives in
        # ``vietnam_algo.libs.dnse`` but ``trading_token_from_email``
        # is defined in the parent package ``vietnam_algo.libs.daily_engine``,
        # so the previous relative import ``from .daily_engine`` only
        # worked when ``daily_engine.py`` happened to be co-located in
        # this sub-package. That module does not exist here, so the
        # live-mode submit cycle crashed with ``No module named
        # 'vietnam_algo.libs.dnse.daily_engine'`` the first time a
        # bot needed a token refresh.
        from vietnam_algo.libs.daily_engine import trading_token_from_email
        refresh_func = lambda: trading_token_from_email(client)

    if notify_fn is not None:
        notify_fn(
            "Vietnam Algo: refreshing DNSE trading token (lazy)\n"
            f"account: {account_no}\n"
            f"reason: missing or expiring soon"
        )

    new_token = refresh_func()
    state.tradingToken = new_token
    # Try to record expiry
    expiry = parse_jwt_expiry(new_token)
    if expiry is not None and hasattr(state, "tokenExpiry"):
        state.tokenExpiry = expiry.isoformat()
    if hasattr(state, "tokenDate"):
        state.tokenDate = datetime.now(tz=timezone.utc).date().isoformat()
    return new_token, True
