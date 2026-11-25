"""Tests for the 2026-10-31 tuning changes.

The user asked for two things::

    "now seem like the trading session for each 1 hour, i wanna
     change it to 30 mins, and i wanna change the buy window from
     +- 3% to +-1%"

1. The mid-day poll cadence: 1 hour → 30 minutes.
2. The buy-zone tolerance: +-3 % → +-1 % (both sides).

Both are now env-var driven so they can be tuned per environment
without a code edit.
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.schedule import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    poll_interval_seconds,
    should_poll_strategy,
)

ICT = ZoneInfo("Asia/Ho_Chi_Minh")


# -----------------------------------------------------------------------------
# 1. the poll cadence
# -----------------------------------------------------------------------------

def test_the_default_poll_interval_is_30_minutes(monkeypatch) -> None:
    """The mid-day poll runs every 30 minutes, not every hour."""
    monkeypatch.delenv("AUTOFIN_POLL_INTERVAL_SECONDS", raising=False)
    assert DEFAULT_POLL_INTERVAL_SECONDS == 1800
    assert poll_interval_seconds() == 1800


def test_the_poll_fires_after_30_minutes(monkeypatch) -> None:
    """29 minutes → no; 30 minutes → yes."""
    monkeypatch.delenv("AUTOFIN_POLL_INTERVAL_SECONDS", raising=False)
    now = datetime.datetime(2026, 11, 2, 10, 0, tzinfo=ICT)

    at_29 = (now - datetime.timedelta(minutes=29)).isoformat()
    assert not should_poll_strategy(now, at_29)

    at_30 = (now - datetime.timedelta(minutes=30)).isoformat()
    assert should_poll_strategy(now, at_30)

    at_45 = (now - datetime.timedelta(minutes=45)).isoformat()
    assert should_poll_strategy(now, at_45)


def test_the_poll_interval_is_env_overridable(monkeypatch) -> None:
    monkeypatch.setenv("AUTOFIN_POLL_INTERVAL_SECONDS", "900")
    assert poll_interval_seconds() == 900

    now = datetime.datetime(2026, 11, 2, 10, 0, tzinfo=ICT)
    at_16 = (now - datetime.timedelta(minutes=16)).isoformat()
    assert should_poll_strategy(now, at_16)


def test_a_nonsense_poll_interval_falls_back_to_the_default(monkeypatch) -> None:
    """A zero/negative/garbage interval must not hammer the gateway.

    The bot's own loop sleeps 60 s, so a 0-second interval would poll
    on EVERY loop — an autofin-gateway hammer.
    """
    for bad in ("0", "-1", "abc", "", "   "):
        monkeypatch.setenv("AUTOFIN_POLL_INTERVAL_SECONDS", bad)
        assert poll_interval_seconds() == 1800, bad

    # Below the floor (60 s) → the default, not the requested value.
    monkeypatch.setenv("AUTOFIN_POLL_INTERVAL_SECONDS", "5")
    assert poll_interval_seconds() == 1800


def test_the_poll_still_respects_the_trading_phase(monkeypatch) -> None:
    """A shorter interval must not make the bot poll outside the
    trading hours (lunch break, after close, weekends).
    """
    monkeypatch.delenv("AUTOFIN_POLL_INTERVAL_SECONDS", raising=False)

    # Lunch break (11:45 ICT) — long past the interval, but off-hours.
    lunch = datetime.datetime(2026, 11, 2, 11, 45, tzinfo=ICT)
    assert not should_poll_strategy(lunch, None)

    # Saturday.
    sat = datetime.datetime(2026, 10, 31, 10, 0, tzinfo=ICT)
    assert not should_poll_strategy(sat, None)

    # A normal mid-day moment with no previous poll → fires.
    mid = datetime.datetime(2026, 11, 2, 10, 0, tzinfo=ICT)
    assert should_poll_strategy(mid, None)


# -----------------------------------------------------------------------------
# 2. the buy-zone tolerance
# -----------------------------------------------------------------------------

def test_the_default_buy_zone_tolerance_is_1_percent(monkeypatch) -> None:
    from vietnam_algo.algos.autofin_signal import (
        DEFAULT_BUY_ZONE_TOLERANCE_PCT,
        buy_zone_tolerance_pct,
    )

    monkeypatch.delenv("AUTOFIN_BUY_ZONE_TOLERANCE_PCT", raising=False)
    monkeypatch.delenv("AUTOFIN_BUY_ZONE_TOLERANCE_ABOVE_PCT", raising=False)
    monkeypatch.delenv("AUTOFIN_BUY_ZONE_TOLERANCE_BELOW_PCT", raising=False)

    assert DEFAULT_BUY_ZONE_TOLERANCE_PCT == 0.01
    assert buy_zone_tolerance_pct() == (0.01, 0.01)


def test_the_buy_zone_tolerance_is_env_overridable(monkeypatch) -> None:
    from vietnam_algo.algos.autofin_signal import buy_zone_tolerance_pct

    monkeypatch.setenv("AUTOFIN_BUY_ZONE_TOLERANCE_PCT", "0.03")
    assert buy_zone_tolerance_pct() == (0.03, 0.03)

    # The two sides can be overridden independently.
    monkeypatch.setenv("AUTOFIN_BUY_ZONE_TOLERANCE_ABOVE_PCT", "0.05")
    monkeypatch.setenv("AUTOFIN_BUY_ZONE_TOLERANCE_BELOW_PCT", "0.005")
    assert buy_zone_tolerance_pct() == (0.05, 0.005)


def test_a_nonsense_buy_zone_tolerance_falls_back(monkeypatch) -> None:
    from vietnam_algo.algos.autofin_signal import buy_zone_tolerance_pct

    for bad in ("5.0", "-0.5", "abc"):
        monkeypatch.setenv("AUTOFIN_BUY_ZONE_TOLERANCE_PCT", bad)
        assert buy_zone_tolerance_pct() == (0.01, 0.01), bad


def test_the_entry_zone_uses_the_1_percent_band(monkeypatch, tmp_path) -> None:
    """End-to-end through ``decide_orders``: the zone [17100, 17400].

    1 % band → lower edge 16929, upper edge 17574.
    """
    from vietnam_algo.algos.autofin_signal import decide_orders
    from vietnam_algo.libs.autofin_signals import AutofinSignal
    from vietnam_algo.libs.dry_run_state import DryRunState, save_dry_run_state

    monkeypatch.delenv("AUTOFIN_BUY_ZONE_TOLERANCE_PCT", raising=False)
    save_dry_run_state(
        DryRunState(initial_nav=100_000_000, cash=100_000_000),
        tmp_path, strategy_name="autofin_signal",
    )
    sig = AutofinSignal(
        symbol="ABB", company_name="test",
        last_recomment="Khuyến nghị đầu tư",
        supply_from=17100, supply_to=17400,
        tp_from=18400, tp_to=18400,
        sl_from=14700, sl_to=15900,
        rate_for_nav=0.30,
        is_waitting_buy=False, is_waitting_sell=False,
        market_type=4, raw={},
    )

    def _buys(price: float):
        orders, _, _ = decide_orders(
            holding=[], signals=[sig], our_deals={},
            our_positions={}, nav=100_000_000,
            current_prices={"ABB": price},
            data_dir=tmp_path, actioned_deals=set(),
        )
        return [o for o in orders if o["side"] == "NB"]

    # The upper edge is INCLUSIVE.
    assert len(_buys(17574.0)) == 1, "1% above supply_T must still enter"
    assert _buys(17575.0) == [], "just above 1% must skip"

    # The lower edge is INCLUSIVE.
    assert len(_buys(16929.0)) == 1, "1% below supply_F must still enter"
    assert _buys(16928.0) == [], "just below 1% must skip"

    # A price that was INSIDE the old 3 % band but is OUTSIDE the new
    # 1 % band — this is the behaviour change the user asked for.
    # 17,600 is 1.15 % above supply_T=17,400.
    assert _buys(17600.0) == [], (
        "17,600 was inside the old 3% band but must be skipped now"
    )


def test_the_entry_zone_respects_an_env_override(monkeypatch, tmp_path) -> None:
    """A 3 % override restores the old behaviour."""
    from vietnam_algo.algos.autofin_signal import decide_orders
    from vietnam_algo.libs.autofin_signals import AutofinSignal
    from vietnam_algo.libs.dry_run_state import DryRunState, save_dry_run_state

    monkeypatch.setenv("AUTOFIN_BUY_ZONE_TOLERANCE_PCT", "0.03")
    save_dry_run_state(
        DryRunState(initial_nav=100_000_000, cash=100_000_000),
        tmp_path, strategy_name="autofin_signal",
    )
    sig = AutofinSignal(
        symbol="ABB", company_name="test",
        last_recomment="Khuyến nghị đầu tư",
        supply_from=17100, supply_to=17400,
        tp_from=18400, tp_to=18400,
        sl_from=14700, sl_to=15900,
        rate_for_nav=0.30,
        is_waitting_buy=False, is_waitting_sell=False,
        market_type=4, raw={},
    )

    orders, _, _ = decide_orders(
        holding=[], signals=[sig], our_deals={},
        our_positions={}, nav=100_000_000,
        current_prices={"ABB": 17600.0},  # 1.15% above → inside 3%
        data_dir=tmp_path, actioned_deals=set(),
    )
    assert len([o for o in orders if o["side"] == "NB"]) == 1
