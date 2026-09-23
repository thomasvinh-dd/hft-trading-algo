"""Regression tests for the 2026-09-22 autofin partial-close bugs.

User feedback 2026-09-22:

1. 'today I see this message autofin_signal: new partial close ...
   → DNSE: SELL 100 ASP @ 16,700 VND ... but in eod report of
   autofin bot I do not see any order related to asp'
2. 'by the way the suggestion say "Tỷ trọng 30 % danh mục nắm
   giữ" mean 500 * 30 %, round to 200 share, not 100 share'

Two layered bugs:

1. **JSON-overwrite race** — mid-day polls plan a partial close
   and write it to ``latest_orders.autofin_signal.json``, but the
   NEXT poll (60 seconds later, plans nothing new) overwrites the
   JSON with ``items: []``. The dry-run ``_apply_dry_run_fills``
   only runs at EOD/SOD, so by then the JSON is empty and the
   partial close is never applied to the simulated state. The
   EOD positions section keeps showing the pre-close qty, so the
   operator can't see what was actually sold during the day.

   Fix: ``runner.run_auto_bot`` now calls
   ``_apply_dry_run_fills`` after every mid-day poll's
   ``generate_orders``, in addition to the EOD/SOD paths. The
   per-order dedup in ``apply_orders_to_state`` keeps subsequent
   polls from re-applying.

2. **Stale-fraction race** — autofin publishes the new TP step's
   ``lastRecomment`` ("💰 Chốt lãi lần 2 mức giá 16,700") BEFORE
   updating the /signal-deals history with the matching "Tỷ trọng
   30 %" line. The first poll after the suggestion therefore sees
   the recomment text but a STALE fraction (the previous step's
   10 %), undersells the deal: 500 × 10 % → 50 → round-up to 100
   shares, instead of 500 × 30 % → 150 → round-up to 200. On a
   later cycle autofin catches up the history, but the recomment
   is unchanged so the bot would never re-fire.

   Fix: ``AutofinSignalAdapter._plan_partial_close_orders`` now
   tracks ``_last_partial_fraction`` per deal. When the recomment
   matches a previously-seen step but the latest fraction differs
   from the one we sold at, the bot RE-FIRES the partial close
   with the corrected fraction (selling additional shares to
   reach the target).

These tests assert both fixes without depending on the live
autofin API or DNSE — they exercise the pure logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


# 1. runner.apply-during-mid-day-poll regression ----------------------------------


def _runner_source() -> str:
    return (_PROJECT_ROOT / "vietnam_algo" / "bots" / "runner.py").read_text(
        encoding="utf-8"
    )


def test_runner_applies_dry_run_fills_after_mid_day_poll():
    """The mid-day poll path must call ``_apply_dry_run_fills``
    after ``strategy_adapter.generate_orders`` returns. Without
    this, partial closes planned mid-day never reach the dry-run
    state — the JSON gets overwritten with ``[]`` on the next
    poll, and the EOD's positions section shows the pre-close
    qty. The 2026-09-22 ASP SELL 100 incident went unnoticed
    because of this exact omission.
    """
    src = _runner_source()
    # Scope to the mid-day poll block (between the autofin poll
    # log line and the EOD block).
    poll_start = src.find("Mid-day autofin poll")
    eod_start = src.find("if should_run_eod")
    assert poll_start > 0 and eod_start > poll_start, (
        "Couldn't locate mid-day poll block in runner.py"
    )
    poll_block = src[poll_start:eod_start]
    # Both call sites must be present in the mid-day block:
    #   1. strategy_adapter.generate_orders (the actual poll)
    #   2. _apply_dry_run_fills (the apply — NEW as of 2026-09-22)
    assert "strategy_adapter.generate_orders(" in poll_block
    assert "_apply_dry_run_fills(" in poll_block, (
        "mid-day poll must call _apply_dry_run_fills after "
        "generate_orders — without it partial closes planned "
        "mid-day never reach the dry-run state (ASP 2026-09-22 "
        "incident). The apply path exists only at EOD/SOD, "
        "but mid-day polls overwrite latest_orders.json with "
        "[] on the next tick, so the order is lost."
    )
    # The apply must come AFTER the generate_orders call site
    adapter_call = poll_block.find("strategy_adapter.generate_orders(")
    apply_call = poll_block.find("_apply_dry_run_fills(")
    assert apply_call > adapter_call, (
        f"_apply_dry_run_fills must be called AFTER "
        f"strategy_adapter.generate_orders in the mid-day "
        f"poll; otherwise it would re-apply yesterday's plan "
        f"before the new poll has produced today's items. "
        f"(adapter call at {adapter_call}, apply call at "
        f"{apply_call})"
    )


# 2. autofin partial-close fraction-change regression ----------------------------


def _autofin_signal_source() -> str:
    return (_PROJECT_ROOT / "vietnam_algo" / "algos" / "autofin_signal.py").read_text(
        encoding="utf-8"
    )


def test_partial_close_tracks_last_partial_fraction():
    """``_plan_partial_close_orders`` must persist the fraction
    it sold at as ``_last_partial_fraction`` on the deal dict.
    The next cycle uses this to detect autofin's publish-lag
    race (suggestion text posted before the history catches up).
    """
    src = _autofin_signal_source()
    # The assignment must run AFTER the row is appended, in BOTH
    # the take_profit_lap / reduce_lap branch AND the
    # capital_protect branch (otherwise the variable is
    # UnboundLocalError for capital_protect tests).
    assert 'sig["_last_partial_fraction"] = fraction' in src, (
        "_plan_partial_close_orders must record the fraction "
        "sold at so the next cycle can detect a fraction change "
        "(2026-09-22 ASP incident: autofin published 'TP step 2' "
        "before updating history from 10% → 30%, bot sold only "
        "100 of the intended 200 shares)."
    )


def test_partial_close_refires_when_fraction_changes():
    """When ``recomment_changed=False`` AND ``nP_changed=False``,
    the partial-close planner skips the deal. But the autofin
    publish-lag race produces exactly this state — the recomment
    matches a previously-seen step but the fraction has changed
    in the background. The planner must additionally fire when
    ``autofin_fractions[did]`` differs from
    ``_last_partial_fraction`` (more than floating-point noise).
    """
    src = _autofin_signal_source()
    assert "_last_partial_fraction" in src, (
        "the partial-close planner must reference "
        "_last_partial_fraction so it can detect autofin's "
        "publish-lag race (recomment matches but fraction differs)."
    )
    # The OR-gate that decides whether to plan a partial close
    # must include the new fraction_changed flag.
    assert "fraction_changed" in src, (
        "the partial-close planner must compute a fraction_changed "
        "flag and include it in the OR-gate that decides whether to "
        "skip or plan the deal — without this the bot never "
        "re-fires after autofin's history catches up."
    )
    # Whitespace-tolerant check on the OR-gate.
    or_gate_pattern = (
        "if not (nP_changed or recomment_changed or fraction_changed):"
    )
    if or_gate_pattern not in src:
        # Allow some indentation variation.
        import re as _re
        m = _re.search(
            r"if not \(\s*nP_changed\s+or\s+recomment_changed\s+or\s+fraction_changed",
            src,
        )
        assert m, (
            f"the OR-gate that skips the deal must include "
            f"``fraction_changed`` — looked for "
            f"``{or_gate_pattern}`` (no whitespace variant either)."
        )


def test_partial_close_capital_protect_also_tracks_fraction():
    """``capital_protect`` events (🛡️ Chốt lãi X % bảo toàn vốn)
    must also populate ``_last_partial_fraction`` so the
    fraction-change detection covers them. Without this, the
    variable is unbound for capital_protect tests and a
    subsequent capital_protect change with the same recomment
    text would never re-fire.
    """
    src = _autofin_signal_source()
    # The capital_protect branch sets ``fraction = ...``
    # before appending the row.
    capital_block_idx = src.find('elif action.action == "capital_protect":')
    assert capital_block_idx > 0
    next_branch_idx = src.find('else:', capital_block_idx)
    capital_block = src[capital_block_idx:next_branch_idx]
    assert "fraction =" in capital_block, (
        "capital_protect branch must populate the local "
        "``fraction`` variable (used by _last_partial_fraction "
        "tracking after the row is appended)."
    )


# 3. Functional test of the fraction-change re-fire logic -------------------------


def test_plan_partial_close_refires_on_fraction_change(monkeypatch):
    """End-to-end: simulate the autofin publish-lag race and
    verify the bot re-fires on a later cycle when the fraction
    catches up to the correct value (0.1 → 0.3).
    """
    from vietnam_algo.algos.autofin_signal import (
        AutofinSignalAdapter,
        _plan_partial_close_orders,
    )

    deal_id = "1789084800:ASP"
    # Initial state after TP step 1 was applied:
    # - we hold 500 ASP
    # - _last_recomment = "💰 Chốt lãi lần 1 mức giá 15,800"
    # - _last_numberOfProfit = 1
    # - _last_partial_fraction = 0.10
    sig = {
        "_id": 1789084800,
        "symbol": "ASP",
        "lastRecomment": "💰 Chốt lãi lần 1 mức giá 15,800",
        "numberOfProfit": 1,
        "_last_numberOfProfit": 1,
        "_last_recomment": "💰 Chốt lãi lần 1 mức giá 15,800",
        "_last_partial_fraction": 0.10,
        "_our_qty": 500,
        "tP_F": 15800,
        "tP_T": 20800,
        "supply_F": 14000,
        "supply_T": 14300,
        "sL_F": 12750,
        "sL_T": 13450,
    }

    # autofin publish-lag scenario: the holding's lastRecomment
    # has been updated to step 2, but the history's most-recent
    # "Tỷ trọng X %" line still reflects step 1 (stale fraction).
    holding = [{
        "_id": 1789084800,
        "symbol": "ASP",
        "numberOfProfit": 2,  # autofin incremented nP
        "lastRecomment": "💰 Chốt lãi lần 2 mức giá 16,700",
        "tP_F": 15800,
        "tP_T": 20800,
    }]
    our_held = {deal_id: sig}
    positions = {"ASP": 500}

    # Cycle 1: fraction is stale (0.10 from TP step 1).
    # Expected: bot fires partial close at 100 shares (500 × 10 %
    # = 50 → round-up to 100). This is the historical bad
    # behaviour.
    rows_v1 = _plan_partial_close_orders(
        our_held, holding, our_positions=positions,
        board_lot=100, autofin_fractions={deal_id: 0.10},
    )
    assert len(rows_v1) == 1
    assert rows_v1[0]["quantity"] == 100
    assert sig["_last_partial_fraction"] == 0.10

    # Now autofin catches up: history's most-recent "Tỷ trọng"
    # line is updated from 0.10 to 0.30. The bot re-fetches:
    rows_v2 = _plan_partial_close_orders(
        our_held, holding, our_positions=positions,
        board_lot=100, autofin_fractions={deal_id: 0.30},
    )
    # Cycle 2: recomment matches the previously-seen step, nP
    # is unchanged, but fraction differs. Expected: bot RE-FIRES
    # with the corrected 0.30 fraction → 500 × 30 % = 150 →
    # round-up to 200 shares.
    assert len(rows_v2) == 1, (
        f"bot must re-fire when fraction changes from "
        f"{sig.get('_last_partial_fraction')!r}; got rows_v2={rows_v2}"
    )
    assert rows_v2[0]["quantity"] == 200, (
        f"500 × 30 % = 150 → round-up to 200 shares; "
        f"got quantity={rows_v2[0]['quantity']}"
    )
    assert sig["_last_partial_fraction"] == 0.30


def test_plan_partial_close_does_not_refire_when_fraction_unchanged():
    """Sanity check: when the fraction is unchanged from the
    last sell, the bot must NOT re-fire even if the recomment
    matches. Without this guard the bot would sell the same
    shares every cycle (dedup-by-fraction is the only thing
    preventing it).
    """
    from vietnam_algo.algos.autofin_signal import _plan_partial_close_orders

    deal_id = "1789084800:ASP"
    sig = {
        "_id": 1789084800,
        "symbol": "ASP",
        "lastRecomment": "💰 Chốt lãi lần 2 mức giá 16,700",
        "numberOfProfit": 2,
        "_last_numberOfProfit": 2,
        "_last_recomment": "💰 Chốt lãi lần 2 mức giá 16,700",
        "_last_partial_fraction": 0.30,
        "_our_qty": 500,
        "tP_F": 15800,
        "tP_T": 20800,
    }
    holding = [{
        "_id": 1789084800,
        "symbol": "ASP",
        "numberOfProfit": 2,
        "lastRecomment": "💰 Chốt lãi lần 2 mức giá 16,700",
        "tP_F": 15800,
        "tP_T": 20800,
    }]
    rows = _plan_partial_close_orders(
        {deal_id: sig}, holding, our_positions={"ASP": 500},
        board_lot=100, autofin_fractions={deal_id: 0.30},
    )
    assert rows == [], (
        "recomment + nP + fraction all match the previous "
        "step — bot must skip (re-firing would double-sell)."
    )


# 4. ASP 500-share scenario (matches user's Sep-22 complaint) --------------------


def test_plan_partial_close_500_shares_30pct_sells_200():
    """End-to-end replay of the 2026-09-22 ASP scenario with the
    CORRECT starting qty of 500 (not 600 — the user's complaint
    said '30 % of 500 share (the current holding stock not
    origin 600 share)'). autofin's recomment says TP step 2 at
    16,700 with 'Tỷ trọng 30 % danh mục nắm giữ' (30 % of the
    held position). Expected:
      delta = int(500 × 0.30) = 150 → round-up to 200 shares
      _our_qty after sell: max(0, 500 - 200) = 300
    """
    from vietnam_algo.algos.autofin_signal import _plan_partial_close_orders

    deal_id = "1789084800:ASP"
    sig = {
        "_id": 1789084800,
        "symbol": "ASP",
        "lastRecomment": "💰 Chốt lãi lần 1 mức giá 15,800",
        "numberOfProfit": 1,
        "_last_numberOfProfit": 1,
        "_last_recomment": "💰 Chốt lãi lần 1 mức giá 15,800",
        "_last_partial_fraction": 0.10,
        "_our_qty": 500,  # 600 - 100 (TP step 1 already applied)
        "tP_F": 15800,
        "tP_T": 20800,
    }
    holding = [{
        "_id": 1789084800,
        "symbol": "ASP",
        "numberOfProfit": 2,  # autofin bumped nP to 2 (TP step 2)
        "lastRecomment": "💰 Chốt lãi lần 2 mức giá 16,700",
        "tP_F": 15800,
        "tP_T": 20800,
    }]
    rows = _plan_partial_close_orders(
        {deal_id: sig}, holding, our_positions={"ASP": 500},
        board_lot=100, autofin_fractions={deal_id: 0.30},
    )
    assert len(rows) == 1
    order = rows[0]
    assert order["side"] == "NS"
    assert order["symbol"] == "ASP"
    assert order["price"] == 16700
    assert order["quantity"] == 200, (
        f"500 × 30 % = 150 → round-up to 200 (board lot); "
        f"got quantity={order['quantity']} — bot must match the "
        f"user's expectation of selling 200 shares, not 100."
    )
    # _our_qty tracked on the deal dict for the next cycle —
    # this is what the next `_plan_partial_close_orders` reads
    # back to detect the autofin-side lag on a future cycle.
    assert sig["_our_qty"] == 300, (
        f"after selling 200 from 500, _our_qty should be 300; "
        f"got {sig['_our_qty']}"
    )
    assert sig["_last_partial_fraction"] == 0.30
    assert sig["_last_recomment"] == "💰 Chốt lãi lần 2 mức giá 16,700"
    assert sig["_last_numberOfProfit"] == 2


def test_plan_partial_close_500_shares_10pct_then_30pct():
    """Two-step replay: cycle 1 sells at 10 % (TP step 1),
    cycle 2 sells at 30 % (TP step 2) on a 500-share position
    that was already at 500 (the original 600-share entry lost
    100 shares on the prior TP step that's reflected in our
    starting qty). This is the full flow the operator wanted
    to see in the Sep-22 EOD report.
    """
    from vietnam_algo.algos.autofin_signal import _plan_partial_close_orders

    deal_id = "1789084800:ASP"
    sig = {
        "_id": 1789084800,
        "symbol": "ASP",
        "lastRecomment": "Khuyến nghị đầu tư",
        "numberOfProfit": 0,
        "_last_numberOfProfit": 0,
        "_last_recomment": "Khuyến nghị đầu tư",
        "_our_qty": 500,
        "tP_F": 15800,
        "tP_T": 20800,
    }
    # Cycle 1: TP step 1 fires with stale fraction (10 %).
    holding_v1 = [{
        "_id": 1789084800,
        "symbol": "ASP",
        "numberOfProfit": 1,
        "lastRecomment": "💰 Chốt lãi lần 1 mức giá 15,800",
        "tP_F": 15800,
        "tP_T": 20800,
    }]
    rows_v1 = _plan_partial_close_orders(
        {deal_id: sig}, holding_v1, our_positions={"ASP": 500},
        board_lot=100, autofin_fractions={deal_id: 0.10},
    )
    assert len(rows_v1) == 1
    assert rows_v1[0]["quantity"] == 100  # 500 × 10 % = 50 → round-up
    assert sig["_our_qty"] == 400  # 500 - 100
    assert sig["_last_partial_fraction"] == 0.10

    # Cycle 2: TP step 2 fires with corrected fraction (30 %).
    # After cycle 1, the live qty is 400 (200 = 100 already sold).
    # But the user reads 500 × 30 % = 200 (round up); the bot
    # reads our_positions["ASP"]=400, so 400 × 30 % = 120 → 200
    # (round-up). Same end result (sell 200), just the path is
    # different from "starting at 500".
    holding_v2 = [{
        "_id": 1789084800,
        "symbol": "ASP",
        "numberOfProfit": 2,
        "lastRecomment": "💰 Chốt lãi lần 2 mức giá 16,700",
        "tP_F": 15800,
        "tP_T": 20800,
    }]
    rows_v2 = _plan_partial_close_orders(
        {deal_id: sig}, holding_v2, our_positions={"ASP": 400},
        board_lot=100, autofin_fractions={deal_id: 0.30},
    )
    assert len(rows_v2) == 1
    assert rows_v2[0]["quantity"] == 200  # 400 × 30 % = 120 → round-up
    assert sig["_our_qty"] == 200  # 400 - 200
    assert sig["_last_partial_fraction"] == 0.30
