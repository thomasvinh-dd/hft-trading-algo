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
