"""Debug the test_momentum_apply_walks_full_backtest_csv failure."""
import csv as _csv
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/home/vst/Documents/Ai-trade/hft-trading-algo")

from vietnam_algo.bots.runner import _apply_dry_run_fills
from vietnam_algo.libs.config import StrategyConfig
from vietnam_algo.libs.daily_engine import EngineConfig
from vietnam_algo.libs.dry_run_state import load_dry_run_state, _state_path

with tempfile.TemporaryDirectory() as tmp_dir:
    tmp = Path(tmp_dir)
    (tmp / "live").mkdir(parents=True, exist_ok=True)
    (tmp / "backtests").mkdir(parents=True, exist_ok=True)

    engine_config = EngineConfig(
        dataDir=tmp, strategy="momentum",
        updateSymbols=False, updatePrices=False,
    )
    strategy_config = StrategyConfig()

    orders_csv = tmp / "backtests" / "liquid_momentum_rotation_orders.csv"
    with orders_csv.open("w", encoding="utf-8", newline="") as f:
        writer = _csv.DictWriter(
            f,
            fieldnames=["orderId", "date", "symbol", "side", "price",
                        "quantity", "value", "fee", "reason"],
        )
        writer.writeheader()
        writer.writerow({
            "orderId": "1", "date": "2026-09-08", "symbol": "PVT",
            "side": "BUY", "price": "21722", "quantity": "2900",
            "value": "62992930", "fee": "94489",
            "reason": "top_rank",
        })
        writer.writerow({
            "orderId": "2", "date": "2026-09-09", "symbol": "PVT",
            "side": "SELL", "price": "21850", "quantity": "2900",
            "value": "63365000", "fee": "95047",
            "reason": "rank_exit",
        })

    live_dir = tmp / "live"
    (live_dir / "latest_orders.json").write_text(json.dumps({
        "tradeDate": "2026-09-10", "items": []
    }))
    result = {
        "latestOrdersJson": str(live_dir / "latest_orders.json"),
        "latestOrdersCsv": str(orders_csv),
        "tradeDate": "2026-09-10",
        "count": 0,
    }
    print(f"orders CSV: {orders_csv}")
    print(f"latest_orders.json items: {json.loads((live_dir / 'latest_orders.json').read_text())['items']}")
    print(f"orders.csv exists: {orders_csv.exists()}")
    _apply_dry_run_fills(
        result, engine_config, "0003937868", strategy_config,
        strategy_name="momentum",
    )
    state = load_dry_run_state(tmp, strategy_name="momentum")
    print(f"\n=== Final state ===")
    print(f"cash: {state.cash}")
    print(f"positions: {state.positions}")
    print(f"realized_pnl: {state.realized_pnl}")
    print(f"closed_trades: {state.closed_trades}")
    state_path = _state_path(tmp, "momentum")
    print(f"\nstate file: {state_path} exists={state_path.exists()}")
    if state_path.exists():
        print(state_path.read_text())
