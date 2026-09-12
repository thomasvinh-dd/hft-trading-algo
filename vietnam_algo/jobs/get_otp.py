"""Standalone job: DNSE email OTP -> fresh trading token, written to a
shared file that all trading bots can read.

Run this once per VN trading day, ideally at 08:30 ICT (just before
the 09:00 ICT start-of-day briefing).

Idempotent:
  - skips when a token from today's date already exists in the shared
    ``data/live/dnse_trading_token.json`` file.
  - the underlying ``trading_token_from_email`` only sends a fresh OTP
    email when DNSE actually requires one (lazy refresh path).

Why this is its own job
-----------------------

Both autofin and momentum bots used to call ``trading_token_from_email``
on the first submit cycle, sometimes at the same minute. DNSE appears
to only accept one OTP request per session — the second bot's call
would error or "no-otp-email" the existing one out. By having a single
job write the shared ``dnse_trading_token.json`` file, we cap OTP
generation at exactly one per trading day.

The bots are then expected to read the shared file instead of hitting
DNSE themselves; their lazy-refresh path inside
``ensure_fresh_trading_token`` already short-circuits when the
in-memory token is fresh, so we just need to give it a way to read
the shared file first.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..daily_engine import trading_token_from_email
from ..data import DEFAULT_DATA_DIR, read_json, write_json
from ..dnse_client import DnseClient
from ..token_store import parse_jwt_expiry


SHARED_TOKEN_PATH = DEFAULT_DATA_DIR / "live" / "dnse_trading_token.json"


@dataclass
class SharedTradingToken:
    token: str
    account_no: str
    issued_at: str  # ISO 8601 UTC
    expires_at: str | None  # ISO 8601 UTC, parsed from JWT exp claim
    date: str  # YYYY-MM-DD (VN local date when the token was issued)


def _load_shared() -> dict | None:
    if not SHARED_TOKEN_PATH.exists():
        return None
    try:
        payload = read_json(SHARED_TOKEN_PATH)
        if not isinstance(payload, dict):
            return None
        return payload
    except Exception:
        return None


def _today_utc() -> str:
    # We compare on the issue date so multiple jobs run on the same
    # UTC day stamp the same token once and skip subsequent runs.
    return datetime.now(tz=timezone.utc).date().isoformat()


def already_refreshed_today() -> bool:
    """True if the shared token was issued for today's UTC date."""
    payload = _load_shared()
    if not payload:
        return False
    return payload.get("date") == _today_utc() and bool(payload.get("token"))


def run(client: DnseClient, account_no: str) -> SharedTradingToken:
    """Fetch a fresh trading token via DNSE email-OTP and persist it.

    Returns the SharedTradingToken; raises on failure. Skips work if a
    token is already in the shared store and was issued today.
    """
    if already_refreshed_today():
        payload = _load_shared() or {}
        return SharedTradingToken(
            token=str(payload.get("token") or ""),
            account_no=str(payload.get("account_no") or account_no),
            issued_at=str(payload.get("issued_at") or ""),
            expires_at=payload.get("expires_at"),
            date=str(payload.get("date") or ""),
        )

    print(f"[get_otp] refreshing trading token for {account_no}")
    new_token = trading_token_from_email(client, otp_type="email_otp")
    expiry = parse_jwt_expiry(new_token)
    issued = datetime.now(tz=timezone.utc).isoformat()
    payload = {
        "token": new_token,
        "account_no": account_no,
        "issued_at": issued,
        "expires_at": expiry.isoformat() if expiry else None,
        "date": _today_utc(),
    }
    SHARED_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(SHARED_TOKEN_PATH, payload)
    print(f"[get_otp] wrote {SHARED_TOKEN_PATH}")
    return SharedTradingToken(
        token=new_token,
        account_no=account_no,
        issued_at=issued,
        expires_at=payload["expires_at"],
        date=payload["date"],
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m vietnam_algo.jobs.get_otp``.

    Reads DNSE_API_KEY / DNSE_API_SECRET / DNSE_ACCOUNT_NO from the
    environment (same as the auto-trading-bot), runs ``run()``, and
    returns 0/1. Designed for cron invocation — exit 0 on success.
    """
    from ..main import dnse_client_from_args  # deferred to avoid a
                                              # cycle in some test runners
    api_key = os.environ.get("DNSE_API_KEY", "")
    api_secret = os.environ.get("DNSE_API_SECRET", "")
    account_no = os.environ.get("DNSE_ACCOUNT_NO", "")
    if not api_key or not api_secret:
        print("ERROR: DNSE_API_KEY and DNSE_API_SECRET must be set", file=sys.stderr)
        return 2
    if not account_no:
        print("ERROR: DNSE_ACCOUNT_NO must be set", file=sys.stderr)
        return 2
    import argparse
    parser = argparse.ArgumentParser(description="Refresh DNSE trading token for the day")
    parser.add_argument("--account-no", default=account_no)
    parser.parse_args(argv)
    client = DnseClient(api_key=api_key, api_secret=api_secret)
    try:
        token = run(client, account_no=account_no)
    except Exception as exc:
        print(f"[get_otp] FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(token), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
