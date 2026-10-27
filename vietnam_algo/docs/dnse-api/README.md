# DNSE OpenAPI Reference (locally archived 2026-10-08)

Frozen local copy of the public DNSE OpenAPI spec pages so the team
isn't dependent on `developers.dnse.com.vn` being reachable to keep the
trading bot alive.

## Source URLs

- Trading: <https://developers.dnse.com.vn/docs/dnse/trading>
- Account: <https://developers.dnse.com.vn/docs/dnse/account>
- Market data: <https://developers.dnse.com.vn/docs/dnse/market-data>
- Trading guide (vòng đời lệnh, body examples): <https://developers.dnse.com.vn/docs/guide/trading-api/trading_order>
- Trading account guide: <https://developers.dnse.com.vn/docs/guide/trading-api/trading_account>
- Margin guide: <https://developers.dnse.com.vn/docs/guide/trading-api/dnse_margin>

Raw HTML for every endpoint page is in this directory as `doc_*.html`
plus `guide-trading_order.html`. The Docusaurus SPA loads the actual
request/response schema client-side, so the HTML only contains the
endpoint path + status codes; the *content* in this file is hand-written
from the guide pages (`guide-trading_order.html`,
`guide-trading_account.html`) which DO contain the JSON body examples.

## Auth

All endpoints use HMAC-SHA256 over the **path only** (NOT the query
string, NOT the body), per the `signing_string()` helper in
`vietnam_algo/libs/dnse/client.py`. Signed headers:

| Header | Value |
|---|---|
| `x-api-key` | API key from `DNSE_API_KEY` env |
| `X-Signature` | HMAC-SHA256 of `METHOD\nPATH\nDATE\nNONCE` |
| `version` | API version, default `2026-07-23` |
| `Date` | RFC 7231 GMT date, default header name (override via `DATE_HEADER` env) |

Order endpoints add:

| Header | Value |
|---|---|
| `trading-token` | `UUID` from `2-fa-verification` (auth.py:trading_token) |

`trading-token` is a **HEADER**, not a body field. The current
bot-side `place_stock_order` call sites incorrectly pass it as a body
kwarg; the fix is to forward it via `extra_headers={"trading-token": ...}`
to `DnseClient.request()`.

## Endpoints

### Trading

| Method | Path | Use |
|---|---|---|
| `POST` | `/accounts/{accountNo}/orders` | Place order (NORMAL/STOP/OCO) |
| `GET`  | `/accounts/{accountNo}/orders` | Sổ lệnh — order book (open orders) |
| `GET`  | `/accounts/{accountNo}/orders/{orderId}` | Order detail |
| `PUT`  | `/accounts/{accountNo}/orders/{orderId}` | Replace order (price+quantity) |
| `DELETE` | `/accounts/{accountNo}/orders/{orderId}` | Cancel order |
| `GET`  | `/accounts/{accountNo}/orders/history` | Order history (today or by date range) |
| `GET`  | `/accounts/{accountNo}/executions/{orderId}` | Executions (fills) for an order |
| `POST` | `/positions/{positionId}/close` | Close position |
| `POST` | `/positions/{positionId}/reverse` | Reverse position |
| `POST` | `/positions/{positionId}/pnl-configs` | TP/SL for a position |
| `PATCH` | `/accounts/{accountNo}/pnl-configs` | TP/SL for a sub-account |

### Account

| Method | Path | Use |
|---|---|---|
| `GET` | `/accounts` | All accounts (with sub-accounts `id`, `dealAccount`, `derivativeAccount`) |
| `GET` | `/accounts/{accountNo}/balances` | Cash + margin info |
| `GET` | `/accounts/{accountNo}/loan-packages` | Available loan packages |
| `GET` | `/accounts/{accountNo}/ppse` | Sức mua / sức bán (max qty for a symbol) |
| `GET` | `/accounts/{accountNo}/positions` | Current positions |
| `GET` | `/positions/{positionId}` | Position detail |
| `GET` | `/accounts/{accountNo}/loans` | Active loans |

### Market data

| Method | Path | Use |
|---|---|---|
| `GET` | `/market/instruments` | All tradable instruments (paginated) |
| `GET` | `/price/ohlc` | OHLC bars (resolution `1`=1min, `5`/`15`/`60`) |
| `GET` | `/price/{symbol}/close` | Closing price for a symbol |
| `GET` | `/price/{symbol}/quotes/latest` | Best bid/ask latest |
| `GET` | `/price/{symbol}/quotes` | Full bid/ask depth |
| `GET` | `/price/{symbol}/trades/latest` | Latest trades |
| `GET` | `/price/{symbol}/trades/history` | Trade history |
| `GET` | `/price/{symbol}/trades/step` | Volume per price step |
| `GET` | `/price/{symbol}/bidask/latest` | Best bid/ask latest (alt endpoint) |
| `GET` | `/price/{symbol}/bidask/history` | Bid/ask history |
| `GET` | `/price/{symbol}/foreign` | Foreign investor data |
| `GET` | `/price/session` | Trading session info |
| `GET` | `/price/expected` | Expected match price |
| `GET` | `/price/market-index` | Market index data |
| `GET` | `/price/market-index/{symbol}/history` | Index history |
| `GET` | `/price/volume-profile` | Volume profile |
| `GET` | `/market/working-dates` | Trading days (1y window) |

### Auth (token issuance)

| Method | Path | Use |
|---|---|---|
| `POST` | `/auth/email-otp` | Send email OTP (requires basic auth) |
| `POST` | `/auth/handoff-otp` | Handoff Smart OTP |
| `POST` | `/auth/2fa` | Verify OTP, returns trading-token |

## Place order — `POST /accounts/{accountNo}/orders`

### Query parameters

| Param | Required | Values | Notes |
|---|---|---|---|
| `marketType` | yes | `STOCK` / `DERIVATIVE` / `BOND` | |
| `orderCategory` | yes | `NORMAL` / `STOP` / `OCO` | STOP/OCO are STOCK+DERIVATIVE only |

### Headers (in addition to standard auth)

| Header | Value |
|---|---|
| `trading-token` | UUID returned by `/auth/2fa` |
| `Content-Type` | `application/json` |

### Body — NORMAL

```json
{
  "symbol": "HPG",
  "side": "NB",
  "orderType": "LO",
  "price": 25950,
  "quantity": 100,
  "loanPackageId": 5757
}
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `symbol` | yes | string | Ticker, uppercase |
| `loanPackageId` | yes | int | Loan package id (call `/loan-packages` first) |
| `side` | yes | enum | `NB` = mua (buy), `NS` = bán (sell) |
| `quantity` | yes | int | 100-share board lot for STOCK |
| `orderType` | yes | enum | `LO` (limit), `MTL` (market-to-limit). HOSE also: `ATO`, `ATC`. HNX: `MAK`, `MOK`, `PLO`. UPCOM: `LO` only. |
| `price` | yes | int | In VND, > 0 and within ±price-ceiling/floor. For non-LO: send 0. |

### Order status lifecycle (NORMAL)

| Status | Meaning |
|---|---|
| `pending` | Newly created, internal validation |
| `pendingNew` | Validated, waiting to be sent to Sở |
| `new` | On the exchange, waiting to match |
| `partiallyFilled` | Partial fill |
| `filled` | Fully filled |
| `pendingReplace` | Replace request in progress |
| `pendingCancel` | Cancel request in progress |
| `canceled` | Successfully cancelled |
| `rejected` | Rejected (margin, qty, etc.) |
| `expired` | Expired (session end or GTD timeout) |
| `doneForDay` | EOD cleanup |

### Response (200)

A JSON object with the order's full state. DNSE does not publish a
formal response schema in the guide, but the call sites in
`trading_engine.py:821-830` and `order_monitor.py:171-180` look for
`response.get("orderId")` and `response.get("id")`. The order detail
endpoint returns more fields (typically `id`, `symbol`, `side`,
`quantity`, `price`, `orderType`, `orderStatus`, `filledQuantity`,
`canceledQuantity`, `createdAt`, `accountNo`, `loanPackageId`).

## Order detail — `GET /accounts/{accountNo}/orders/{orderId}`

Returns the full order state. Key fields used by the bot's ledger:
`orderStatus` (the enum above), `filledQuantity`, `canceledQuantity`.

## Replace order — `PUT /accounts/{accountNo}/orders/{orderId}`

Same path with PUT, same query params (`marketType`, `orderCategory`).
Body:

```json
{
  "price": 25950,
  "quantity": 100
}
```

Only allowed when the order is in `new` or `partiallyFilled` state.

## Cancel order — `DELETE /accounts/{accountNo}/orders/{orderId}`

Same path with DELETE. No body. Returns the cancelled order's state.

## Order history — `GET /accounts/{accountNo}/orders/history`

Returns a list of historical orders. The bot's `order_monitor.py:417-418`
and `daily_engine.py:418-419` use this to reconcile the bot's ledger
against DNSE's view (open order count + recent activity).

## Executions — `GET /accounts/{accountNo}/executions/{orderId}`

Returns fill details for an order (timestamp, price, quantity). The
`order_monitor.filled_quantity()` helper accepts multiple field-name
variants: `filledQuantity` / `matchedQuantity` / `matchQuantity` /
`filledQty` / `matchedQty`. The DNSE spec is the first of these.

## Why this doc exists

On 2026-10-08 the autofin bot sent a Telegram message at 04:30 ICT
saying "→ DNSE: BUY 1700 ABB @ 17,400 VND" and then sat there. The user
saw no OTP and no order on DNSE. Root cause: `DnseClient` had no
`place_stock_order` method — it was never implemented. The error was
caught silently by `daily_engine.execute_orders_with_ledger`'s
try/except. The 3 call sites in `daily_engine.py`, `trading_engine.py`,
and `order_monitor.py` had been assuming it existed since the
`feat(autofin): wire live submit` commit (`2ef273f`).

The fix adds 4 methods (`place_stock_order`, `order_detail`,
`replace_order`, `cancel_order`) to `DnseClient`, and makes the runner
report actual submission outcomes via Telegram so the user is never
left guessing whether the order went through.

## Field-name convention reminder

- `trading-token` header is the **`UUID` string** from
  `auth.create_trading_token()`, NOT the bot's `tradingToken` field on
  the body. The body doesn't carry it.
- `loanPackageId` is an **`int`** (e.g. `1775` for HDB margin), not a
  string.
- `price` is in **VND** (full đồng, not kVND). Our local prices are in
  kVND × 1000 before being sent.
- `quantity` is **shares**, must be a multiple of the board lot
  (100 for STOCK).
- `side` is **`NB` / `NS`**, not `BUY` / `SELL` (the bot's
  `trading_engine.dnse_side()` does the mapping).
- `orderType` is **`LO`** for the LO limit orders the bot currently
  places. The bot has never placed MTL/ATO/ATC orders.
- `marketType` for our STOCK orders is **`STOCK`**.
- `orderCategory` for our LO orders is **`NORMAL`**.
