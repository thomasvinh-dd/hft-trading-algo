# live submit: 2026-10-12 ABB saga

## What happened

The autofin bot's `force-run-trading --live-submit` failed to place
its ABB order across four attempts on 2026-10-12. Each attempt
revealed a different wrong assumption in the submit pipeline:

| Time (ICT) | Attempt | Error | Root cause |
|---|---|---|---|
| 13:24 | #1 | `loanPackageId is required` (HTTP 400) | Place_stock_order omitted the field when `loanPackageId=None`; assumed it was optional |
| 13:25 | #2 | `loanPackageId is required` (HTTP 400) | Fixed to send `loanPackageId=0`; server treats 0 as "no value", not "no package" |
| 14:31 | #3 | `INVALID_ORDER_TYPE_FOR_THE_EXCHANGE` (HTTP 400) | Session classifier returned `ATC` (14:30-14:45 is ATC), but ABB is on UPCOM which only accepts `LO` |
| 14:48 | #4 | **SUCCESS** — order 335881 placed, `status=New`, `loanPackageId=1036` | All three bugs fixed |

Order 283481 (the manual test of the loanPackageId=1036 fix at
13:48 ICT) was also placed successfully and then manually canceled
to keep the ledger clean.

## The final fix (3 parts)

### 1. ``DnseClient.get_cash_loan_package_id(account_no, symbol)``

Looks up the per-symbol cash package id (the one with
``type == "N"`` from the ``/accounts/{accountNo}/loan-packages``
endpoint). For ABB on account 0003937868 that's ``1036``
("GD tiền mặt" / cash transaction). For other symbols the id
will differ — the lookup is mandatory.

Raises ``RuntimeError`` with a clear message if:
- the server returns no packages for the symbol
- the server returns packages but none has ``type="N"``

### 2. ``_submit_autofin_deals_to_dnse`` looks up the cash pkg per symbol

The runner now calls
``client.get_cash_loan_package_id(account_no, sym)`` once per
unique symbol in the submit batch, caches the result, and uses
it as the ``loanPackageId`` on each ``DnseOrder``. Falls back
to ``loan_package_id_by_symbol`` override or to the
launch-script's default as a last resort. Lookup errors are
accumulated and reported in the submit result.

### 3. ``_submit_autofin_deals_to_dnse`` overrides orderType for UPCOM/HNX

The orderType now depends on BOTH the trading session AND the
symbol's market:

| Session | HOSE (HSX) | HNX (HNX) | UPCOM (UPX) |
|---|---|---|---|
| ATO 09:00-09:15 | ATO (price=0) | **LO** (downgraded) | **LO** (downgraded) |
| Continuous 09:15-11:30, 13:00-14:30 | LO (price=signal) | LO (price=signal) | LO (price=signal) |
| ATC 14:30-14:45 | ATC (price=0) | ATC (price=0) | **LO** (downgraded) |
| Post-close 14:45-15:00 | LO (price=signal) | LO (price=signal) | **LO** (post-close closed on UPCOM) |

When the orderType is downgraded from ATO/ATC to LO, the price
is restored from the signal price (0 → signal_price) so the
order has a real limit instead of being treated as a 0-priced
auction.

## Known limitation: SL before BUY fills

The strategy plans BUY and SL together. The runner submits both,
but the SL gets rejected with
``HTTP 400 TRADE_QUANTITY_NOT_ENOUGH "deal not found"`` because
the BUY hasn't filled yet (it's a limit order at 17,500 VND
waiting for a match).

Two options to address this:
1. Submit only the BUY; let the order monitor + the next mid-day
   poll re-plan the SL once the BUY fills. This is what the
   current code does — the SL gets a "TRADE_QUANTITY_NOT_ENOUGH"
   error logged to the ledger, and on the next poll the strategy
   will see the filled position and plan a fresh SL.
2. Hold the SL in a pending queue and submit it via the order
   monitor once the BUY's `fillQuantity` goes positive. More
   complex; not implemented yet (open work item).

The current behaviour is acceptable because the SL is re-planned
on every poll — the only downside is one extra `orderLedger` row
with `status=unknown, error=TRADE_QUANTITY_NOT_ENOUGH` per cycle.

## What we did about the rising-ask problem

On 2026-10-12 14:48 ICT we placed ABB BUY 100 @ 17,500 (live
ask was 17,500 at the time). The ask drifted to 17,600 over the
next 12 minutes and stayed there; the order never filled and
expired at EOD. We placed the order at the right price at the
time, but the market kept moving away.

The fix is **re-pricing stale unfilled limit orders** (see
``vietnam_algo/bots/runner.py:_maybe_reprice_stale_order``):

- After ``ORDER_REPRICE_AFTER_SECONDS`` (default 900 = 15 min) of
  being open, the order monitor looks up the live quote for the
  symbol.
- For **BUY** orders: re-prices UP. If the best-ask is **above
  the current limit** but **below the cap**
  (``original_price * (1 + ORDER_REPRICE_TOLERANCE_PCT)``,
  default 3 % above the original price), the monitor:
  - Cancels the old order
  - Places a new order at the new best-ask
  - Updates the ledger entry: ``price`` = new price,
    ``repriceCount`` += 1, ``lastRepricedAt`` = now, ``originalPrice``
    preserved from first submit
  - Sends a Telegram: "🔁 Vietnam Algo: order ABB RE-PRICED
    17,500 → 17,600"
- For **SELL** orders: re-prices DOWN. If the best-bid is
  **below the current limit** but **above the cap**
  (``original_price * (1 - ORDER_REPRICE_TOLERANCE_PCT)``,
  default 3 % below the original price), the monitor:
  - Cancels the old order
  - Places a new order at the new best-bid
  - Sends a Telegram: "🔁 Vietnam Algo: order VCB RE-PRICED
    20,000 ↓ 19,500"
- If the relevant market-side price is **beyond the cap**, the
  monitor:
  - Cancels the old order
  - Marks the ledger entry ``status="cancelled"`` with
    ``error="best-ask X > cap Y; cancelled and abandoned"`` (or
    ``best-bid X < cap Y`` for SELL)
  - Sends a Telegram: "🛑 Vietnam Algo: order ABB ABANDONED
    (best-ask above cap)"
- The cap is computed from ``originalPrice`` (the FIRST submit
  price), not the current (possibly re-priced) price. This
  prevents a runaway market from compounding the cap.

Kill-switch: at most ``ORDER_MAX_REPRICES`` (default 1, set to
2 in the production launch script) re-prices per order. After
that, the order is abandoned no matter what.

The next time ABB (or any other stock) drifts away from our
limit, the bot will automatically chase up to the cap and
Telegram the operator. No manual intervention needed.

## Auto-recovery of failed entries (2026-10-13)

After the 2026-10-12 ABB BUY at 17,500 expired at EOD without
filling, the next-morning poll would have filtered ABB out as
"already actioned" — even though we never actually held a
position. The operator had to manually reset the actioned state
to re-attempt, which is fragile and error-prone.

The fix (commit ``df6a971``):
``AutofinSignalAdapter._prune_terminal_unfilled_deals`` derives
the actioned set from:

1. The persistent actioned set on disk
2. PLUS deals we still hold (``our_deals[did]._our_qty > 0``)
3. PLUS deals with at least one non-terminal order on the books
   (open / partial / submitting / unknown / prepared)

and REMOVES deals whose latest order is in a terminal state
WITHOUT a fill (expired / cancelled / rejected / closed /
dry_run / skipped). After this, ABB (which has only an
"expired" order on the books) is automatically re-actionable
next day — no manual reset needed.

The function is best-effort: any failure (missing ledger, parse
error, ledger corruption) is treated as "no pruning" so we
don't accidentally drop a deal that's still in flight.

### Known caveat: dry-run pollution

When ``force-run-trading`` runs in **dry-run** mode (without
``--live-submit``), the strategy still updates
``our_deals[did]._our_qty`` to the planned quantity (line 2120
in ``autofin_signal.py``). In live mode this is correct: the
filled order actually changes our position. In dry-run, we
never executed the trade, but the state file records it as if
we did. This is a side-effect of the dry-run reusing the same
state file as live mode.

**Impact on the auto-recovery fix**: a dry-run will leave the
state saying ``_our_qty = 100`` for the new entry. When the
next live cycle loads the state, ``_our_qty > 0`` → the deal
is treated as "held" → the cleanup doesn't prune it from
actioned_deals.

**Workaround until fixed**: if you ran a dry-run and now need
the live bot to re-attempt the same deal, manually reset the
state:
```python
import json
p = "/root/hft-trading-algo/vietnam_algo/data/live/autofin_signal_state.json"
s = json.load(open(p))
s["our_deals"]["<DID>"]["_our_qty"] = 0
s["actioned_deals"] = [d for d in s["actioned_deals"] if d != "<DID>"]
json.dump(s, open(p, "w"), indent=2, sort_keys=True)
```

**Proper fix** (open work item): separate the dry-run and
live state, or guard the line-2120 update with ``if not
dry_run`` (track via a top-level ``is_dry_run`` parameter
plumbed through ``decide_orders``).

## Tests added (770 → 773)

- ``vietnam_algo/tests/test_dnse_cash_loan_package.py``: +4 tests
  for the cash-pkg lookup helper
- ``vietnam_algo/tests/test_trading_window_extension.py``: +3 tests
  for the market-aware orderType override (UPCOM-ATC, HNX-ATO,
  HOSE-ATC unchanged)
- ``vietnam_algo/tests/test_trading_window_extension.py``: 4
  pre-existing tests updated to use ``stub_client.market_by_symbol``
  and assert the new ``loanPackageId=1036`` (was 0)

## Commits

- ``99eab02`` fix(submit): look up cash package id per symbol
  (DNSE rejects 0)
- ``6361c5a`` fix(submit): market-aware orderType — UPCOM only
  supports LO
- (prior) ``921151e`` fix(place_stock_order): send
  loanPackageId=0 for cash orders (not omit) — supers ded by
  ``99eab02``
- (prior) ``3a300ec`` fix(autofin): bot does not use margin —
  submit cash orders only — this commit was a half-fix; the
  full fix is in 99eab02

## End-to-end verification

On the VPS at 14:48 ICT on 2026-10-12:

```
$ python3 -m vietnam_algo.main force-run-trading \
    --strategy autofin_signal \
    --account-no 0003937868 \
    --loan-package-id 1775 \
    --live-submit
...
strategy result: 2 order(s) planned
  ...
submit result: 1 submitted, 0 skipped-existing, 1 unknown (of 2 planned)
  dnseOrderIds: ['335881']
```

The "1 unknown" is the SL order that failed because the BUY
hadn't filled yet (see "Known limitation" above).

The running autofin bot picked up order 335881 on its next
60-second poll:

```
[2026-10-12T14:48:52+07:00] order monitor: polled=1 throttle_skipped=0 changes=0 terminal=0 errors=0
[2026-10-12T14:49:52+07:00] order monitor: polled=1 throttle_skipped=0 changes=0 terminal=0 errors=0
...
```

When the BUY fills (or is canceled, or the SL is placed), the
order monitor will detect the state change and send a Telegram
notification.

## What to monitor

```bash
# Order status
python3 -c "
import json, sys
sys.path.insert(0, '/root/hft-trading-algo')
from vietnam_algo.libs.dnse.client import DnseClient
import os
c = DnseClient(api_key=os.environ['DNSE_API_KEY'], api_secret=os.environ['DNSE_API_SECRET'])
with open('/root/hft-trading-algo/vietnam_algo/data/live/dnse_trading_token.json') as fp:
    tok = json.load(fp)['token']
print(c.order_detail('0003937868', '335881', trading_token=tok))
"

# Bot log
tail -f /root/logs/autofin.log
```
