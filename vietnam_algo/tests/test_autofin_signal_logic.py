"""Tests for autofin signal-selection logic + order-plan computation."""
from __future__ import annotations

import json
import sys
sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

import vietnam_algo.strategies_impl.autofin_signal as mod
from vietnam_algo.strategies_impl.autofin_signal import (
    AutofinSignalAdapter,
    _plan_orders_for_new_positions,
    _plan_close_orders,
    parse_signal,
)


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
) -> object:
    sl_to = sl_to if sl_to is not None else supply_to - 100
    raw = {
        "symbol": symbol,
        "lastIndex": last_index,
        "statusDeal": {"market_TypePrice": 1},
        "supply_F": supply_to - 100,
        "supply_T": supply_to,
        "tP_F": tp_from,
        "tP_T": tp_from + 100,
        "sL_F": sl_to - 100,
        "sL_T": sl_to,
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
    sigs = [
        _make_signal("A", 0.30, last_index=5),
        _make_signal("B", 0.30, last_index=4),
        _make_signal("C", 0.30, last_index=3),
        _make_signal("D", 0.30, last_index=2),
        _make_signal("E", 0.30, last_index=1),
    ]
    selected = mod._select_signals_within_nav_budget(sigs, nav_budget_pct=1.0)
    assert len(selected) == 3
    assert {s.symbol for s in selected} == {"A", "B", "C"}
    assert abs(sum(s.rate_for_nav for s in selected) - 0.90) < 1e-6


def test_select_within_nav_budget_excludes_zero_nav():
    sigs = [
        _make_signal("A", 0.50),
        _make_signal("B", 0.00),
        _make_signal("C", 0.40),
    ]
    selected = mod._select_signals_within_nav_budget(sigs, nav_budget_pct=1.0)
    assert len(selected) == 2
    assert {s.symbol for s in selected} == {"A", "C"}


def test_select_within_nav_budget_prioritizes_risk_reward():
    a = _make_signal("A", 0.30, last_index=100, supply_to=10000, tp_from=11000, sl_to=9500)
    b = _make_signal("B", 0.30, last_index=50, supply_to=10000, tp_from=13000, sl_to=9000)
    selected = mod._select_signals_within_nav_budget([a, b], nav_budget_pct=1.0)
    assert [s.symbol for s in selected] == ["B", "A"]


def test_select_within_nav_budget_uses_rating_and_winrate():
    a = _make_signal("A", 0.30, supply_to=10000, tp_from=12000, sl_to=9000, rating=1, win_rate=0.1)
    b = _make_signal("B", 0.30, supply_to=10000, tp_from=11500, sl_to=9500, rating=9, win_rate=0.9)
    selected = mod._select_signals_within_nav_budget([a, b], nav_budget_pct=1.0)
    assert [s.symbol for s in selected] == ["B", "A"]


def test_plan_new_positions_emits_buy_tp_sl_bracket():
    sig = _make_signal("APH", 0.15, supply_to=6100, tp_from=6500, sl_to=5320)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=1_000_000_000, our_positions={}
    )
    sides = [r["side"] for r in rows]
    assert sides.count("NB") == 1
    assert sides.count("NS") == 2
    buy = next(r for r in rows if r["side"] == "NB")
    assert buy["price"] == 6100


def test_plan_new_positions_skips_when_target_too_small():
    sig = _make_signal("APH", 0.05, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=10_000_000, our_positions={}
    )
    assert rows == []


def test_plan_new_positions_no_op_when_match():
    sig = _make_signal("APH", 0.10, supply_to=6100)
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=610_000, our_positions={"APH": 100}
    )
    assert rows == []


def test_plan_close_orders_only_closes_what_autofin_exited():
    our_deals = {
        "111:APH": {"symbol": "APH", "_our_qty": 200},
        "222:VHM": {"symbol": "VHM", "_our_qty": 100},
    }
    autofin_holding = [
        {"_id": 111, "symbol": "APH", "statusDeal": {"isClosed": False}},
        # VHM exited → close
    ]
    rows = _plan_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "VHM"


# ── Rich cycle-summary formatter (Telegram message) ───────────────────────


def test_format_cycle_summary_shows_bot_signal_and_dnse_order():
    """The EOD Telegram message must show:
      1. which bot is sending (autofin_signal)
      2. what autofin signal triggered each order
      3. the actual DNSE order we'll send
    """
    adapter = AutofinSignalAdapter()
    result = {
        "tradeDate": "2026-09-07",
        "latestOrdersCsv": "/path/to/latest_orders.autofin_signal.csv",
        "latestOrdersJson": "/path/to/latest_orders.autofin_signal.json",
        "signals": {
            "autofin_holding": 20,
            "autofin_risk": 1,
            "autofin_closing": 2,
            "fetched": 20,
            "deals_tracked": 3,
            "new_count": 1,
            "close_count": 1,
            "partial_count": 1,
            "account": "0003937868",
            "nav": 1_500_000_000,
        },
        "deals_actioned": [
            {
                "action": "new_entry",
                "symbol": "TDP",
                "deal_id": "1787270400:TDP",
                "lastRecomment": "Khuyến nghị đầu tư",
                "rateForNav": 0.15,
                "numberOfProfit": 0,
                "dnse_orders": [
                    {"symbol": "TDP", "side": "NB", "price": 32000, "quantity": 700},
                    {"symbol": "TDP", "side": "NS", "price": 36000, "quantity": 700},
                    {"symbol": "TDP", "side": "NS", "price": 28500, "quantity": 700},
                ],
            },
            {
                "action": "partial_close",
                "symbol": "HCM",
                "deal_id": "1785888000:HCM",
                "lastRecomment": "💰 Chốt lãi lần 1 mức giá 26,900",
                "rateForNav": 0.30,
                "numberOfProfit": 1,
                "dnse_orders": [
                    {"symbol": "HCM", "side": "NS", "price": 26900, "quantity": 100},
                ],
            },
            {
                "action": "full_close",
                "symbol": "L40",
                "deal_id": "1756166400:L40",
                "lastRecomment": "(autofin exited — not in holding)",
                "rateForNav": 0.0,
                "numberOfProfit": 0,
                "dnse_orders": [
                    {"symbol": "L40", "side": "NS", "price": 0, "quantity": 200},
                ],
            },
        ],
    }
    msg = adapter.format_cycle_summary(result)
    # 1. Bot identity
    assert "[autofin_signal]" in msg
    # 2. Signal context (autofin recomment) shown for each deal
    assert "Khuyến nghị đầu tư" in msg
    assert "Chốt lãi lần 1 mức giá 26,900" in msg
    # 3. Actual DNSE orders shown
    assert "BUY 700 TDP @ 32,000 VND" in msg
    assert "SELL 100 HCM @ 26,900 VND" in msg
    assert "SELL 200 L40 @ market VND" in msg
    # grouping
    assert "New entries (1)" in msg
    assert "Partial closes (1)" in msg
    assert "Full closes (1)" in msg


def test_format_cycle_summary_handles_empty_cycle():
    adapter = AutofinSignalAdapter()
    result = {
        "tradeDate": "2026-09-07",
        "signals": {"account": "0003937868", "nav": 0},
        "deals_actioned": [],
    }
    msg = adapter.format_cycle_summary(result)
    assert "[autofin_signal]" in msg
    assert "No orders planned" in msg
    assert "NAV=0" in msg


def test_format_cycle_summary_includes_portfolio_data():
    """The EOD summary should include the live portfolio snapshot:
    cash, NAV, and per-position unrealized P&L."""
    adapter = AutofinSignalAdapter()
    result = {
        "tradeDate": "2026-09-07",
        "signals": {"account": "0003937868", "nav": 0},
        "deals_actioned": [],
    }
    portfolio = {
        "balance": {
            "totalCash": 50_000_000,
            "availableCash": 30_000_000,
            "withdrawableCash": 30_000_000,
        },
        "positions": [
            {
                "symbol": "HCM",
                "quantity": 1000,
                "avgPrice": 26126,
                "marketPrice": 27500,
                "marketValue": 27_500_000,
                "costBasis": 26_126_000,
                "unrealizedPnl": 1_374_000,
                "unrealizedPnlPct": 5.26,
            },
        ],
        "totals": {
            "stockValue": 27_500_000,
            "portfolioValue": 57_500_000,
            "totalUnrealizedPnl": 1_374_000,
            "totalUnrealizedPnlPct": 2.45,
            "positionCount": 1,
        },
    }
    msg = adapter.format_cycle_summary(result, portfolio=portfolio)
    assert "CURRENT PORTFOLIO" in msg
    assert "30,000,000" in msg or "30.000.000" in msg  # availableCash
    assert "NAV:" in msg
    assert "HCM" in msg  # the position
    assert "uPnL" in msg


def test_format_cycle_summary_handles_none_portfolio():
    """Passing portfolio=None must not crash."""
    adapter = AutofinSignalAdapter()
    result = {
        "tradeDate": "2026-09-07",
        "signals": {"account": "0003937868", "nav": 0},
        "deals_actioned": [],
    }
    msg = adapter.format_cycle_summary(result, portfolio=None)
    assert "[autofin_signal]" in msg
    assert "portfolio" in msg.lower()  # shows "unavailable" note


# ── Start-of-day message (morning briefing) ───────────────────────────────


def test_start_of_day_message_shows_portfolio_and_orders(tmp_path, monkeypatch):
    from vietnam_algo.notifier import start_of_day_message
    portfolio = {
        "balance": {"totalCash": 50_000_000, "availableCash": 30_000_000},
        "positions": [
            {"symbol": "HCM", "quantity": 1000, "avgPrice": 26126, "marketPrice": 27500,
             "marketValue": 27_500_000, "costBasis": 26_126_000, "unrealizedPnl": 1_374_000,
             "unrealizedPnlPct": 5.26},
        ],
        "totals": {"stockValue": 27_500_000, "portfolioValue": 57_500_000,
                   "totalUnrealizedPnl": 1_374_000, "totalUnrealizedPnlPct": 2.45, "positionCount": 1},
    }
    orders_json = tmp_path / "latest_orders.json"
    orders_json.write_text(json.dumps({
        "count": 1, "items": [
            {"symbol": "HCM", "side": "BUY", "price": 26126, "quantity": 3000,
             "reason": "top_rank", "date": "2026-09-07"},
        ],
    }))
    msg = start_of_day_message(
        portfolio=portfolio,
        today="2026-09-07",
        orders_json_path=str(orders_json),
        strategy_name="momentum",
        account="0003937868",
    )
    # Bot identity + heading
    assert "START OF DAY" in msg
    assert "momentum" in msg
    assert "0003937868" in msg
    # Portfolio section
    assert "CURRENT PORTFOLIO" in msg
    assert "30,000,000" in msg
    assert "NAV:" in msg
    # Today's planned orders section
    assert "Today's planned orders" in msg
    assert "BUY 3000 HCM" in msg
    # Next-step hint
    assert "09:16 ICT" in msg


def test_start_of_day_message_handles_empty_orders(tmp_path):
    from vietnam_algo.notifier import start_of_day_message
    msg = start_of_day_message(
        portfolio=None,
        today="2026-09-07",
        orders_json_path=str(tmp_path / "missing.json"),  # doesn't exist
        strategy_name="momentum",
        account="0003937868",
    )
    assert "START OF DAY" in msg
    assert "No orders planned" in msg
    assert "portfolio snapshot: unavailable" in msg


def test_should_run_start_of_day_respects_trading_calendar():
    from vietnam_algo.schedule import (
        VIETNAM_TZ, should_run_start_of_day, TradingSchedule,
    )
    from datetime import datetime
    # Mon Sep 7 2026 — weekday
    mon = datetime(2026, 9, 7, 9, 30, tzinfo=VIETNAM_TZ)
    assert should_run_start_of_day(mon, last_date=None) is True
    # Already sent today
    assert should_run_start_of_day(mon, last_date="2026-09-07") is False
    # Before 09:00
    early = datetime(2026, 9, 7, 8, 59, tzinfo=VIETNAM_TZ)
    assert should_run_start_of_day(early, last_date=None) is False
    # Sat Sep 5 — weekend
    sat = datetime(2026, 9, 5, 10, 0, tzinfo=VIETNAM_TZ)
    assert should_run_start_of_day(sat, last_date=None) is False
    # Vietnam holiday Sep 2
    holiday = datetime(2026, 9, 2, 10, 0, tzinfo=VIETNAM_TZ)
    assert should_run_start_of_day(holiday, last_date=None, holidays={"2026-09-02"}) is False


# ── Per-order Telegram notifications (mid-day) ───────────────────────────


def test_per_order_message_contains_bot_id_signal_and_dnse_order(monkeypatch):
    """The short per-order Telegram message must still show:
      1. which bot is sending (autofin_signal)
      2. what signal triggered the order
      3. the actual DNSE order
    """
    import vietnam_algo.strategies_impl.autofin_signal as mod
    sent: list[str] = []
    monkeypatch.setattr(mod, "notify", lambda text, config=None: sent.append(text) or {"ok": True})

    adapter = AutofinSignalAdapter()
    deals = [
        {
            "action": "partial_close",
            "symbol": "HCM",
            "deal_id": "1785888000:HCM",
            "lastRecomment": "💰 Chốt lãi lần 1 mức giá 26,900",
            "rateForNav": 0.30,
            "numberOfProfit": 1,
            "dnse_orders": [
                {"symbol": "HCM", "side": "NS", "price": 26900, "quantity": 100, "reason": "autofin_partial:deal#1785888000:HCM:..."},
            ],
        },
    ]
    adapter._notify_new_orders(deals)
    assert len(sent) == 1
    msg = sent[0]
    # 1. bot identity
    assert "autofin_signal" in msg
    assert "new partial close" in msg
    # 2. signal context
    assert "💰 Chốt lãi lần 1 mức giá 26,900" in msg
    assert "deal#1785888000:HCM" in msg
    # 3. DNSE order
    assert "SELL 100 HCM @ 26,900 VND" in msg
    # notified keys recorded
    assert len(adapter._notified_order_keys) == 1


def test_per_order_message_not_re_notified_when_unchanged(monkeypatch):
    """The same order should not be re-notified on the next cycle."""
    import vietnam_algo.strategies_impl.autofin_signal as mod
    sent: list[str] = []
    monkeypatch.setattr(mod, "notify", lambda text, config=None: sent.append(text) or {"ok": True})

    adapter = AutofinSignalAdapter()
    deals = [
        {
            "action": "partial_close",
            "symbol": "HCM",
            "deal_id": "1785888000:HCM",
            "lastRecomment": "💰 Chốt lãi lần 1 mức giá 26,900",
            "rateForNav": 0.0,
            "numberOfProfit": 1,
            "dnse_orders": [
                {"symbol": "HCM", "side": "NS", "price": 26900, "quantity": 100, "reason": "autofin_partial:deal#1785888000:HCM:..."},
            ],
        },
    ]
    adapter._notify_new_orders(deals)
    adapter._notify_new_orders(deals)  # second call
    assert len(sent) == 1, f"expected 1 notification, got {len(sent)}"


def test_per_order_message_groups_bracket_legs_into_one_telegram(monkeypatch):
    """A new-entry BUY+TP+SL bracket should produce ONE Telegram message,
    not three (the user wants one notification per deal action)."""
    import vietnam_algo.strategies_impl.autofin_signal as mod
    sent: list[str] = []
    monkeypatch.setattr(mod, "notify", lambda text, config=None: sent.append(text) or {"ok": True})

    adapter = AutofinSignalAdapter()
    deals = [
        {
            "action": "new_entry",
            "symbol": "TDP",
            "deal_id": "1787270400:TDP",
            "lastRecomment": "Khuyến nghị đầu tư",
            "rateForNav": 0.15,
            "numberOfProfit": 0,
            "dnse_orders": [
                {"symbol": "TDP", "side": "NB", "price": 32000, "quantity": 700, "reason": "autofin_buy:deal#1787270400:TDP:..."},
                {"symbol": "TDP", "side": "NS", "price": 36000, "quantity": 700, "reason": "autofin_tp:deal#1787270400:TDP:..."},
                {"symbol": "TDP", "side": "NS", "price": 28500, "quantity": 700, "reason": "autofin_sl:deal#1787270400:TDP:..."},
            ],
        },
    ]
    adapter._notify_new_orders(deals)
    assert len(sent) == 1
    msg = sent[0]
    assert "new entry" in msg
    assert "BUY 700 TDP @ 32,000 VND" in msg
    assert "SELL 700 TDP @ 36,000 VND" in msg  # TP
    assert "SELL 700 TDP @ 28,500 VND" in msg  # SL
    # all 3 legs recorded as notified
    assert len(adapter._notified_order_keys) == 3
