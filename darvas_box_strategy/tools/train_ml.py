"""Train an ML entry filter on historical signals.

Workflow:
1. Run base strategy on TRAIN period (2019-2022), collect all signals
2. For each signal, extract features + label (win = realized_pnl > 0)
3. Train gradient boosting classifier
4. Save model to disk

The trained model can be loaded via BreakoutConfig.ml_model_path.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path("/root/hft-trading-algo")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "darvas_box_strategy"))

import json
import math
import pickle
from datetime import date
from collections import Counter

from darvas import (
    StrategyConfig, BaseConfig, BreakoutConfig, BacktestConfig, TrailingConfig,
    UniverseConfig, ExitsConfig,
    run_backtest, load_price_bars,
)
from darvas.data_loader import derive_meta, passes_universe
from darvas.signal_generator import _extract_features
from darvas.base_detector import BaseScanner

PRICES_DIR = PROJECT_ROOT / "vietnam_algo" / "data" / "prices"
TRAIN_END = date(2022, 12, 31)


def slice_bars(bars, start_date=None, end_date=None):
    out = []
    for b in bars:
        if start_date and b.date < start_date: continue
        if end_date and b.date > end_date: continue
        out.append(b)
    return out


def build_universe():
    base_cfg = BaseConfig(min_days=5, max_days=30, max_range_pct=0.030)
    brk_cfg = BreakoutConfig(require_ma200=True, min_history_days=200)
    universe_cfg = UniverseConfig(min_avg_value_20d=1.0e9, min_price=5_000.0, max_price=200_000.0)
    temp_cfg = StrategyConfig(universe=universe_cfg, breakout=brk_cfg, base=base_cfg)
    tickers = []
    for path in sorted(PRICES_DIR.glob("*.json")):
        if path.name == "E1VFVN30.json": continue
        ticker = path.stem.upper()
        bars = load_price_bars(path)
        if len(bars) < 200: continue
        meta = derive_meta(ticker, bars)
        if passes_universe(meta, temp_cfg): tickers.append(ticker)
    return tickers


def collect_features_and_labels(tickers, cfg):
    """For each signal in TRAIN period, extract features and label."""
    X, y = [], []
    scanner_count = 0
    for ticker in tickers:
        path = PRICES_DIR / f"{ticker}.json"
        bars = load_price_bars(path)
        if not bars or len(bars) < cfg.breakout.min_history_days:
            continue
        # Slice to train period
        bars_train = slice_bars(bars, None, TRAIN_END)
        if len(bars_train) < cfg.breakout.min_history_days:
            continue
        scanner = BaseScanner(bars_train, cfg.base, ticker=ticker)
        nav = cfg.backtest.start_nav
        from darvas.signal_generator import generate_signal
        from darvas.executor import simulate_trade
        for i in range(cfg.breakout.min_history_days, len(bars_train)):
            base = scanner.find_base(as_of_idx=i)
            if base is None or base.end_idx >= i:
                continue
            signal = generate_signal(bars_train, base, nav=nav, cfg=cfg, as_of_idx=i)
            if signal is None:
                continue
            scanner_count += 1
            # Extract features at signal time
            features = _extract_features(bars_train, i, base, bars_train[i])
            if features is None:
                continue
            # Simulate trade to get label
            trade = simulate_trade(signal, bars_train[i + 1:], cfg,
                                   atr_value=signal.atr_value)
            label = 1 if trade.realized_pnl > 0 else 0
            X.append(features)
            y.append(label)
    print(f"  Scanned {scanner_count} signals", flush=True)
    return X, y


def main():
    print("Building universe...", flush=True)
    tickers = build_universe()
    print(f"Universe: {len(tickers)}", flush=True)

    # Use BASELINE config (without ML filter, since we're training the filter)
    cfg = StrategyConfig(
        base=BaseConfig(min_days=5, max_days=30, max_range_pct=0.030),
        breakout=BreakoutConfig(
            volume_mult=2.0, high_conviction_mult=3.0,
            require_ma200=True, min_history_days=200,
            require_close_above_ma=20,
            # ML fields off
            ml_model_path="", ml_min_prob=0.5,
        ),
        backtest=BacktestConfig(
            start_nav=1_000_000_000.0,
            max_concurrent_positions=10,
            cooldown_bars_after_stop=5,
            cost_bps=25.0,
            size_low=0.15, size_high=0.30,
        ),
        trailing=TrailingConfig(trail_mode="pct", trail_from_peak_pct=0.07),
    )

    print("Collecting features and labels from TRAIN period...", flush=True)
    X, y = collect_features_and_labels(tickers, cfg)
    print(f"  Got {len(X)} samples, label balance: {Counter(y)}", flush=True)

    if len(X) < 100:
        print("Too few samples; aborting.", flush=True)
        return

    print("Training GradientBoostingClassifier...", flush=True)
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import cross_val_score
    import numpy as np

    X_arr = np.array(X)
    y_arr = np.array(y)

    model = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.05,
        subsample=0.8, random_state=42,
    )
    # Cross-val on TRAIN
    scores = cross_val_score(model, X_arr, y_arr, cv=5, scoring="accuracy")
    print(f"  CV accuracy: {scores.mean():.3f} +/- {scores.std():.3f}", flush=True)

    # Train on all
    model.fit(X_arr, y_arr)
    train_acc = model.score(X_arr, y_arr)
    print(f"  Train accuracy: {train_acc:.3f}", flush=True)

    # Feature importance
    feat_names = ["mom_20", "mom_60", "atr_pct", "vol_ratio", "rng_pct",
                  "dist_ma20", "days_in_base", "above_ma50", "above_ma200"]
    print("  Feature importance:")
    for name, imp in sorted(zip(feat_names, model.feature_importances_),
                              key=lambda x: -x[1]):
        print(f"    {name:>14}: {imp:.3f}")

    # Save
    out_path = Path("/root/hft-trading-algo/darvas_box_strategy/tools/ml_model.pkl")
    import joblib
    joblib.dump(model, out_path)
    print(f"\nModel saved to {out_path}", flush=True)


if __name__ == "__main__":
    main()
