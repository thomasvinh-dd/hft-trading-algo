# autofin_signal: risk-adjusted ranking + active portfolio management

Per user feedback (after running the strategy in dry-run):

> "the method choose the signal that make total NAV near 100 % is not correct,
>  we need to priority the signal that potentially make higher profit with
>  lower risk"

> "how we are managing our portfolio? it's only signal so will now
>  manage the portfolio for us"

Two changes:
1. **Risk-adjusted signal ranking** (replaces recency-based)
2. **Active portfolio management** (TP/SL brackets, position closing)

## 1. Risk-adjusted ranking

Before: rank by `(-lastIndex, -rateForNav)` — newest first, then largest allocation.
That's a poor proxy for "best opportunity".

After: rank by `(-risk_reward_ratio, -rating, -win_rate)` — highest risk/reward first.

```python
def _score(s: AutofinSignal) -> tuple[float, float, float]:
    entry  = (s.supply_from + s.supply_to) / 2
    target = (s.tp_from + s.tp_to) / 2
    stop   = max(s.sl_from or 0, s.sl_to or 0)
    rating  = float(s.raw.get("rS_Rating_IDB") or 0)
    winrate = float(s.raw.get("rateWinByPrice") or 0)
    if entry <= 0 or target <= 0 or stop <= 0 or entry <= stop:
        rr = 0.0
    else:
        rr = (target - entry) / (entry - stop)  # higher is better
    return (-rr, -rating, -winrate)

# greedily pick top signals by this score until cumulative rateForNav > 100 %
```

`risk_reward_ratio = (target - entry) / (entry - stop)` measures upside vs downside.
- High RR: target far above entry, stop close to entry.
- Low RR: target barely above entry, stop far below.

A signal with 30 % rateForNav and RR=3 is selected over a signal with 30 % rateForNav and RR=0.5.

## 2. Active portfolio management

The signal bot now actively manages our DNSE portfolio, not just mirrors it.

### Decision tree per symbol

```
selected ∩ autofin_open   → BUY/SELL delta vs current holding
selected \ autofin_open   → BUY new position
autofin_open \ selected   → SELL (close position, autofin no longer holds)
not in either             → ignore
```

### TP/SL brackets on every new entry

For every BUY (entry), we now also queue a paired SELL at:
- `tp_from` zone low (take profit)
- `sl_from` zone low (stop loss)

This means the position auto-exits when price moves favorably or unfavorably. We don't need to monitor positions manually.

```python
if side == "NB" and delta > 0:
    rows.append({"side": "NB", "price": supply_to, "quantity": delta,
                 "reason": "autofin_mirror:nav15pct:..."})
    if tp_from > 0:
        rows.append({"side": "NS", "price": tp_from, "quantity": delta,
                     "reason": "autofin_take_profit:12000"})
    if sl_from > 0:
        rows.append({"side": "NS", "price": sl_from, "quantity": delta,
                     "reason": "autofin_stop_loss:9400"})
```

## Example output (run against real autofin API)

```
signals fetched: 20
autofin open symbols: ADS, AIG, APH, BAF, CDC, DHC, DRI, DXP, GMD,
                    HDB, HHP, KDC, KLB, PVP, SAB, SVN, TTA, VEA, VHM, VNM

selected (by risk-adjusted return, NAV-budgeted):
  BAF   rateForNav=30%  (newest, top RR)
  APH   rateForNav=15%  (high rating)
  VHM   rateForNav=30%  (highest absolute risk-adjusted)
  DHC   rateForNav=15%
  total: 90% of NAV

excluded (over budget):
  SVN, HHP, VEA, DRI, CDC, ADS, KLB, PVP, DXP, TTA, VNM, GMD,
  AIG, HDB, SAB, KDC  (16 symbols × 15-30% each = ~345% extra)

orders planned: 0  (NAV=0; no NAV = no orders)
```

Once the DNSE account is funded, the strategy will produce `1 BUY + 1 SELL(TP) + 1 SELL(SL)` per selected signal, plus `1 SELL` per closed position (autofin exited but we still hold).

## Files

| File | Change |
|---|---|
| `vietnam_algo/strategies_impl/autofin_signal.py` | _select_signals_within_nav_budget now ranks by RR/rating/winrate; _compute_order_plan emits BUY + TP + SL brackets + close positions |
| `vietnam_algo/tests/test_autofin_signal_logic.py` | +5 tests: risk_reward priority, rating priority, TP/SL brackets, no_op_when_match, subthreshold skip, close-on-autofin-exit |

## Tests

- 192/192 green (was 188; +4 new tests)
- 4 previously-passing tests updated to reflect new 3-order-per-BUY shape (BUY + TP + SL)
- 1 test fixed (autofin_open \ selected test was checking both APH and OTHER closure; APH is in selected so doesn't close)

## Commit

`8e5d7d2 Upg autofin_signal: risk-adjusted ranking + TP/SL brackets + active portfolio mgmt`
