"""autofin backtest — replay autofin.vn's closed deals as if we had mirrored them.

Two modes are provided, deliberately separated so that look-ahead
bias can never sneak into the headline number.

1. ``run_autofin_backtest`` — **strict, no look-ahead**. The headline
   numbers. For each closed deal we emit:

   - 1 BUY at ``price_Open`` on ``time_Open`` date
   - N partial-TP SELLs, one per ``numberOfProfit`` step. The step
     price is held constant at the OPEN price — we *do not* know in
     real time at what intra-position price autofin took the TP, so
     the closest honest assumption is "fill at the deal's reference
     open price". Each step pays its own round-trip cost (fee +
     slippage).
   - 1 final SELL at ``price_Closed`` on ``time_Closed`` date.

   Defaults: 0.12% fee per side (DNSE standard), 0.1% slippage per
   fill (round-trip ≈ 0.44% for a 2-step trade, ≈ 0.6% for a 4-step
   trade). NAV compounds chronologically so overlapping positions
   are honoured.

2. ``run_hindsight_variants`` — **biased, clearly labelled**. Runs
   the same dataset but with hindsight-only filters:

   - ``min_profit_steps``: skip deals whose final ``numberOfProfit``
     is below a threshold. This is a pure lookahead filter — in real
     time the bot does not know in advance how many TP steps a deal
     will eventually take.
   - ``stop_loss_pct``: model a stop-loss at X% below entry. The
     current implementation checks the *final* close price against
     the SL level, which is also hindsight (real SL needs intraday
     price data which we don't have).

   Use these only to bound the upper-edge of the strategy. The
   honest baseline is mode 1.

References:
  - autofin API doc (user spec 2026-09-06): ``/closing-signals``
    returns ``price_Open``, ``price_Closed``, ``time_Open``,
    ``time_Closed``, ``rateForNav`` (target portfolio allocation)
    and ``numberOfProfit`` (number of partial take-profit steps
    taken).
  - DNSE fee schedule (2026): 0.12% per side is the standard retail
    rate for non-margin equity trades.
"""
from __future__ import annotations

import csv
import math
import os
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .autofin_signals import AutofinClient
from .data import DEFAULT_DATA_DIR, write_json


DEFAULT_INITIAL_NAV = 1_000_000_000  # 1B VND
DEFAULT_BOARD_LOT = 100
# DNSE retail default (0.12% per side) — applied as a fraction of value
DEFAULT_FEE_PCT_PER_SIDE = 0.0012
# 0.1% per fill — round-trip is 0.2%, plus 0.4% across 2 partial-TP legs
DEFAULT_SLIPPAGE_PCT_PER_FILL = 0.001
MAX_PAGES = 20


@dataclass
class AutofinTrade:
    """One closed deal replayed as BUY → N×partial-TP SELL → final SELL."""

    deal_id: str
    symbol: str
    company_name: str
    open_date: str
    close_date: str
    holding_days: int
    price_open: float
    price_close: float
    quantity: int
    rate_for_nav: float
    target_value: float
    cost_basis: float
    proceeds: float
    fees_total: float
    slippage_total: float
    pnl_vnd: float
    pnl_pct: float
    nav_after: float
    n_profit_steps: int
    last_recomment: str
    # Per-step partial-TP ledger — one entry per `numberOfProfit` step.
    # Each step records the fill price we used (here = price_open since
    # we have no intra-position price), qty sold, fees, and slippage.
    partial_steps: list[dict[str, Any]] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def _round_to_lots(qty: float, board_lot: int = DEFAULT_BOARD_LOT) -> int:
    if qty <= 0:
        return 0
    return (int(qty) // board_lot) * board_lot


def _parse_dt(s: str) -> datetime | None:
    """Parse autofin date string. Format: 'MM/DD/YYYY HH:MM:SS' (ICT)."""
    if not s:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def fetch_all_closed(
    client: AutofinClient,
    max_pages: int = MAX_PAGES,
    use_full_history: bool = True,
    progress: bool = False,
) -> list[dict[str, Any]]:
    """Fetch all closed deals for the backtest.

    ``use_full_history=True`` (default): fetches the FULL per-symbol
    deal history by combining ``/closing-signals`` (to enumerate
    symbols) with ``/signal-deals?symbol=X`` (to fetch all deals per
    symbol). This typically returns 10-20x more deals than just
    ``/closing-signals`` alone (which only returns the most recent
    closed deal per symbol).

    ``use_full_history=False``: legacy behaviour — paginate
    ``/closing-signals`` only. Each item is one deal (one row per
    unique symbol).

    For 250+ symbols, ``use_full_history=True`` can take 5-10 minutes.
    """
    if use_full_history:
        return client.fetch_all_deals_pages(
            symbols=None, page_size=50, closed_only=True, progress=progress,
        )
    # Legacy: /closing-signals only
    all_items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        items = client.fetch_closing(page_size=50, page_index=page)
        all_items.extend(items)
        if len(items) < 50:
            break
    return all_items


def load_closed_deals_from_json(path: Path) -> list[dict[str, Any]]:
    """Load cached autofin closing-signals records from a JSON file.

    The JSON must be a list of autofin raw deal dicts. Useful for
    offline testing / backtest replay without hitting the autofin API.
    """
    import json
    return json.loads(Path(path).read_text())


def _simulate_trade(
    *,
    qty: int,
    price_open: float,
    price_close: float,
    n_profit_steps: int,
    fee_pct_per_side: float,
    slippage_pct_per_fill: float,
    slippage_split: float = 0.5,
) -> dict[str, Any]:
    """Compute the P&L of one trade with N partial-TP steps and per-step costs.

    Per fill (entry, each partial, final close):
      - BUY: fill at ``price * (1 + slippage_pct/2)``, pay fee_pct × value
      - SELL: fill at ``price * (1 - slippage_pct/2)``, pay fee_pct × value

    We split slippage equally: half adverse on entry, half adverse on
    exit. ``slippage_split`` (0..1) lets a caller move more of the
    slippage onto one side if they prefer a particular bias.

    Partial-TP fill prices use **linear interpolation** between
    ``price_open`` and ``price_close``: leg k (k=1..N) fills at
    ``price_open + k/(N+1) × (price_close - price_open)``, the
    final leg fills at ``price_close``. This is a defensible
    default when we have no other reference for intra-position
    prices — it captures partial gains proportional to "time into
    the trade" without lookahead.

    Each partial sells ``qty // (n_profit_steps + 1)`` shares; the
    remainder is closed on the final leg.

    Returns a dict with cost_basis, proceeds, fees_total, slippage_total,
    pnl, partial_steps ledger.
    """
    half = slippage_pct_per_fill * slippage_split
    eff_open = price_open * (1 + half)
    eff_close = price_close * (1 - half)
    delta = price_close - price_open
    n_legs = n_profit_steps + 1  # N partials + 1 final
    per_leg_base = qty // n_legs
    rem = qty - per_leg_base * n_legs
    leg_qtys = [per_leg_base] * n_profit_steps  # partial sells
    final_qty = per_leg_base + rem  # final leg absorbs the remainder

    # entry: BUY the full qty (we hold all shares from T0 to T_close)
    entry_value = qty * eff_open
    entry_fee = entry_value * fee_pct_per_side
    entry_slip = (eff_open - price_open) * qty
    cash_out = entry_value + entry_fee
    cash_in = 0.0
    fees_total = entry_fee
    slippage_total = entry_slip
    partial_steps: list[dict[str, Any]] = []

    # N partial-TP SELLs during the holding period. Price linearly
    # interpolated between open and close: leg k fills at
    # open + k/(N+1) * delta. SELL-side slippage applied.
    for k in range(1, n_profit_steps + 1):
        sq = leg_qtys[k - 1]
        if sq <= 0:
            continue
        ref_price = price_open + (k / n_legs) * delta
        fill = ref_price * (1 - half)  # SELL-side slippage
        value = sq * fill
        fee = value * fee_pct_per_side
        slip = (ref_price - fill) * sq
        cash_in += value - fee
        fees_total += fee
        slippage_total += slip
        partial_steps.append({
            "step": k,
            "qty": sq,
            "ref_price": ref_price,
            "fill_price": fill,
            "fees": fee,
            "slippage": slip,
        })

    # final close at price_close (with SELL-side slippage)
    if final_qty > 0:
        final_value = final_qty * eff_close
        final_fee = final_value * fee_pct_per_side
        final_slip = (price_close - eff_close) * final_qty
        cash_in += final_value - final_fee
        fees_total += final_fee
        slippage_total += final_slip

    pnl = cash_in - cash_out
    pnl_pct = pnl / cash_out if cash_out > 0 else 0.0

    return {
        "cost_basis": cash_out,
        "proceeds": cash_in,
        "fees_total": fees_total,
        "slippage_total": slippage_total,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "partial_steps": partial_steps,
        "leg_qtys": leg_qtys + [final_qty],
    }


def run_autofin_backtest(
    initial_nav: float = DEFAULT_INITIAL_NAV,
    board_lot: int = DEFAULT_BOARD_LOT,
    fee_pct_per_side: float = DEFAULT_FEE_PCT_PER_SIDE,
    slippage_pct_per_fill: float = DEFAULT_SLIPPAGE_PCT_PER_FILL,
    max_pages: int = MAX_PAGES,
    use_full_history: bool = True,
    client: AutofinClient | None = None,
) -> dict[str, Any]:
    """Strict, no-look-ahead replay of autofin's closed deals.

    For each deal we assume we mirrored autofin's intent: BUY at
    ``price_Open`` with ``NAV * rateForNav`` of capital, then mirror
    N partial-TP SELLs (``numberOfProfit`` steps), then SELL the
    remainder at ``price_Closed``. Each fill pays its own fees +
    slippage. NAV compounds chronologically.

    No hindsight filters are applied. The result is the most
    *defensible* upper bound on the strategy — actual live trading
    will be worse due to entry-timing slippage, partial-fill risk,
    and signal lag.

    Use ``run_hindsight_variants()`` to see what specific hindsight
    filters would have done; those numbers should not be reported
    as live-trading expectations.
    """
    if client is None:
        client = AutofinClient(
            email=os.environ["AUTOFIN_EMAIL"],
            password=os.environ["AUTOFIN_PASSWORD"],
        )
    closed = fetch_all_closed(client, max_pages=max_pages, use_full_history=use_full_history)
    # sort by open time (parsed) so NAV evolves chronologically
    def _sort_key(d: dict[str, Any]) -> datetime:
        sd = d.get("statusDeal", {}) or {}
        return _parse_dt(str(sd.get("time_Open") or "")) or datetime.min
    closed.sort(key=_sort_key)

    nav = float(initial_nav)
    peak_nav = float(initial_nav)
    max_dd_pct = 0.0
    trades: list[AutofinTrade] = []
    skipped: list[dict[str, Any]] = []
    wins = 0
    losses = 0
    pnl_pcts: list[float] = []
    weighted_returns: list[tuple[float, int]] = []  # (pnl_pct, holding_days)

    for d in closed:
        sd = d.get("statusDeal", {}) or {}
        sym = str(d.get("symbol") or "?").upper()
        co = str(d.get("companyName") or "")
        p_open = float(sd.get("price_Open") or 0)
        p_close = float(sd.get("price_Closed") or 0)
        days = int(sd.get("timeHolding") or 0)
        n_profit = int(d.get("numberOfProfit") or 0)
        rate = float(d.get("rateForNav") or 0)
        did = str(d.get("_id") or "")
        rec = str(d.get("lastRecomment") or "")
        t_open = str(sd.get("time_Open") or "")
        t_close = str(sd.get("time_Closed") or "")

        if p_open <= 0 or p_close <= 0 or rate <= 0 or nav <= 0:
            skipped.append({
                "deal_id": did, "symbol": sym,
                "reason": "missing price or rate" if (p_open <= 0 or p_close <= 0 or rate <= 0) else "nav=0",
            })
            continue

        # size the position (no hindsight filter — every deal gets a chance)
        target_value = nav * rate
        raw_qty = target_value / p_open
        qty = _round_to_lots(raw_qty, board_lot)
        if qty < board_lot:
            skipped.append({
                "deal_id": did, "symbol": sym,
                "reason": f"qty={qty} < {board_lot} lot",
            })
            continue

        sim = _simulate_trade(
            qty=qty,
            price_open=p_open,
            price_close=p_close,
            n_profit_steps=n_profit,
            fee_pct_per_side=fee_pct_per_side,
            slippage_pct_per_fill=slippage_pct_per_fill,
        )
        nav += sim["pnl"]

        if sim["pnl"] > 0:
            wins += 1
        elif sim["pnl"] < 0:
            losses += 1
        pnl_pcts.append(sim["pnl_pct"])
        weighted_returns.append((sim["pnl_pct"], max(days, 1)))

        peak_nav = max(peak_nav, nav)
        dd_pct = (peak_nav - nav) / peak_nav if peak_nav > 0 else 0
        max_dd_pct = max(max_dd_pct, dd_pct)

        trades.append(AutofinTrade(
            deal_id=did,
            symbol=sym,
            company_name=co,
            open_date=t_open,
            close_date=t_close,
            holding_days=days,
            price_open=p_open,
            price_close=p_close,
            quantity=qty,
            rate_for_nav=rate,
            target_value=target_value,
            cost_basis=sim["cost_basis"],
            proceeds=sim["proceeds"],
            fees_total=sim["fees_total"],
            slippage_total=sim["slippage_total"],
            pnl_vnd=sim["pnl"],
            pnl_pct=sim["pnl_pct"],
            nav_after=nav,
            n_profit_steps=n_profit,
            last_recomment=rec,
            partial_steps=sim["partial_steps"],
        ))

    final_nav = nav
    total_pnl = final_nav - initial_nav
    total_pnl_pct = total_pnl / initial_nav if initial_nav > 0 else 0
    win_rate = wins / len(trades) if trades else 0
    avg_return = statistics.mean(pnl_pcts) if pnl_pcts else 0
    median_return = statistics.median(pnl_pcts) if pnl_pcts else 0
    stdev_return = statistics.stdev(pnl_pcts) if len(pnl_pcts) > 1 else 0

    # annualized return based on date span of trades
    if trades:
        first = _parse_dt(trades[0].open_date)
        last = _parse_dt(trades[-1].close_date)
        if first and last and last > first:
            span_days = (last - first).days or 1
            years = span_days / 365.0
            annualized = (final_nav / initial_nav) ** (1.0 / years) - 1 if (years > 0 and final_nav > 0 and initial_nav > 0) else 0
        else:
            annualized = 0
            years = 0
            span_days = 0
    else:
        annualized = 0
        years = 0
        span_days = 0

    # Time-weighted (holding-period-weighted) Sharpe. We treat each
    # trade as one observation whose return is annualized by
    # 365/holding_days, then compute mean/stdev across trades and
    # annualize the Sharpe by sqrt(N_trades_per_year). This avoids
    # the sqrt(365) shortcut which is only valid for daily data.
    if pnl_pcts and stdev_return > 0 and years > 0:
        annualized_returns = [r * (365.0 / max(d, 1)) for r, d in weighted_returns]
        ann_mean = statistics.mean(annualized_returns)
        ann_stdev = statistics.stdev(annualized_returns) if len(annualized_returns) > 1 else 0
        sharpe = ann_mean / ann_stdev if ann_stdev > 0 else 0
    else:
        sharpe = 0

    total_fees = sum(t.fees_total for t in trades)
    total_slippage = sum(t.slippage_total for t in trades)

    return {
        "initial_nav": initial_nav,
        "final_nav": final_nav,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "annualized_pct": annualized,
        "years": years,
        "span_days": span_days,
        "trade_count": len(trades),
        "skipped_count": len(skipped),
        "win_count": wins,
        "loss_count": losses,
        "win_rate_pct": win_rate,
        "max_drawdown_pct": max_dd_pct,
        "avg_return_pct": avg_return,
        "median_return_pct": median_return,
        "stdev_return_pct": stdev_return,
        "sharpe_time_weighted": sharpe,
        "total_fees": total_fees,
        "total_slippage": total_slippage,
        "fee_pct_per_side": fee_pct_per_side,
        "slippage_pct_per_fill": slippage_pct_per_fill,
        "mode": "strict_no_lookahead",
        "trades": trades,
        "skipped": skipped,
    }


def run_hindsight_variants(
    initial_nav: float = DEFAULT_INITIAL_NAV,
    board_lot: int = DEFAULT_BOARD_LOT,
    fee_pct_per_side: float = DEFAULT_FEE_PCT_PER_SIDE,
    slippage_pct_per_fill: float = DEFAULT_SLIPPAGE_PCT_PER_FILL,
    max_pages: int = MAX_PAGES,
    min_profit_steps_values: tuple[int, ...] = (1, 2, 3),
    stop_loss_pct_values: tuple[float, ...] = (0.05, 0.10, 0.15),
    use_full_history: bool = True,
    client: AutofinClient | None = None,
) -> dict[str, Any]:
    """Run the same dataset under multiple hindsight-only filters.

    Returns a dict mapping filter name → summary metrics. **None of
    these numbers should be reported as live-trading expectations.**
    They bound the upper edge of what the strategy *could* have done
    if it had access to future information.

    Filters:
      - ``min_profit_steps=N``: only count deals whose final
        ``numberOfProfit`` ≥ N. In real time the bot does not know
        this in advance.
      - ``stop_loss_pct=X``: when the final close would have triggered
        a stop at X% below open, model the exit at the SL level. Real
        SL needs intraday data which we don't have, so this is a
        worst-case assumption.
    """
    if client is None:
        client = AutofinClient(
            email=os.environ["AUTOFIN_EMAIL"],
            password=os.environ["AUTOFIN_PASSWORD"],
        )
    closed = fetch_all_closed(
        client, max_pages=max_pages, use_full_history=use_full_history,
    )

    def _sort_key(d: dict[str, Any]) -> datetime:
        sd = d.get("statusDeal", {}) or {}
        return _parse_dt(str(sd.get("time_Open") or "")) or datetime.min
    closed.sort(key=_sort_key)

    def _run_filter(predicate) -> dict[str, Any]:
        nav = float(initial_nav)
        peak = float(initial_nav)
        max_dd = 0.0
        wins = 0
        losses = 0
        n = 0
        returns: list[float] = []
        skipped_count = 0
        for d in closed:
            sd = d.get("statusDeal", {}) or {}
            sym = str(d.get("symbol") or "?").upper()
            p_open = float(sd.get("price_Open") or 0)
            p_close = float(sd.get("price_Closed") or 0)
            n_profit = int(d.get("numberOfProfit") or 0)
            rate = float(d.get("rateForNav") or 0)
            if not predicate(d, p_open, p_close, n_profit, rate, nav):
                skipped_count += 1
                continue
            if p_open <= 0 or p_close <= 0 or rate <= 0 or nav <= 0:
                skipped_count += 1
                continue
            target = nav * rate
            qty = _round_to_lots(target / p_open, board_lot)
            if qty < board_lot:
                skipped_count += 1
                continue
            sim = _simulate_trade(
                qty=qty,
                price_open=p_open,
                price_close=p_close,
                n_profit_steps=n_profit,
                fee_pct_per_side=fee_pct_per_side,
                slippage_pct_per_fill=slippage_pct_per_fill,
            )
            nav += sim["pnl"]
            if sim["pnl"] > 0:
                wins += 1
            elif sim["pnl"] < 0:
                losses += 1
            returns.append(sim["pnl_pct"])
            n += 1
            peak = max(peak, nav)
            max_dd = max(max_dd, (peak - nav) / peak if peak > 0 else 0)
        total_pct = (nav - initial_nav) / initial_nav if initial_nav > 0 else 0
        win_rate = wins / n if n else 0
        avg = statistics.mean(returns) if returns else 0
        stdev = statistics.stdev(returns) if len(returns) > 1 else 0
        return {
            "trades": n,
            "skipped": skipped_count,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate,
            "total_pnl_pct": total_pct,
            "final_nav": nav,
            "max_drawdown_pct": max_dd,
            "avg_return_pct": avg,
            "stdev_return_pct": stdev,
            "sharpe": (avg / stdev) if stdev > 0 else 0,
        }

    variants: dict[str, Any] = {}
    for mps in min_profit_steps_values:
        def _p(d, p_o, p_c, n_p, r, nav_, _mps=mps):
            return n_p >= _mps
        key = f"min_profit_steps>={mps}"
        variants[key] = _run_filter(_p)
    for sl in stop_loss_pct_values:
        def _p(d, p_o, p_c, n_p, r, nav_, _sl=sl):
            if p_o <= 0:
                return False
            return p_c > p_o * (1 - _sl)
        key = f"stop_loss_only>={sl:.0%}"
        variants[key] = _run_filter(_p)
    return {
        "mode": "hindsight_variants",
        "warning": (
            "These numbers use FUTURE information (final numberOfProfit, "
            "final close price) to filter trades. They are upper bounds, "
            "not expectations."
        ),
        "variants": variants,
    }


def write_backtest_outputs(
    result: dict[str, Any],
    out_dir: Path,
    prefix: str = "autofin_backtest",
) -> tuple[Path, Path]:
    """Write JSON summary + CSV per-trade ledger."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{prefix}.json"
    csv_path = out_dir / f"{prefix}_trades.csv"

    json_payload = {k: v for k, v in result.items() if k != "trades"}
    json_payload["trades"] = [t.to_row() for t in result["trades"]]
    write_json(json_path, json_payload)

    if result["trades"]:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            fieldnames = list(result["trades"][0].to_row().keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in result["trades"]:
                writer.writerow(t.to_row())

    return json_path, csv_path


def format_backtest_report(result: dict[str, Any]) -> str:
    """Telegram-friendly summary of the backtest result."""
    lines: list[str] = []
    lines.append("🤖 Vietnam Algo: autofin_signal BACKTEST (strict, no look-ahead)")
    lines.append(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} ICT")
    lines.append("")
    lines.append("💼 Backtest assumptions")
    lines.append(f"  • initial NAV: {result['initial_nav']:,.0f} VND")
    lines.append(f"  • board lot: {DEFAULT_BOARD_LOT}")
    lines.append(
        f"  • fee per side: {result['fee_pct_per_side']:.4%}, "
        f"slippage per fill: {result['slippage_pct_per_fill']:.4%}"
    )
    lines.append(
        f"  • source: autofin.vn /closing-signals, "
        f"all {result['trade_count'] + result['skipped_count']} closed deals"
    )
    lines.append("")
    lines.append("📈 Summary")
    lines.append(
        f"  • trades: {result['trade_count']}  "
        f"skipped: {result['skipped_count']}  "
        f"win: {result['win_count']}  loss: {result['loss_count']}"
    )
    lines.append(
        f"  • final NAV: {result['final_nav']:,.0f} VND  "
        f"({result['total_pnl_pct']:+.1%})"
    )
    lines.append(
        f"  • total P&L: {result['total_pnl']:+,.0f} VND  "
        f"over {result['years']:.2f}y → annualized {result['annualized_pct']:+.1%}"
    )
    lines.append(
        f"  • win rate: {result['win_rate_pct']:.1%}  "
        f"avg return: {result['avg_return_pct']:+.1%}  "
        f"median: {result['median_return_pct']:+.1%}"
    )
    lines.append(
        f"  • max drawdown: {result['max_drawdown_pct']:.1%}  "
        f"Sharpe (time-weighted): {result['sharpe_time_weighted']:.2f}"
    )
    lines.append(
        f"  • total fees: {result['total_fees']:,.0f} VND  "
        f"slippage: {result['total_slippage']:,.0f} VND"
    )
    lines.append("")
    lines.append("🏆 Top 5 winners")
    winners = sorted(result["trades"], key=lambda t: -t.pnl_vnd)[:5]
    for t in winners:
        lines.append(
            f"  • {t.symbol:6s}  {t.pnl_pct:+6.1%}  "
            f"{t.pnl_vnd:+12,.0f} VND  ({t.holding_days}d, {t.n_profit_steps} TP)"
        )
    if result["trades"]:
        lines.append("")
        lines.append("💀 Top 5 losers")
        losers = sorted(result["trades"], key=lambda t: t.pnl_vnd)[:5]
        for t in losers:
            lines.append(
                f"  • {t.symbol:6s}  {t.pnl_pct:+6.1%}  "
                f"{t.pnl_vnd:+12,.0f} VND  ({t.holding_days}d, {t.n_profit_steps} TP)"
            )
    return "\n".join(lines)
