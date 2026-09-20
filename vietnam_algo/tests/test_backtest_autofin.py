"""Tests for the autofin backtest (closing-signals replay).

Phase 1: strict no-look-ahead replay with realistic per-step fees + slippage.
The headline numbers from `run_autofin_backtest` MUST NOT use any hindsight
filter — those live in `run_hindsight_variants` and are clearly labelled.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

import vietnam_algo.libs.backtest_autofin as mod
from vietnam_algo.libs.backtest_autofin import (
    AutofinTrade,
    _round_to_lots,
    _simulate_trade,
    run_autofin_backtest,
    run_hindsight_variants,
    write_backtest_outputs,
)


def _make_closed_deal(
    symbol: str = "L40",
    deal_id: str = "1756166400",
    p_open: float = 12200.0,
    p_close: float = 35200.0,
    days: int = 22,
    n_profit: int = 15,
    rate: float = 0.15,
    t_open: str = "08/26/2025 07:00:00",
    t_close: str = "09/29/2025 07:00:00",
    rec: str = "Đóng DEAL chạm điểm chặn lãi (Lãi: 230.32 %)",
) -> dict:
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
            "isWin": p_close > p_open,
        },
        "numberOfProfit": n_profit,
        "rateForNav": rate,
        "lastRecomment": rec,
        "rateIncome": (p_close - p_open) / p_open if p_open > 0 else 0,
    }


class FakeClient:
    def __init__(self, deals: list[dict]):
        self._deals = deals
    def fetch_closing(self, page_size: int = 50, page_index: int = 1):
        start = (page_index - 1) * page_size
        return self._deals[start:start + page_size]
    def fetch_all_deals_pages(
        self, symbols=None, page_size: int = 50,
        closed_only: bool = True, progress: bool = False,
    ):
        if closed_only:
            return [d for d in self._deals if d.get("statusDeal", {}).get("isClosed", True)]
        return self._deals


def test_round_to_lots_basic():
    assert _round_to_lots(150) == 100
    assert _round_to_lots(199) == 100
    assert _round_to_lots(200) == 200
    assert _round_to_lots(250) == 200
    assert _round_to_lots(0) == 0


def test_simulate_trade_no_steps_is_buy_then_sell():
    """Zero partial-TP steps → straight BUY + SELL, fees+slippage on each leg."""
    sim = _simulate_trade(
        qty=10_000,
        price_open=10_000,
        price_close=11_000,
        n_profit_steps=0,
        fee_pct_per_side=0.001,
        slippage_pct_per_fill=0.001,
    )
    # entry @ 10000*(1+0.0005) = 10005
    # exit @ 11000*(1-0.0005) = 10994.5
    # cost_basis ≈ 10005 * 10000 + fee ≈ 100_050_000 + 100_050 = 100_150_050
    # proceeds ≈ 10994.5 * 10000 - fee ≈ 109_945_000 - 109_945 = 109_835_055
    # pnl ≈ 9_685_005
    assert sim["partial_steps"] == []
    assert sim["fees_total"] > 0
    assert sim["slippage_total"] > 0
    assert sim["pnl"] > 0
    # gross return is (11000 - 10000) / 10000 = 10%; net is lower
    assert sim["pnl_pct"] < 0.10
    assert sim["pnl_pct"] > 0.08


def test_simulate_trade_with_partial_steps_splits_qty():
    """N partial-TP steps split qty across N+1 sell legs (N partials + final).

    Entry buys the FULL qty (we hold everything from T0 to T_close
    and trim along the way). Each partial sells qty/(N+1). The
    TWAP fill model gives the partials intermediate prices.
    """
    sim = _simulate_trade(
        qty=12_000,
        price_open=10_000,
        price_close=12_000,
        n_profit_steps=3,
        fee_pct_per_side=0.0,
        slippage_pct_per_fill=0.0,
    )
    # 3 partials + 1 final = 4 sell legs, 3000 each
    assert len(sim["partial_steps"]) == 3
    assert all(s["qty"] == 3000 for s in sim["partial_steps"])
    assert sim["leg_qtys"] == [3000, 3000, 3000, 3000]
    # TWAP fill prices: 10500, 11000, 11500 for the 3 partials
    assert abs(sim["partial_steps"][0]["ref_price"] - 10500) < 1
    assert abs(sim["partial_steps"][1]["ref_price"] - 11000) < 1
    assert abs(sim["partial_steps"][2]["ref_price"] - 11500) < 1
    # Pnl = 3000*(10500-10000) + 3000*(11000-10000) + 3000*(11500-10000) + 3000*(12000-10000)
    #     = 1.5M + 3M + 4.5M + 6M = 15M
    assert abs(sim["pnl"] - 15_000_000) < 1


def test_simulate_trade_partial_steps_pay_per_leg_costs():
    """Each partial-TP leg pays its own fee+slippage."""
    sim_no = _simulate_trade(
        qty=10_000, price_open=10_000, price_close=10_000,
        n_profit_steps=3, fee_pct_per_side=0.0, slippage_pct_per_fill=0.0,
    )
    sim_fee = _simulate_trade(
        qty=10_000, price_open=10_000, price_close=10_000,
        n_profit_steps=3, fee_pct_per_side=0.001, slippage_pct_per_fill=0.001,
    )
    # Flat trade: entry fee+slippage on the buy side, fee+slippage on
    # each of the 4 sell legs (3 partials + final). No pnl in either case
    # when price is flat.
    assert sim_no["pnl"] == 0  # no costs, flat price
    # With costs, pnl is negative because we paid fees on entry + 4 sells.
    assert sim_fee["pnl"] < 0
    assert sim_fee["fees_total"] > 0
    assert sim_fee["slippage_total"] > 0


def test_backtest_basic_profitable_deal():
    """L40: BUY 12200 → 15 partial-TPs + SELL 35200 with 15% NAV → big profit.

    With 15 partials + 1 final leg, the TWAP model captures most of
    the gain through the partials but the partials fill at lower
    prices than 35200 (the final close). So pnl is less than the
    naive "hold all 12200 to close" model (~280M) and lands
    closer to ~150M with default fees+slippage.
    """
    deals = [_make_closed_deal()]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    assert result["trade_count"] == 1
    assert result["win_count"] == 1
    assert result["loss_count"] == 0
    assert result["win_rate_pct"] == 1.0
    # 1B * 0.15 = 150M, / 12200 = 12295 → 122 lots = 12200 shares
    trade = result["trades"][0]
    assert trade.symbol == "L40"
    assert trade.quantity == 12200
    # 15 partial steps + 1 final leg
    assert len(trade.partial_steps) == 15
    # TWAP pnl (without costs) ≈ 144M; with 0.12% fees + 0.1% slippage,
    # land in the 140-150M range. Sanity-check:
    assert 140_000_000 < trade.pnl_vnd < 155_000_000
    assert result["final_nav"] > result["initial_nav"]
    # Strict mode → mode flag set
    assert result["mode"] == "strict_no_lookahead"


def test_backtest_does_not_apply_hindsight_filters_by_default():
    """Default run keeps ALL deals regardless of numberOfProfit or final close."""
    deals = [
        _make_closed_deal(symbol="LOSER", deal_id="1", p_open=10000, p_close=8000, n_profit=0, rate=0.15),
        _make_closed_deal(symbol="WINNER", deal_id="2", p_open=10000, p_close=13000, n_profit=3, rate=0.15),
    ]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    # both should be kept — LOSER has n_profit=0 and closes below entry, but
    # the strict mode does NOT use that hindsight.
    assert result["trade_count"] == 2
    symbols = {t.symbol for t in result["trades"]}
    assert symbols == {"LOSER", "WINNER"}


def test_backtest_losing_deal_decreases_nav():
    """A losing deal reduces NAV."""
    deals = [_make_closed_deal(p_open=10000, p_close=8000, rate=0.30)]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    assert result["trade_count"] == 1
    assert result["win_count"] == 0
    assert result["loss_count"] == 1
    assert result["final_nav"] < result["initial_nav"]


def test_backtest_skips_zero_price_deals():
    """Deals with missing prices are skipped, not crashed."""
    deals = [
        _make_closed_deal(p_open=0, p_close=10000),
        _make_closed_deal(p_open=10000, p_close=0),
        _make_closed_deal(rate=0),
    ]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    assert result["trade_count"] == 0
    assert result["skipped_count"] == 3


def test_backtest_compounds_nav_across_deals():
    """NAV evolves chronologically — second deal sized off post-deal-1 NAV.

    Two deals, each with 0 partial steps (simple BUY→SELL). The
    TWAP fill degenerates to a single sell at price_close when
    n_profit_steps=0, so this test verifies the entry/exit math
    cleanly without the partial-TP interpolation.
    """
    deals = [
        _make_closed_deal(
            symbol="A", deal_id="1", p_open=10000, p_close=12000, rate=0.5,
            t_open="2025-01-01", t_close="2025-02-01", n_profit=0,
        ),
        _make_closed_deal(
            symbol="B", deal_id="2", p_open=20000, p_close=22000, rate=0.5,
            t_open="2025-02-15", t_close="2025-03-15", n_profit=0,
        ),
    ]
    client = FakeClient(deals)
    result = run_autofin_backtest(
        initial_nav=1_000_000_000, fee_pct_per_side=0, slippage_pct_per_fill=0, client=client,
    )
    assert result["trade_count"] == 2
    # Deal A: 500M target / 10000 = 50000 shares → 500 lots = 50000 shares
    # pnl = 50000 * (12000 - 10000) = 100M; nav = 1.1B
    # Deal B: 550M target / 20000 = 27500 shares
    # pnl = 27500 * (22000 - 20000) = 55M; nav = 1.155B
    assert abs(result["final_nav"] - 1_155_000_000) < 1


def test_backtest_with_slippage_reduces_returns():
    """Slippage shrinks both entry cost and exit proceeds."""
    deals = [_make_closed_deal()]
    no_slip = run_autofin_backtest(
        initial_nav=1_000_000_000, slippage_pct_per_fill=0, client=FakeClient(deals),
    )
    with_slip = run_autofin_backtest(
        initial_nav=1_000_000_000, slippage_pct_per_fill=0.01, client=FakeClient(deals),
    )
    assert with_slip["final_nav"] < no_slip["final_nav"]


def test_backtest_with_fees_reduces_returns():
    """Per-side fees reduce both entry and exit."""
    deals = [_make_closed_deal()]
    no_fee = run_autofin_backtest(
        initial_nav=1_000_000_000, fee_pct_per_side=0, client=FakeClient(deals),
    )
    with_fee = run_autofin_backtest(
        initial_nav=1_000_000_000, fee_pct_per_side=0.01, client=FakeClient(deals),
    )
    assert with_fee["final_nav"] < no_fee["final_nav"]


def test_backtest_sorts_by_open_time():
    """Deals are replayed in chronological order even if API returns them shuffled."""
    deals = [
        _make_closed_deal(symbol="LATER", deal_id="2", p_open=10000, p_close=11000, t_open="2025-06-01", t_close="2025-07-01"),
        _make_closed_deal(symbol="EARLY", deal_id="1", p_open=10000, p_close=12000, t_open="2025-01-01", t_close="2025-02-01"),
    ]
    client = FakeClient(deals)
    result = run_autofin_backtest(
        initial_nav=1_000_000_000, fee_pct_per_side=0, slippage_pct_per_fill=0, client=client,
    )
    assert result["trades"][0].symbol == "EARLY"
    assert result["trades"][1].symbol == "LATER"


def test_backtest_writes_outputs(tmp_path: Path):
    """JSON + CSV outputs are written and loadable."""
    deals = [_make_closed_deal(), _make_closed_deal(symbol="X", deal_id="2", p_open=10000, p_close=11000)]
    client = FakeClient(deals)
    result = run_autofin_backtest(
        initial_nav=1_000_000_000, fee_pct_per_side=0, slippage_pct_per_fill=0, client=client,
    )
    json_path, csv_path = write_backtest_outputs(result, tmp_path, prefix="test")
    assert json_path.exists()
    assert csv_path.exists()
    csv_text = csv_path.read_text()
    lines = csv_text.strip().split("\n")
    assert len(lines) == 3  # header + 2 trades


def test_backtest_report_contains_key_metrics():
    """The formatted report must show bot identity, strict-mode label, and core metrics."""
    deals = [_make_closed_deal()]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    report = mod.format_backtest_report(result)
    assert "autofin_signal BACKTEST" in report
    assert "strict, no look-ahead" in report
    assert "trades:" in report
    assert "win rate:" in report
    assert "max drawdown:" in report
    assert "Top 5 winners" in report
    assert "time-weighted" in report  # new Sharpe label


def test_backtest_default_costs_are_realistic():
    """Default fee and slippage must be > 0 (not the placeholder 0)."""
    deals = [_make_closed_deal()]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    assert result["fee_pct_per_side"] > 0
    assert result["slippage_pct_per_fill"] > 0
    assert result["total_fees"] > 0
    assert result["total_slippage"] > 0


def test_hindsight_variants_filters_by_min_profit_steps():
    """run_hindsight_variants applies hindsight filters and labels them clearly."""
    deals = [
        _make_closed_deal(symbol="LOSER", deal_id="1", p_open=10000, p_close=8000, n_profit=0, rate=0.15),
        _make_closed_deal(symbol="WINNER", deal_id="2", p_open=10000, p_close=13000, n_profit=3, rate=0.15),
    ]
    client = FakeClient(deals)
    hv = run_hindsight_variants(initial_nav=1_000_000_000, client=client)
    assert hv["mode"] == "hindsight_variants"
    assert "FUTURE information" in hv["warning"] or "DO NOT USE" in hv["warning"]
    # min_profit_steps>=1 should keep only the WINNER (n_profit >= 1)
    assert hv["variants"]["min_profit_steps>=1"]["trades"] == 1
    # min_profit_steps>=3 keeps only the WINNER (whose n_profit = 3)
    assert hv["variants"]["min_profit_steps>=3"]["trades"] == 1


def test_hindsight_variants_stop_loss_filters_close_below_sl():
    """stop_loss_pct=X% keeps only deals whose final close > entry*(1-X)."""
    deals = [
        _make_closed_deal(symbol="BIG_LOSER", deal_id="1", p_open=10000, p_close=7000, rate=0.15),
        _make_closed_deal(symbol="MILD_LOSER", deal_id="2", p_open=10000, p_close=9600, rate=0.15),
        _make_closed_deal(symbol="WINNER", deal_id="3", p_open=10000, p_close=13000, rate=0.15),
    ]
    client = FakeClient(deals)
    hv = run_hindsight_variants(initial_nav=1_000_000_000, client=client)
    # stop_loss_only>=5% keeps trades that closed ABOVE 9500 → MILD_LOSER + WINNER
    m = hv["variants"]["stop_loss_only>=5%"]
    assert m["trades"] == 2
    # stop_loss_only>=15% keeps trades that closed ABOVE 8500 → still MILD_LOSER + WINNER
    # (BIG_LOSER at 7000 is dropped)
    m15 = hv["variants"]["stop_loss_only>=15%"]
    assert m15["trades"] == 2
    # stop_loss_only>=15% keeps trades that closed ABOVE 8500 → MILD_LOSER + WINNER
    # (BIG_LOSER at 7000 is dropped)
    m15 = hv["variants"]["stop_loss_only>=15%"]
    assert m15["trades"] == 2


def test_strict_vs_hindsight_differs():
    """Strict mode (no filter) and hindsight mode (filter) MUST give different
    numbers when the dataset has losers. This guards against the bug where
    the strict backtest accidentally inherits a hindsight filter."""
    deals = [
        _make_closed_deal(symbol="LOSER", deal_id="1", p_open=10000, p_close=7000, n_profit=0, rate=0.30),
        _make_closed_deal(symbol="WINNER", deal_id="2", p_open=10000, p_close=13000, n_profit=3, rate=0.30),
    ]
    client = FakeClient(deals)
    strict = run_autofin_backtest(initial_nav=1_000_000_000, client=FakeClient(deals))
    hv = run_hindsight_variants(initial_nav=1_000_000_000, client=FakeClient(deals))
    # Strict includes the LOSER
    assert strict["trade_count"] == 2
    # Hindsight with min_profit_steps>=1 skips the LOSER
    assert hv["variants"]["min_profit_steps>=1"]["trades"] == 1
    # Therefore the strict final NAV must be WORSE than the hindsight one
    assert strict["final_nav"] < hv["variants"]["min_profit_steps>=1"]["final_nav"]


def test_backtest_time_weighted_sharpe_present():
    """Sharpe is now time-weighted; verify the field exists and is numeric."""
    deals = [_make_closed_deal(), _make_closed_deal(symbol="X", deal_id="2", p_open=10000, p_close=12000)]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    assert "sharpe_time_weighted" in result


def test_default_initial_nav_matches_dry_run_state():
    """The backtest default ``initial_nav`` must match
    ``libs/dry_run_state.DEFAULT_INITIAL_NAV`` (100M VND). Otherwise
    every signal sizes as if the operator had 1B to invest and the
    backtest result is disconnected from the dry-run reality.

    2026-09-19 user feedback: "Without portfolio management, every
    signal sizes as if it has the full 1B VND NAV." The pre-fix
    default was 1B; this test guards against it regressing.
    """
    from vietnam_algo.libs.dry_run_state import DEFAULT_INITIAL_NAV as DRY_DEFAULT
    from vietnam_algo.libs.backtest_autofin import DEFAULT_INITIAL_NAV as BT_DEFAULT
    assert BT_DEFAULT == DRY_DEFAULT, (
        f"backtest default {BT_DEFAULT} != dry-run default {DRY_DEFAULT}"
    )
    # And the value should be reasonable — not the legacy 1B scale.
    assert BT_DEFAULT <= 100_000_000, (
        f"backtest default {BT_DEFAULT} > 100M — too large for the "
        f"operator's dry-run NAV scale. Pass --initial-nav explicitly "
        f"if you want a larger scale."
    )
    # And the live backtest actually produces a numeric Sharpe when
    # the test deals are processed at the new default.
    deals = [_make_closed_deal(), _make_closed_deal(symbol="X", deal_id="2", p_open=10000, p_close=12000)]
    client = FakeClient(deals)
    result = run_autofin_backtest(client=client)  # default initial_nav
    assert isinstance(result["sharpe_time_weighted"], (int, float))
