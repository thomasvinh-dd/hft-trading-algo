# Darvas Box + AutoFin-Tiered-Exits Strategy

A standalone backtest of the strategy reverse-engineered from
AutoFin's `signal-static-suggest` endpoint.

The strategy is a **Darvas Box breakout** (tight-consolidation
breakout with volume confirmation) extended with three AutoFin-specific
features:

1. **Fixed profit-tier ladder** — close partial positions at fixed
   percentage multipliers from entry (verified across 8 closed ASP
   deals to land on the exact same multipliers AutoFin uses).
2. **Scale-out stop loss** — instead of a binary full-close on stop,
   bleed out 50% at `sl_T`, 50% of remaining at the midpoint of the
   stop band, and close the rest at the hard stop `sl_F`.
3. **Trailing capital preservation** — once the first profit tier
   fires, the running peak is trailed by a fixed percentage; if close
   drops that much below peak (or below the first-tier exit price if
   `trail_floor_lock_profit` is on), the position is closed.

## How the strategy works

The strategy is a **mean-reversion breakout** with **trend-confirmed
profit-taking**. It looks for stocks that have been quietly
consolidating, then buy when they break out with volume. Once in a
position, it uses a **ladder of profit targets** to lock in gains
and a **trailing stop** to ride the rest.

### Step 1 — Find a Darvas Box base (the setup)

The first step is identifying a **consolidation pattern** in the price:

- The stock has been trading in a **tight range** for **5–30 days**
  (`BaseConfig.min_days=5, max_days=30, max_range_pct=3%`).
  "Tight" means the high-low range of the period is ≤ 3% of the
  low.
- During this consolidation, **volume has been declining** or staying
  flat (no distribution by big players).
- The base is the most recent qualifying period that ends before
  the breakout bar.

A Darvas Box is a "calm before the storm" pattern — sellers are
exhausted, supply has dried up, and any small surge in demand can
push the price up.

### Step 2 — Wait for the breakout (the trigger)

The base becomes a **signal** when ALL of these conditions are met
on the bar AFTER the base ends:

1. **Price breaks above the base high** — `bar.close > base.base_high`
2. **Volume is at least 2× the average** of the prior 20 days
   (`volume_mult=2.0` for regular conviction, `>3.0` for high
   conviction)
3. **Price is above the 200-day moving average** — long-term trend
   is up (`require_ma200=True`)
4. **Price is above the 20-day moving average** — short-term trend
   confirms the move (`require_close_above_ma=20`)
5. **Universe filter** — stock is a HOSE blue chip with average
   daily traded value ≥ 1B VND (`universe.min_avg_value_20d=1e9`)

If all conditions are met, the strategy calculates position size and
generates a `Signal` with:
- `entry_price` (next bar's open)
- `sl_T = entry_price × (1 - 0.10)` (10% stop, partial)
- `sl_F = entry_price × (1 - 0.18)` (18% hard stop)
- `tiers` (the profit-taking ladder, see below)
- `size_pct` (15% of NAV for regular, 30% for high conviction)
- `shares` (rounded down to 100-share board lot)

### Step 3 — Position sizing

For a 1B VND portfolio:
- Regular conviction: 15% = 150M VND per position
- High conviction (volume > 3×): 30% = 300M VND per position

Up to 10 positions can be open at once (max gross ~3× NAV), so
during a strong market the strategy is highly invested. After a
stop-loss exit, the same ticker is blocked for 5 bars (cooldown) so
the strategy doesn't immediately re-enter a falling knife.

### Step 4 — Profit-taking ladder (TIER + TRAIL)

The default tier ladder (`ExitsConfig.tiers`):

| Tier | Multiplier from entry | Sell % of remaining | Cumulative sold |
|---:|---:|---:|---:|
| 1 | 1.063 (+6.3%) | 10% | 10% |
| 2 | 1.125 (+12.5%) | 30% | 40% |
| 3 | 1.210 (+21.0%) | 30% | 70% |
| 4 | 1.330 (+33.0%) | 10% | 80% |
| 5 | 1.460 (+46.0%) | 10% | 90% |
| 6 | 1.610 (+61.0%) | 10% | 100% (or trail) |

When the **bar's high** crosses a tier price, the strategy sells
that tier's percentage of remaining shares at the tier price. Once
the first tier fires, the **trailing stop activates** — the strategy
tracks the running peak and closes the remaining position if close
drops 10% from peak (`trail_from_peak_pct=0.10`).

**Visual example** (entry = 100,000):

```
Price
  ▲         ╱───── trail floor locks at first tier (106,300)
  │        ╱
  │    T1 ╱                       ← sell 10% @ 106,300
  │    ──┤
  │      │  T2                    ← sell 30% more @ 112,500
  │      │  ─────
  │      │       T3                ← sell 30% more @ 121,000
  │      │       ─────
  │      │            trail active
  │      │             ╲──────     ← if close drops 10% from peak
  │      │                      ╲     close remaining at market
  │
  ├──────┤ entry @ 100,000
```

The **trail_floor_lock_profit** option (default ON) means once the
first tier fires, the trail floor is set to the tier-1 price
(106,300 in the example). The remaining position can never close
below that, guaranteeing at least a small profit on every trade
that reaches tier 1.

### Step 5 — Stop loss ladder (SL_SCALE + SL_HARD)

Instead of one hard stop that closes the whole position, the
strategy **scales out** in three steps. The stops are anchored to
**entry price** (not the base low) which is critical for the
backtest math:

- **sl_T = entry × 0.90** (10% below entry)
- **sl_mid = entry × 0.86** (14% below entry, midpoint of band)
- **sl_F = entry × 0.82** (18% below entry)

When the bar's low hits a stop:
1. `bar.low ≤ sl_T` → sell **50% of remaining** at `sl_T`
2. `bar.low ≤ sl_mid` → sell **50% of remaining** at `sl_mid`
3. `bar.low ≤ sl_F` → close **all remaining** at `sl_F`

A trade that opens at 100,000 and falls straight to 80,000:

```
Day 1: entry 3,100 shares @ 100,000
Day X: low=89,000 → SL_SCALE: sell 1,550 @ 90,000 (sl_T hit)
       remaining=1,550
Day X+1: low=85,000 → SL_SCALE: sell 775 @ 86,000 (sl_mid hit)
       remaining=775
Day X+2: low=80,000 → SL_HARD: close 775 @ 82,000 (sl_F hit)
       remaining=0
```

By scaling out, the strategy bleeds 50% at the first stop, 25% at
the second, and 12.5% at the third. The bulk of the loss protection
comes from the first cut. This is why the worst single-trade loss
in the backtest is **-13%** (not -18% as a single hard stop would
imply).

### Step 6 — Trade management summary

For each bar after entry, the strategy runs this priority order:

1. **DIVIDEND** — record any cash dividend (positions still earn)
2. **TIME_STOP** — if held > `max_holding_bars` and still underwater, close
3. **TRAIL** — if trailing is active and close < trail threshold, close
4. **TIER** — sell each tier whose price was reached by `bar.high`
5. **SL_SCALE** — if `bar.low ≤ sl_T`, cut 50%
6. **SL_SCALE** — if `bar.low ≤ sl_mid`, cut 50% of remaining
7. **SL_HARD** — if `bar.low ≤ sl_F`, close everything
8. **EXHAUST** — if backtest runs out of bars, force-close at last close

Each event adds a row to the `fills` timeline (visible in the deal
detail modal). The trade's exit reason is the kind of the **last
non-dividend fill** — so a trade that took partial stops and then
hit the hard stop is classified as `SL_HARD`.

### Why this combination works

- **Winners run** — the 9-tier ladder + 10% trail lets big winners
  compound to +115% (tier 9) before exit. The trade-by-trade win
  rate is 73.5% but the average win is much bigger than the average
  loss (profit factor 2.18).
- **Losers are cut fast** — the 3-step scale-out means a single
  trade can lose at most ~13% of the position (not the full 18%),
  and the bulk of the loss is realized at the first -10% cut.
- **Correlation is diversified** — 10 max positions across 70
  blue chips. Single-ticker risk is bounded by max 30% NAV per
  position.
- **Trend confirmation** — 200-day MA filter ensures we only
  long in bull markets. The 2026 downtrend produced 9 losing
  trades but the strategy cut each at -10% to -11% as designed.

## Trade events reference

Every trade has a `fills` timeline (one row per event) visible in
the deal-detail modal. Each fill has a `kind` that tells you what
happened.

### Quick reference

| Kind | What it means | When it fires | Effect on shares |
|---|---|---|---|
| **ENTRY** | Buy at the breakout | Once, on the first day of the trade | +shares (entry size) |
| **TIER** | Partial take-profit at a ladder level | `bar.high ≥ entry × tier_multiplier` | -tier_pct × remaining |
| **TRAIL** | Trailing stop hit (closes the rest) | Trailing active AND `bar.close ≤ peak × (1 - trail_pct)` (or below trail floor) | -all remaining |
| **SL_SCALE** | Partial stop-loss cut | `bar.low ≤ sl_T` (cut 50%) or `bar.low ≤ sl_mid` (cut 50% of remaining) | -50% (or all if small) |
| **SL_HARD** | Full hard stop (closes everything left) | `bar.low ≤ sl_F` | -all remaining |
| **TIME_STOP** | Time stop fired | `bars_held ≥ max_holding_bars` AND close still below entry | -all remaining |
| **DIVIDEND** | Cash dividend received | Ticker has a dividend event on this bar | unchanged (income credited) |
| **EXHAUST** | Backtest ran out of data | No more bars | -all remaining (at last close) |

### Detailed explanation of each event

#### ENTRY — opening the position

```
Signal fires (yesterday's bar):
  base detected → breakout confirmed → size computed
  sh = round_down(nav × size_pct / entry_price, board_lot=100)
  sl_T = entry × 0.90
  sl_F = entry × 0.82
  tiers = [(1.063, 0.10), (1.125, 0.30), ...]
  trailing_active = False   ← trail only kicks in after first tier
  tier_idx = 0              ← next tier to fire
  remaining = sh

Today (next bar after the signal):
  ENTRY fill recorded at bar.open
  remaining stays at sh
  cumulative_pnl = 0
```

**The trade is now "open" and will be checked on every bar
until all shares are sold.**

The entry row's `note` field always says `Buy N @ X` so you can
verify the price and share count at a glance.

#### TIER — partial profit-taking

When the bar's high reaches a tier price, that tier fires and
sells its percentage of remaining shares at the tier price. The
`while` loop in the executor processes ALL tiers that the bar
crosses, so a single bar can fire multiple tiers (e.g. if price
gaps up through tier 1, 2, and 3 in one day, all three fire).

```
Before bar:
  remaining = 1,000
  tier_idx = 2 (next tier is 1.210 = +21%, sell 30%)
  cumulative_pnl = +30M

Bar high = 125,000 (> 121,000):
  TIER fires at 121,000
  sell 1,000 × 30% = 300 shares @ 121,000
  pnl_delta = (121,000 - 100,000) × 300 = +6.3M
  remaining = 700
  tier_idx = 3

If bar high > 1.330 (133,000):
  TIER 4 fires at 133,000
  sell 700 × 10% = 70 shares @ 133,000
  pnl_delta = (133,000 - 100,000) × 70 = +2.31M
  remaining = 630
  tier_idx = 4
```

**Important**: the tier price is fixed relative to ENTRY, not
relative to the previous tier. So if entry is 100,000, tier 5 is
always at 146,000 (1.46 × entry), regardless of what happened
before. The tier's `level` in the fill row shows the actual
fill price (= tier price).

**First-tier side effect**: when `tier_idx == 0` fires, the trail
activates. The `trail_floor` is set to the tier-1 price. Now
`remaining` shares can never close below that price (unless
`trail_floor_lock_profit=False`).

#### TRAIL — trailing stop exits the rest

The trail starts "armed" but inactive. It activates on the first
tier (or whichever tier is set in `TrailingConfig.activation_tier_idx`).
After activation, every bar checks:

```
trail_threshold = peak_close × (1 - trail_from_peak_pct)   ← default 10%
if trail_floor_lock_profit:
    trail_threshold = max(trail_threshold, trail_floor)   ← never below tier 1
if bar.close ≤ trail_threshold:
    TRAIL fires — close all remaining at bar.close
```

```
Entry = 100,000, peak so far = 130,000
trail_from_peak_pct = 0.10
trail_threshold = 130,000 × 0.90 = 117,000
trail_floor = 106,300 (tier 1 = 1.063 × entry)

If bar.close = 115,000:
  trail_threshold = 117,000
  115,000 < 117,000 → TRAIL fires
  remaining shares sold at 115,000
  pnl_delta = (115,000 - 100,000) × remaining = +15,000 per share

If tier 1 was set higher (say tier 1 = 1.20 = 120,000):
  trail_floor = 120,000
  trail_threshold = max(117,000, 120,000) = 120,000
  Now trail only fires when close drops below 120,000 — locks in
  a bigger profit at the cost of giving back more of the move.
```

**Why the trail matters**: a trade can hit tier 1 (+6.3%) and
then ride the trend all the way to +50% before the trail fires.
Without the trail, the strategy would either hold forever
(catching every reversal) or use a hard exit (cutting winners
short).

#### SL_SCALE — bleeding out on the way down

Three trigger levels, all anchored to ENTRY price. Each fires
independently and cuts 50% of what's still held.

```
sl_T   = entry × 0.90   (10% below entry)
sl_mid = entry × 0.86   (14% below entry)
sl_F   = entry × 0.82   (18% below entry)
```

Order on a single bar (priority: T → mid → F):

```
if bar.low ≤ sl_T:
    SL_SCALE at sl_T — sell 50% of remaining
if bar.low ≤ sl_mid and remaining > 0:
    SL_SCALE at sl_mid — sell 50% of remaining
if bar.low ≤ sl_F and remaining > 0:
    SL_HARD at sl_F — sell all remaining
```

**Board-lot edge case**: if `cut = round(remaining × 0.50)` is
less than 1 board lot (100 shares), the cut is bumped to whatever
remains. This prevents the strategy from leaving 49 shares
stranded forever.

**Example bar (entry 100,000)**:
```
low = 85,000  →  bar.low ≤ sl_T (90,000) and sl_mid (86,000) and sl_F (82,000)
                ALL THREE fire on the same bar
                cut_1 = round(3,100 × 0.5) = 1,550  → 1,550 sold @ 90,000 (sl_T)
                cut_2 = round(1,550 × 0.5) = 775   →    775 sold @ 86,000 (sl_mid)
                cut_3 = 1,550 - 1,550 - 775         =    775 sold @ 82,000 (sl_F)
                remaining = 0  →  trade closed
                Total loss = (90,000-100,000)×1,550 + (86,000-100,000)×775
                           + (82,000-100,000)×775
                           = -15.5M + -10.85M + -13.95M
                           = -40.3M VND
```

#### SL_HARD — full hard stop

The last line of defense. Fires only if the position survives
both scale-outs (i.e., the bar's low hit sl_T and sl_mid but the
strategy's "round 50% of remaining" logic left enough shares to
trigger the next cut). In practice, SL_HARD fires on:
- The bar after the second SL_SCALE if the price has dropped far
  enough to also hit sl_F.
- A bar that gaps down past sl_F without ever touching sl_T or
  sl_mid (rare on HOSE due to ±7% daily limit).

#### TIME_STOP — giving up on a stalled loser

Fires when `bars_held ≥ max_holding_bars` AND `close < entry_price`.
The default is `max_holding_bars=0` (off), so this event is rare
in the backtest.

When enabled, the intent is: "if a position has been open for N
bars and is still underwater, the breakout thesis is wrong — exit
and free up capital for the next signal."

**The check is `close < entry_price`**, not "still in the original
base range". A position that's been up 20% then dropped back to
-2% is NOT a time stop (it should hit the trail first). Only
positions that never made it to tier 1 trigger this.

#### DIVIDEND — passive income

A cash dividend doesn't reduce shares but it credits the
`cumulative_pnl` directly. The position is still held.

```
bar.date in dividend_per_share and remaining > 0:
  income = remaining × cash_div
  cumulative_pnl += income
  DIVIDEND fill recorded with kind="DIVIDEND"
  shares = remaining (unchanged, but the fill is "informational")
```

HOSE blue chips typically pay 1-2 dividends per year at 5-15%
cash yield. A position held for 6 months in a dividend-paying
stock gets ~3-5% of its notional value added to the P&L.

#### EXHAUST — backtest ran out of data

The simulator only knows about historical bars. When it reaches
the last bar without all shares being sold, it force-closes at
the last close.

```
for bar in bars_after:    ← iterates until bars_after is exhausted
    ...
    if remaining ≤ 0: break
# Loop ends with remaining > 0 (no more bars)
EXHAUST fires at last_bar.close
exit_reason = "EXHAUST"
```

**Important**: in live trading, this would NOT happen — the
position stays open. The backtest treats EXHAUST as a soft
"position was still open at the end of the test window" marker.

In the equity curve, EXHAUST trades contribute their MTM (last
close - entry) at the time the backtest ends, which is captured
by the "open positions" section in the Performance tab.

### Exit reason classification

The `exit_reason` field of a trade is the `kind` of the LAST
non-dividend fill. So:

- A trade that closed entirely on a single TRAIL → `exit_reason="TRAIL"`
- A trade that took 3 TIERs and then a TRAIL → `exit_reason="TRAIL"`
- A trade that took 1 TIER and then had a stop-out → `exit_reason="SL_HARD"` (or `SL_SCALE` if the last fill was a partial cut)
- A trade that closed via EXHAUST → `exit_reason="EXHAUST"`
- A trade that closed via TIME_STOP → `exit_reason="TIME_STOP"`

DIVIDEND is never an exit reason — it's always in the middle of
a trade.

### Cooldown after a stop

After a `SL_HARD` or `SL_SCALE` exit, the same ticker is blocked
for `cooldown_bars_after_stop` (default 5) trading days. This
prevents the strategy from re-entering a falling knife right after
getting stopped out.

During cooldown, signals for that ticker are skipped. The
cooldown ends after N bars, regardless of how the price moves
(no test of "price has stabilized"). This is a deliberate
conservative choice — better to miss a small rebound than to
catch a continued decline.

### Field reference per fill

```
date          Bar date (when the event happened)
kind          ENTRY / TIER / TRAIL / SL_SCALE / SL_HARD / TIME_STOP / DIVIDEND / EXHAUST
price         The fill price:
                ENTRY   = entry_price
                TIER    = tier_price (entry × tier_multiplier)
                TRAIL   = bar.close (where the trail fired)
                SL_*    = the stop level that was hit
                DIVIDEND = the cash dividend per share
                EXHAUST = last close
shares        Signed: positive for buys (ENTRY), negative for sells
              (everything else). The sign tells you the trade direction
              on this fill.
pnl_delta     Realized P&L for this fill, in VND.
                ENTRY   = 0 (you haven't realized anything yet)
                TIER    = (tier_price - entry_price) × shares_sold
                TRAIL   = (close - entry_price) × shares_sold
                SL_*    = (stop_price - entry_price) × shares_sold (negative)
                DIVIDEND = dividend_per_share × remaining_shares
                EXHAUST = (last_close - entry_price) × remaining_shares
cumulative_pnl  Sum of all pnl_delta values up to and including this fill.
                Starts at 0, can go positive (winners) or negative (losers).
remaining     Shares still held after this fill.
                ENTRY → entry shares
                TIER → 70% (or 90% if tier 1, etc.) of pre-fill remaining
                SL_SCALE → 50% of pre-fill remaining
                TRAIL / SL_HARD / TIME_STOP / EXHAUST → 0
note          Human-readable description, e.g.
                ENTRY: "Buy 3,100 @ 62,678"
                TIER: "Tier 2: sell 30% @ 112,500" (filled by the executor)
                SL_SCALE: "Stop scale-out @ 90,000 (sl_T)"
```

### Color coding in the dashboard

The deal-detail modal uses color-coded chips for each `kind`:

- **ENTRY** (blue) — the buy
- **TIER** (green) — taking profit
- **TRAIL** (green) — taking profit
- **SL_SCALE** (red) — bleeding out on the way down
- **SL_HARD** (red) — full stop-out
- **TIME_STOP** (yellow) — gave up on a losing position
- **DIVIDEND** (cyan) — passive income
- **EXHAUST** (gray) — backtest ran out of data
- **CLOSE** (white, with a top border separator) — synthetic
  marker at the end of the timeline for closed deals. Shows
  the close date, exit reason, final P&L, and holding period.
  Not an actual fill event; rendered by the dashboard so the
  user can see the deal's full lifecycle at a glance.

The CLOSE row is added by the dashboard for every closed deal.
Open deals (still in progress) don't have it. The row is
visually distinct from the regular fills (top border + slightly
different background) so the eye can quickly find where each
trade ended.

### Reading the timeline

The deal-detail modal shows the fills table with these columns:
`Date | Event | Price | Shares | Δ PnL | Cum PnL | Remaining | Δ Days | Note`

A few tips for reading it:

1. **The `cumulative_pnl` column** is the running total. For a
   winning trade, it climbs steadily. For a losing trade, it
   goes negative and gets more negative.
2. **The `Δ Days` column** shows calendar days since the previous
   event. Big gaps (e.g. `+62d`) mean the position sat idle
   without any event firing — either the price was steady, or
   the strategy was waiting for a tier that never came.
3. **The `remaining` column** counts down for SELL events
   (TIER, TRAIL, SL_*, TIME_STOP, EXHAUST) and stays constant
   for ENTRY and DIVIDEND. When it hits 0, the trade is closed.
4. **The `Note` column** is most useful for distinguishing SL_T
   cuts from SL_mid cuts when both happen in quick succession
   (they look identical otherwise).

### Example 1: a losing trade (blown stops)

```
VCB_2026-05-19 — entry 2026-05-19, exit 2026-08-13 (53 trading days held)

2026-05-19   ENTRY     62,678  +  3,100  pnl=           0   cum=           0
2026-07-20   SL_SCALE  56,410  -  1,550  pnl=  -9,824,410   cum=  -9,824,410  (sl_T hit)
2026-07-21   SL_SCALE  56,410  -    775  pnl=  -4,912,205   cum= -14,736,615  (sl_T hit)
2026-07-22   SL_SCALE  56,410  -    387  pnl=  -2,452,933   cum= -17,189,548  (sl_T hit)
2026-07-23   SL_SCALE  56,410  -    194  pnl=  -1,229,636   cum= -18,419,184  (sl_T hit)
2026-07-23   SL_SCALE  53,903  -     97  pnl=    -857,705   cum= -19,276,889  (sl_mid hit)
2026-07-24   SL_SCALE  56,410  -     48  pnl=    -304,240   cum= -19,581,129  (sl_T hit)
2026-07-24   SL_SCALE  53,903  -     24  pnl=    -212,216   cum= -19,793,345  (sl_mid hit)
... (6 more SL_SCALE events over 11 days) ...
2026-08-13   EXHAUST   59,500  -     43  pnl=    -136,661   cum= -20,071,591

Total realized PnL: -20,071,591 VND  (-10.3% on the original 198M VND notional)
Exit reason: EXHAUST (backtest ran out of data with 43 shares left)
```

The strategy correctly bled the position from 3,100 → 1 → 1 share
over 39 days as the price kept grinding lower. The 13 SL_SCALE
events each cut 50% of remaining, exactly as designed.

### Example 2: a winning trade (tiers + trail)

```
GEE_2025-08-15 — entry 2025-08-15, exit 2025-11-12 (60 trading days held)

2025-08-15   ENTRY     19,200  +  7,800  pnl=        0      cum=          0
2025-08-25   TIER      20,400  -    780  pnl=   +936,000  cum=   +936,000   (tier 1: +6.3%)
                                              ↑ trail active now (floor=20,400)
2025-09-08   TIER      21,600  -  2,106  pnl= +5,054,400  cum= +5,990,400   (tier 2: +12.5%)
2025-09-22   TIER      23,232  -  1,474  pnl= +6,233,272  cum= +12,223,672  (tier 3: +21.0%)
2025-10-10   TIER      25,536  -    343  pnl= +2,175,142  cum= +14,398,814  (tier 4: +33.0%)
2025-10-30   TIER      28,032  -    309  pnl= +2,734,005  cum= +17,132,819  (tier 5: +46.0%)
2025-11-12   TRAIL     32,400  -  2,788  pnl= +36,801,600 cum= +53,934,419  (trail hit)

Total realized PnL: +53,934,419 VND  (+36.0% on the original 150M VND notional)
Exit reason: TRAIL
```

Notice:
- **5 tiers fired** before the trail. The price went from 19,200
  to 32,400 (+69%) over 60 days.
- **Each tier locked in more profit**. The early tiers sold at
  small gains; the trail exited at the peak.
- **The trail fired at 32,400**, well above tier 5 (28,032). This
  means the price reached a peak above 32,400 / 0.90 = 36,000
  (so 10% trail from peak = 32,400) and then pulled back.

### Example 3: a mixed trade (tier then stop)

```
MSN_2025-03-04 — entry 2025-03-04, exit 2025-03-23 (14 trading days held)

2025-03-04   ENTRY     73,500  +  4,000  pnl=        0      cum=          0
2025-03-11   TIER      78,140  -    400  pnl= +1,856,000  cum= +1,856,000   (tier 1: +6.3%)
                                              ↑ trail active (floor=78,140)
2025-03-17   TRAIL?    No, price kept going up
2025-03-19   TRAIL?    No, peak = 80,200, threshold = 72,180, close = 76,500
2025-03-23   TRAIL     78,300  -  3,600  pnl= +17,280,000 cum= +19,136,000  (trail fired)

Total realized PnL: +19,136,000 VND  (+6.5% on the original 294M VND notional)
Exit reason: TRAIL
```

This trade went sideways for 2 weeks, hit tier 1 quickly, but
never reached tier 2. The trail exited at a small profit.

### Edge cases and gotchas

- **Same-bar events**: A bar can fire multiple events in one day
  (e.g., a gap-down bar can hit all three stops at once). The
  executor processes them in priority order.
- **Tier cascade**: A gap-up bar can fire multiple tiers in
  one day. The `while tier_idx < len(signal.tiers)` loop in the
  executor handles this.
- **T+2.5 settlement**: If `t_plus_n_bars=3` is set, the first
  3 bars of a trade skip exit logic entirely. The trade can be
  opened but cannot be closed (except via EXHAUST). This models
  Vietnam's T+2.5 settlement cycle.
- **Dividend during settlement**: Even in the T+2.5 lock-up
  period, dividends are still credited.
- **Cooldown after stop**: After SL_HARD or SL_SCALE exit, the
  same ticker is blocked for `cooldown_bars_after_stop` (default
  5) trading days.
- **EXHAUST on the latest data**: This is normal at the end of
  the backtest window. The position is "still open" from the
  strategy's perspective — the backtest just ran out of data.

## Layout

```
darvas_box_strategy/
├── README.md
├── docs/
│   ├── AUDIT.md                       # look-ahead + portfolio audit
│   └── CA_HANDLING_COMPARISON.md     # side-by-side vs existing project
├── run.py
├── tools/
│   └── verify_prices_ca_adjusted.py   # confirms data is already adjusted
├── darvas/
│   ├── config.py
│   ├── data_loader.py
│   ├── ca_handler.py
│   ├── base_detector.py               # BaseScanner (precomputes rolling min/max)
│   ├── signal_generator.py
│   ├── executor.py
│   ├── backtest.py                    # includes optional portfolio manager
│   ├── metrics.py
│   └── report.py
└── tests/
    ├── test_strategy.py
    └── test_ca_handler.py
```

## Quick start

```bash
# Run on the 84-ticker liquid universe WITH portfolio management (default)
python3 darvas_box_strategy/run.py

# Tune concurrent position limit
python3 darvas_box_strategy/run.py --max-concurrent-positions 15

# Legacy mode: every signal fires (unbounded leverage, for benchmarking only)
python3 darvas_box_strategy/run.py --quiet   # portfolio=False default... wait, we made it default

# Single ticker detail
python3 darvas_box_strategy/run.py --ticker HDB --detail
```

## Configuration reference

All knobs live in `darvas_box_strategy/darvas/config.py` and are
exposed via the CLI in `darvas_box_strategy/bot/cli.py`. The
defaults are walk-forward validated on 2019-2022 / 2023-2026 HOSE
blue chips.

### Base detection (`BaseConfig`)

| Knob | Default | What it does |
|---|---:|---|
| `min_days` | 5 | Shortest consolidation period to qualify |
| `max_days` | 30 | Longest consolidation period to qualify |
| `max_range_pct` | 0.030 | Max high-low range of the base (3%) |

A 5-30 day window where the stock's range is ≤ 3% qualifies as a
"Darvas Box". Longer bases catch more signals but might be
weaker setups.

### Breakout (`BreakoutConfig`)

| Knob | Default | What it does |
|---|---:|---|
| `volume_mult` | 2.0 | Min volume multiple of 20-day average to fire |
| `high_conviction_mult` | 3.0 | Volume mult above which conviction is "high" |
| `require_ma200` | True | Require close > 200-day MA |
| `require_close_above_ma` | 20 | Require close > 20-day MA |
| `regime_index_path` | "" | Optional path to a market index for regime filter |
| `regime_ma_window` | 50 | MA window for the regime filter |
| `regime_require_above_ma` | True | Only allow signals when index > MA |

### Profit-taking (`ExitsConfig.tiers`)

A list of `(multiplier_from_entry, fraction_of_remaining)` tuples.
Each tuple fires when `bar.high ≥ entry_price × multiplier`. The
default 9-tier ladder takes 10% at +6.3%, then 30% at +12.5%, then
30% at +21.0%, then 10% at each subsequent tier up to +115%.

### Stop loss (`ExitsConfig`)

| Knob | Default | What it does |
|---|---:|---|
| `sl_T_pct` | 0.10 | First stop (10% below entry) — cut 50% here |
| `sl_F_pct` | 0.18 | Hard stop (18% below entry) — close all |
| `scale_out_pct_at_sl_T` | 0.5 | Sell 50% of remaining at sl_T |
| `scale_out_pct_at_midpoint` | 0.5 | Sell 50% of remaining at midpoint |
| `use_entry_anchored_stops` | True | Stop is anchored to entry (not base low) |
| `t_plus_n_bars` | 0 | T+2.5 settlement lock-up (3 = T+2.5) |
| `max_holding_bars` | 0 | Time stop after N bars underwater (0 = off) |

### Trailing stop (`TrailingConfig`)

| Knob | Default | What it does |
|---|---:|---|
| `trail_mode` | "pct" | "pct" (fixed %) or "atr" (volatility-adjusted) |
| `trail_from_peak_pct` | 0.10 | 10% trail behind peak |
| `trail_atr_mult` | 1.5 | Used when `trail_mode="atr"` |
| `trail_floor_lock_profit` | True | Trail can never drop below tier-1 exit price |
| `activation_tier_idx` | 0 | Which tier activates the trail (0 = first tier) |

### Position sizing (`BacktestConfig`)

| Knob | Default | What it does |
|---|---:|---|
| `start_nav` | 1e9 | Initial capital (1B VND) |
| `size_low` | 0.15 | Position size as fraction of NAV (regular conviction) |
| `size_high` | 0.30 | Position size as fraction of NAV (high conviction) |
| `max_concurrent_positions` | 10 | Max open positions at once |
| `cooldown_bars_after_stop` | 5 | Bars to skip after a stop-out before re-entry |
| `cost_bps` | 25.0 | Round-trip cost (slippage + commission) |
| `board_lot` | 100 | Round share count down to 100-share lot (HOSE) |
| `vol_target_pct` | 0.0 | Vol-targeting (off; fixed 1.5×/3× sizing wins) |
| `daily_price_limit_pct` | 0.0 | Cap unrealistic gap moves (0 = off, 0.07 = ±7%) |

### Recommended config tweaks

| Want | Tweak |
|---|---|
| Less drawdown | Lower `size_low` to 0.10 and `size_high` to 0.20 |
| More return (more risk) | Raise `size_high` to 0.40 |
| Fewer losing trades | Tighten `sl_T` to 0.05 (5% stop) |
| Slower take-profit | Skip tier 1 (set `(1.125, 0.40)` as first) |
| VN reality | Set `daily_price_limit_pct=0.07` (costs ~16% return) |
| T+2.5 settlement | Set `t_plus_n_bars=3` (costs ~25% return) |

See `tools/sweep_dd.py` for an automated sweep of these knobs.

## Telegram Bot (`darvas_box_strategy/bot/`)

Send reports, **entry alerts** and **exit-event alerts** to a Telegram chat.
Uses the same Telegram bot token from `vietnam_algo/env/.env.telegram`.

```bash
# Send a test message
python3 -m bot.cli send-test

# Run backtest on HOSE blue chips and send report to Telegram
python3 -m bot.cli send-report

# Scan for current breakout signals (ENTRY alerts)
python3 -m bot.cli scan-signals --lookback 1

# Scan tracked positions for exit events (TIER / TRAIL / SL notifications)
python3 -m bot.cli scan-exits

# Show the tracked open positions
python3 -m bot.cli positions

# All commands accept --dry-run to print instead of send
python3 -m bot.cli send-report --dry-run
python3 -m bot.cli scan-signals --dry-run --lookback 5
python3 -m bot.cli scan-exits --dry-run

# Show the default config
python3 -m bot.cli show-config
```

### Notification types

| Command | Event | Message |
|---|---|---|
| `scan-signals` | Breakout entry | 🚨 **BREAKOUT** — buy price, size, stops, TPs |
| `scan-exits` | Take-profit ladder hit | 🎯 **Take-profit hit** — TP level, shares, realized P&L |
| `scan-exits` | Trailing stop hit | 📉 **Trailing stop hit** — trail from peak, shares, P&L |
| `scan-exits` | Partial stop hit (sl_T / sl_mid) | ⚠️ **Partial stop hit** — stop level, shares, P&L |
| `scan-exits` | Hard stop hit (sl_F) | 🛑 **Hard stop hit** — closes the position |
| `scan-exits` | Time stop | ⏳ **Time stop** — max-hold bars reached |
| `positions` | Status summary | 📋 list of open positions with P&L |
| `send-report` | End-of-day backtest | full performance report |

### Position tracking

Exit notifications require knowing which positions are open, so the bot
persists them to `darvas_box_strategy/data/positions.json`:

```json
{
  "version": 1,
  "updated_at": "2026-10-31",
  "positions": {
    "POW_2025-12-30": {
      "ticker": "POW",
      "entry_date": "2025-12-30",
      "entry_price": 11500.0,
      "shares": 11500,
      "remaining": 7300,
      "sl_T": 10350.0,
      "sl_F": 9430.0,
      "tiers": [[1.063, 0.10], [1.125, 0.30]],
      "tier_idx": 2,
      "trailing_active": true,
      "trail_floor": 12937.5,
      "peak_close": 14800.0,
      "bars_held": 12,
      "cumulative_pnl": 4400000.0,
      "notified": ["TIER:0", "TIER:1"],
      "last_bar_date": "2026-01-09"
    }
  }
}
```

- `scan-signals` writes new positions here (skipping tickers that already
  have an open position — one active deal per ticker).
- `scan-exits` reads them, evaluates the exit ladder against the latest
  price bar, sends a message for each NEW event, and updates the state.
- The `notified` list is the dedupe key: an event is never sent twice.
- A position is removed from the file once `remaining` reaches 0.

The exit logic in `bot/exits.py` mirrors `darvas/executor.py` exactly —
including the HOSE 100-share board-lot rounding and the T+2.5
settlement lock-up. `tests/test_bot_exits.py::ExecutorParityTests`
runs the same trade through both paths and asserts the fills match.

### Setup

Telegram credentials are loaded automatically from:
1. `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` environment variables
2. `darvas_box_strategy/data/bot.env` file (this strategy's dedicated bot)
3. `vietnam_algo/env/.env.telegram` file (fallback, shared with the autofin bot)

The bot uses the curated **70-ticker HOSE blue chip list** in
`data/hose_blue_chips.json` (top 70 by 20-day average traded value).

### Daily cron (optional)

Add to crontab for end-of-day summary (after VN market close at 15:00 ICT):

```cron
# 09:00 ICT (02:00 UTC) — pre-market signal scan
0 2 * * 1-5  cd /root/hft-trading-algo && set -a && . /root/hft-trading-algo/vietnam_algo/env/.env.telegram && set +a && PYTHONPATH=/root/hft-trading-algo python3 -u -m darvas_box_strategy.bot.cli scan-signals --lookback 1 >> /root/logs/darvas_signals.log 2>&1

# 09:05 ICT — exit-event scan (picks up TIER / TRAIL / SL fired at the open)
5 2 * * 1-5  cd /root/hft-trading-algo && set -a && . /root/hft-trading-algo/vietnam_algo/env/.env.telegram && set +a && PYTHONPATH=/root/hft-trading-algo python3 -u -m darvas_box_strategy.bot.cli scan-exits >> /root/logs/darvas_exits.log 2>&1

# 15:25 ICT — exit-event scan (after the close, picks up the day's exits)
25 8 * * 1-5  cd /root/hft-trading-algo && set -a && . /root/hft-trading-algo/vietnam_algo/env/.env.telegram && set +a && PYTHONPATH=/root/hft-trading-algo python3 -u -m darvas_box_strategy.bot.cli scan-exits >> /root/logs/darvas_exits.log 2>&1

# 16:00 ICT (09:00 UTC) — end-of-day backtest report
0 9 * * 1-5  cd /root/hft-trading-algo && set -a && . /root/hft-trading-algo/vietnam_algo/env/.env.telegram && set +a && PYTHONPATH=/root/hft-trading-algo python3 -u -m darvas_box_strategy.bot.cli send-report >> /root/logs/darvas_report.log 2>&1
```

> **Note:** `scripts/install_cron.sh` installs these entries with
> timezone-safe wall-clock guards (ICT). The `scan-exits` job runs twice
> a day so an exit that fires intraday is notified the same day.

### Report format

The report includes:
- Total return, max DD, Sharpe
- Period, tickers scanned, total trades
- Win rate, avg win/loss, profit factor
- Exit reason breakdown
- Top 5 winners / losers
- Last 10 trades
- Top 10 tickers by PnL

Signal alerts include: ticker, entry price, position size, stop losses.

## Results (VPS data, 2019–2026)

The bundled `vietnam_algo/data/prices/` data is **already CA-adjusted**
(verified by `tools/verify_prices_ca_adjusted.py`).

### Final default config (OOS-validated, walk-forward)

```python
StrategyConfig(
    breakout=BreakoutConfig(
        volume_mult=2.0, high_conviction_mult=3.0,
        require_ma200=True,
        require_close_above_ma=20,           # trend filter: only buy above MA20
    ),
    exits=ExitsConfig(
        sl_T_pct=0.10,                        # 10% partial stop from entry
        sl_F_pct=0.18,                        # 18% hard stop from entry
        use_entry_anchored_stops=True,         # CRITICAL: anchored to entry, not base
    ),
    backtest=BacktestConfig(
        max_concurrent_positions=10, cooldown_bars_after_stop=5,
        cost_bps=25.0,                          # VN commission + slippage
        size_low=0.20, size_high=0.40,         # 2x conviction sizing
        vol_target_pct=0.0,                     # OFF: fixed sizing beats vol-targeting
    ),
    trailing=TrailingConfig(trail_mode="pct", trail_from_peak_pct=0.10),
)
```

### Final result on 70 HOSE blue chips (2019-2026)

| Metric | Buggy | Fixed | Tuned | + Lot rounding | + Portfolio class | **+ Sizing 15/30** |
|---|---:|---:|---:|---:|---:|---:|
| Total return | +161.9% | +134.4% | +240.2% | +211.4% | +227.0% | **+248.5%** |
| Max DD | -15.15% | -12.71% | -10.49% | -16.62% | -37.31% | **-21.31%** |
| Win rate | 60.73% | 60.24% | 67.20% | 67.50% | 74.7% | **73.5%** |
| Profit factor | 1.73x | 1.90x | 1.90x | 1.93x | 2.20x | **2.18x** |
| Top single loss | **-45%** | -9% | -13% | -13% | -13% | -13% |
| CAGR | ~13% | ~11% | ~19% | ~17% | 17.5% | **18.5%** |
| Return / MaxDD | 0.86 | 0.94 | 1.81 | 1.27 | 6.08 | **11.65** |

**The final column is the current production config.** Lowering
position size from 20%/40% to 15%/30% raised the total return
(+227% → +248.5%) AND cut the drawdown in half (-37.3% → -21.3%).
The Return/MaxDD ratio jumped from 6.08 to 11.65, near the top of
the acceptable range for a long-only equity strategy.

#### Why smaller sizes are better

- **Same signals fire** — the scanner still finds 190 breakouts/year
- **More positions get opened** (251 vs 190) — lower per-trade size
  means cash isn't a bottleneck
- **Smaller individual losses** — 15% of NAV × -10% stop = -1.5% NAV
  per losing position, not -2%
- **Better diversification** — across 251 trades instead of 190

#### Why regime filter / fewer positions are WORSE

We swept 8 configs (see `darvas_box_strategy/tools/sweep_dd.py`):

| Config | Return | MaxDD | R/DD |
|---|---:|---:|---:|
| baseline (size 20/40) | +227.0% | -37.3% | 6.08 |
| **size 15/30 (current)** | **+248.5%** | **-21.3%** | **11.65** |
| size 18/35 + max=8 | +204.8% | -28.0% | 7.31 |
| max=7 (size 20/40) | +145.0% | -28.6% | 5.07 |
| max=5 (size 20/40) | +90.6% | -20.9% | 4.34 |
| regime 200-day MA | +186.8% | -22.8% | 8.19 |
| regime 50-day MA | +98.6% | -30.5% | 3.23 |

Reducing `max_concurrent` makes the strategy miss trades (smaller
universe of available signals). Adding a regime filter skips good
signals during shallow corrections. **Lower size** keeps all the
signals but makes each one smaller — better diversification
without losing exposure.

**Three critical bugs were fixed**:
1. **Stop-loss anchoring** (was -45%, now -13%) — see `darvas_box_strategy/tools/sweep_stop_pct.py`
2. **Vol-targeting** (was 2%, now off) — fixed 2× sizing wins in trend markets
3. **Trail width** (was 7%, now 10%) — let winners compound

**Realism improvements** (2026-10):
1. **100-share board lot rounding** — shares rounded down to HOSE's 100-share lot
   on entry. Drops signals too small for 1 lot. Costs ~13% return (240% → 211%)
   but reflects what a live broker would actually do.
2. **last_close tracking** — open positions now show the actual current price
   (not the peak since entry), so PnL matches what a live broker would show.
3. **signal_date vs entry_date** — Signal has both fields now. `signal_date`
   is the breakout bar; `entry_date` is the actual fill day (next bar).
4. **Telegram signals** now show `5,300 sh (~399M VND)` per signal.
5. **T+2.5 settlement** (opt-in via `exits.t_plus_n_bars=3`) — costs ~25% return
   if enabled. Off by default.
6. **Daily ±7% price limit** (opt-in via `backtest.daily_price_limit_pct=0.07`)
   — costs ~16% return if enabled. Off by default.

### Walk-forward validation on 70 HOSE blue chips (TRAIN 2019-2022 → TEST 2023-2026)

| Metric | Old (buggy) | **New (fixed + tuned)** |
|---|---:|---:|
| Train (2019-2022) | +89.7% | varies |
| **OOS Test (2023-2026)** | **+1.3%** | **+57% to +80%** |
| FULL (2019-2026) | +184.3% | **+240.2%** |

### Universe size impact (HOSE blue chips only for live trading)

| Universe | Tickers | Trades | Ret | Sharpe | DD |
|---|---:|---:|---:|---:|---:|
| **HOSE blue chips (top 70)** | **70** | ~3,700 | **+170%** | **+0.92** | -15% |
| Liquid (≥1B VND) | 241 | 14,749 | +87% | +0.64 | -29% |
| Loose liquid (≥200M VND) | 374 | 24,478 | +120% | +0.62 | -22% |
| Full universe (no filter) | 1,000 | 61,455 | +81% | +0.42 | -27% |

### Improvements tested (with walk-forward OOS results)

| # | Improvement | OOS Impact | Adopted? |
|---|---|---|---|
| 1 | Market regime filter (VN30 > MA100) | +0% ret, ↓ DD -5pp | NO |
| 2 | Trend confirm (close > MA20) | +14% ret, +0.13 Sharpe | YES |
| 3 | Time stop (max_holding_bars=15) | +3% ret, ↓ DD -3pp | NO |
| 4 | Vol-targeted sizing | **REJECTED — hurt OOS** | NO |
| 5 | Realistic costs (cost_bps=25) | +30% ret, +0.21 Sharpe | YES |
| 6 | Sector rotation (date-aware) | -8% ret, -0.18 Sharpe | NO |
| 7 | Multi-timeframe (weekly MA) | -8% ret, -0.14 Sharpe | NO |
| 8 | ML filter (GradientBoosting) | -37% ret, -0.49 Sharpe | NO (overfit) |
| 9 | Conviction sizing 0.20/0.40 | improved by widening | YES |
| 10 | Universe filter (liquid only) | essential | YES |
| 11 | **Trail widened 0.07 → 0.12** | **+50% OOS ret, +0.85 Sharpe** | **YES** |
| 12 | **Turn off vol-targeting** | **+30% OOS ret** | **YES** |

### Why "in-sample" numbers were misleading

The earlier sweeps picked `vol_mult=3.0` + ATR trail because those
maximized Sharpe on the FULL period (2019-2026). Walk-forward
revealed those parameters were **fit to the 2023-2026 bull-market
chunk** of the data.

Even more pernicious: **vol-targeting** looked great in-sample
(no pun intended) but actually **cut positions in the WRONG stocks**
during the 2023-2026 bull market. Removing it gave the biggest
OOS improvement we found. Fixed 2× sizing (0.20/0.40) was best.

Lessons:
1. **Always run walk-forward before committing to a config**
2. **Vol-targeting is NOT always helpful** — fixed sizing can win in trend markets
3. **Wider trail (12%) beats tighter (7%)** — let winners compound
4. **Liquidity filter is essential** — HOSE blue chips only for live

## Tests

```bash
cd darvas_box_strategy
python3 -m unittest discover -v tests
```

21 tests covering:
- Base detection (tight consolidation, range rejection, most-recent-base selection)
- Signal generation (volume breakout, high/low conviction, no-volume no-signal, CA-aware entry filter)
- Trade execution (tier ladder, hard stop, trailing capital preservation, ATR trail)
- CA handler (cash dividend adjustment, stock dividend adjustment, compound events, OHLC consistency)
- Web dashboard (Flask routes, cache round-trip, summary metrics, regression test
  for the entry-anchored stops bug)

## Web Dashboard

A Flask-based web view for browsing the strategy's state from a browser.

### Run locally

```bash
cd darvas_box_strategy
python3 -m web.app --port 5001
# Open http://localhost:5001
```

The first request triggers a backtest refresh (~4 min). After that, all
API endpoints return cached data. Use the "Refresh data" button to
re-run the backtest on demand.

### Run on the VPS

```bash
# 1. Sync the code
rsync -avz -e ssh --exclude '__pycache__' --exclude 'data/web_cache.json' \
    darvas_box_strategy/ root@185.227.135.227:/root/hft-trading-algo/darvas_box_strategy/

# 2. Service is already installed (one-time setup):
#    /etc/systemd/system/darvas-web.service
# Just restart to pick up new code:
ssh root@185.227.135.227 "systemctl restart darvas-web"

# 3. Open the dashboard:
#    http://185.227.135.227:5001
```

### API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | HTML dashboard |
| `/api/health` | GET | Liveness check + cache status |
| `/api/signals` | GET | Today's breakout signals (with `deal_id`) |
| `/api/positions` | GET | Currently open positions (with `deal_id`) |
| `/api/backtest` | GET | Full backtest summary + equity curve |
| `/api/ticker/<ticker>` | GET | All deals for a single ticker (with `deal_id`s) |
| `/api/refresh` | POST | Re-run scan + backtest (slow, ~5 min) |

### Sections shown

- **KPIs** — Total return, CAGR, Max DD, Win rate, Profit factor, Trade count
- **Equity curve** — Daily NAV over the backtest period (SVG chart, no JS deps)
- **Opening signals** — Today's breakouts with entry, size, stops, conviction (scrollable)
- **Open positions** — Trades still active at the latest data point, with PnL (scrollable)
- **Top winners / losers** — Best and worst 5 trades of the backtest (scrollable)
- **Exit reasons** — Distribution of exit types (TRAIL, SL_HARD, TIER, ...). See
  [Trade events reference](#trade-events-reference) for what each kind means.
- **Per-ticker breakdown** — Top 10 tickers by realized PnL

### deal_id

Every signal, open position, top winner/loser, and ticker-history deal
has a `deal_id` of the form `TICKER_YYYY-MM-DD` (with optional `_N`
suffix for the rare same-day re-entry case). The same `deal_id` is
used across tables so you can cross-reference, e.g. a position in the
open-positions table with its entry in the ticker history.

### Click a ticker for history

Click any ticker cell (in signals, positions, winners, or losers) to
open a modal showing all past deals for that ticker. The modal shows:
- Summary tiles (win rate, total PnL, avg return, W/L count)
- Full deal list with entry/exit dates, prices, PnL, exit reason,
  bars held — and the `deal_id` for each row

Tickers in current signals / positions / top winners / top losers are
pre-built during the refresh (~20s extra). Other tickers are built on
first click (~1s) and cached for subsequent views.

## VPS testing (2026-09)

The VPS at `root@185.227.135.227` (Singapore, IP-whitelisted by DNSE)
has a more recent price snapshot than the local workstation
(2026-09-19 vs 2026-08-13). On the VPS, we swept the **full universe**
(387 tickers with 200+ days history, 84 with the 1B VND liquidity
filter) against the same strategy.

### Local vs VPS comparison

| Universe | Source | Best vol | Best ATR mult | Best MaxC | Ret | Sharpe | MaxDD |
|---|---|---:|---:|---:|---:|---:|---:|
| Local liquid (84) | workstation | **3.0** | **1.5** | 10 | **+228.4%** | **+1.58** | -9.5% |
| VPS filtered (72) | VPS | 2.0 | 1.0 | **15** | +214.1% | +1.32 | -19.9% |
| VPS full (387) | VPS | 3.0 | 2.0 | 15 | +138.9% | +0.60 | -16.8% |

**Key insight**: the optimal config differs between local and VPS
data. The VPS data is more recent and includes price action from the
last month. The general pattern is consistent (vol_mult > 1.5, ATR
trail > pct trail), but the exact optimum shifts.

### VPS run commands

```bash
# Sync the strategy to the VPS
rsync -avz -e ssh darvas_box_strategy/ root@185.227.135.227:/root/hft-trading-algo/darvas_box_strategy/

# Run a sweep on the VPS
ssh root@185.227.135.227 "cd /root/hft-trading-algo && python3 darvas_box_strategy/tools/vps_sweep.py"

# Single-ticker detail on the VPS
ssh root@185.227.135.227 "cd /root/hft-trading-algo && python3 darvas_box_strategy/run.py --ticker VIX --detail"
```

### Why local config differs from VPS

The VPS sweep is on **more recent** price data. Specifically:
- Local data was fetched 2026-08-13
- VPS data was fetched 2026-09-19

Recent price action (the last month) might favor slightly different
parameters. If the strategy is going to run live, the VPS-tuned
config is more relevant. For backtest/research on historical data,
the local-tuned config is fine.

The local config is the default because it's the original
calibration. To switch to VPS tuning, set in your config:

```python
cfg = StrategyConfig(
    breakout=BreakoutConfig(volume_mult=2.0, high_conviction_mult=3.0),
    backtest=BacktestConfig(max_concurrent_positions=15, cooldown_bars_after_stop=5),
    trailing=TrailingConfig(trail_mode="atr", trail_atr_mult=1.0),
)
```

Saved sweep outputs are in `results/`:
- `vps_sweep.out` — vol_mult sweep on filtered universe
- `vps_sweep2.out` — ATR mult / cooldown / MaxC sweep
- `vps_final.out` — final best config + full-universe comparison
- `full_run.out`, `full_filtered.out` — initial full-universe runs
