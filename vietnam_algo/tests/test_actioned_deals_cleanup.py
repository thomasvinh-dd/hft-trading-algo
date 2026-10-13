"""Tests for the actioned-deals cleanup logic in
``vietnam_algo.algos.autofin_signal.AutofinSignalAdapter._prune_terminal_unfilled_deals``.

User audit 2026-10-13: "previous day we still make the deal of ABB
work so we should consider it as fresh on next day, no need to
reset manually like this."

The 2026-10-12 ABB BUY at 17,500 was placed but expired at EOD
without filling. The deal's ``actioned_deals`` entry persisted
across the bot restart, so on 2026-10-13 the morning poll filtered
ABB out as "already actioned" — even though we never actually held
the position. The operator had to manually reset the actioned
state to re-attempt.

Fix: ``_prune_terminal_unfilled_deals`` derives the actioned set
from the persistent disk list PLUS:

  1. deals we still hold (``our_deals[did]._our_qty > 0``)
  2. deals with at least one non-terminal order on the books
     (open / partial / submitting / unknown / prepared)

and REMOVES deals whose latest order is in a terminal state
without a fill (expired / cancelled / rejected / closed / dry_run
/ skipped). After this, ABB (which has only an "expired" order on
the books) is automatically re-actionable next day.

These tests pin the behavior.
"""
from __future__ import annotations

from dataclasses import replace

import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.algos.autofin_signal import AutofinSignalAdapter
from vietnam_algo.libs.order_ledger import LedgerOrder


def _ledger_order(
    deal_id: str,
    status: str,
    updated_at: str = "2026-10-12T08:00:00Z",
    symbol: str = "ABB",
) -> LedgerOrder:
    return LedgerOrder(
        runId="2026-10-12",
        sourceOrderId=12345,
        sourceDate="2026-10-12",
        symbol=symbol,
        side="NB",
        quantity=100,
        price=17500,
        reason=f"autofin_buy:deal#{deal_id}:nav30pct:Khuyến nghị đầu tư",
        status=status,
        dnseOrderId=f"dnse-{deal_id}",
        createdAt="2026-10-12T07:00:00Z",
        updatedAt=updated_at,
    )


def _write_ledger(tmp_path: Path, items: list[LedgerOrder]) -> Path:
    """Write a minimal ``order_ledger.json`` to ``tmp_path/live/order_ledger.json``."""
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "updatedAt": "2026-10-12T08:00:00Z",
        "count": len(items),
        "items": [item.__dict__ for item in items],
    }
    (live_dir / "order_ledger.json").write_text(json.dumps(payload, indent=2, default=str))
    return tmp_path


def test_empty_actioned_returns_empty(tmp_path) -> None:
    """If the persistent actioned set is empty, return empty (no-op)."""
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(set(), {}, tmp_path)
    assert result == set()


def test_held_deal_is_kept_even_if_no_orders(tmp_path) -> None:
    """A deal we currently hold is ALWAYS actioned, even if there
    are no orders in the ledger. This handles the case where the
    order was placed long ago and the ledger was wiped (or this is
    a recovery from a partial state).
    """
    our_deals = {"1785456000:HDB": {"_our_qty": 100}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {"1785456000:HDB"}, our_deals, tmp_path,
    )
    assert "1785456000:HDB" in result


def test_expired_order_no_position_releases_deal(tmp_path) -> None:
    """The 2026-10-12 ABB case: order placed, expired at EOD, no fill.
    No position. The deal should be REMOVED from actioned so it can
    be re-attempted next day.
    """
    did = "1789344000:ABB"
    _write_ledger(tmp_path, [_ledger_order(did, "expired")])
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, {}, tmp_path,
    )
    assert did not in result


def test_rejected_order_no_position_releases_deal(tmp_path) -> None:
    """Rejected (server-side rejection, e.g. margin / symbol invalid)
    should also release the deal — we never filled, we never held.
    """
    did = "1789344000:ABB"
    _write_ledger(tmp_path, [_ledger_order(did, "rejected")])
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, {}, tmp_path,
    )
    assert did not in result


def test_cancelled_order_no_position_releases_deal(tmp_path) -> None:
    """A cancelled order (operator cancelled, or the order monitor
    cancelled it during a re-price attempt that didn't reach the
    re-place phase) should release the deal IF there's no other
    open order for it.
    """
    did = "1789344000:ABB"
    _write_ledger(tmp_path, [_ledger_order(did, "cancelled")])
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, {}, tmp_path,
    )
    assert did not in result


def test_open_order_keeps_deal_actioned(tmp_path) -> None:
    """An open (in-flight) order keeps the deal actioned. We don't
    want to issue a duplicate order while the original is still
    working through the exchange.
    """
    did = "1789344000:ABB"
    _write_ledger(tmp_path, [_ledger_order(did, "open")])
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, {}, tmp_path,
    )
    assert did in result


def test_partial_fill_keeps_deal_actioned(tmp_path) -> None:
    """A partially-filled order is non-terminal — keep actioned."""
    did = "1789344000:ABB"
    _write_ledger(tmp_path, [_ledger_order(did, "partial")])
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, {}, tmp_path,
    )
    assert did in result


def test_filled_then_sold_releases_deal(tmp_path) -> None:
    """Filled + sold (qty=0 in our_deals) is "terminal-without-
    current-position" — the deal should be re-actionable. This
    handles the case where we closed out and autofin republishes
    the same deal as a fresh entry.
    """
    did = "1789344000:ABB"
    _write_ledger(tmp_path, [_ledger_order(did, "filled")])
    our_deals = {did: {"_our_qty": 0}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    assert did not in result


def test_filled_and_held_keeps_deal_actioned(tmp_path) -> None:
    """Filled + still holding (qty > 0) → keep actioned. The
    "current_qty > 0" check in _plan_orders_for_new_positions is
    the actual guard against re-entry, but actioned_deals is the
    first-line defense.
    """
    did = "1789344000:ABB"
    _write_ledger(tmp_path, [_ledger_order(did, "filled")])
    our_deals = {did: {"_our_qty": 100}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    assert did in result


def test_repriced_order_keeps_deal_actioned(tmp_path) -> None:
    """Re-priced orders leave an old "cancelled" order and a new
    "open" order in the ledger. The latest one (by updatedAt) is
    the re-priced one — it's "open", so the deal is kept actioned.
    """
    did = "1789344000:ABB"
    items = [
        _ledger_order(did, "cancelled", updated_at="2026-10-12T07:30:00Z"),
        _ledger_order(did, "open", updated_at="2026-10-12T07:35:00Z"),
    ]
    _write_ledger(tmp_path, items)
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, {}, tmp_path,
    )
    assert did in result


def test_no_ledger_file_keeps_deal_actioned(tmp_path) -> None:
    """If the ledger doesn't exist (first-run, fresh install), the
    cleanup is a no-op — we keep the persistent set intact. The
    caller (load_state) handles the no-file case via the
    ``try/except`` around the whole block.
    """
    # Don't write the ledger file
    did = "1789344000:ABB"
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, {}, tmp_path,
    )
    assert did in result


def test_mixed_deals_pruned_independently(tmp_path) -> None:
    """A realistic mix: some deals get pruned, some kept. The
    function must handle each one based on its own state, not
    short-circuit on the first prune.
    """
    abb = "1789344000:ABB"   # expired, not held → prune
    hdb = "1786492800:HDB"   # open → keep
    tlg = "1786579200:TLG"   # held (qty > 0) → keep
    vcb = "1789344000:VCB"   # rejected, not held → prune
    items = [
        _ledger_order(abb, "expired", symbol="ABB"),
        _ledger_order(hdb, "open", symbol="HDB"),
        _ledger_order(vcb, "rejected", symbol="VCB"),
    ]
    _write_ledger(tmp_path, items)
    our_deals = {tlg: {"_our_qty": 100}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {abb, hdb, tlg, vcb}, our_deals, tmp_path,
    )
    assert abb not in result  # pruned
    assert hdb in result  # kept (open)
    assert tlg in result  # kept (held)
    assert vcb not in result  # pruned


def test_orphan_deal_no_orders_kept_conservatively(tmp_path) -> None:
    """A deal in the persistent actioned set with NO order ledger
    entries at all is a stale entry (probably from a prior bot
    version). Keep it conservative: assume still actioned so we
    don't double-enter.
    """
    did = "1789344000:ABB"
    _write_ledger(tmp_path, [])  # empty ledger
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, {}, tmp_path,
    )
    assert did in result


def test_order_ledger_unreadable_falls_back_gracefully(tmp_path, monkeypatch) -> None:
    """If ``load_ledger`` raises, the cleanup returns the original
    set unchanged. The ``try/except`` in ``_load_state`` will then
    fall back to the no-prune path entirely.
    """
    from vietnam_algo.algos import autofin_signal
    def boom(data_dir):
        raise RuntimeError("ledger file is corrupted")
    monkeypatch.setattr(autofin_signal, "load_ledger", boom)
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {"1789344000:ABB"}, {}, tmp_path,
    )
    assert "1789344000:ABB" in result


# -----------------------------------------------------------------------------
# Dry-run pollution guard (2026-10-13)
#
# The planning step sets ``_our_qty`` from the planned order quantity
# even when the order never actually fills. So a state file might have
# ``our_deals["1785888000:BAF"]["_our_qty"] = 200`` from a previous
# planning pass whose BUY order was later cancelled. The cleanup must
# reset _our_qty to 0 in that case, otherwise:
#   1. the deal is treated as "held" (held_dids includes it)
#   2. the deal is therefore kept in actioned
#   3. the strategy filters it out as "already actioned"
#   4. we never re-plan the entry
# -----------------------------------------------------------------------------


def test_cancelled_order_with_stale_our_qty_resets_to_zero(tmp_path) -> None:
    """_our_qty=200 but the latest order is cancelled → reset qty, release deal.

    Reproduces the 2026-10-13 10:52 ICT incident: BAF BUY 200 was
    planned, ``_our_qty`` was set to 200, then DNSE cancelled the
    order. Without the dry-run pollution guard, the cleanup kept
    BAF in actioned (because ``_our_qty > 0``) and the next mid-day
    poll never re-planned the entry.
    """
    did = "1785888000:BAF"
    _write_ledger(tmp_path, [_ledger_order(did, "cancelled", symbol="BAF")])
    our_deals = {did: {"_our_qty": 200, "_state": "planned"}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    # Cleanup released the deal from actioned.
    assert did not in result
    # The dry-run pollution guard reset _our_qty to 0 in place.
    assert our_deals[did]["_our_qty"] == 0


def test_rejected_order_with_stale_our_qty_resets_to_zero(tmp_path) -> None:
    """Same as above but the order was rejected, not cancelled."""
    did = "1786492800:HDB"
    _write_ledger(tmp_path, [_ledger_order(did, "rejected", symbol="HDB")])
    our_deals = {did: {"_our_qty": 100}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    assert did not in result
    assert our_deals[did]["_our_qty"] == 0


def test_expired_order_with_stale_our_qty_resets_to_zero(tmp_path) -> None:
    """Same as above but the order expired at EOD."""
    did = "1786579200:TLG"
    _write_ledger(tmp_path, [_ledger_order(did, "expired", symbol="TLG")])
    our_deals = {did: {"_our_qty": 100}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    assert did not in result
    assert our_deals[did]["_our_qty"] == 0


def test_filled_order_with_our_qty_preserved(tmp_path) -> None:
    """_our_qty=100 + status=filled → qty preserved, deal stays actioned.

    Filled is a terminal state BUT we DID receive the shares, so
    the dry-run pollution guard must NOT reset _our_qty to 0 here.
    Re-entering a held position is forbidden by the "current_qty > 0"
    check in ``_plan_orders_for_new_positions`` and would be a
    double-buy.
    """
    did = "1789344000:ABB"
    _write_ledger(tmp_path, [_ledger_order(did, "filled", symbol="ABB")])
    our_deals = {did: {"_our_qty": 100}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    # Filled + held → keep actioned.
    assert did in result
    # _our_qty preserved (we actually own the shares).
    assert our_deals[did]["_our_qty"] == 100


def test_open_order_with_our_qty_preserved(tmp_path) -> None:
    """_our_qty=200 + status=open → qty preserved, deal stays actioned.

    Open means the order is still in flight. We don't know if it
    will fill, so the planning step's _our_qty value is kept. The
    deal stays actioned so the strategy doesn't issue a duplicate.
    """
    did = "1785888000:BAF"
    _write_ledger(tmp_path, [_ledger_order(did, "open", symbol="BAF")])
    our_deals = {did: {"_our_qty": 200}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    assert did in result
    assert our_deals[did]["_our_qty"] == 200


def test_cancelled_with_stale_our_qty_in_mixed_actioned_set(tmp_path) -> None:
    """Mixed: ABB held (filled), BAF stale (cancelled + _our_qty=200).

    The cleanup should release BAF from actioned (and reset its qty)
    while keeping ABB in actioned (and preserving its qty).
    """
    abb_did = "1789344000:ABB"
    baf_did = "1785888000:BAF"
    _write_ledger(tmp_path, [
        _ledger_order(abb_did, "filled", symbol="ABB"),
        _ledger_order(baf_did, "cancelled", symbol="BAF"),
    ])
    our_deals = {
        abb_did: {"_our_qty": 100},  # actually held
        baf_did: {"_our_qty": 200},  # stale: BUY was cancelled
    }
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {abb_did, baf_did}, our_deals, tmp_path,
    )
    # ABB kept (held + filled), BAF released (cancelled + no actual position).
    assert abb_did in result
    assert baf_did not in result
    # Qty preserved for ABB, reset for BAF.
    assert our_deals[abb_did]["_our_qty"] == 100
    assert our_deals[baf_did]["_our_qty"] == 0


def test_dry_run_status_with_stale_our_qty_resets_to_zero(tmp_path) -> None:
    """Status="dry_run" (planning-only simulation) also resets _our_qty.

    The dry-run path uses status="dry_run" to mark orders that were
    simulated but never submitted. Even though it's "terminal" in
    the ledger sense, the order never actually filled, so _our_qty
    must be reset.
    """
    did = "1786492800:ABC"
    _write_ledger(tmp_path, [_ledger_order(did, "dry_run", symbol="ABC")])
    our_deals = {did: {"_our_qty": 100}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    assert did not in result
    assert our_deals[did]["_our_qty"] == 0


# -----------------------------------------------------------------------------
# Filled-order reconciliation (2026-10-13)
#
# The state file's ``_our_qty`` can drift from reality (operator
# reset, code bug, dry-run pollution). The ledger's lastDetail is
# the source of truth for filled quantity. The cleanup must treat
# a deal as "held" if the latest order is ``filled`` with
# filledQuantity > 0, even if ``_our_qty == 0`` in the state file.
# Otherwise the deal gets released and the strategy re-plans a
# double-buy of the same position.
# -----------------------------------------------------------------------------


def test_filled_order_with_zero_state_qty_is_treated_as_held(tmp_path) -> None:
    """_our_qty=0 in state but ledger says filled+100 → reconcile to 100, hold.

    Reproduces the 2026-10-13 13:18 ICT bug: ABB was filled with
    100 shares but the state file had ``_our_qty=0`` (operator
    reset to a clean slate earlier). The cleanup was treating
    ``filled`` as terminal-without-fill and releasing ABB, so
    the strategy planned a new ABB BUY 500.
    """
    did = "1789344000:ABB"
    # Build a filled order with filledQuantity=100 in lastDetail.
    filled_order = _ledger_order(did, "filled", symbol="ABB")
    # Patch the lastDetail to have filledQuantity=100.
    filled_order = replace(filled_order, lastDetail={"filledQuantity": 100, "orderStatus": "Filled"})
    _write_ledger(tmp_path, [filled_order])
    # State file says _our_qty=0 (stale/wrong).
    our_deals = {did: {"_our_qty": 0}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    # Treated as held because the latest order is filled.
    assert did in result
    # _our_qty reconciled to 100 (matches the filledQuantity).
    assert our_deals[did]["_our_qty"] == 100


def test_filled_order_with_zero_fill_is_not_treated_as_held(tmp_path) -> None:
    """Status=filled but filledQuantity=0 (edge case) → release deal.

    A status of "filled" with filledQuantity=0 is a weird edge case
    (DNSE shouldn't return this combo, but defensive). The cleanup
    should NOT treat this as a held position because no shares
    were received.
    """
    did = "1789344000:ABB"
    filled_order = _ledger_order(did, "filled", symbol="ABB")
    filled_order = replace(filled_order, lastDetail={"filledQuantity": 0, "orderStatus": "Filled"})
    _write_ledger(tmp_path, [filled_order])
    our_deals = {did: {"_our_qty": 0}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    # No shares were actually received → release the deal.
    assert did not in result
    assert our_deals[did]["_our_qty"] == 0


def test_filled_order_with_stale_state_qty_reconciles_upward(tmp_path) -> None:
    """State says 50 shares, ledger says 100 filled → reconcile to 100.

    If the state file has a smaller _our_qty than the ledger's
    filledQuantity, the ledger wins. The cleanup updates the
    state file to match the truth so future cycles see the right
    position.
    """
    did = "1789344000:ABB"
    filled_order = _ledger_order(did, "filled", symbol="ABB")
    filled_order = replace(filled_order, lastDetail={"filledQuantity": 100, "orderStatus": "Filled"})
    _write_ledger(tmp_path, [filled_order])
    our_deals = {did: {"_our_qty": 50}}  # stale: actually 100
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    assert did in result
    # State reconciled to ledger's truth.
    assert our_deals[did]["_our_qty"] == 100


def test_filled_order_with_correct_state_qty_no_change(tmp_path) -> None:
    """State says 100, ledger says 100 filled → no change, deal held."""
    did = "1789344000:ABB"
    filled_order = _ledger_order(did, "filled", symbol="ABB")
    filled_order = replace(filled_order, lastDetail={"filledQuantity": 100, "orderStatus": "Filled"})
    _write_ledger(tmp_path, [filled_order])
    our_deals = {did: {"_our_qty": 100}}
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    assert did in result
    assert our_deals[did]["_our_qty"] == 100  # unchanged


def test_partial_fill_order_treated_as_held(tmp_path) -> None:
    """Status=partial with filledQuantity>0 → held (partial fill)."""
    did = "1785888000:BAF"
    partial_order = _ledger_order(did, "partial", symbol="BAF")
    partial_order = replace(partial_order, lastDetail={"filledQuantity": 50, "orderStatus": "Partial"})
    _write_ledger(tmp_path, [partial_order])
    our_deals = {did: {"_our_qty": 0}}  # state stale
    result = AutofinSignalAdapter._prune_terminal_unfilled_deals(
        {did}, our_deals, tmp_path,
    )
    # Partial fill → kept actioned (order still in flight).
    assert did in result


