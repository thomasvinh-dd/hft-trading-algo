"""DNSE integration: HTTP client, OAuth/email-OTP auth, order helpers.

Sub-modules:
  - client : DnseClient — REST API wrapper (accounts, instruments,
             prices, orders, positions, loan packages).
  - auth   : Trading-token storage helpers, JWT/UUID freshness check,
             shared token file used by the cron'd get_otp job.
  - otp    : Gmail IMAP helper that extracts the OTP code from a
             DNSE notification email.

Re-export the public surface here so callers can do
``from vietnam_algo.libs.dnse import DnseClient`` rather than
reaching into ``.client`` / ``.auth`` / ``.otp`` separately.
"""
from .auth import (
    DEFAULT_DATA_DIR,
    SHARED_TOKEN_PATH,
    ensure_fresh_trading_token,
    is_token_fresh,
    load_shared_trading_token,
    parse_jwt_expiry,
)
from .client import DnseClient
from .otp import extract_otp, latest_email_otp

__all__ = [
    "DEFAULT_DATA_DIR",
    "DnseClient",
    "SHARED_TOKEN_PATH",
    "ensure_fresh_trading_token",
    "extract_otp",
    "is_token_fresh",
    "latest_email_otp",
    "load_shared_trading_token",
    "parse_jwt_expiry",
]
