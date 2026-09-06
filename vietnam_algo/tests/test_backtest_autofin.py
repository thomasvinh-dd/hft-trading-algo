"""Tests for the autofin backtest (closing-signals replay)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

import vietnam_algo.backtest_autofin as mod
from vietnam_algo.backtest_autofin import (
    AutofinTrade,
    _round_to_lots,
    run_autofin_backtest,
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


def test_round_to_lots_basic():
    assert _round_to_lots(150) == 100
    assert _round_to_lots(199) == 100
    assert _round_to_lots(200) == 200
    assert _round_to_lots(250) == 200
    assert _round_to_lots(0) == 0


class FakeClient:
    def __init__(self, deals: list[dict]):
        self._deals = deals
    def fetch_closing(self, page_size: int = 50, page_index: int = 1):
        start = (page_index - 1) * page_size
        return self._deals[start:start + page_size]


def test_backtest_basic_profitable_deal():
    """L40: BUY 12200 → SELL 35200 with 15% NAV → big profit."""
    deals = [_make_closed_deal()]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    assert result["trade_count"] == 1
    assert result["win_count"] == 1
    assert result["loss_count"] == 0
    assert result["win_rate_pct"] == 1.0
    # 1B * 0.15 = 150M, / 12200 = 12295 shares, rounded to 122 lots = 12200 shares
    # cost = 12200 * 12200 = 148,840,000 VND
    # proceeds = 12200 * 35200 = 429,440,000 VND
    # pnl = 280,600,000 VND
    trade = result["trades"][0]
    assert trade.symbol == "L40"
    assert trade.quantity == 12200
    assert abs(trade.pnl_vnd - 280_600_000) < 1
    assert result["final_nav"] > result["initial_nav"]


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
        _make_closed_deal(p_open=0, p_close=10000),  # missing open
        _make_closed_deal(p_open=10000, p_close=0),  # missing close
        _make_closed_deal(rate=0),  # missing rate
    ]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    assert result["trade_count"] == 0
    assert result["skipped_count"] == 3


def test_backtest_compounds_nav_across_deals():
    """NAV evolves chronologically — second deal sized off post-deal-1 NAV."""
    deals = [
        _make_closed_deal(
            symbol="A", deal_id="1", p_open=10000, p_close=12000, rate=0.5,
            t_open="2025-01-01", t_close="2025-02-01",
        ),
        _make_closed_deal(
            symbol="B", deal_id="2", p_open=20000, p_close=22000, rate=0.5,
            t_open="2025-02-15", t_close="2025-03-15",
        ),
    ]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    assert result["trade_count"] == 2
    # First deal: 500M target / 10000 = 50000 shares → 500 lots = 50000 shares
    # pnl = 50000 * 2000 = 100M; nav = 1.1B
    # Second deal: 550M target / 20000 = 27500 shares → 27500 shares
    # pnl = 27500 * 2000 = 55M; nav = 1.155B
    assert abs(result["final_nav"] - 1_155_000_000) < 1


def test_backtest_with_slippage_reduces_returns():
    """Slippage shrinks both entry cost (positive) and exit proceeds (negative)."""
    deals = [_make_closed_deal()]
    client = FakeClient(deals)
    no_slip = run_autofin_backtest(initial_nav=1_000_000_000, slippage_pct=0, client=FakeClient(deals))
    with_slip = run_autofin_backtest(initial_nav=1_000_000_000, slippage_pct=0.01, client=FakeClient(deals))
    assert with_slip["final_nav"] < no_slip["final_nav"]


def test_backtest_with_fees_reduces_returns():
    """Per-share fees reduce both entry and exit."""
    deals = [_make_closed_deal()]
    client = FakeClient(deals)
    no_fee = run_autofin_backtest(initial_nav=1_000_000_000, fee_per_share=0, client=FakeClient(deals))
    with_fee = run_autofin_backtest(initial_nav=1_000_000_000, fee_per_share=100, client=FakeClient(deals))
    assert with_fee["final_nav"] < no_fee["final_nav"]


def test_backtest_sorts_by_open_time():
    """Deals are replayed in chronological order even if API returns them shuffled."""
    deals = [
        _make_closed_deal(symbol="LATER", deal_id="2", p_open=10000, p_close=11000, t_open="2025-06-01", t_close="2025-07-01"),
        _make_closed_deal(symbol="EARLY", deal_id="1", p_open=10000, p_close=12000, t_open="2025-01-01", t_close="2025-02-01"),
    ]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    assert result["trades"][0].symbol == "EARLY"
    assert result["trades"][1].symbol == "LATER"


def test_backtest_writes_outputs(tmp_path: Path):
    """JSON + CSV outputs are written and loadable."""
    deals = [_make_closed_deal(), _make_closed_deal(symbol="X", deal_id="2", p_open=10000, p_close=11000)]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    json_path, csv_path = write_backtest_outputs(result, tmp_path, prefix="test")
    assert json_path.exists()
    assert csv_path.exists()
    # CSV header + 2 rows
    csv_text = csv_path.read_text()
    lines = csv_text.strip().split("\n")
    assert len(lines) == 3  # header + 2 trades


def test_backtest_report_contains_key_metrics():
    """The formatted report must show bot identity and core metrics."""
    deals = [_make_closed_deal()]
    client = FakeClient(deals)
    result = run_autofin_backtest(initial_nav=1_000_000_000, client=client)
    report = mod.format_backtest_report(result)
    assert "autofin_signal BACKTEST" in report
    assert "trades:" in report
    assert "win rate:" in report
    assert "max drawdown:" in report
    assert "Top 5 winners" in report
