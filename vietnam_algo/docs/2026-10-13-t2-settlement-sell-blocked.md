# T+2 settlement blocks ABB SELL order (2026-10-13)

## What happened

After the 13:11:23 UTC BUY 100 ABB @ 17,700 FILLED
(order 133161), the manual attempt to place a SELL SL at
15,930 (10% below entry) failed:

```
DNSE POST /accounts/0003937868/orders -> HTTP 400
{"status":400,"code":"TRADE_QUANTITY_NOT_ENOUGH","message":"Trade quantity not enough"}
```

The error fires for any qty (1, 50, 100) — so it's not a
quantity issue, it's a settlement issue.

## Root cause: Vietnam T+2 settlement

Vietnamese exchanges (HOSE, HNX, UPCOM) settle trades on
**T+2** (2 trading days after trade day). On T+0 (today,
the trade day), the position shows as OPEN in DNSE but
``tradeQuantity`` is 0 (the trade is pending settlement).
The ``openQuantity`` of 100 is the *promised* size after
settlement, not the tradeable size.

| Day | Date | Can we SELL the ABB bought on 10-13? |
|---|---|---|
| T+0 | 2026-10-13 Tue | No (just bought, pending settlement) |
| T+1 | 2026-10-14 Wed | No (1 day into settlement) |
| T+2 | 2026-10-15 Thu | **Yes** (settlement complete) |
| T+2 (if holiday) | 2026-10-16 Fri | (would shift if 10-15 is a holiday) |

So the SL SELL attempt will keep failing until 2026-10-15.

## Bot impact

The autofin bot's strategy only plans the SL at the
*initial BUY* (in the same cycle). The BUY/SL are placed
together; the BUY is allocated immediately, the SL is
queued. If the SL is rejected (e.g. because the BUY hadn't
filled by the time the SL is sent, which was the 2026-10-12
ABB case), the strategy doesn't retry it on the next cycle
because the BUY is then "current_qty > 0" → skipped from
the new-entry path, and the SL is treated as a
"partial-close" path (which only fires for TP-step signals
from autofin, not user-defined SLs).

So the position is currently **un-hedged**: 100 ABB @ 17,700
without a stop-loss. The autofin service will publish TP
steps as ABB rises, and the bot will plan those SELLs once
the position is settled (T+2).

## Mitigation: wait for T+2 + manually re-plan SL

The simplest fix is to wait until 2026-10-15 (Thursday) ICT
and then either:

1. Let the bot's order monitor re-attempt the SL (if we
   add a "retry-SL for held positions" path to the order
   monitor). Currently the bot doesn't have this path, so
   the SL will silently keep failing.

2. Manually place a SELL SL via the DNSE client:
```python
from vietnam_algo.libs.dnse.client import DnseClient
c = DnseClient(api_key=..., api_secret=...)
c.place_stock_order(
    account_no="0003937868",
    trading_token=tok,
    symbol="ABB",
    side="NS",
    quantity=100,
    price=15930,  # 10% below entry
    loan_package_id=1036,  # cash
    order_type="LO",
)
```

3. Or wait for autofin to publish a TP step and the bot
   will plan a SELL automatically.

## Open work item: persistent SL across re-attempts

The cleanest fix is to make the strategy persistent about
its SL — once a BUY fills, the SL is tracked in the
strategy state and re-attempted on every cycle until it
succeeds. This would handle the T+2 case (SL is rejected
today, re-attempted tomorrow, succeeds on Thursday).

Implementation sketch:
- After a BUY FILLS, store the SL plan in
  ``self._our_deals[did]["_pending_sl"] = {price, qty, side}``
- On every cycle, check the pending SL; if it's missing
  from the order ledger, re-attempt to place it
- Once placed successfully, remove from pending_sl

This is tracked as a follow-up; the current 100 ABB is
un-hedged until T+2.
