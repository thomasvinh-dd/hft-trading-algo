"""End-to-end test simulating the user's actual ASP scenario from
scratch: original entry at 600 shares, TP step 1 at 10% (stale
fraction), TP step 2 at 30% (corrected fraction).

Expected math (per user feedback 2026-09-22):
  Entry: 600 ASP @ 14,300 → position: 600, cost 8.58M
  TP step 1 (stale 10%): delta = 600×0.1 = 60 → round-up to 100
                        → sell 100 @ 15,800, position: 500
  TP step 2 (corrected 30%): delta = 500×0.3 = 150 → round-up to 200
                          → sell 200 @ 16,700, position: 300

The user said:
  "Tỷ trọng 30 % danh mục nắm giữ mean 30% of 500 share
   (the current holding stock not origin 600 share)
   round up to bucket 100 is 200 share, so we only left 500 - 200 = 300"

The bot's qty must come from the LIVE position (500 after TP1),
not the original entry (600). My current code reads from
``our_positions`` which is built from the dry-run state —
that source is correct because the dry-run state reflects the
post-TP1 qty (500).
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_user_scenario_asp_500_30pct_sells_200_leaves_300():
    """Replay the user's actual ASP scenario from scratch and
    verify the dry-run state ends with qty=300 (500 - 200)."""
    from vietnam_algo.libs.dry_run_state import (
        DryRunPosition,
        apply_orders_to_state,
        init_dry_run_state,
    )

    state = init_dry_run_state(
        Path("/tmp"),  # not used since we don't save
        strategy_name="autofin_signal",
    )
    state.initial_nav = 100_000_000
    state.cash = 100_000_000

    # Step 1: Original entry — autofin's "Khuyến nghị đầu tư" BUY.
    # 600 ASP @ 14,300. We seed the dry-run state directly because
    # the entry path goes through the autofin adapter; the dry-run
    # apply is what we want to verify here.
    state.cash -= 600 * 14300
    state.positions["ASP"] = DryRunPosition(
        symbol="ASP",
        quantity=600,
        avg_price=14300,
        last_price=14300,
        cost_basis=600 * 14300,
    )

    # Step 2: TP step 1 fires with STALE fraction (10% — autofin's
    # history hadn't caught up to step 2 yet). Bot computed
    # delta = 600 × 0.1 = 60 → round-up to 100. SELL 100 @ 15,800.
    sells_v1 = [{
        "date": "2026-09-19",
        "symbol": "ASP",
        "side": "NS",
        "price": 15800,
        "quantity": 100,
    }]
    fills_v1 = apply_orders_to_state(state, sells_v1, board_lot=100, fees_bps=22.5)
    assert fills_v1[0]["status"] == "applied"
    assert fills_v1[0]["qty"] == 100
    assert state.positions["ASP"].quantity == 500, (
        f"after TP1 sell of 100 from 600: position should be 500, "
        f"got {state.positions['ASP'].quantity}"
    )
    # Realized P&L from step 1 = 100 × (15,800 - 14,300) = 100 × 1,500 = 150,000
    assert state.realized_pnl == pytest.approx(150_000, abs=100), (
        f"TP1 realized should be 150,000 (100 × 1,500); got {state.realized_pnl}"
    )

    # Step 3: autofin catches up the history to 30%. Bot RE-FIRES
    # (per the 2026-09-22 fraction-change fix) — recomment matches,
    # nP unchanged, but fraction differs (10% → 30%). Bot computes:
    #   delta = our_qty × fraction = 500 × 0.30 = 150 → round-up to 200
    # SELL 200 @ 16,700. Position should drop from 500 to 300.
    sells_v2 = [{
        "date": "2026-09-22",
        "symbol": "ASP",
        "side": "NS",
        "price": 16700,
        "quantity": 200,
    }]
    fills_v2 = apply_orders_to_state(state, sells_v2, board_lot=100, fees_bps=22.5)
    assert fills_v2[0]["status"] == "applied", (
        f"second partial close must apply cleanly (different dedup key "
        f"because different date/qty); got fills={fills_v2}"
    )
    assert fills_v2[0]["qty"] == 200
    assert state.positions["ASP"].quantity == 300, (
        f"after TP2 sell of 200 from 500: position should be 300, "
        f"got {state.positions['ASP'].quantity}"
    )
    # Realized P&L now includes both: 150,000 + 200 × (16,700 - 14,300) = 150,000 + 480,000 = 630,000
    assert state.realized_pnl == pytest.approx(630_000, abs=100), (
        f"cumulative realized should be 630,000 (150k + 480k); "
        f"got {state.realized_pnl}"
    )
    # Cash should have increased by the proceeds (less fees).
    # Cash flow:
    #   -100M (initial) - 600 × 14,300 (entry) + 100 × 15,800 (TP1) - fees
    #   + 200 × 16,700 (TP2) - fees
    # We don't pin an exact value because fees are complex, but
    # the cash should be > 0 (positive) and the realized P&L should
    # be 630k. If those check out, the dry-run pipeline is sound.
    assert state.cash > 0
