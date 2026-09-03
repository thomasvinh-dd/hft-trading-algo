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
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable


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


def is_token_fresh(token: str | None, leeway_seconds: int = 300) -> bool:
    """True if token is set AND not expiring within `leeway_seconds`.

    `leeway_seconds` defaults to 5 minutes — a token about to expire
    in <5min is treated as stale so we have time to refresh before use.
    """
    if not token or token == "dry-run-token":
        return False
    expiry = parse_jwt_expiry(token)
    if expiry is None:
        # Can't determine expiry — assume stale to force a refresh.
        return False
    return (expiry - datetime.now(tz=timezone.utc)).total_seconds() > leeway_seconds


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
        from .daily_engine import trading_token_from_email
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
