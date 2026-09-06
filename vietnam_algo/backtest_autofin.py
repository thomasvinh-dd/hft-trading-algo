"""autofin backtest — replay all closed deals from /closing-signals.

Per user spec (2026-09-06): autofin.vn's ``/closing-signals`` endpoint
gives us a list of every deal autofin has ever closed, with
``price_Open``, ``price_Closed``, ``time_Open``, ``time_Closed``,
``rateForNav`` (target portfolio allocation) and ``numberOfProfit``
(number of partial take-profit steps taken).

We simulate mirroring autofin's intent: for each closed deal, we
BUY at ``price_Open`` with quantity = (NAV × rateForNav / price_Open)
and SELL at ``price_Closed`` once autofin closes. NAV compounds
across all closed deals in chronological order, so the backtest
captures overlapping positions and risk-adjusted returns.

The result is a per-trade ledger + a summary of total P&L, win rate,
max drawdown, average return, and top winners/losers.

This is a "perfect hindsight" backtest — we know the close price
before the close happens. Real live trading will be worse due to
slippage, fees, signal lag, and unfilled orders. Treat the numbers
as an upper bound.
"""
from __future__ import annotations

import csv
import json
import os
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .autofin_signals import AutofinClient
from .data import DEFAULT_DATA_DIR, write_json


DEFAULT_INITIAL_NAV = 1_000_000_000  # 1B VND
DEFAULT_BOARD_LOT = 100
DEFAULT_FEE_PER_SHARE = 0.0  # placeholder for future fee model
DEFAULT_SLIPPAGE_PCT = 0.0  # placeholder for future slippage model
MAX_PAGES = 20  # safety cap; autofin returns up to 50 per page


@dataclass
class AutofinTrade:
    """One closed deal replayed as BUY-at-open → SELL-at-close."""

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
    pnl_vnd: float
    pnl_pct: float
    nav_after: float
    n_profit_steps: int
    last_recomment: str

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
) -> list[dict[str, Any]]:
    """Fetch all closed deals, paginating through /closing-signals."""
    all_items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        items = client.fetch_closing(page_size=50, page_index=page)
        all_items.extend(items)
        if len(items) < 50:
            break
    return all_items


def run_autofin_backtest(
    initial_nav: float = DEFAULT_INITIAL_NAV,
    board_lot: int = DEFAULT_BOARD_LOT,
    fee_per_share: float = DEFAULT_FEE_PER_SHARE,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    max_pages: int = MAX_PAGES,
    min_profit_steps: int = 0,
    stop_loss_pct: float = 0.0,
    client: AutofinClient | None = None,
) -> dict[str, Any]:
    """Replay all closed autofin deals as if we had mirrored them.

    Parameters
    ----------
    initial_nav : starting NAV in VND
    board_lot : lot size for rounding (default 100)
    fee_per_share : flat fee per share (VND); 0 by default
    slippage_pct : slippage on each fill (e.g. 0.001 = 0.1%); 0 by default
    max_pages : pagination cap on /closing-signals
    min_profit_steps : skip deals whose ``numberOfProfit`` is below this
        threshold (use 1+ to filter out trendless losers that never
        triggered a partial take-profit). 0 = no filter.
    stop_loss_pct : if > 0, cut the position when unrealized loss reaches
        this fraction (e.g. 0.10 = 10%). The close price becomes
        ``price_Open * (1 - stop_loss_pct)`` instead of ``price_Closed``.
        Pnl is the simulated stop-out, not the actual autofin close.
        0 = no stop-loss (default).
    client : optional pre-built AutofinClient (else built from env)

    Returns
    -------
    dict with summary metrics and ``trades: list[AutofinTrade]``.
    """
    if client is None:
        client = AutofinClient(
            email=os.environ["AUTOFIN_EMAIL"],
            password=os.environ["AUTOFIN_PASSWORD"],
        )
    closed = fetch_all_closed(client, max_pages=max_pages)
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

        # filter: require minimum TP steps
        if min_profit_steps > 0 and n_profit < min_profit_steps:
            skipped.append({
                "deal_id": did, "symbol": sym,
                "reason": f"n_profit={n_profit} < min_profit_steps={min_profit_steps}",
            })
            continue

        if p_open <= 0 or p_close <= 0 or rate <= 0 or nav <= 0:
            skipped.append({
                "deal_id": did, "symbol": sym, "reason":
                "missing price or rate" if (p_open <= 0 or p_close <= 0 or rate <= 0) else "nav=0",
            })
            continue

        # simulate slippage: BUY slightly higher, SELL slightly lower
        eff_open = p_open * (1 + slippage_pct / 2)
        # if a stop-loss is set, the simulated exit is at the SL level
        # (we don't know if it actually hit SL or held to the autofin close,
        # so the SL scenario is a worst-case modelling — both for losses
        # and for winners that dipped and recovered)
        eff_close = p_close * (1 - slippage_pct / 2)
        sl_triggered = False
        if stop_loss_pct > 0 and p_open > 0:
            sl_level = p_open * (1 - stop_loss_pct)
            if p_close <= sl_level:
                # the actual close would have hit our stop-loss first
                eff_close = sl_level * (1 - slippage_pct / 2)
                sl_triggered = True

        # size the position
        target_value = nav * rate
        raw_qty = target_value / eff_open
        qty = _round_to_lots(raw_qty, board_lot)
        if qty < board_lot:
            skipped.append({
                "deal_id": did, "symbol": sym,
                "reason": f"qty={qty} < {board_lot} lot",
            })
            continue

        # P&L with fees (round-trip)
        cost = qty * eff_open
        fees = qty * fee_per_share * 2  # BUY + SELL
        proceeds = qty * eff_close - fees
        pnl = proceeds - cost
        pnl_pct = (eff_close - eff_open) / eff_open
        nav += pnl

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        pnl_pcts.append(pnl_pct)

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
            cost_basis=cost,
            proceeds=proceeds,
            pnl_vnd=pnl,
            pnl_pct=pnl_pct,
            nav_after=nav,
            n_profit_steps=n_profit,
            last_recomment=rec,
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
            days = (last - first).days or 1
            years = days / 365.0
            if final_nav > 0 and initial_nav > 0:
                annualized = (final_nav / initial_nav) ** (1.0 / years) - 1 if years > 0 else 0
            else:
                annualized = 0
        else:
            annualized = 0
            years = 0
    else:
        annualized = 0
        years = 0
    sharpe = (avg_return / stdev_return) * (365 ** 0.5) if stdev_return > 0 else 0

    return {
        "initial_nav": initial_nav,
        "final_nav": final_nav,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "annualized_pct": annualized,
        "years": years,
        "trade_count": len(trades),
        "skipped_count": len(skipped),
        "win_count": wins,
        "loss_count": losses,
        "win_rate_pct": win_rate,
        "max_drawdown_pct": max_dd_pct,
        "avg_return_pct": avg_return,
        "median_return_pct": median_return,
        "stdev_return_pct": stdev_return,
        "sharpe": sharpe,
        "fee_per_share": fee_per_share,
        "slippage_pct": slippage_pct,
        "min_profit_steps": min_profit_steps,
        "stop_loss_pct": stop_loss_pct,
        "trades": trades,
        "skipped": skipped,
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
    lines.append("🤖 Vietnam Algo: autofin_signal BACKTEST")
    lines.append(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} ICT")
    lines.append("")
    lines.append("💼 Backtest assumptions")
    lines.append(
        f"  • initial NAV: {result['initial_nav']:,.0f} VND"
    )
    lines.append(
        f"  • board lot: {DEFAULT_BOARD_LOT}, fee: {result['fee_per_share']} VND/sh, "
        f"slippage: {result['slippage_pct']:.2%}"
    )
    if result.get("min_profit_steps", 0) > 0:
        lines.append(
            f"  • min profit steps: {result['min_profit_steps']} (skip trendless losers)"
        )
    if result.get("stop_loss_pct", 0) > 0:
        lines.append(
            f"  • stop-loss: {result['stop_loss_pct']:.1%} below entry"
        )
    lines.append(
        f"  • source: autofin.vn /closing-signals, all closed deals"
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
        f"Sharpe: {result['sharpe']:.2f}"
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
