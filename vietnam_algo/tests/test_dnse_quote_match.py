from __future__ import annotations

from vietnam_algo.libs.order_monitor import quote_match_price


SAMPLE_DNSE_QUOTE = {
    "quotes": [
        {
            "marketId": "STO",
            "boardId": "G1",
            "isin": "VN000000VCB4",
            "symbol": "VCB",
            "bid": [
                {"price": 60.0, "quantity": 22750},
                {"price": 59.9, "quantity": 20980},
                {"price": 59.8, "quantity": 22000},
            ],
            "offer": [
                {"price": 60.1, "quantity": 130},
                {"price": 60.2, "quantity": 10340},
                {"price": 60.3, "quantity": 8190},
            ],
            "time": "2026-08-28 14:45:05.718",
        }
    ]
}


def test_quote_match_price_dnse_nested_shape_buy() -> None:
    """Buy side (NB) should pick the lowest offer (best ask) = 60.1 -> 60."""
    assert quote_match_price(SAMPLE_DNSE_QUOTE, "NB") == 60


def test_quote_match_price_dnse_nested_shape_sell() -> None:
    """Sell side (NS) should pick the highest bid (best bid) = 60.0 -> 60."""
    assert quote_match_price(SAMPLE_DNSE_QUOTE, "NS") == 60


def test_quote_match_price_legacy_flat_shape() -> None:
    """Legacy flat keys (bidPrice1 / askPrice1) still work."""
    flat = {"bidPrice1": 19.5, "askPrice1": 20.0}
    assert quote_match_price(flat, "NB") == 20
    assert quote_match_price(flat, "NS") == 20  # rounds 19.5 -> 20


def test_quote_match_price_handles_missing_side() -> None:
    empty = {"quotes": [{"bid": [], "offer": []}]}
    assert quote_match_price(empty, "NB") is None
    assert quote_match_price(empty, "NS") is None


def test_quote_match_price_handles_none() -> None:
    assert quote_match_price(None, "NB") is None
    assert quote_match_price({}, "NB") is None
