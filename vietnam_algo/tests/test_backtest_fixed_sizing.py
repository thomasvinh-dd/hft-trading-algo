"""End-to-end test of the daily-replay backtest with fixed-sizing strategy.

This synthesizes a small dataset of autofin deals and runs
run_daily_replay_backtest() with a fake client. It validates:

- Each deal is entered at most once (no death-spiral re-entries)
- Positions persist across days (no daily SELL on a held deal)
- Partial-TP signals reduce position (mirror autofin numberOfProfit)
- Full-close on autofin exit (close day) sells remaining position
- Final NAV is non-catastrophic (no -99% blowup)

We can't run against the live autofin API in CI (network), so this
mocks the data.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

from vietnam_algo.libs.backtest_autofin_daily import (
    DayState,
    run_daily_replay_backtest,
)


def _make_deal(
    symbol: str,
    deal_id: str,
    p_open: float,
    p_close: float,
    n_profit: int,
    t_open: str,
    t_close: str,
    rate: float = 0.15,
    rec: str = "Đóng DEAL chạm điểm chặn lãi",
    days: int = 30,
) -> dict:
    """Synthesize a deal. If n_profit > 0, also generates a synthetic
    ``hisRecomment`` array with N partial-TP events distributed
    evenly over the holding period (for tests that need them)."""
    from datetime import datetime, timedelta
    parts = []
    t_open_dt = datetime.strptime(t_open, "%m/%d/%Y %H:%M:%S")
    parts.append(
        f'<div><p>Ngày khuyến nghị: {t_open_dt:%d/%m/%Y}</p>'
        f'<p>🟢 Vùng mua: {int(p_open)} - {int(p_open)}</p></div>'
    )
    if n_profit > 0:
        for k in range(1, n_profit + 1):
            day_off = int(k * days / (n_profit + 1))
            tp_price = int(p_open + (p_close - p_open) * (k / (n_profit + 1)))
            parts.append(
                f'<div><p>[{symbol}] 💰 Chốt lãi lần {k} mức giá {tp_price:,}</p>'
                f'<p>Tỷ trọng 10 % danh mục nắm giữ</p>'
                f'<p>Nắm giữ: {day_off} ngày</p></div>'
            )
    parts.append(f'<div><p>{rec}</p></div>')

    return {
        "_id": int(deal_id),
        "symbol": symbol,
        "companyName": f"Test {symbol}",
        "statusDeal": {
            "price_Open": p_open,
            "time_Open": t_open,
            "price_Closed": p_close,
            "time_Closed": t_close,
            "timeHolding": days,
            "isClosed": True,
        },
        "numberOfProfit": n_profit,
        "rateForNav": rate,
        "lastRecomment": rec,
        "supply_F": p_open,
        "supply_T": p_open,
        "tP_F": p_close,
        "tP_T": p_close,
        "sL_F": p_open * 0.9,
        "sL_T": p_open * 0.9,
        "hisRecomment": parts,
    }


class FakeClient:
    def __init__(self, deals: list[dict]):
        self._deals = deals
    def fetch_closing(self, page_size: int = 50, page_index: int = 1):
        # ignore pagination, return all
        return self._deals
    def fetch_all_deals_pages(
        self, symbols=None, page_size: int = 50,
        closed_only: bool = True, progress: bool = False,
    ):
        # For tests: just return whatever deals we were given (assume all closed).
        if closed_only:
            return [d for d in self._deals if d.get("statusDeal", {}).get("isClosed", True)]
        return self._deals


def test_fixed_sizing_no_death_spiral_on_losing_deal():
    """A single losing deal under fixed sizing should NOT trigger a
    chain of BUY/SELL re-entries. The deal is opened once, held, then
    closed at the loss. No NAV-based rebalancing."""
    deals = [_make_deal(
        symbol="LOSER",
        deal_id="1700000001",
        p_open=10000,
        p_close=8000,  # -20% loss
        n_profit=0,  # no partial TPs
        t_open="01/15/2024 07:00:00",
        t_close="02/15/2024 07:00:00",
    )]
    client = FakeClient(deals)
    result = run_daily_replay_backtest(
        initial_nav=1_000_000_000,
        fee_pct_per_side=0.0012,
        slippage_pct_per_fill=0.001,
        max_pages=1,
        client=client,
    )
    # Should have exactly ONE entry BUY (initial) + ONE final close SELL
    # (when LOSER is no longer in holding on close day). The TP/SL
    # bracket orders are conditional but won't fire (TP=8000 < price,
    # SL=9000 < entry but price never reaches SL on TWAP path that
    # goes straight from 10000 to 8000 at close).
    fills = result["filled_orders"]
    buys = [f for f in fills if f["side"] == "BUY"]
    sells = [f for f in fills if f["side"] == "SELL"]
    # We should have exactly ONE BUY (initial entry, fixed-size)
    assert len(buys) == 1, f"expected 1 BUY, got {len(buys)}: {buys}"
    # Exactly ONE SELL (the final close on close day)
    assert len(sells) == 1, f"expected 1 SELL, got {len(sells)}: {sells}"
    # Total fees: 4 fills (1 BUY + 1 SELL × 1 each side of bracket
    # is conditional, not filled). Actually only 2 actual fills:
    # 1 BUY + 1 SELL. Each has 1 fee side. So fees = 2 * (value * 0.0012).
    # Final NAV: should be negative but not catastrophic (-20% of position)
    assert result["final_nav"] > 0
    assert result["total_pnl_pct"] > -0.30  # no worse than -30% on one deal


def test_fixed_sizing_no_rebalance_on_winning_deal():
    """A winning deal should be entered once and held to close, with no
    re-entries or top-ups as NAV grows. Synthetic partial-TPs DO fire
    (since autofin's lastRecomment changes aren't in our snapshot)."""
    deals = [_make_deal(
        symbol="WINNER",
        deal_id="1700000002",
        p_open=10000,
        p_close=13000,  # +30% win
        n_profit=2,
        t_open="01/15/2024 07:00:00",
        t_close="02/15/2024 07:00:00",
    )]
    client = FakeClient(deals)
    result = run_daily_replay_backtest(
        initial_nav=1_000_000_000,
        fee_pct_per_side=0.0012,
        slippage_pct_per_fill=0.001,
        max_pages=1,
        client=client,
    )
    fills = result["filled_orders"]
    buys = [f for f in fills if f["side"] == "BUY"]
    sells = [f for f in fills if f["side"] == "SELL"]
    # 1 BUY (entry) — never a top-up
    assert len(buys) == 1, f"expected 1 BUY, got {len(buys)}"
    # 2 partial-TP SELLs + 1 final close SELL = 3 SELLs total.
    # Synthetic TPs evenly distributed over 30-day holding period.
    synthetic_sells = [s for s in sells if "synthetic" in s.get("reason", "")]
    final_closes = [s for s in sells if "autofin_closed" in s.get("reason", "")]
    assert len(synthetic_sells) == 2, f"expected 2 synthetic TPs, got {len(synthetic_sells)}"
    assert len(final_closes) == 1, f"expected 1 final close, got {len(final_closes)}"
    # Final NAV should be > initial (positive return)
    assert result["final_nav"] > result["initial_nav"]


def test_fixed_sizing_multiple_deals_no_overlap_rebalance():
    """Multiple overlapping deals should each be entered ONCE and held
    independently. No cross-deal rebalancing. Synthetic partial-TPs fire
    for each deal based on its own numberOfProfit count."""
    deals = [
        _make_deal(
            symbol="AAA", deal_id="1700000001", p_open=10000, p_close=11000,
            n_profit=1, t_open="01/15/2024 07:00:00",
            t_close="02/15/2024 07:00:00", rate=0.15,
        ),
        _make_deal(
            symbol="BBB", deal_id="1700000002", p_open=20000, p_close=19000,
            n_profit=0, t_open="01/25/2024 07:00:00",
            t_close="02/25/2024 07:00:00", rate=0.15,
        ),
        _make_deal(
            symbol="CCC", deal_id="1700000003", p_open=30000, p_close=33000,
            n_profit=2, t_open="02/05/2024 07:00:00",
            t_close="03/05/2024 07:00:00", rate=0.15,
        ),
    ]
    client = FakeClient(deals)
    result = run_daily_replay_backtest(
        initial_nav=1_000_000_000,
        fee_pct_per_side=0.0012,
        slippage_pct_per_fill=0.001,
        max_pages=1,
        client=client,
    )
    fills = result["filled_orders"]
    buys = [f for f in fills if f["side"] == "BUY"]
    # Each deal entered exactly once
    assert len(buys) == 3, f"expected 3 BUYs, got {len(buys)}: {[b['symbol'] for b in buys]}"
    # Sell count: AAA has 1 TP + 1 close = 2; BBB has 0 TP + 1 close = 1;
    # CCC has 2 TPs + 1 close = 3. Total = 6.
    assert len([f for f in fills if f["side"] == "SELL"]) == 6
    symbols_bought = sorted([b["symbol"] for b in buys])
    assert symbols_bought == ["AAA", "BBB", "CCC"]


def test_fixed_sizing_does_not_re_enter_after_full_close():
    """After a full close, the symbol's state is cleared. If autofin
    later re-suggests the same symbol, we should re-enter (it's a new
    deal). But within the same deal lifecycle, no re-entry."""
    deals = [_make_deal(
        symbol="REUSE",
        deal_id="1700000004",
        p_open=10000, p_close=11000, n_profit=0,
        t_open="01/15/2024 07:00:00",
        t_close="01/25/2024 07:00:00", rate=0.15,
    )]
    client = FakeClient(deals)
    result = run_daily_replay_backtest(
        initial_nav=1_000_000_000,
        fee_pct_per_side=0.0012,
        slippage_pct_per_fill=0.001,
        max_pages=1,
        client=client,
    )
    fills = result["filled_orders"]
    buys = [f for f in fills if f["side"] == "BUY"]
    # 10-day holding, single entry, single close. No re-entry.
    assert len(buys) == 1


def test_fixed_sizing_survives_loss_without_blowup():
    """A series of small losses under fixed sizing should NOT compound
    into a -90%+ blowup (the original NAV-rebalance bug). Each loss
    is contained to its own position size."""
    # 5 deals, each -10% loss, NAV-budgeted 0.15 each = 75% max deployed
    start = date(2024, 1, 1)
    deals = []
    for i in range(5):
        deals.append(_make_deal(
            symbol=f"L{i:02d}", deal_id=str(1700000000 + i),
            p_open=10000, p_close=9000, n_profit=0,
            t_open=(start + timedelta(days=i*5)).strftime("%m/%d/%Y 07:00:00"),
            t_close=(start + timedelta(days=i*5 + 30)).strftime("%m/%d/%Y 07:00:00"),
            rate=0.15,
        ))
    client = FakeClient(deals)
    result = run_daily_replay_backtest(
        initial_nav=1_000_000_000,
        fee_pct_per_side=0.0012,
        slippage_pct_per_fill=0.001,
        max_pages=1,
        client=client,
    )
    # Worst case: 5 × 15% × -10% = -7.5% loss, plus fees (a few percent).
    # Should NOT be -90%+ (that was the bug).
    assert result["total_pnl_pct"] > -0.20, (
        f"fixed-sizing should contain losses; got {result['total_pnl_pct']:.2%}"
    )
