r"""Regression tests for the autofin partial-close parser.

User feedback 2026-09-24: 'i see that the HNM already have
signal like \"[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1\" but our
system not catch that signal, how we are look for a signal
now'.

Root cause: the ``reduce_lap`` regex was
``Hạ tỷ trọng lần\s*(\d+)`` — it expected "Hạ tỷ trọng lần N"
with NO percent between "Hạ" and "tỷ trọng". autofin's actual
format is "Hạ X % tỉ/tỷ trọng lần N" — the % sits BETWEEN
"Hạ" and "tỷ trọng", and the spelling varies between
"tỷ trọng" (y-with-dot, no space) and "tỉ trọng" (i-with-dot
+ space — `tỉ` is U+1EC9, a different character). The regex
missed both shapes and silently classified the action as
``unknown``, so the bot never fired the partial close.

Fix:
  - regex now ``Hạ\s+([\d,\.]+)\s*%\s*t[ỷỉy]?\s*trọng\s+lần\s*(\d+)``
    — captures the percent, accepts both "tỷ" and "tỉ" spellings
    (the `ỹ` was a stray extra I removed).
  - handler now stores the percent (group 1) on the
    PartialCloseAction so ``_plan_partial_close_orders`` can
    use it as the fall-back fraction when
    ``autofin_fractions[did]`` isn't available.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _parse(rec: str):
    from vietnam_algo.algos.autofin_signal import _parse_partial_close
    return _parse_partial_close(rec)


# -- parse_partial_close coverage -------------------------------------------------


def test_reduce_lap_tỉ_trọng_user_signal():
    """The exact signal the user pasted on 2026-09-24.

    '[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1' used to be classified
    as ``unknown`` because:
      (a) the regex required "tỷ trọng" (no space) — autofin's
          actual UI uses "tỉ trọng" (i-with-dot, U+1EC9) with
          a space between it and "trọng", and
      (b) the regex required NO % between "Hạ" and "tỷ trọng" —
          autofin emits "Hạ X % tỷ trọng" with the percent
          in the middle.
    """
    a = _parse("[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1")
    assert a.action == "reduce_lap", (
        f"expected reduce_lap, got {a.action!r} — the user's HNM "
        f"signal '[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1' would not be "
        f"actioned"
    )
    assert a.lap == 1
    assert a.percent == pytest_approx(50.0) if False else a.percent == 50.0


def test_reduce_lap_tỷ_trọng_alternative_spelling():
    """Some autofin deals use the y-with-dot spelling without
    a space: "Hạ 30 % tỷ trọng lần 1". This shape used to
    match the OLD regex but the new regex still matches it.
    """
    a = _parse("⚠️ Hạ 30 % tỷ trọng lần 1")
    assert a.action == "reduce_lap"
    assert a.lap == 1
    assert a.percent == 30.0


def test_reduce_lap_higher_lap_number():
    """Lap numbers > 1 are valid: "Hạ 25 % tỉ trọng lần 2"."""
    a = _parse("Hạ 25 % tỉ trọng lần 2")
    assert a.action == "reduce_lap"
    assert a.lap == 2
    assert a.percent == 25.0


def test_reduce_lap_full_close_100pct():
    """A 100 % reduce is effectively a full close — the bot
    should still classify it as reduce_lap so ``_plan_close_orders``
    can pick it up (or ``_plan_partial_close_orders`` can fire
    a clean SELL)."""
    a = _parse("Hạ 100 % tỷ trọng lần 3")
    assert a.action == "reduce_lap"
    assert a.lap == 3
    assert a.percent == 100.0


def test_take_profit_lap_still_works():
    """Sanity check: the TP regex is unchanged and still works."""
    a = _parse("💰 Chốt lãi lần 2 mức giá 16,700")
    assert a.action == "take_profit_lap"
    assert a.lap == 2
    assert a.price == 16700.0


def test_stop_loss_close_still_works():
    """Sanity check: the stop_loss_close regex is unchanged and
    still works (uses a different shape — "cân nhắc hạ" + price)."""
    a = _parse(
        "[HDB] 🛑 Chạm điểm cắt lỗ, cân nhắc hạ 100 % tỷ trọng "
        "Giá: 25,400 - Nắm giữ: 8 ngày Lỗ: -8.66 %"
    )
    assert a.action == "stop_loss_close"
    assert a.percent == 100.0
    assert a.price == 25400.0


# -- plan_partial_close_orders end-to-end ---------------------------------------


def test_plan_reduce_lap_sells_50pct_of_position():
    """Functional: when a reduce_lap fires with 50 % fraction
    and we hold 100 shares, we should sell 50 (round-up to 100).
    """
    from vietnam_algo.algos.autofin_signal import _plan_partial_close_orders

    deal_id = "1789084800:HNM"
    sig = {
        "_id": 1789084800,
        "symbol": "HNM",
        "lastRecomment": "Khuyến nghị đầu tư",
        "numberOfProfit": 0,
        "_last_numberOfProfit": 0,
        "_last_recomment": "Khuyến nghị đầu tư",
        "_our_qty": 100,
        "tP_F": 8000,
        "tP_T": 9000,
    }
    holding = [{
        "_id": 1789084800,
        "symbol": "HNM",
        "numberOfProfit": 1,  # bumped by the reduce event
        "lastRecomment": "[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1",
        "tP_F": 8000,
        "tP_T": 9000,
    }]
    rows = _plan_partial_close_orders(
        {deal_id: sig}, holding, our_positions={"HNM": 100},
        board_lot=100,
        # autofin_fractions would normally provide 0.5 here, but
        # even without it the parser's percent is now stored on
        # PartialCloseAction and used as the fall-back fraction.
    )
    assert len(rows) == 1, (
        f"expected the reduce to fire and sell 100 (50 × 100), "
        f"got {len(rows)} rows"
    )
    assert rows[0]["side"] == "NS"
    assert rows[0]["symbol"] == "HNM"
    assert rows[0]["quantity"] == 100, (
        f"100 × 50 % = 50 → round-up to 100; got {rows[0]['quantity']}"
    )


# Helper shim — we don't import pytest at the top so this file
# stays import-clean for ad-hoc `python -m pytest` runs.
def pytest_approx(v):
    return v  # not used; the tests use direct equality above
