# Order-status polling and update logic

The auto-trading bot polls DNSE every 60 s for status updates on
every non-terminal order in the ledger. On any state change it
sends a Telegram notification so the operator can react without
opening the DNSE web UI.

## Flow

```
+---------------------+     +-------------------------+
| run_auto_bot loop   |     |  DNSE /accounts/.../    |
| (every 60 s)        |     |  orders/{orderId}       |
+---------------------+     +-------------------------+
        |                              ^
        v                              |
+---------------------+                |
| _monitor_order_     |  GET order_detail()  |
| statuses()          |  with trading-token  |
|  in runner.py:1064  +----------------->---+
+---------------------+
        |
        | normalize_ledger_status(order_status_from_detail(detail))
        v
+---------------------+
| compare new status  |
| to previous in      |
| ledger              |
+---------------------+
        |
        v
+---------------------+
| if changed: notify  |  <-- Telegram:
| (Telegram) + write  |      "📋 Vietnam Algo: order
| back to ledger      |       ABB 335881 → EXPIRED"
+---------------------+
```

## Components

### 1. ``_monitor_order_statuses`` (vietnam_algo/bots/runner.py:1064)

Called at the top of every loop iteration in ``run_auto_bot``
(line 1269). For each non-terminal order in the ledger:

- Skips orders with empty ``dnseOrderId`` (not yet placed)
- Skips orders already in a terminal state (``filled``,
  ``cancelled``, ``rejected``, ``expired``, ``closed``,
  ``dry_run``, ``skipped``)
- Throttles per-order (default 60 s) to avoid hammering DNSE
- Calls ``client.order_detail(account_no, order_id, trading_token=...)``
- Compares new status to previous status
- If changed, sends Telegram via ``_format_status_change_message``
- Updates ledger with new status + lastDetail

The throttle is in-memory only — bot restart resets it, which
is the right behaviour: after a restart we want to re-poll
every order because status may have changed while we were down.

### 2. ``order_status_from_detail`` (vietnam_algo/libs/order_ledger.py:117)

Extracts the order's status string from a DNSE order-detail
payload. Handles multiple field-name conventions
(``status``, ``orderStatus``, ``state``) and nested responses
(``order.orderStatus``, ``data[].orderStatus``).

Converts the DNSE wire format (camelCase) to UPPER_SNAKE:

| DNSE wire | Internal |
|---|---|
| ``partiallyFilled`` | ``PARTIALLY_FILLED`` |
| ``pendingNew`` | ``PENDING_NEW`` |
| ``pendingCancel`` | ``PENDING_CANCEL`` |
| ``cancelledByRightsEvent`` | ``CANCELLED_BY_RIGHTS_EVENT`` |

### 3. ``normalize_ledger_status`` (vietnam_algo/libs/order_ledger.py:160)

Maps the UPPER_SNAKE status to a small set of internal states
that the ledger and the monitor care about:

| Internal | UPPER_SNAKE inputs | Terminal? |
|---|---|---|
| ``filled`` | ``FILLED``, ``MATCHED`` | yes |
| ``partial`` | ``PARTIALLY_FILLED``, ``PARTIAL_FILLED``, ``PARTIALLY_MATCHED`` | no |
| ``cancelled`` | ``CANCELLED``, ``CANCELED``, ``CANCELLED_BY_RIGHTS_EVENT`` | yes |
| ``rejected`` | ``REJECTED`` | yes |
| ``expired`` | ``EXPIRED`` | yes |
| ``closed`` | ``CLOSED``, ``DONE_FOR_DAY`` | yes |
| ``pending_cancel`` | ``PENDING_CANCEL`` | no (in-flight) |
| ``pending_replace`` | ``PENDING_REPLACE`` | no (in-flight) |
| ``open`` | ``NEW``, ``OPEN``, ``PENDING``, ``PENDING_NEW``, ``WAITING``, ``QUEUED`` | no |
| ``unknown`` | empty / unparseable | no |

Terminal statuses are skipped by the monitor — once an order is
``filled`` / ``cancelled`` / ``rejected`` / ``expired`` /
``closed`` we never poll it again. The Telegram notification
fires on the transition INTO a terminal state.

The 2026-10-12 fix added ``pending_cancel`` and
``pending_replace`` as distinct non-terminal states (previously
both mapped to ``"unknown"`` which was confusing — operators
couldn't tell "cancel in flight" from "the monitor failed to
parse the response").

### 4. ``_format_status_change_message`` (vietnam_algo/bots/runner.py:1018)

Builds the Telegram message for a state change. Includes:

- Header icon (✅ filled, 🚫 cancelled, ❌ rejected, ⏰ expired)
- Side label (``BUY`` / ``SELL``) + symbol
- Previous → current transition
- Key order fields (qty, price, dnseOrderId, sourceOrderId)
- If rejected/cancelled: ``rejectReason`` / ``cancelReason``
  from the detail payload
- If filled: ``filledQuantity`` + ``averagePrice``

The full message looks like:

```
✅ Vietnam Algo: order ABB 335881 → FILLED
  symbol: ABB
  side: BUY
  qty: 100
  price: 17,500
  averagePrice: 17,500
  filledQuantity: 100
  sourceOrderId: 64535428285614
  runId: 2026-10-12
  reason: autofin_buy:deal#1789344000:ABB:nav30pct:Khuyến nghị đầu tư
```

## Verified end-to-end

The 2026-10-12 ABB live-submit saga tested the full pipeline:

| Time (ICT) | Order state | Telegram? | Ledger |
|---|---|---|---|
| 14:48:08 | placed → ``New`` → ``open`` | summary from force-run | status=open, dnseOrderId=335881 |
| 14:48:52 | monitor poll: still ``New`` → ``open`` | no (no change) | unchanged |
| 15:05:00 | DNSE expires the order at EOD: ``Expired`` | yes (next monitor cycle) | status=expired, canceledQuantity=100 |
| 15:06:56 | monitor poll: still ``Expired`` → ``expired`` | no (terminal, skipped) | unchanged |
| 15:30+ | monitor poll: still ``expired`` | no (terminal, skipped) | unchanged |

The monitor's behaviour is: poll every non-terminal order every
60 s; fire a Telegram on every state change; never poll a
terminal order again. This matches the design and is verified by
44 unit tests in ``vietnam_algo/tests/test_order_status_normalizer.py``.

## Tuning

- ``ORDER_MONITOR_THROTTLE_SECONDS`` (env, default 60) — per-order
  poll throttle. Lower = more responsive but more DNSE calls.
- The bot's ``--sleep-seconds`` (CLI, default 60) — main loop
  frequency. The monitor piggy-backs on this so it's the upper
  bound on how often we check.

## Why no Telegram on first place?

The submit path (``execute_orders_with_ledger`` in
``vietnam_algo/libs/daily_engine.py:481``) does NOT send a
Telegram on successful submit — it just writes the new status
to the ledger. Telegram notifications come from:

1. The ``force_run_summary_message`` at the end of
   ``force-run-trading`` (one Telegram per invocation,
   includes ``dnseOrderIds``) — covers ad-hoc bot re-runs.
2. The ``submit_message`` at the end of the legacy 09:16
   submit cycle — covers the 09:16 daily submit.
3. The order monitor on every state CHANGE — covers fills,
   cancels, rejects, expirations.

This split is intentional: the submit path prints to the ledger
only (the strategy is the source of truth), and the monitor
fires on real exchange-side events. If you want a "just placed"
notification for every order, that's a separate change to
``execute_orders_with_ledger`` (open work item, not done).

## Tests

- ``vietnam_algo/tests/test_order_status_normalizer.py`` (44 tests):
  status mapping, terminal-state classification, end-to-end
  transition scenarios, priority order for status-field lookup
- ``vietnam_algo/tests/test_order_status_monitor.py`` (19 tests):
  monitor flow (no orders / status change / no change /
  terminal skipped / without dnseOrderId / throttled /
  zero-throttle / API error / attribute error / multiple
  orders / partial fill / no client / no token / token
  loaded from shared file / missing shared token /
  persists ledger to disk)
