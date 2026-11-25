"""Tests for the HOSE price-tick helper added after the 2026-10-21
BAF incident (``Invalid price lot`` rejection).

The autofin-recommended BAF exit at 30,950 produced
``sell_price = round(30950 × 0.99) = 30640``, which is a multiple
of 10 but NOT of 50 — the HOSE tick for prices 10,000-49,950.
DNSE rejected the order with HTTP 400 ``INVALID_PRICE_LOT`` and
the SELL never reached the exchange.

The fix round-DOWN ``sell_price`` to the nearest valid tick so
the SELL crosses at or BELOW the best-bid (always marketable).
HOSE tick rule::

    <  10,000 VND →  10 VND
    10,000-49,950  →  50 VND
    >= 50,000 VND → 100 VND

HNX/UPCOM default to 100-VND tick by the same helper.
"""

from vietnam_algo.bots.runner import _hose_price_tick


def test_hose_tick_below_10k_uses_10_vnd():
    # 5,000 VND range → tick=10
    assert _hose_price_tick(9_995) == 9_990
    assert _hose_price_tick(5_123) == 5_120
    assert _hose_price_tick(1_005) == 1_000


def test_hose_tick_10k_to_50k_uses_50_vnd():
    # BAF case: 30,640 -> 30,600 (round down to nearest 50)
    assert _hose_price_tick(30_950) == 30_950  # already aligned
    assert _hose_price_tick(30_640) == 30_600
    assert _hose_price_tick(15_001) == 15_000
    assert _hose_price_tick(49_999) == 49_950


def test_hose_tick_above_50k_uses_100_vnd():
    # TLG @ 57,200 range → tick=100
    assert _hose_price_tick(57_500) == 57_500
    assert _hose_price_tick(57_777) == 57_700
    assert _hose_price_tick(99_999) == 99_900
    assert _hose_price_tick(100_000) == 100_000


def test_hose_tick_zero_handles_safely():
    # 0 / negative inputs return 0 (caller falls back to market
    # or skips the order).
    assert _hose_price_tick(0) == 0
    assert _hose_price_tick(-100) == 0


def test_hose_tick_boundary_exact_values():
    # Exact boundaries: <10k / >=10k / >=50k
    assert _hose_price_tick(9_999) == 9_990   # tick=10 (since <10k)
    assert _hose_price_tick(10_000) == 10_000  # tick=50
    assert _hose_price_tick(49_999) == 49_950  # tick=50
    assert _hose_price_tick(50_000) == 50_000  # tick=100


def test_hose_tick_rounds_down_not_up():
    """Verify the helper always rounds DOWN (so the SELL crosses
    the bid immediately). A bid-floor scenario: bid=30,150
    → 30,150 × 0.99 = 29,848.5 → 29,849 → tick=50 → 29,800.
    Rounding up to 29,850 would put the sell ABOVE the bid
    (which is now 30,150), leaving it resting for 1+ tick.
    """
    assert _hose_price_tick(30_848) == 30_800   # 30,848 floor
    assert _hose_price_tick(30_849) == 30_800
    assert _hose_price_tick(30_850) == 30_850   # already aligned
    assert _hose_price_tick(30_851) == 30_850


def test_stx_hnx_uses_a_flat_100_vnd_tick():
    """DNSE labels HNX equities STX (the 2026-11-16 CTP incident).

    HNX requires a multiple of 100 at every price level. The HOSE
    ladder would turn CTP's 6,633 into 6,630, which DNSE rejected with
    INVALID_PRICE_LOT.
    """
    assert _hose_price_tick(6_633, "STX") == 6_600
    assert _hose_price_tick(6_630, "STX") == 6_600
    assert _hose_price_tick(8_613, "STX") == 8_600
    assert _hose_price_tick(1_633, "stx") == 1_600
    # A failed market lookup must use the conservative 100-VND grid,
    # not guess the HOSE 10-VND grid.
    assert _hose_price_tick(6_633, "UNKNOWN_100") == 6_600


def test_stx_does_not_change_the_hose_ladder():
    """The STX fix must not alter valid HOSE prices."""
    assert _hose_price_tick(6_633, "STO") == 6_630
    assert _hose_price_tick(6_633, None) == 6_630
    assert _hose_price_tick(9_995, "STO") == 9_990
    assert _hose_price_tick(30_640, "STO") == 30_600
    assert _hose_price_tick(49_999, "STO") == 49_950
    assert _hose_price_tick(57_777, "STO") == 57_700
