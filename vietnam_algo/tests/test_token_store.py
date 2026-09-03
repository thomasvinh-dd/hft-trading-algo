"""Tests for token expiry parsing + freshness check."""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone, timedelta

import pytest

from vietnam_algo.token_store import parse_jwt_expiry, is_token_fresh


def _make_jwt(exp_unix: float) -> str:
    """Build a minimal JWT (header.payload.signature) with the given exp."""
    def b64u(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64u({'alg': 'none'})}.{b64u({'exp': exp_unix})}.fake-sig"


def test_parse_jwt_expiry_returns_datetime_for_valid_token():
    exp = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    exp_unix = exp.timestamp()
    tok = _make_jwt(exp_unix)
    parsed = parse_jwt_expiry(tok)
    assert parsed is not None
    assert abs((parsed - exp).total_seconds()) < 1


def test_parse_jwt_expiry_returns_none_for_empty():
    assert parse_jwt_expiry("") is None
    assert parse_jwt_expiry(None) is None  # type: ignore[arg-type]


def test_parse_jwt_expiry_returns_none_for_garbage():
    assert parse_jwt_expiry("not-a-jwt") is None
    assert parse_jwt_expiry("abc.def") is None
    assert parse_jwt_expiry("a.b.c.d") is None
    assert parse_jwt_expiry(".....") is None


def test_parse_jwt_expiry_returns_none_for_expired_no_claim():
    # payload has no `exp` claim
    tok = f"hdr.{base64.urlsafe_b64encode(b'{}').rstrip(b'=').decode()}.sig"
    assert parse_jwt_expiry(tok) is None


def test_is_token_fresh_for_fresh_token():
    exp = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    assert is_token_fresh(_make_jwt(exp.timestamp())) is True


def test_is_token_fresh_for_expired_token():
    exp = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    assert is_token_fresh(_make_jwt(exp.timestamp())) is False


def test_is_token_fresh_within_leeway_window():
    # 60s in future — with default leeway 300s, this is stale.
    exp = datetime.now(tz=timezone.utc) + timedelta(seconds=60)
    assert is_token_fresh(_make_jwt(exp.timestamp()), leeway_seconds=300) is False
    # With 30s leeway, it would be fresh.
    assert is_token_fresh(_make_jwt(exp.timestamp()), leeway_seconds=30) is True


def test_is_token_fresh_for_dry_run_placeholder():
    assert is_token_fresh("dry-run-token") is False
    assert is_token_fresh(None) is False  # type: ignore[arg-type]
    assert is_token_fresh("") is False
