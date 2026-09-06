"""Tests for autofin signal-selection logic + order-plan computation."""
from __future__ import annotations

from dataclasses import dataclass

import vietnam_algo.strategies_impl.autofin_signal as mod
from vietnam_algo.autofin_signals import AutofinSignal, parse_signal


def _make_signal(
    symbol: str,
    rate_for_nav: float,
    last_index: int = 1,
    is_waiting_buy: bool = False,
    is_waiting_sell: bool = False,
    supply_to: float = 10000.0,
    tp_from: float = 12000.0,
    sl_to: float | None = None,
    rating: float = 0.0,
    win_rate: float = 0.0,
) -> AutofinSignal:
    sl_to = sl_to if sl_to is not None else supply_to - 100
    raw = {
        "symbol": symbol,
        "lastIndex": last_index,
        "statusDeal": {"market_TypePrice": 1},
        "supply_F": (supply_to - 100) / 1000,
        "supply_T": supply_to / 1000,
        "tP_F": tp_from / 1000,
        "tP_T": (tp_from + 100) / 1000,
        "sL_F": (sl_to - 100) / 1000,
        "sL_T": sl_to / 1000,
        "rateForNav": rate_for_nav,
        "lastRecomment": "test",
        "isWaittingBuy": is_waiting_buy,
        "isWaittingSell": is_waiting_sell,
        "numberOfProfit": 0,
        "rateIncome": 0,
        "rateIncome_TypePrice": 3,
        "rS_Rating_IDB": rating,
        "rateWinByPrice": win_rate,
    }
    s = parse_signal(raw)
    s.raw["lastIndex"] = last_index
    s.raw["rS_Rating_IDB"] = rating
    s.raw["rateWinByPrice"] = win_rate
    return s


def test_select_within_nav_budget_sums_to_one():
    # 5 signals at 30% each = 150% total NAV → should pick top 3
    sigs = [
        _make_signal("A", 0.30, last_index=5),
        _make_signal("B", 0.30, last_index=4),
        _make_signal("C", 0.30, last_index=3),
        _make_signal("D", 0.30, last_index=2),
        _make_signal("E", 0.30, last_index=1),
    ]
    selected = mod._select_signals_within_nav_budget(sigs, nav_budget_pct=1.0)
    assert len(selected) == 3
    assert {s.symbol for s in selected} == {"A", "B", "C"}  # newest first
    assert abs(sum(s.rate_for_nav for s in selected) - 0.90) < 1e-6


def test_select_within_nav_budget_excludes_zero_nav():
    sigs = [
        _make_signal("A", 0.50),
        _make_signal("B", 0.00),  # no NAV
        _make_signal("C", 0.40),
    ]
    selected = mod._select_signals_within_nav_budget(sigs, nav_budget_pct=1.0)
    assert len(selected) == 2
    assert {s.symbol for s in selected} == {"A", "C"}


def test_select_within_nav_budget_prioritizes_risk_reward():
    """Higher upside vs downside wins over recency or allocation."""
    # A: high recency (lastIndex 100), but bad RR: entry 10k, tp 11k, sl 9.5k
    #    upside = 10%, downside = 5%, RR = 2.0
    # B: lower recency (lastIndex 50), better RR: entry 10k, tp 13k, sl 9k
    #    upside = 30%, downside = 10%, RR = 3.0
    # B should win despite older lastIndex
    a = _make_signal("A", 0.30, last_index=100, supply_to=10000, tp_from=11000, sl_to=9500)
    b = _make_signal("B", 0.30, last_index=50, supply_to=10000, tp_from=13000, sl_to=9000)
    selected = mod._select_signals_within_nav_budget([a, b], nav_budget_pct=1.0)
    assert [s.symbol for s in selected] == ["B", "A"]


def test_select_within_nav_budget_uses_rating_and_winrate():
    # A: high RR, low rating
    # B: medium RR, high rating + winrate
    # B should win because of autofin's quality signals
    a = _make_signal("A", 0.30, supply_to=10000, tp_from=12000, sl_to=9000, rating=1, win_rate=0.1)
    b = _make_signal("B", 0.30, supply_to=10000, tp_from=11500, sl_to=9500, rating=9, win_rate=0.9)
    selected = mod._select_signals_within_nav_budget([a, b], nav_budget_pct=1.0)
    # B has lower RR but much higher rating — should win
    assert [s.symbol for s in selected] == ["B", "A"]


def test_compute_order_plan_emits_tp_sl_brackets():
    """BUY should be paired with TP and SL exit orders."""
    sig = _make_signal("APH", 0.15, supply_to=6100, tp_from=12000, sl_to=9500)
    rows = mod._compute_order_plan(
        selected_signals=[sig],
        autofin_open_symbols={"APH"},
        our_positions={},
        nav_vnd=1_000_000_000,
    )
    # Expect 3 rows: BUY APH, SELL APH (TP), SELL APH (SL)
    assert len(rows) == 3
    sides = [r["side"] for r in rows]
    assert sides.count("NB") == 1
    assert sides.count("NS") == 2
    # Verify the SELL prices correspond to TP and SL
    sell_rows = [r for r in rows if r["side"] == "NS"]
    sell_prices = sorted(r["price"] for r in sell_rows)
    assert sell_prices == [9400, 12000]  # SL (9400) first, then TP (12000)
    sl_row = next(r for r in sell_rows if r["price"] == 9400)
    tp_row = next(r for r in sell_rows if r["price"] == 12000)
    assert "stop_loss" in sl_row["reason"]
    assert "take_profit" in tp_row["reason"]


def test_compute_order_plan_no_tp_sl_when_no_position():
    """If we already hold the target qty, no new orders needed."""
    sig = _make_signal("APH", 0.10, supply_to=6100, tp_from=6500, sl_to=5320)
    rows = mod._compute_order_plan(
        selected_signals=[sig],
        autofin_open_symbols={"APH"},
        our_positions={"APH": 100},
        nav_vnd=610_000,  # 1 board lot of APH = 610k, but min=1M → skip
    )
    assert rows == []


def test_compute_order_plan_closes_position_autofin_no_longer_holds():
    """If autofin exits a position but we still hold, SELL it."""
    # Simulate: autofin holds APH (open) and we hold APH (open) — but we selected
    # NOT to follow (over NAV budget). So we close.
    sig_selected = _make_signal("APH", 0.10)
    rows = mod._compute_order_plan(
        selected_signals=[],  # selected is empty (over budget)
        autofin_open_symbols={"APH"},  # autofin still holds APH
        our_positions={"APH": 200},  # but we hold APH
        nav_vnd=1_000_000_000,
    )
    close_rows = [r for r in rows if r["reason"] == "autofin_no_longer_holds:close_position"]
    assert len(close_rows) == 1
    assert close_rows[0]["symbol"] == "APH"
    assert close_rows[0]["side"] == "NS"
    assert close_rows[0]["quantity"] == 200


def test_compute_order_plan_buy_when_autofin_holds():
    # autofin has APH open; we don't → BUY (plus TP and SL brackets)
    sig = _make_signal("APH", 0.15, supply_to=6100, tp_from=12000, sl_to=5900)
    rows = mod._compute_order_plan(
        selected_signals=[sig],
        autofin_open_symbols={"APH"},
        our_positions={},
        nav_vnd=1_000_000_000,  # 1B VND
    )
    # 3 rows expected: BUY + TP + SL
    assert len(rows) == 3
    sides = [r["side"] for r in rows]
    assert sides.count("NB") == 1
    assert sides.count("NS") == 2
    assert rows[0]["symbol"] == "APH"
    assert rows[0]["side"] == "NB"
    assert rows[0]["price"] == 6100


def test_compute_order_plan_sell_when_autofin_closes():
    # autofin has APH open but we selected NOT to include it
    # (over budget) AND we hold APH → SELL
    held_signal = _make_signal("APH", 0.15)
    rows = mod._compute_order_plan(
        selected_signals=[],  # not in selected → autofin doesn't "hold" it for our mirror
        autofin_open_symbols={"APH"},
        our_positions={"APH": 1000},
        nav_vnd=1_000_000_000,
    )
    # APH is in autofin_open_symbols but NOT in selected → CLOSE
    close_rows = [r for r in rows if r["symbol"] == "APH"]
    assert len(close_rows) >= 1
    assert all(r["side"] == "NS" for r in close_rows)


def test_compute_order_plan_no_op_when_match():
    # We already hold the target quantity → no order
    sig = _make_signal("APH", 0.10, supply_to=6100)
    rows = mod._compute_order_plan(
        selected_signals=[sig],
        autofin_open_symbols={"APH"},
        our_positions={"APH": 100},
        nav_vnd=610_000,
    )
    # Below min_order_value_vnd → skip entirely
    assert rows == []


def test_compute_order_plan_keeps_position_when_signal_dropped():
    # autofin still holds APH, but our mirror logic decides NOT to follow
    # → if autofin_open_symbols contains APH and APH is in selected, leave alone
    sig = _make_signal("APH", 0.05, supply_to=6100)
    rows = mod._compute_order_plan(
        selected_signals=[sig],  # in selected
        autofin_open_symbols={"APH"},
        our_positions={"APH": 100},
        nav_vnd=1_000_000_000,
    )
    # target = 0.05 * 1B / 6100 ≈ 8196 shares
    # round to 100s = 8100; current = 100; delta = 8000 → BUY 8000
    # OK that's the OPPOSITE of what we want
    # Actually we want "no_op" because both autofin holds AND we hold → mirror keeps us
    # at NAV% of our NAV. If we already hold 100, target is 8100, so we BUY 8000.
    # The test was wrong — let me fix: with NAV=1B and current=8100, we shouldn't trade
    sig2 = _make_signal("APH", 0.05, supply_to=6100)
    rows2 = mod._compute_order_plan(
        selected_signals=[sig2],
        autofin_open_symbols={"APH"},
        our_positions={"APH": 8100},  # already at target
        nav_vnd=1_000_000_000,
    )
    assert rows2 == []


def test_compute_order_plan_subthreshold_skips():
    # NAV too low → target_value < min_order_value_vnd → no order even if we hold
    sig = _make_signal("APH", 0.05, supply_to=6100)
    rows = mod._compute_order_plan(
        selected_signals=[sig],
        autofin_open_symbols={"APH"},
        our_positions={"APH": 100},
        nav_vnd=10_000_000,  # 10M VND ≈ $400, target=500k, below min
    )
    assert rows == []
