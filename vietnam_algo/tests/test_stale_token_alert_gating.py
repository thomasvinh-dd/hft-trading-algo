"""Tests for the stale-token ALERT gating (2026-10-31).

The operator reported the alert firing at 22:46 on a SATURDAY::

    🚨 Vietnam Algo: DNSE trading token is STALE
    account: 0003937868
    issued_at: 2026-10-29T01:30:26.712817+00:00
    The 08:30-ICT get_otp cron may have missed its window. Orders
    will be SKIPPED (not sent with a bad token) until the token
    refreshes.

There was no trading session and no order to send — the alert was
pure noise.

The user's rule: the alert should only fire in a LIVE TRADING SESSION,
and only when there is actually an ORDER that the stale token blocks.
The token is renewed lazily (when a new order is planned), so a stale
token is only actionable at the moment it would block a submit.

The alert therefore requires ALL of:
  1. At least one order to send.
  2. Inside a live trading session.
  3. The shared token genuinely stale.

It is still throttled to once per ICT day.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ICT = ZoneInfo("Asia/Ho_Chi_Minh")


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------

def _write_stale_token(data_dir: Path, issued_at: str = "2026-10-29T01:30:26+00:00") -> None:
    """Write a shared trading token that is OLD (past the 12h window)."""
    live = data_dir / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "dnse_trading_token.json").write_text(json.dumps({
        "token": "stale-token-value",
        "account_no": "0003937868",
        "issued_at": issued_at,
        "date": issued_at[:10],
    }))


def _write_fresh_token(data_dir: Path, issued_at: str | None = None) -> None:
    """Write a shared trading token that is FRESH (<12h old)."""
    if issued_at is None:
        issued_at = datetime.datetime.now(tz=ICT).isoformat()
    live = data_dir / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "dnse_trading_token.json").write_text(json.dumps({
        "token": "fresh-token-value",
        "account_no": "0003937868",
        "issued_at": issued_at,
        "date": issued_at[:10],
    }))


def _deals(n: int = 1) -> list[dict]:
    return [{
        "action": "new_entry",
        "symbol": "VOS",
        "deal_id": "1792627200:VOS",
        "dnse_orders": [{
            "symbol": "VOS", "side": "NB", "price": 11100,
            "quantity": 300, "kind": "entry",
            "reason": "autofin_buy:deal#1792627200:VOS:nav15pct:x",
        }],
    }] * n


def _at(hh: int, mm: int, day: int = 2, month: int = 11) -> datetime.datetime:
    return datetime.datetime(2026, month, day, hh, mm, tzinfo=ICT)


def _call(monkeypatch, tmp_path, deals, when, alert_state=None):
    import vietnam_algo.bots.runner as runner_mod

    messages: list[str] = []
    monkeypatch.setattr(runner_mod, "notify", lambda m: messages.append(m))
    monkeypatch.setattr(
        runner_mod, "vietnam_now", lambda: when,
    )

    sent = runner_mod._notify_stale_token_if_blocking(
        tmp_path,
        "0003937868",
        deals,
        now=when,
        alert_state=alert_state if alert_state is not None else {},
    )
    return sent, messages


# -----------------------------------------------------------------------------
# the rule
# -----------------------------------------------------------------------------

def test_no_alert_without_orders(tmp_path, monkeypatch) -> None:
    """No orders to send -> the token is not blocking anything."""
    _write_stale_token(tmp_path)
    sent, messages = _call(monkeypatch, tmp_path, [], _at(10, 0))
    assert sent is False
    assert messages == []


def test_no_alert_outside_the_trading_session(tmp_path, monkeypatch) -> None:
    """The exact reported case: Saturday evening, no session."""
    _write_stale_token(tmp_path)
    # 2026-10-31 is a Saturday.
    sent, messages = _call(
        monkeypatch, tmp_path, _deals(),
        datetime.datetime(2026, 10, 31, 22, 46, tzinfo=ICT),
    )
    assert sent is False, "Saturday must not raise the alert"
    assert messages == []


@pytest.mark.parametrize(
    "when",
    [
        datetime.datetime(2026, 10, 31, 6, 0, tzinfo=ICT),    # Sat 06:00
        datetime.datetime(2026, 10, 31, 22, 46, tzinfo=ICT),  # Sat 22:46
        datetime.datetime(2026, 11, 1, 10, 0, tzinfo=ICT),   # Sun 10:00
        datetime.datetime(2026, 11, 2, 8, 0, tzinfo=ICT),    # Mon 08:00 (pre-open)
        datetime.datetime(2026, 11, 2, 12, 0, tzinfo=ICT),   # Mon 12:00 (lunch)
        datetime.datetime(2026, 11, 2, 16, 0, tzinfo=ICT),   # Mon 16:00 (after close)
    ],
)
def test_no_alert_outside_a_trading_session(tmp_path, monkeypatch, when) -> None:
    """Only a live trading session may raise the alert."""
    _write_stale_token(tmp_path)
    sent, messages = _call(monkeypatch, tmp_path, _deals(), when)
    assert sent is False, when
    assert messages == [], when


@pytest.mark.parametrize(
    "when",
    [
        _at(9, 30),
        _at(10, 0),
        _at(11, 0),
        _at(13, 30),
        _at(14, 0),
    ],
)
def test_alert_fires_in_a_session_with_orders(tmp_path, monkeypatch, when) -> None:
    """In a live session, with orders, and a stale token -> ALERT."""
    _write_stale_token(tmp_path)
    sent, messages = _call(monkeypatch, tmp_path, _deals(), when)
    assert sent is True, when
    assert len(messages) == 1, messages
    assert "STALE" in messages[0]
    assert "ORDERS BLOCKED" in messages[0]
    assert "VOS" in messages[0]


def test_no_alert_when_the_token_is_fresh(tmp_path, monkeypatch) -> None:
    """A fresh token is not stale — nothing is blocked."""
    _write_fresh_token(tmp_path)
    sent, messages = _call(monkeypatch, tmp_path, _deals(), _at(10, 0))
    assert sent is False
    assert messages == []


def test_no_alert_when_there_is_no_shared_token(tmp_path, monkeypatch) -> None:
    """No token file at all -> the lazy refresh path handles it."""
    sent, messages = _call(monkeypatch, tmp_path, _deals(), _at(10, 0))
    assert sent is False
    assert messages == []


def test_the_alert_is_throttled_to_once_per_day(tmp_path, monkeypatch) -> None:
    """A missed cron must not spam — at most one alert per ICT day."""
    _write_stale_token(tmp_path)
    state: dict = {}

    sent, _ = _call(monkeypatch, tmp_path, _deals(), _at(10, 0), state)
    assert sent is True

    # Later the same day -> suppressed.
    sent, _ = _call(monkeypatch, tmp_path, _deals(), _at(13, 0), state)
    assert sent is False, "the second alert on the same day is suppressed"

    # The next day -> fires again.
    sent, _ = _call(
        monkeypatch, tmp_path, _deals(),
        datetime.datetime(2026, 11, 3, 10, 0, tzinfo=ICT), state,
    )
    assert sent is True


def test_the_alert_names_the_blocked_orders(tmp_path, monkeypatch) -> None:
    """The operator must see WHICH orders are stuck."""
    _write_stale_token(tmp_path)
    deals = [
        {"action": "new_entry", "symbol": "VOS", "deal_id": "1",
         "dnse_orders": [{"symbol": "VOS", "side": "NB", "price": 11100,
                          "quantity": 300, "kind": "entry", "reason": "x"}]},
        {"action": "partial_close", "symbol": "TCB", "deal_id": "2",
         "dnse_orders": [{"symbol": "TCB", "side": "NS", "price": 22500,
                          "quantity": 100, "kind": "tp", "reason": "y"}]},
    ]
    sent, messages = _call(monkeypatch, tmp_path, deals, _at(10, 0))
    assert sent is True
    msg = messages[0]
    assert "VOS" in msg
    assert "TCB" in msg
    assert "2" in msg, "the order count must be reported"


# -----------------------------------------------------------------------------
# the per-cycle hydration block must NOT alert any more
# -----------------------------------------------------------------------------

def test_the_source_no_longer_alerts_from_the_hydration_block() -> None:
    """The unconditional alert was removed from the per-cycle block.

    The hydration block runs every ~60 s, all day and all night. It
    must only LOG (throttled to once an hour), never notify.
    """
    src = (Path(__file__).resolve().parents[1] / "bots" / "runner.py").read_text()
    # The old inline notify is gone.
    assert "🚨 Vietnam Algo: DNSE trading token is STALE\\n" not in src, (
        "the unconditional stale-token Telegram alert is still in the source"
    )
    # The new, gated alert exists.
    assert "ORDERS BLOCKED" in src
    assert "_notify_stale_token_if_blocking" in src
