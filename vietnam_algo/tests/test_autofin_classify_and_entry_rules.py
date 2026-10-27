"""Tests for the autofin ``last_recomment`` classifier and the
new-entry rules introduced per user feedback 2026-09-21.

User quote: "seem like we are parsing the autofin signal wrong, the
signal like autofin_signal: new entry / signal: 💰 Chốt lãi lần 1
mức giá 16,600 should not be new entry, it should be partial sell.
Let set again our new entry rule:
- New entry only at start of day when available_cash >= 5% NAV
- Top suggestion from autofin (one entry per day)
- For TP/SL/reduce signals on held deals, fire immediately
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

from vietnam_algo.libs.autofin_signals import (
    RECOMMENT_KIND_CAPITAL_PROTECT,
    RECOMMENT_KIND_CLOSE,
    RECOMMENT_KIND_ENTRY,
    RECOMMENT_KIND_REDUCE,
    RECOMMENT_KIND_TP,
    RECOMMENT_KIND_UNKNOWN,
    classify_recomment,
)


def test_classify_recomment_fresh_entry():
    """Khuyến nghị đầu tư / Ngày khuyến nghị → entry signal."""
    assert classify_recomment("Khuyến nghị đầu tư") == RECOMMENT_KIND_ENTRY
    assert (
        classify_recomment(
            "Ngày khuyến nghị: 21/09/2026, Vùng mua: 20000 - 21000"
        )
        == RECOMMENT_KIND_ENTRY
    )
    # Case-insensitive (Vietnamese diacritics preserved)
    assert classify_recomment("khuyến nghị mua") == RECOMMENT_KIND_ENTRY


def test_classify_recomment_tp_step():
    """💰 Chốt lãi lần N mức giá P → TP signal, NOT entry."""
    assert (
        classify_recomment("💰 Chốt lãi lần 1 mức giá 16,600")
        == RECOMMENT_KIND_TP
    )
    assert (
        classify_recomment(
            "[ASP] 💰 Chốt lãi lần 1 mức giá 15,800 "
            "Tỷ trọng 10 % danh mục nắm giữ"
        )
        == RECOMMENT_KIND_TP
    )
    # User's specific example — the one that triggered the bug
    assert (
        classify_recomment("💰 Chốt lãi lần 1 mức giá 16,600")
        == RECOMMENT_KIND_TP
    )


def test_classify_recomment_capital_protect():
    """🛡️ Chốt lãi X % bảo toàn vốn → capital_protect signal."""
    assert (
        classify_recomment(
            "🛡️ Chốt lãi 28.57 % bảo toàn vốn tại 15,800"
        )
        == RECOMMENT_KIND_CAPITAL_PROTECT
    )


def test_classify_recomment_reduce():
    """⚠️ Hạ X % tỉ trọng lần N → reduce signal."""
    assert (
        classify_recomment("⚠️ Hạ 30 % tỉ trọng lần 1, Giá: 15,500")
        == RECOMMENT_KIND_REDUCE
    )


def test_classify_recomment_full_close():
    """Đóng DEAL … → close signal. Checked BEFORE TP because the
    close text usually embeds the TP price.
    """
    assert (
        classify_recomment(
            "Đóng DEAL chạm điểm chặn lãi (Lãi: 34.88 %)"
        )
        == RECOMMENT_KIND_CLOSE
    )
    # The TP-step text says "Chốt lãi lần 1 mức giá X" without
    # "Đóng DEAL" — that's a partial close, not a full close.
    assert (
        classify_recomment("💰 Chốt lãi lần 1 mức giá 15,800")
        == RECOMMENT_KIND_TP
    )


def test_classify_recomment_unknown_when_empty():
    """Empty / random text → unknown (be conservative: not an entry)."""
    assert classify_recomment("") == RECOMMENT_KIND_UNKNOWN
    assert classify_recomment("test") == RECOMMENT_KIND_UNKNOWN
    assert classify_recomment("random noise") == RECOMMENT_KIND_UNKNOWN


# --- New entry rule tests -----------------------------------------------


from vietnam_algo.algos.autofin_signal import _plan_orders_for_new_positions
from vietnam_algo.algos.autofin_signal import parse_signal as _parse_signal


def _make_signal(
    symbol: str,
    rate_for_nav: float,
    deal_id: str = "12345",
    supply_to: float = 10000.0,
    tp_from: float = 12000.0,
    sl_to: float = 9000.0,
    last_recomment: str = "Khuyến nghị đầu tư",
) -> object:
    raw = {
        "_id": int(deal_id) if deal_id.isdigit() else deal_id,
        "lastIndex": 1,
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
        "market_TypePrice": 1,
    }
    return _parse_signal(raw)


def test_plan_new_positions_skips_tp_signals():
    """The bug fix: a signal with ``last_recomment = '💰 Chốt lãi …'``
    must NOT produce a BUY order. It belongs to the partial-close
    path, not new-entry.
    """
    tp_signal = _make_signal(
        "HHP",
        rate_for_nav=0.15,
        deal_id="1787529600",
        last_recomment="💰 Chốt lãi lần 1 mức giá 16,600",
    )
    rows = _plan_orders_for_new_positions(
        selected=[tp_signal],
        nav_vnd=1_000_000_000,
        our_positions={},
    )
    assert rows == [], (
        f"TP signal should not generate BUY, got: {rows}"
    )


def test_plan_new_positions_skips_close_signals():
    """Full-close signal must NOT generate BUY either."""
    close_signal = _make_signal(
        "HHP",
        rate_for_nav=0.15,
        deal_id="1787529600",
        last_recomment="Đóng DEAL chạm điểm cắt lỗ",
    )
    rows = _plan_orders_for_new_positions(
        selected=[close_signal],
        nav_vnd=1_000_000_000,
        our_positions={},
    )
    assert rows == []


def test_plan_new_positions_accepts_entry_signal():
    """A signal with last_recomment = 'Khuyến nghị đầu tư' IS eligible."""
    entry_signal = _make_signal(
        "PVT",
        rate_for_nav=0.15,
        deal_id="99",
        last_recomment="Khuyến nghị đầu tư",
    )
    rows = _plan_orders_for_new_positions(
        selected=[entry_signal],
        nav_vnd=1_000_000_000,
        our_positions={},
    )
    buys = [r for r in rows if r["side"] == "NB"]
    assert len(buys) == 1
    assert buys[0]["symbol"] == "PVT"


def test_plan_new_positions_cash_gate_5pct():
    """If available cash < 5% of NAV, skip ALL new entries.

    Default 100M NAV → 5M cash threshold. Pass 1M available cash;
    the gate should block.
    """
    sig = _make_signal("AAA", 0.15, deal_id="1")
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=100_000_000,
        our_positions={},
        available_cash_vnd=1_000_000,  # 1% of NAV — below 5% gate
    )
    buys = [r for r in rows if r["side"] == "NB"]
    assert buys == [], (
        "5% cash gate should block entries when cash is only 1%"
    )
    # And 5% exactly should still be blocked (strict <)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=100_000_000,
        our_positions={},
        available_cash_vnd=4_999_999,  # just under 5%
    )
    assert [r for r in rows if r["side"] == "NB"] == []
    # 5% exactly should pass (gate is `>= 5%` to allow 5% boundary)
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=100_000_000,
        our_positions={},
        available_cash_vnd=5_000_000,  # exactly 5%
    )
    assert len([r for r in rows if r["side"] == "NB"]) == 1
    # 6% should pass too
    rows = _plan_orders_for_new_positions(
        selected=[sig],
        nav_vnd=100_000_000,
        our_positions={},
        available_cash_vnd=6_000_000,  # 6% — passes
    )
    assert len([r for r in rows if r["side"] == "NB"]) == 1


def test_plan_new_positions_top_n_filter():
    """``top_n=1`` (default) means only the first signal in the list
    can produce an entry — even if subsequent signals also qualify.

    User spec 2026-09-21: "check the signal suggestion on autofin for
    top suggestion" — exactly one entry attempt per call.
    """
    sig_a = _make_signal("AAA", 0.15, deal_id="1", supply_to=10000)
    sig_b = _make_signal("BBB", 0.15, deal_id="2", supply_to=10000)
    sig_c = _make_signal("CCC", 0.15, deal_id="3", supply_to=10000)
    rows = _plan_orders_for_new_positions(
        selected=[sig_a, sig_b, sig_c],
        nav_vnd=1_000_000_000,
        our_positions={},
    )
    # Each entry emits BUY + SL bracket (2 orders).
    # top_n=1 → 1 entry → 2 orders.
    buys = [r for r in rows if r["side"] == "NB"]
    sells = [r for r in rows if r["side"] == "NS"]
    assert len(buys) == 1, (
        f"top_n=1 should produce exactly one BUY, got {len(buys)}"
    )
    assert buys[0]["symbol"] == "AAA", "first signal wins"
    assert len(sells) == 1, "BUY + SL bracket pair"


def test_plan_new_positions_top_n_can_be_increased():
    """``top_n=10`` allows more entries — used for backtests and the
    held-unentered expansion where multiple deals qualify."""
    sigs = [
        _make_signal(f"S{i}", 0.15, deal_id=str(i))
        for i in range(5)
    ]
    rows = _plan_orders_for_new_positions(
        selected=sigs,
        nav_vnd=1_000_000_000,
        our_positions={},
        top_n=10,
    )
    buys = [r for r in rows if r["side"] == "NB"]
    # 5 BUY entries (each paired with an SL bracket)
    assert len(buys) == 5
    # Only the first 5 symbols got entries
    symbols = sorted({b["symbol"] for b in buys})
    assert symbols == ["S0", "S1", "S2", "S3", "S4"]
