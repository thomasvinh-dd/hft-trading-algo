from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from vietnam_algo.libs.data import PriceBar
from vietnam_algo.libs.data_investing import (
    DEFAULT_INVESTING_PRICES_DIR,
    InvestingSymbol,
    fetch_investing_history,
    fetch_investing_history_chunked,
    fetch_investing_symbols,
    load_investing_id_map,
    load_investing_prices,
    merge_history_with_prices,
    parse_investing_bars,
    parse_investing_equities_list_html,
    save_investing_id_map,
    save_investing_prices,
    sync_investing_prices,
    ticker_to_slug,
)


SAMPLE_PAYLOAD = {
    "data": [
        {
            "date": "2024-01-02",
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 104.0,
            "volume": 12345,
        },
        {
            "date": "2023-12-29",
            "open": 98.0,
            "high": 101.0,
            "low": 97.0,
            "close": 100.0,
            "volume": 9876,
        },
    ]
}


def test_parse_investing_bars_sorts_and_floats() -> None:
    bars = parse_investing_bars(SAMPLE_PAYLOAD)
    assert len(bars) == 2
    assert [bar.time for bar in bars] == ["2023-12-29", "2024-01-02"]
    assert bars[1].close == pytest.approx(104.0)
    assert bars[1].volume == pytest.approx(12345.0)


def test_parse_investing_bars_alt_schema() -> None:
    payload = [
        {
            "tradeDate": "2024-05-01",
            "openPrice": 10,
            "highPrice": 11,
            "lowPrice": 9,
            "closePrice": 10.5,
            "v": 100,
        }
    ]
    bars = parse_investing_bars(payload)
    assert len(bars) == 1
    assert bars[0].time == "2024-05-01"
    assert bars[0].close == pytest.approx(10.5)


def test_parse_investing_bars_handles_empty_and_bad_rows() -> None:
    assert parse_investing_bars({}) == []
    assert parse_investing_bars({"data": "not a list"}) == []
    bars = parse_investing_bars({"data": [{}, {"date": "2024-01-02"}, {"date": "bad"}]})
    assert bars == []


def test_parse_investing_bars_accepts_candles_schema() -> None:
    payload = {
        "data": {
            "candles": [
                {"date": "2024-02-01T00:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
            ]
        }
    }
    bars = parse_investing_bars(payload)
    assert len(bars) == 1
    assert bars[0].time == "2024-02-01"


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    bars = parse_investing_bars(SAMPLE_PAYLOAD)
    path = save_investing_prices("VCB", bars, pair_id=9999, data_dir=tmp_path)
    assert path == tmp_path / DEFAULT_INVESTING_PRICES_DIR / "VCB.json"
    loaded = load_investing_prices("VCB", data_dir=tmp_path)
    assert [bar.time for bar in loaded] == ["2023-12-29", "2024-01-02"]
    assert loaded[0].close == pytest.approx(100.0)


def test_save_investing_prices_lowercases_ticker(tmp_path: Path) -> None:
    bars = parse_investing_bars(SAMPLE_PAYLOAD)
    path = save_investing_prices("vcb", bars, pair_id=10, data_dir=tmp_path)
    assert path.name == "VCB.json"


def test_load_investing_prices_missing_file(tmp_path: Path) -> None:
    assert load_investing_prices("ZZZ", data_dir=tmp_path) == []


def test_save_investing_id_map_roundtrip(tmp_path: Path) -> None:
    syms = [
        InvestingSymbol(ticker="ACB", pair_id=41913, slug="asia-commercial-bank", name="Asia Commercial Bank", exchange="HoSE", sector="Ngân hàng"),
        InvestingSymbol(ticker="VCB", pair_id=12345, slug="vietcombank", name="Vietcombank", exchange="HoSE", sector="Ngân hàng"),
    ]
    path = save_investing_id_map(syms, tmp_path)
    assert path == tmp_path / "investing_id_map.json"
    loaded = load_investing_id_map(path)
    assert loaded == {"ACB": 41913, "VCB": 12345}


def test_load_investing_id_map_handles_alternative_keys(tmp_path: Path) -> None:
    payload = {"items": [{"ticker": "AAA", "id": 1}, {"Symbol": "BBB", "ID": "2"}]}
    path = tmp_path / "investing_id_map.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_investing_id_map(path) == {"AAA": 1, "BBB": 2}


def test_parse_equities_list_html_extracts_pairs_and_slugs() -> None:
    html = """
    <html><body>
      <a href="/equities/asia-commercial-bank-historical-data" data-pair-id="41913">Asia Commercial Bank</a>
      <a href="/equities/hph-historical-data" data-pair="4567">HPH</a>
    </body></html>
    """
    symbols = parse_investing_equities_list_html(html)
    pair_ids = {sym.pair_id for sym in symbols}
    assert 41913 in pair_ids and 4567 in pair_ids
    hph = next(s for s in symbols if s.pair_id == 4567)
    assert hph.slug == "hph"
    assert hph.ticker == "HPH"
    assert hph.name and "HPH" in hph.name
    acb_parent = next(s for s in symbols if s.pair_id == 41913)
    assert acb_parent.slug == "asia-commercial-bank"
    assert acb_parent.ticker == ""
    assert acb_parent.name and "Asia Commercial Bank" in acb_parent.name


def test_parse_equities_list_html_picks_up_short_slug_tickers() -> None:
    html = """
    <a href="/equities/vcb-historical-data" data-pair-id="11">Vietcombank</a>
    <a href="/equities/hph-historical-data" data-pair-id="22">HPH</a>
    """
    symbols = parse_investing_equities_list_html(html)
    pairs = {sym.ticker: sym.pair_id for sym in symbols}
    assert pairs == {"VCB": 11, "HPH": 22}


def test_ticker_to_slug_normalises() -> None:
    assert ticker_to_slug("HPH") == "hph"
    assert ticker_to_slug("VND") == "vnd"
    assert ticker_to_slug("  abc xyz  ") == "abc-xyz"


def test_merge_history_with_prices_prefers_extra_on_overlap() -> None:
    primary = [
        PriceBar(time="2024-01-02", open=100, high=105, low=99, close=104, volume=10),
        PriceBar(time="2024-01-03", open=105, high=110, low=104, close=108, volume=15),
    ]
    extra = [
        PriceBar(time="2024-01-02", open=999, high=999, low=999, close=999, volume=999),
        PriceBar(time="2023-12-29", open=98, high=101, low=97, close=100, volume=5),
    ]
    merged = merge_history_with_prices(primary, extra)
    assert [bar.time for bar in merged] == ["2023-12-29", "2024-01-02", "2024-01-03"]
    assert merged[1].close == pytest.approx(999.0)


def test_sync_investing_prices_skips_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / DEFAULT_INVESTING_PRICES_DIR).mkdir()
    existing = tmp_path / DEFAULT_INVESTING_PRICES_DIR / "VCB.json"
    existing.write_text(json.dumps({"items": [{"time": "x", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]}), encoding="utf-8")

    called = {"count": 0}

    def fake_fetch(pair_id, start_date, end_date, *args, **kwargs):
        called["count"] += 1
        return [PriceBar(time=start_date, open=1, high=1, low=1, close=1, volume=1)]

    monkeypatch.setattr("vietnam_algo.libs.data_investing.fetch_investing_history_chunked", fake_fetch)
    paths = sync_investing_prices(
        id_map={"VCB": 1, "ACB": 2},
        start_date="2024-01-01",
        end_date="2024-12-31",
        data_dir=tmp_path,
        skip_existing=True,
    )
    assert called["count"] == 1
    assert "ACB" in paths and "VCB" in paths


def test_sync_investing_prices_validates_dates(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        sync_investing_prices(id_map={"VCB": 1}, start_date="2024-bad", end_date="2024-01-01", data_dir=tmp_path)
    with pytest.raises(ValueError):
        sync_investing_prices(id_map={"VCB": 1}, start_date="2024-06-01", end_date="2024-01-01", data_dir=tmp_path)
    with pytest.raises(ValueError):
        sync_investing_prices(id_map={}, start_date="2024-01-01", end_date="2024-02-01", data_dir=tmp_path)


def test_sync_investing_prices_handles_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(pair_id, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("vietnam_algo.libs.data_investing.fetch_investing_history_chunked", fake_fetch)
    paths = sync_investing_prices(
        id_map={"FAIL": 7},
        start_date="2024-01-01",
        end_date="2024-02-01",
        data_dir=tmp_path,
        skip_existing=False,
    )
    assert paths == {}


def test_fetch_investing_history_requires_iso_dates() -> None:
    with pytest.raises(ValueError):
        fetch_investing_history(pair_id=1, start_date="2024-01", end_date="2024-01-31")
    with pytest.raises(ValueError):
        fetch_investing_history(pair_id=1, start_date="2024-12-31", end_date="2024-01-01")


def test_investing_symbol_url_helpers() -> None:
    sym = InvestingSymbol(ticker="ACB", pair_id=41913, slug="asia-commercial-bank")
    assert sym.historical_url.endswith("/equities/asia-commercial-bank-historical-data")
    assert "41913" in sym.api_url_template
    assert "start-date={start}" in sym.api_url_template


def test_load_investing_prices_skips_bad_rows(tmp_path: Path) -> None:
    path = tmp_path / DEFAULT_INVESTING_PRICES_DIR
    path.mkdir()
    (path / "VCB.json").write_text(
        json.dumps({"items": [{"time": "2024-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}, {"broken": True}]}),
        encoding="utf-8",
    )
    bars = load_investing_prices("VCB", data_dir=tmp_path)
    assert len(bars) == 1
    assert bars[0].time == "2024-01-01"


def test_fetch_investing_symbols_raises_on_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_html(url, *args, **kwargs):
        raise RuntimeError(f"{url} -> Connection reset")

    monkeypatch.setattr("vietnam_algo.libs.data_investing.fetch_investing_html", fake_html)
    with pytest.raises(RuntimeError, match="Connection reset"):
        fetch_investing_symbols()


def test_fetch_investing_history_chunked_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    payload_a = {"data": [{"date": "2024-01-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]}
    payload_b = {"data": [{"date": "2024-01-02", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2}]}
    payload_c = {"data": [{"date": "2024-02-01", "open": 3, "high": 3, "low": 3, "close": 3, "volume": 3}]}

    responses = iter([payload_a, payload_b, payload_c])

    def fake_fetch(pair_id, start_date, end_date, *args, **kwargs):
        return parse_investing_bars(next(responses))

    monkeypatch.setattr("vietnam_algo.libs.data_investing.fetch_investing_history", fake_fetch)
    bars = fetch_investing_history_chunked(pair_id=1, start_date="2024-01-01", end_date="2024-02-15", chunk_days=20, sleep_seconds=0)
    times = [bar.time for bar in bars]
    assert times == ["2024-01-02", "2024-02-01"]
    assert bars[0].close == pytest.approx(2.0)
