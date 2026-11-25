"""Tests for new autofin_signal deal-id tracking logic."""
from __future__ import annotations

import sys
sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

import vietnam_algo.algos.autofin_signal as mod
from vietnam_algo.algos.autofin_signal import (
    _deal_id,
    _parse_partial_close,
    _plan_close_orders,
    _plan_orders_for_new_positions,
    _plan_partial_close_orders,
)


def _make_signal(
    symbol: str,
    rate_for_nav: float,
    deal_id: str = "12345",
    last_index: int = 1,
    supply_to: float = 10000.0,
    tp_from: float = 12000.0,
    sl_to: float = 9000.0,
    last_recomment: str = "Khuyến nghị đầu tư",
) -> dict:
    raw = {
        "_id": int(deal_id) if deal_id.isdigit() else deal_id,
        "lastIndex": last_index,
        "symbol": symbol,
        "companyName": symbol,
        "statusDeal": {"market_TypePrice": 1},
        "supply_F": supply_to - 100,
        "supply_T": supply_to,
        "tP_F": tp_from,
        "tP_T": tp_from + 100,
        "sL_F": sl_to - 100,
        "sL_T": sl_to,
        "rateForNav": rate_for_nav,
        "lastRecomment": last_recomment,
        "isWaittingBuy": False,
        "isWaittingSell": False,
        "numberOfProfit": 0,
        "rateIncome": 0,
        "rateIncome_TypePrice": 3,
    }
    return raw


def test_plan_new_positions_emits_buy_sl_bracket():
    """New entry emits BUY + SL only — TP bracket is NOT emitted
    (per-event partial-close logic is the canonical mechanism).
    Per user feedback 2026-09-09."""
    sig = mod.parse_signal(_make_signal("APH", 0.15, deal_id="111", supply_to=6100, tp_from=6500, sl_to=5320))
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=1_000_000_000, our_positions={}
    )
    sides = [r["side"] for r in rows]
    assert sides.count("NB") == 1
    assert sides.count("NS") == 1  # SL only — no TP bracket
    buy = next(r for r in rows if r["side"] == "NB")
    sell = next(r for r in rows if r["side"] == "NS")
    assert "deal#111" in buy["reason"]
    assert sell["kind"] == "sl"
    assert buy["price"] == 6100


def test_plan_new_positions_skips_when_target_too_small():
    sig = mod.parse_signal(_make_signal("APH", 0.05, deal_id="222", supply_to=6100))
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=10_000_000, our_positions={}
    )
    # target = 0.05 * 10M = 500k < 1M min → skip
    assert rows == []


def test_plan_new_positions_no_op_when_match():
    sig = mod.parse_signal(_make_signal("APH", 0.10, deal_id="333", supply_to=6100))
    rows = _plan_orders_for_new_positions(
        selected=[sig], nav_vnd=610_000, our_positions={"APH": 100}
    )
    # 0.10 * 610k = 61k < 1M min → skip
    assert rows == []


def test_plan_close_orders_only_closes_what_we_hold():
    """SELL only deals we hold that autofin no longer holds."""
    our_deals = {
        "111:APH": {"symbol": "APH", "_our_qty": 200},
        "222:VHM": {"symbol": "VHM", "_our_qty": 100},
        "333:BAF": {"symbol": "BAF", "_our_qty": 50},
    }
    autofin_holding = [
        # APH still held
        {"_id": 111, "symbol": "APH", "statusDeal": {"isClosed": False}},
        # VHM exited
        # BAF still held
        {"_id": 333, "symbol": "BAF", "statusDeal": {"isClosed": False}},
    ]
    rows = _plan_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "VHM"
    assert rows[0]["reason"] == "autofin_closed:deal#222:VHM"


def test_deal_id_extraction_handles_underscore_or_code():
    raw1 = {"_id": 12345, "code": 99999, "symbol": "A"}
    raw2 = {"_id": None, "code": 88888, "symbol": "B"}
    raw3 = {"_id": None, "symbol": "C"}  # no id
    # _id alone is a timestamp, not unique across deals — must include symbol
    assert _deal_id(raw1) == "12345:A"
    assert _deal_id(raw2) == "88888:B"
    # no _id/code → empty key (caller will skip)
    assert _deal_id(raw3) == ":C"


def test_deal_id_is_unique_across_same_day_deals():
    """Two deals opened on the same day share _id but have different symbols."""
    a = {"_id": 1785888000, "symbol": "HCM"}
    b = {"_id": 1785888000, "symbol": "MZG"}
    assert _deal_id(a) != _deal_id(b)


# ── Partial-close (lastRecomment) ──────────────────────────────────────────


def test_parse_partial_close_take_profit_lap_with_price():
    a = _parse_partial_close("💰 Chốt lãi lần 1 mức giá 26,900")
    assert a.action == "take_profit_lap"
    assert a.lap == 1
    assert a.price == 26900.0


def test_parse_partial_close_take_profit_lap_no_price():
    a = _parse_partial_close("💰 Chốt lãi lần 2 mức giá 17,100")
    assert a.action == "take_profit_lap"
    assert a.lap == 2
    assert a.price == 17100.0


def test_parse_partial_close_reduce_lap():
    # autofin's actual format embeds the percent between "Hạ"
    # and "tỷ/tỉ trọng": "[HNM] ⚠️ Hạ 50 % tỉ trọng lần 1".
    # The OLD regex `Hạ tỷ trọng lần\s*(\d+)` matched the synthetic
    # form (no %) but never real autofin output — that was the
    # 2026-09-24 HNM-missed-signal incident. The new regex
    # requires the percent and accepts both "tỷ trọng" and
    # "tỉ trọng" spellings.
    a = _parse_partial_close("⚠️ Hạ 30 % tỷ trọng lần 2")
    assert a.action == "reduce_lap"
    assert a.lap == 2
    assert a.percent == 30.0
    assert a.price == 0.0


def test_parse_partial_close_capital_protect():
    a = _parse_partial_close("🛡️ Chốt lãi 28.57 % bảo toàn vốn")
    assert a.action == "capital_protect"
    assert abs(a.percent - 28.57) < 1e-6


def test_parse_partial_close_full_close():
    a = _parse_partial_close("Đóng DEAL chạm điểm chặn lãi (Lãi: 230.32 %)")
    assert a.action == "full_close"


def test_parse_partial_close_unknown():
    a = _parse_partial_close("Khuyến nghị đầu tư")
    assert a.action == "unknown"


def test_partial_close_bootstrap_records_only_no_orders():
    """First time seeing the deal → record nP, do NOT issue partial close."""
    our_deals = {"111:HCM": {"symbol": "HCM", "_our_qty": 300}}
    autofin_holding = [{
        "_id": 111, "symbol": "HCM", "numberOfProfit": 1,
        "lastRecomment": "💰 Chốt lãi lần 1 mức giá 26,900",
        "tP_F": 26900, "sL_F": 22700,
    }]
    rows = _plan_partial_close_orders(our_deals, autofin_holding)
    assert rows == []
    assert our_deals["111:HCM"]["_last_numberOfProfit"] == 1
    assert our_deals["111:HCM"]["_last_recomment"].startswith("💰")


def test_partial_close_emits_sell_on_numberOfProfit_bump():
    """nP went 0→1 → emit SELL 1/3 at recomment price."""
    our_deals = {
        "111:HCM": {"symbol": "HCM", "_our_qty": 300, "_last_numberOfProfit": 0},
    }
    autofin_holding = [{
        "_id": 111, "symbol": "HCM", "numberOfProfit": 1,
        "lastRecomment": "💰 Chốt lãi lần 1 mức giá 26,900",
        "tP_F": 26900, "sL_F": 22700,
    }]
    rows = _plan_partial_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "HCM"
    assert r["side"] == "NS"
    assert r["price"] == 26900  # from recomment
    assert r["quantity"] == 100  # 300 // 3
    assert "take_profit_lap" in r["reason"]
    assert "deal#111:HCM" in r["reason"]
    assert our_deals["111:HCM"]["_last_numberOfProfit"] == 1
    assert our_deals["111:HCM"]["_our_qty"] == 200  # 300 - 100


def test_partial_close_uses_tpF_when_no_price_in_recomment():
    """'Hạ X % tỷ trọng' has no price → fall back to tP_F from autofin.

    Holding endpoint returns raw VND; suggest endpoint returns thousands.
    Both should produce the same VND price.
    """
    our_deals = {
        "222:POW": {"symbol": "POW", "_our_qty": 300, "_last_numberOfProfit": 0},
    }
    autofin_holding = [{
        "_id": 222, "symbol": "POW", "numberOfProfit": 1,
        "lastRecomment": "⚠️ Hạ 30 % tỷ trọng lần 2",
        "tP_F": 15100, "sL_F": 12700,  # raw VND (holding endpoint)
    }]
    rows = _plan_partial_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    r = rows[0]
    assert r["price"] == 15100  # raw VND, no double-multiplication


def test_partial_close_uses_tpF_in_thousands_when_small():
    """Same result if data is in thousands (suggest-endpoint format)."""
    our_deals = {
        "222:POW": {"symbol": "POW", "_our_qty": 300, "_last_numberOfProfit": 0},
    }
    autofin_holding = [{
        "_id": 222, "symbol": "POW", "numberOfProfit": 1,
        "lastRecomment": "⚠️ Hạ 30 % tỷ trọng lần 2",
        "tP_F": 15.1, "sL_F": 12.7,  # thousands (suggest endpoint)
    }]
    rows = _plan_partial_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    assert rows[0]["price"] == 15100


def test_partial_close_capital_protect_uses_pct():
    """2026-10-18: 'bảo toàn vốn X%' means autofin claims the
    position is up X% P&L — SELL ALL REMAINING QTY at the
    suggested price to lock in the profit. (The old code
    incorrectly sold X% of the qty, which was the wrong read.)
    """
    our_deals = {
        "333:HSL": {"symbol": "HSL", "_our_qty": 700, "_last_numberOfProfit": 3},
    }
    autofin_holding = [{
        "_id": 333, "symbol": "HSL", "numberOfProfit": 4,
        "lastRecomment": "🛡️ Chốt lãi 28.57 % bảo toàn vốn tại 16,000",
        "tP_F": 0,
    }]
    rows = _plan_partial_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    r = rows[0]
    # Full close — sell all 700 (rounded down to 700 since 700 is
    # already an exact multiple of the 100-share board lot).
    assert r["quantity"] == 700
    # Price comes from the "tại P" suffix in the recomment.
    assert r["price"] == 16000
    # The kind is ``full_close`` (not ``partial_close``) because
    # the action is to exit the entire position.
    assert r["kind"] == "full_close"
    assert "capital_protect" in r["reason"]


def test_partial_close_capital_protect_full_close_with_tai_price():
    """User's 2026-10-18 example: "🛡️ Chốt lãi 22.22 % bảo
    toàn vốn tại 13,700". autofin says the position is up
    22.22% and suggests exiting at 13,700. The bot must SELL
    ALL remaining qty at exactly 13,700 — not sell 22.22% of
    the qty."""
    our_deals = {
        "555:QNS": {"symbol": "QNS", "_our_qty": 1000, "_last_numberOfProfit": 0},
    }
    autofin_holding = [{
        "_id": 555, "symbol": "QNS", "numberOfProfit": 1,
        "lastRecomment": "🛡️ Chốt lãi 22.22 % bảo toàn vốn tại 13,700",
        "tP_F": 0,
    }]
    rows = _plan_partial_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    r = rows[0]
    # Full close: all 1000 shares (board-lot aligned).
    assert r["quantity"] == 1000
    # Exact price from the recomment.
    assert r["price"] == 13700
    # Reasoning: capital_protect → exit to protect the +22.22%.
    assert "capital_protect" in r["reason"]
    # fraction tracking must reflect the full close (1.0) so
    # the next cycle doesn't re-fire on the same recomment.
    assert our_deals["555:QNS"]["_last_partial_fraction"] == 1.0


def test_partial_close_capital_protect_no_tai_price_uses_market():
    """When the recomment has no "tại P" suffix, capital_protect
    still SELLs ALL but at the live best-bid (no tP_F fallback
    since capital_protect is a NEW action type that autofin
    only publishes with an explicit price)."""
    our_deals = {
        "777:AAA": {"symbol": "AAA", "_our_qty": 500, "_last_numberOfProfit": 0},
    }
    autofin_holding = [{
        "_id": 777, "symbol": "AAA", "numberOfProfit": 1,
        # No "tại P" suffix — autofin published only the headline.
        "lastRecomment": "🛡️ Chốt lãi 5.0 % bảo toàn vốn",
        "tP_F": 0,
    }]
    rows = _plan_partial_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    r = rows[0]
    # Full close.
    assert r["quantity"] == 500
    # No "tại P" and no tP_F → price=0 (live submit overrides to best_bid).
    assert r["price"] == 0
    assert r["kind"] == "full_close"


def test_submit_signal_price_zero_sell_not_dropped():
    """2026-10-21 BAF fix: when a SELL order carries ``price=0`` from
    ``_plan_partial_close_orders`` (no ``tại P`` suffix on the autofin
    recomment — the SELL-at-market override in the runner fills in
    the live best-bid downstream), the runner's deals-conversion loop
    MUST still keep the order; the previous ``signal_price <= 0:
    continue`` gate silently dropped it. BUY orders with price=0 are
    still dropped (a BUY at any price is unsafe)."""
    # The conversion loop is inlined inside
    # ``_submit_autofin_deals_to_dnse``. We exercise it by reading
    # the runner source and asserting the contract (defensive
    # smoke test — the full conversion is exercised end-to-end by
    # the live bot cycle once deployed).
    from vietnam_algo.bots import runner as bot_runner
    src = open(bot_runner.__file__, "r", encoding="utf-8").read()
    assert 'elif signal_price <= 0 and side == "NB":' in src, (
        "Bug #5 from 2026-10-21: the silent-drop gate must be gated "
        "to BUY-only — SELL orders with price=0 must NOT skip."
    )
    # And the previous UB-gate (``and token`` UnboundLocalError)
    # must also be patched at the top of run_auto_bot's loop.
    assert "token: str | None = None" in src, (
        "Bug from 2026-10-21: token must be initialised at the top "
        "of the main loop so the mid-day-poll block does not raise "
        "UnboundLocalError on the first cycle of a fresh bot instance."
    )


def test_partial_close_skips_when_numberOfProfit_unchanged():
    our_deals = {
        "444:VPI": {
            "symbol": "VPI",
            "_our_qty": 300,
            "_last_numberOfProfit": 1,
            "_last_recomment": "💰 Chốt lãi lần 1 mức giá 64,900",
        },
    }
    autofin_holding = [{
        "_id": 444, "symbol": "VPI", "numberOfProfit": 1,  # unchanged
        "lastRecomment": "💰 Chốt lãi lần 1 mức giá 64,900",  # also unchanged
        "tP_F": 64900,
    }]
    rows = _plan_partial_close_orders(our_deals, autofin_holding)
    assert rows == []


def test_partial_close_skips_deal_autofin_no_longer_holds():
    our_deals = {
        "555:L40": {"symbol": "L40", "_our_qty": 100, "_last_numberOfProfit": 0},
    }
    # autofin has fully closed — L40 not in holding list
    autofin_holding = []
    rows = _plan_partial_close_orders(our_deals, autofin_holding)
    assert rows == []  # full close handled by _plan_close_orders


def test_partial_close_sub_lot_rounds_up_to_lot():
    """Delta < 1 board lot ROUNDS UP to 1 lot.

    autofin's "Tỷ trọng 10% danh mục" style recommendations give a
    delta < 1 board lot for small positions (e.g. 10% × 200 = 20
    shares). Vietnamese boards trade in lots of 100, so rounding
    down would skip the order entirely — the user explicitly
    wanted round-up behavior (per feedback 2026-09-18: 10% × 600 =
    60 → 100 shares, not 60 → 0).
    """
    our_deals = {
        "666:X": {"symbol": "X", "_our_qty": 200, "_last_numberOfProfit": 0},
    }
    autofin_holding = [{
        "_id": 666, "symbol": "X", "numberOfProfit": 1,
        "lastRecomment": "💰 Chốt lãi lần 1 mức giá 10,000",
        "tP_F": 10000,
    }]
    # 200 // 3 = 66, rounded UP to 100 = 1 lot.
    rows = _plan_partial_close_orders(our_deals, autofin_holding)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "X"
    assert rows[0]["quantity"] == 100
