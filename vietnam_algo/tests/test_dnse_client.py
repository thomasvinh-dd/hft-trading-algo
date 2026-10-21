from __future__ import annotations

import json
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vietnam_algo.libs.dnse.client import (  # noqa: E402
    DEFAULT_DNSE_BASE_URL,
    DnseClient,
    fetch_dnse_history_chunked,
    parse_accounts,
    parse_instruments,
    parse_price_bars,
    signing_string,
    sync_dnse_instruments,
    sync_dnse_prices,
    utc_timestamp,
)


# -----------------------------------------------------------------------------
# signing_string / auth
# -----------------------------------------------------------------------------

def test_signing_string_format_excludes_query() -> None:
    body = signing_string("get", "/accounts", "Mon, 31 Aug 2026 02:39:14 +0000", "abc")
    assert "(request-target): get /accounts" in body
    assert "date: " in body
    assert "nonce: abc" in body


def test_signing_string_does_not_include_query() -> None:
    body = signing_string("get", "/price/ohlc?type=STOCK&symbol=VCB", "Mon, 31 Aug 2026 02:39:14 +0000", "abc")
    assert "?type=STOCK" not in body
    assert "symbol=VCB" not in body
    assert "/price/ohlc" in body
    assert "(request-target): get /price/ohlc?" not in body  # query stripped


# -----------------------------------------------------------------------------
# parse_price_bars
# -----------------------------------------------------------------------------

def test_parse_price_bars_handles_dnse_dict_format() -> None:
    # Unix seconds (1e9 range). 2023-12-01 = 1701388800.
    payload = {
        "t": [1701302400, 1701388800, 1701475200],
        "o": [100.0, 102.0, 101.5],
        "h": [105.0, 104.0, 103.0],
        "l": [99.5, 101.0, 100.5],
        "c": [104.0, 103.0, 102.5],
        "v": [12345, 23456, 11223],
        "nextTime": 0,
    }
    bars = parse_price_bars(payload)
    assert len(bars) == 3
    # Timestamps fall on 2023-11-30, 2023-12-01, 2023-12-02 in UTC
    expected = [datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() for ts in [1701302400, 1701388800, 1701475200]]
    assert [bar["time"] for bar in bars] == expected
    assert bars[1]["close"] == pytest.approx(103.0)
    assert bars[1]["volume"] == pytest.approx(23456)


def test_parse_price_bars_handles_list_format() -> None:
    payload = [
        {"date": "2024-01-02", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100},
        {"time": "2023-12-29", "open": 1, "high": 2, "low": 0.5, "close": 1.7, "volume": 50},
    ]
    bars = parse_price_bars(payload)
    assert [bar["time"] for bar in bars] == ["2023-12-29", "2024-01-02"]
    assert bars[0]["close"] == pytest.approx(1.7)


def test_parse_price_bars_handles_short_aliases() -> None:
    payload = [
        {"t": 1701302400, "o": 100, "h": 105, "l": 99, "c": 104, "v": 12345},
        {"t": "2024-01-02T00:00:00Z", "c": 200, "o": 198, "h": 201, "l": 197},
    ]
    bars = parse_price_bars(payload)
    assert len(bars) == 2
    assert bars[1]["close"] == pytest.approx(200)


def test_parse_price_bars_dedups_unix_seconds_vs_iso() -> None:
    # 1701388800 = 2023-12-01 00:00:00 UTC — same date as the ISO string below.
    payload = [
        {"t": 1701388800, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0},
        {"date": "2023-12-01", "o": 2, "h": 2, "l": 2, "c": 2, "v": 0},
    ]
    bars = parse_price_bars(payload)
    assert len(bars) == 1


def test_parse_price_bars_returns_empty_on_garbage() -> None:
    assert parse_price_bars({}) == []
    assert parse_price_bars({"data": "garbage"}) == []
    assert parse_price_bars("not a dict") == []
    assert parse_price_bars(None) == []


# -----------------------------------------------------------------------------
# parse_instruments / parse_accounts
# -----------------------------------------------------------------------------

def test_parse_instruments_filters_sto_only() -> None:
    raw = {
        "data": [
            {"symbol": "VCB", "marketId": "STO", "isin": "VN000000VCB4", "shortName": "VCB"},
            {"symbol": "VN30F1M", "marketId": "DVX", "securityGroupId": "FU"},
            {"symbol": "HPG", "marketId": "STO"},
            {},
        ]
    }
    out = parse_instruments(raw)
    assert [it["symbol"] for it in out] == ["VCB", "HPG"]


def test_parse_instruments_keeps_upx_for_upcom_stocks() -> None:
    """UPCoM stocks (e.g. QNS, VGT) have ``marketId="UPX"``.

    Pre-2026-09-19 the filter only kept ``marketId="STO"`` which
    silently dropped all UPCoM tickers — the price sync skipped
    them and the bot marked-to-market using yesterday's close.
    See QNS incident 2026-09-19: bot reported 50,100 VND but the
    real Sep 18 close was 49,800 VND because the QNS price file
    had never been refreshed past Sep 17.

    2026-10-21 incident (MST): the prior filter excluded ``STX``
    thinking it was ETFs, but the live DNSE response labelled
    303 ordinary HNX equities (e.g. MST, NTP) as ``STX`` post the
    HOSE/HNX/HCM exchange merger. The filter dropped them ALL,
    the ``build_liquid_universe`` then had no MST metadata, the
    backtest consequently held 9,500 MST shares forever (no SELL
    exit generated), and the bot reported ``count: 0`` orders
    every EOD. We now keep STX too. The universe's downstream
    ``is_excluded_symbol(prefix)`` (``E1`` for ETFs, ``FU`` for
    futures, ``FUE`` for fund units) still strips instruments
    like ``E1VFVN30`` before they reach the liquid universe.
    """
    raw = {
        "data": [
            {"symbol": "QNS", "marketId": "UPX", "shortName": "Đường Quảng Ngãi"},
            {"symbol": "VGT", "marketId": "UPX"},
            {"symbol": "VCB", "marketId": "STO"},
            {"symbol": "MST", "marketId": "STX"},  # HNX equity (Q3 2026)
            {"symbol": "NTP", "marketId": "STX"},  # HNX-30 constituent
            {"symbol": "VN30F1M", "marketId": "DVX"},  # derivatives: skip
            {"symbol": "E1VFVN30", "marketId": "STX"},  # ETF: passed-through, filtered later
            {"symbol": "BAB118001", "marketId": "HCX"},  # bond: skip
        ]
    }
    out = parse_instruments(raw)
    assert [it["symbol"] for it in out] == [
        "QNS", "VGT", "VCB", "MST", "NTP", "E1VFVN30",
    ]


def test_parse_instruments_keeps_stx_for_hnx_equities() -> None:
    """2026-10-21 incident (MST): DNSE labels ordinary HNX equities
    (MST, NTP) with ``marketId="STX"`` post the HOSE/HNX/HCM exchange
    merger. The prior filter excluded ``STX`` thinking it was ETFs,
    but the live DNSE response showed 303 HNX symbols as ``STX`` —
    the filter dropped them ALL, the universe had no MST metadata,
    and the backtest consequently held MST forever without an exit.
    This test pins that MST/NTP survive ``parse_instruments`` so the
    downstream ``build_liquid_universe`` can include them.
    """
    raw = {
        "data": [
            {"symbol": "MST", "marketId": "STX", "name": "CTCP Đầu tư MST"},
            {"symbol": "NTP", "marketId": "STX", "name": "CTCP Nhựa Thiếu niên Tiền Phong"},
            # ETFs and bond are filtered later by is_excluded_symbol
            # (prefixes: 'E1', 'FU', 'FUE') in build_liquid_universe,
            # so they survive parse_instruments but get dropped
            # before reaching the liquid universe.
            {"symbol": "E1VFVN30", "marketId": "STX"},
            {"symbol": "FUEMAV30", "marketId": "STX"},
            {"symbol": "VCB", "marketId": "STO"},
            {"symbol": "VN30F1M", "marketId": "DVX"},
        ]
    }
    out = parse_instruments(raw)
    symbols = [it["symbol"] for it in out]
    # MST and NTP MUST be in the output (regression guard).
    assert "MST" in symbols, (
        "MST (HNX stock labelled as STX) was dropped from "
        "parse_instruments — the backtest cannot plan MST exits "
        "without this symbol in the universe."
    )
    assert "NTP" in symbols, (
        "NTP (HNX-30 constituent labelled as STX) was dropped — "
        "same bug as MST."
    )
    # Derivatives STO/MST are kept.
    assert "VCB" in symbols
    # DVX derivatives dropped.
    assert "VN30F1M" not in symbols


def test_parse_symbols_maps_stx_to_hnx_exchange_code() -> None:
    """``parse_symbols`` translates DNSE's ``marketId`` into a
    StockSymbol ``exchangeCode`` value that's accepted by the
    universe filter (``DEFAULT_EXCHANGES = {"HOSE", "HNX", "UPCOM",
    "STO", "UPX"}``). STX (HNX equities post-merger) must map to
    "HNX" so the universe build accepts the parsed symbol. Without
    this mapping, MST would have ``exchangeCode="STX"`` and be
    filtered out by ``is_excluded_symbol`` (which only excludes by
    ticker prefix, not by market label — but the universe's
    ``allowed_exchanges`` check at libs/universe.py:118 would drop
    it because "STX" ∉ DEFAULT_EXCHANGES).
    """
    from vietnam_algo.libs.data import parse_symbols
    raw = {
        "data": [
            {"symbol": "MST", "marketId": "STX", "name": "MST"},
            {"symbol": "VCB", "marketId": "STO", "name": "VCB"},
            {"symbol": "QNS", "marketId": "UPX", "name": "QNS"},
        ]
    }
    syms, _ = parse_symbols(raw)
    by_ticker = {s.ticker: s.exchangeCode for s in syms}
    assert by_ticker["MST"] == "HNX", (
        f"MST (marketId=STX) must be tagged exchangeCode=HNX, got "
        f"{by_ticker['MST']!r}. With the wrong code the universe "
        f"filter at libs/universe.py:118 drops MST (STX ∉ "
        f"DEFAULT_EXCHANGES)."
    )
    assert by_ticker["VCB"] == "STO"
    assert by_ticker["QNS"] == "UPX"


def test_data_parse_price_bars_converts_kvnd_to_vnd() -> None:
    """DNSE returns prices in *thousands of VND*.

    The legacy vietnamfinance.vn source returned absolute VND
    (e.g. ``close: 27500.0`` for HDB). All downstream code
    (mark-to-market, MV calc, autofin BUY sizing) assumes
    absolute VND, so the data-layer wrapper multiplies by 1000.
    Without this the bot would mark a position to 27.5 VND
    instead of 27,500 VND (QNS / HDB Sep 18 incident 2026-09-19).
    """
    from vietnam_algo.libs.data import parse_price_bars
    raw = {
        "t": [1789610400, 1789696800],  # Sep 17, 18
        "o": [27.5, 27.3],
        "h": [27.8, 27.7],
        "l": [27.4, 27.2],
        "c": [27.6, 27.5],
        "v": [100, 200],
    }
    bars = parse_price_bars(raw)
    assert len(bars) == 2
    # DNSE convention: kVND → multiply by 1000
    assert bars[0].close == 27_600.0  # 27.6 * 1000
    assert bars[1].close == 27_500.0
    assert bars[0].open == 27_500.0
    assert bars[1].high == 27_700.0


def test_universe_load_price_bars_handles_both_items_and_bars_keys() -> None:
    """Legacy vietnamfinance files used ``items``; new DNSE writes
    ``bars``. The loader must accept either so the bot works
    during the migration window.
    """
    import json
    from pathlib import Path
    from vietnam_algo.libs.universe import load_price_bars
    tmp = Path("/tmp/_universe_load_test")
    tmp.mkdir(exist_ok=True)
    prices_dir = tmp / "prices"
    prices_dir.mkdir(exist_ok=True)
    (prices_dir / "HPG.json").write_text(json.dumps({
        "items": [
            {"time": "2026-09-17", "open": 21200, "high": 21250,
             "low": 20950, "close": 20950, "volume": 100},
        ]
    }))
    (prices_dir / "QNS.json").write_text(json.dumps({
        "bars": [
            {"time": "2026-09-18", "open": 49800, "high": 50000,
             "low": 49500, "close": 49800, "volume": 50},
        ]
    }))
    hpg = load_price_bars("HPG", data_dir=tmp)
    qns = load_price_bars("QNS", data_dir=tmp)
    assert hpg[0].close == 20950.0
    assert qns[0].close == 49800.0


def test_parse_accounts_picks_id_and_derivative_status() -> None:
    payload = {
        "name": "Test",
        "investorId": "1001",
        "custodyCode": "CUST1",
        "accounts": [
            {
                "id": "0001",
                "dealAccount": True,
                "derivativeAccount": False,
                "derivative": {"status": "ACTIVE"},
            }
        ],
    }
    investor, accts = parse_accounts(payload)
    assert investor == "1001"
    assert len(accts) == 1
    assert accts[0].id == "0001"
    assert accts[0].derivative_status == "ACTIVE"


# -----------------------------------------------------------------------------
# utc_timestamp
# -----------------------------------------------------------------------------

def test_utc_timestamp_basic() -> None:
    assert utc_timestamp("2024-01-02") == 1704153600


# -----------------------------------------------------------------------------
# DnseClient.request — mocked transport, verifies header set + signing string
# -----------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body: object, status: int = 200) -> None:
        self.body = body
        self.status_code = status

    def read(self) -> bytes:
        return json.dumps(self.body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


class _FakeHTTPError(urllib.error.HTTPError):
    """urllib.error.HTTPError shim — supports the client's .read() call."""

    def __init__(self, url: str, code: int, body: str) -> None:
        super().__init__(url, code, f"HTTP {code}", {}, None)
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body


def _client() -> DnseClient:
    return DnseClient(api_key="k" * 8, api_secret="s" * 16)


def test_dnse_client_request_uses_date_header_not_xaux(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return _FakeResp({"ok": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = _client()
    client.request("GET", "/accounts")
    headers_lower = {k.lower() for k in captured["headers"].keys()}
    assert "date" in headers_lower
    assert "x-aux-date" not in headers_lower
    assert "x-api-key" in headers_lower
    assert "version" in headers_lower
    assert "x-signature" in headers_lower


def test_dnse_client_request_puts_version_header_value(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    def fake_urlopen(req, timeout=20):
        headers = {k.lower(): v for k, v in req.header_items()}
        captured["version"] = headers.get("version")
        return _FakeResp({"ok": True})
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = _client()
    client.request("GET", "/accounts")
    assert captured["version"] == "2026-07-23"


def test_dnse_client_request_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_urlopen(req, timeout=20):
        calls.append(1)
        if len(calls) < 2:
            raise _FakeHTTPError(req.full_url, 429, "Too Many Requests")
        return _FakeResp({"ok": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    client = DnseClient("k", "s", max_retries=2)
    client.request("GET", "/accounts")
    assert len(calls) == 2


def test_dnse_client_request_raises_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=20):
        raise _FakeHTTPError(req.full_url, 500, "Internal")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    client = DnseClient("k", "s", max_retries=1)
    with pytest.raises(RuntimeError, match="500"):
        client.request("GET", "/accounts")


# -----------------------------------------------------------------------------
# sync_dnse_instruments / sync_dnse_prices (mocked fetch + chunks)
# -----------------------------------------------------------------------------

def test_sync_dnse_instruments_writes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_list(self, limit=100, page_size_max=30):
        return [{"symbol": "VCB", "marketId": "STO", "isin": "X"}, {"symbol": "HPG", "marketId": "STO"}]

    monkeypatch.setattr(DnseClient, "list_instruments_paginated", fake_list)
    client = _client()
    path = sync_dnse_instruments(client, tmp_path)
    payload = json.loads(path.read_text())
    assert payload["count"] == 2
    assert payload["items"][0]["symbol"] == "VCB"
    assert "openapi.dnse.com.vn" in payload["source"]


def test_fetch_dnse_history_chunked_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter([
        {"t": [1701302400, 1701388800], "o": [1, 2], "h": [1, 2], "l": [1, 2], "c": [10, 20], "v": [0, 0]},
        {"t": [1701388800, 1701475200], "o": [30, 40], "h": [30, 40], "l": [30, 40], "c": [200, 300], "v": [0, 0]},
        {"t": [1701475200], "o": [50], "h": [50], "l": [50], "c": [400], "v": [0]},
    ])

    def fake_get_ohlc(self, **kwargs):
        return next(responses)

    monkeypatch.setattr(DnseClient, "get_ohlc", fake_get_ohlc)
    client = _client()
    bars = fetch_dnse_history_chunked(
        client=client,
        symbol="VCB",
        from_date="2023-12-01",
        to_date="2023-12-03",
        chunk_days=1,
        sleep_seconds=0,
    )
    assert [bar["close"] for bar in bars] == [10, 200, 400]


def test_sync_dnse_prices_skips_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prices_dir = tmp_path / "prices_dnse"
    prices_dir.mkdir()
    existing = prices_dir / "VCB.json"
    existing.write_text(json.dumps({"items": [{"time": "2024-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]}))

    called = {"count": 0}

    def fake_fetch(client, symbol, from_date, to_date, **kwargs):
        called["count"] += 1
        return [{"time": "2024-01-02", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2}]

    monkeypatch.setattr("vietnam_algo.libs.dnse.client.fetch_dnse_history_chunked", fake_fetch)
    paths = sync_dnse_prices(
        client=_client(),
        symbols=["VCB", "ACB"],
        from_date="2024-01-01",
        to_date="2024-12-31",
        data_dir=tmp_path,
        skip_existing=True,
        sleep_seconds=0,
    )
    assert called["count"] == 1  # VCB skipped, ACB fetched
    assert "VCB" in paths and "ACB" in paths
    # ACB path should have been freshly written
    payload = json.loads((tmp_path / "prices_dnse" / "ACB.json").read_text())
    assert payload["count"] == 1
