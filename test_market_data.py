"""Synthetic-only market v1 tests; all HTTP is MockTransport, no database."""

import csv
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

import aeternitas
from market_data import (CANDLES_URL, MarketDataset, MarketError, SessionCalendar, dumps,
                         fetch_finnhub, load_csv, normalize_finnhub, query)
from point_in_time import parse_timestamp


FIXTURE = Path(__file__).parent / "tests/fixtures/market_daily_synthetic_v1.csv"
CALENDAR = FIXTURE.with_name("market_calendar_synthetic_v1.json")
KEY = "synthetic-candle-token-not-a-real-key"
LATE = "2025-03-03T12:00:00Z"


class TestBudget:
    __test__ = False
    def __init__(self): self.pauses = []
    def remaining(self): return 60
    def pause(self, seconds): self.pauses.append(seconds)


def request_query(data=None, **overrides):
    return query(data or load_csv(FIXTURE), **{"symbol": "DEMO", "start": "2025-01-01", "end": "2025-02-28",
                 "cutoff": LATE, **overrides})


def payload():
    # Same OHLCV as the first two CSV rows; epochs are local midnight in New York.
    return {"s": "ok", "o": [10, 11], "h": [12, 13], "l": [9, 10], "c": [11, 12], "v": [100, 200],
            "t": [int(parse_timestamp(d).timestamp()) for d in ["2025-01-02T05:00:00Z", "2025-01-03T05:00:00Z"]]}


def decode(data):
    raw = json.dumps(data).encode()
    return normalize_finnhub(raw, {"sha256": hashlib.sha256(raw).hexdigest(), "fetched_at": LATE},
        symbol="DEMO", exchange="XSIM", timezone_name="America/New_York", currency="USD", data_mode="controlled_test")


def fetch(tmp_path, handler, **overrides):
    return fetch_finnhub(tmp_path, KEY, symbol=overrides.get("symbol", "DEMO"),
        start=overrides.get("start", "2025-01-01"), end=overrides.get("end", "2025-01-31"),
        exchange="XSIM", timezone_name="America/New_York", currency="USD", budget=overrides.get("budget", TestBudget()),
        transport=httpx.MockTransport(handler), data_mode="controlled_test")


def write_rows(tmp_path, mutate):
    with FIXTURE.open() as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    mutate(fields, rows)
    path = tmp_path / "synthetic.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader(); writer.writerows(rows)
    return path


def test_csv_decimal_zero_volume_dates_and_source_fingerprint():
    dataset = load_csv(FIXTURE)
    assert dataset.bars[0].open == Decimal("10")
    assert dataset.bars[3].volume == 0
    assert dataset.source["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    result = request_query()
    assert len(result["records"]) == 6 and not result["prediction_created"]
    assert result["records"][0]["interval"] == "1d"
    assert all(r["strict_replay_eligible"] and r["data_mode"] == "controlled_test" for r in result["records"])


@pytest.mark.parametrize("field,value", [("open", "0"), ("low", "-1"), ("high", "9"), ("close", "99"),
    ("open", "NaN"), ("close", "Infinity"), ("volume", "-1"), ("volume", "1.5"),
    ("open", ""), ("trade_date", "2025-1-02"), ("available_at", "2025-01-03"),
    ("available_at", "2025-01-01T00:00:00Z"), ("adjustment", "adjusted"), ("currency", "usd")])
def test_invalid_csv_is_rejected_atomically(tmp_path, field, value):
    path = write_rows(tmp_path, lambda fields, rows: rows[-1].update({field: value}))
    with pytest.raises(MarketError, match="invalid_csv_row"): load_csv(path)


def test_missing_column_and_duplicate_records_are_not_silently_accepted(tmp_path):
    path = write_rows(tmp_path, lambda fields, rows: rows.append(rows[0].copy()))
    with pytest.raises(MarketError, match="duplicate"): load_csv(path)
    path.write_text("symbol,trade_date\nDEMO,2025-01-01\n")
    with pytest.raises(MarketError, match="columns"): load_csv(path)


def test_query_orders_dates_filters_symbol_and_checks_range():
    dataset = load_csv(FIXTURE)
    reversed_data = MarketDataset(tuple(reversed(dataset.bars)), dataset.source)
    result = request_query(reversed_data, start="2025-01-03", end="2025-01-06")
    assert [str(r["trade_date"]) for r in result["records"]] == ["2025-01-03", "2025-01-06"]
    with pytest.raises(MarketError, match="no_data"): request_query(symbol="OTHER")
    with pytest.raises(MarketError, match="invalid_query"): request_query(start="2025-03-01")


@pytest.mark.parametrize("cutoff,count", [("2025-01-03T05:00:59Z", 0), ("2025-01-03T05:01:00Z", 1),
                                      ("2025-01-04T05:01:00Z", 2)])
def test_both_publication_and_capture_must_be_known_by_cutoff(cutoff, count):
    result = request_query(cutoff=cutoff)
    assert len(result["records"]) == count
    assert len(result["rejected"]) == 6 - count


def test_unknown_availability_does_not_become_a_retrieval_publication():
    data = load_csv(FIXTURE)
    bar = replace(data.bars[0], available_at=None, availability_basis="unknown")
    dataset = MarketDataset((bar,), data.source)
    strict = request_query(dataset)
    assert strict["status"] == "unavailable" and "availability_time_unknown" in strict["rejected"][0]["reasons"]
    captured = request_query(dataset, time_policy="captured")
    assert captured["records"][0]["available_at"] is None
    assert captured["records"][0]["strict_replay_eligible"] is False
    assert request_query(dataset, time_policy="captured", cutoff="2025-01-02T12:00:00Z")["status"] == "unavailable"


@pytest.mark.parametrize("adjustment", ["split_adjusted", "total_return_adjusted", "unknown"])
def test_adjustment_types_are_explicit_and_not_strictly_admitted(adjustment):
    data = load_csv(FIXTURE)
    dataset = MarketDataset((replace(data.bars[0], adjustment=adjustment),), data.source)
    assert request_query(dataset)["status"] == "unavailable"
    record = request_query(dataset, time_policy="captured")["records"][0]
    assert record["adjustment"] == adjustment and not record["strict_replay_eligible"]
    mixed = MarketDataset((data.bars[0], replace(data.bars[1], adjustment=adjustment)), data.source)
    with pytest.raises(MarketError, match="mixed_market"): request_query(mixed, time_policy="captured")


def test_weekly_and_monthly_ohlcv_use_explicit_synthetic_sessions():
    calendar = SessionCalendar.load(CALENDAR)
    weekly = request_query(interval="1w", calendar=calendar)
    assert len(weekly["records"]) == 3
    w = weekly["records"][0]
    assert (w["open"], w["high"], w["low"], w["close"], w["volume"]) == (10, 13, 9, 12, 300)
    assert str(w["period_start"]) == "2024-12-30" and str(w["period_end"]) == "2025-01-05"
    monthly = request_query(interval="1mo", calendar=calendar)
    assert len(monthly["records"]) == 2
    m = monthly["records"][0]
    assert (m["open"], m["high"], m["low"], m["close"], m["volume"]) == (10, 15, 9, 14, 600)
    assert m["complete"] and m["strict_replay_eligible"]
    assert m["eligible_not_before"] == parse_timestamp("2025-02-01T05:00:00Z")
    assert json.loads(dumps(monthly))["calendar"]["version"] == "synthetic-calendar-v1"


def test_missing_calendar_missing_daily_data_and_unfinished_period_are_explicit():
    assert request_query(interval="1w")["status"] == "unavailable"
    calendar = SessionCalendar.load(CALENDAR)
    data = load_csv(FIXTURE)
    missing = MarketDataset(tuple(b for b in data.bars if str(b.trade_date) != "2025-01-03"), data.source)
    result = request_query(missing, interval="1w", calendar=calendar)
    assert any("missing_daily_sessions" in r["reasons"] for r in result["rejected"])
    result = request_query(interval="1mo", calendar=calendar, cutoff="2025-01-08T06:00:00Z")
    assert not result["records"]
    partial = request_query(interval="1mo", calendar=calendar, cutoff="2025-01-08T06:00:00Z", include_incomplete=True)
    assert not partial["records"][0]["complete"] and "period_not_closed" in partial["records"][0]["warnings"]


def test_calendar_identity_coverage_and_knowledge_boundary():
    calendar = SessionCalendar.load(CALENDAR)
    with pytest.raises(MarketError, match="calendar_identity"):
        request_query(calendar=replace(calendar, exchange="XNYS"))
    result = request_query(interval="1w", calendar=replace(calendar, coverage_start=load_csv(FIXTURE).bars[0].trade_date))
    assert result["rejected"]
    future = replace(calendar, available_at=parse_timestamp("2030-01-01T00:00:00Z"))
    assert request_query(calendar=future)["status"] == "unavailable"


def test_finnhub_and_csv_share_daily_model_and_keep_distinct_provenance():
    api, csv_data = decode(payload()), load_csv(FIXTURE)
    fields = ("symbol", "exchange", "timezone", "trade_date", "open", "high", "low", "close", "volume", "currency")
    for left, right in zip(api.bars, csv_data.bars):
        assert {f: getattr(left, f) for f in fields} == {f: getattr(right, f) for f in fields}
    assert api.bars[0].adjustment == "split_adjusted" and api.bars[0].available_at is None
    assert request_query(api)["status"] == "unavailable"


@pytest.mark.parametrize("mutation", ["no_data", "empty", "missing", "length", "duplicate", "bad_status", "bad_time", "price"])
def test_bad_finnhub_shapes_are_not_successful_empty_data(mutation):
    data = payload()
    if mutation == "no_data": data = {"s": "no_data"}
    elif mutation == "empty": data = {"s": "ok", **{k: [] for k in "ohlcvt"}}
    elif mutation == "missing": del data["c"]
    elif mutation == "length": data["c"].pop()
    elif mutation == "duplicate": data["t"][1] = data["t"][0]
    elif mutation == "bad_status": data["s"] = "error"
    elif mutation == "bad_time": data["t"][0] = True
    else: data["o"][0] = 0
    with pytest.raises(MarketError): decode(data)


@pytest.mark.parametrize("symbol,start,end", [("DEMO", "2025-01-01", "2025-01-31"), ("MSFT", "2025-01-02", "2025-01-03")])
def test_request_parameters_header_secret_and_saved_snapshot(tmp_path, symbol, start, end):
    seen = []
    def provider(request):
        seen.append(request)
        assert request.headers["X-Finnhub-Token"] == KEY
        assert KEY not in str(request.url)
        return httpx.Response(200, json=payload())
    result = fetch(tmp_path, provider, symbol=symbol, start=start, end=end)
    parameters = dict(seen[0].url.params)
    assert parameters["symbol"] == symbol and parameters["resolution"] == "D"
    assert int(parameters["from"]) == int(parse_timestamp(start + "T00:00:00-05:00").timestamp())
    assert {b.symbol for b in result.bars} == {symbol}
    assert (tmp_path / result.source["snapshot_path"]).exists()
    assert all(KEY.encode() not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_bounded_retries_and_no_key_leak(tmp_path, status):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(status, json={"error": KEY})
    with pytest.raises(MarketError) as error: fetch(tmp_path, provider)
    assert KEY not in str(error.value) and KEY not in repr(error.value)
    assert len(calls) == (3 if status in {429, 500} else 1)
    assert not list(tmp_path.rglob("*.bin"))


def test_timeout_invalid_body_no_data_and_credential_echo(tmp_path):
    def timeout(request): raise httpx.ReadTimeout(KEY, request=request)
    with pytest.raises(MarketError, match="network / retry"): fetch(tmp_path, timeout)
    for wire in [b"not-json", json.dumps({"error": KEY}).encode(), b'{"s":"no_data"}']:
        with pytest.raises(MarketError) as error:
            fetch(tmp_path, lambda _: httpx.Response(200, content=wire, headers={"content-type": "application/json"}))
        assert KEY not in str(error.value)
    assert not list(tmp_path.rglob("*.bin"))


def test_rate_limit_retry_after_is_bounded_and_recovery_paced(tmp_path):
    calls, budget = [], TestBudget()
    def provider(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "2"}) if len(calls) == 1 else httpx.Response(200, json=payload())
    assert fetch(tmp_path, provider, budget=budget).bars
    assert budget.pauses == [1, 2, 1]
    with pytest.raises(MarketError, match="wait limit"):
        fetch(tmp_path, lambda _: httpx.Response(429, headers={"Retry-After": "999999"}))


def test_csv_cli_no_config_no_network_and_exclusive_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(aeternitas, "load_config", lambda *_: pytest.fail("CSV must not read credentials"))
    args = ["market", "DEMO", "--csv", str(FIXTURE), "--start", "2025-01-01", "--end", "2025-02-28",
            "--cutoff", LATE, "--interval", "1mo", "--calendar", str(CALENDAR)]
    assert aeternitas.main(args) == 0
    assert len(json.loads(capsys.readouterr().out)["records"]) == 2
    out = tmp_path / "market.json"
    assert aeternitas.main([*args, "--output", str(out)]) == 0
    before = out.read_bytes()
    assert aeternitas.main([*args, "--output", str(out)]) == 1
    assert out.read_bytes() == before


@pytest.mark.parametrize("command", ["market", "market-check"])
def test_help_is_offline(command, monkeypatch):
    monkeypatch.setattr(aeternitas, "load_config", lambda *_: pytest.fail("help must not read credentials"))
    with pytest.raises(SystemExit) as result: aeternitas.main([command, "--help"])
    assert result.value.code == 0


def test_entire_missing_period_and_partial_requested_week_are_diagnosed():
    data, calendar = load_csv(FIXTURE), SessionCalendar.load(CALENDAR)
    missing = MarketDataset(tuple(b for b in data.bars if b.trade_date.month != 2), data.source)
    result = request_query(missing, interval="1mo", calendar=calendar)
    assert len(result["records"]) == 1
    assert result["coverage"]["missing_source_dates"] == ["2025-02-03", "2025-02-04"]
    assert any("no_admissible_daily_sessions" in r["reasons"] for r in result["rejected"])
    clipped = request_query(start="2025-01-03", end="2025-01-03", interval="1w", calendar=calendar)
    assert clipped["status"] == "unavailable"
    assert any("missing_daily_sessions" in r["reasons"] for r in clipped["rejected"])


def test_daily_period_open_and_retrieval_before_availability_are_rejected():
    data = load_csv(FIXTURE)
    bar = replace(data.bars[0], available_at=parse_timestamp("2025-01-02T20:00:00Z"),
                  retrieved_at=parse_timestamp("2025-01-02T20:01:00Z"))
    result = request_query(MarketDataset((bar,), data.source), cutoff="2025-01-02T21:00:00Z")
    assert "daily_period_not_closed" in result["rejected"][0]["reasons"]
    wrong_order = replace(bar, retrieved_at=parse_timestamp("2025-01-02T19:00:00Z"))
    result = request_query(MarketDataset((wrong_order,), data.source))
    assert "retrieval_precedes_availability" in result["rejected"][0]["reasons"]


def test_http_media_and_out_of_range_responses_are_rejected(tmp_path):
    with pytest.raises(MarketError, match="content type"):
        fetch(tmp_path, lambda _: httpx.Response(200, content=b"<html>blocked</html>", headers={"content-type": "text/html"}))
    with pytest.raises(MarketError, match="out_of_range"):
        fetch(tmp_path, lambda _: httpx.Response(200, json=payload()), start="2025-02-01", end="2025-02-28")


def test_request_bounds_respect_exchange_timezone_dst(tmp_path):
    seen = []
    def provider(request):
        seen.append(dict(request.url.params))
        result = payload()
        result["t"] = [int(parse_timestamp(d).timestamp()) for d in ["2025-03-08T05:00:00Z", "2025-03-10T04:00:00Z"]]
        return httpx.Response(200, json=result)
    data = fetch(tmp_path, provider, start="2025-03-08", end="2025-03-10")
    assert [str(b.trade_date) for b in data.bars] == ["2025-03-08", "2025-03-10"]
    assert int(seen[0]["to"]) - int(seen[0]["from"]) + 1 == 3 * 86400 - 3600


def test_invalid_cutoff_or_existing_output_stops_before_online_fetch(tmp_path, monkeypatch, capsys):
    import market_data
    monkeypatch.setattr(aeternitas, "load_config", lambda *_: {})
    monkeypatch.setattr(market_data, "fetch_finnhub", lambda *a, **k: pytest.fail("Invalid arguments must not fetch"))
    args = ["market", "DEMO", "--finnhub", "--start", "2025-01-01", "--end", "2025-01-31"]
    assert aeternitas.main([*args, "--cutoff", "2025-01-01"]) == 1
    existing = tmp_path / "existing.json"; existing.write_text("preserved")
    assert aeternitas.main([*args, "--cutoff", LATE, "--output", str(existing)]) == 1
    assert existing.read_text() == "preserved"


def test_decimal_payload_and_duplicate_json_keys():
    raw = b'{"s":"ok","o":[0.1],"h":[0.3],"l":[0.1],"c":[0.2],"v":[0],"t":[1735794000]}'
    source = {"sha256": hashlib.sha256(raw).hexdigest(), "fetched_at": LATE}
    args = dict(symbol="DEMO", exchange="XSIM", timezone_name="America/New_York", currency="USD")
    assert normalize_finnhub(raw, source, **args).bars[0].close == Decimal("0.2")
    with pytest.raises(MarketError, match="duplicate_json"):
        duplicate = b'{"s":"ok","s":"no_data"}'
        normalize_finnhub(duplicate, {**source, "sha256": hashlib.sha256(duplicate).hexdigest()}, **args)
    with pytest.raises(MarketError, match="source_fingerprint"):
        normalize_finnhub(raw, {**source, "sha256": "0" * 64}, **args)


def test_signed_epoch_is_not_mistaken_for_an_invalid_timestamp():
    data = payload()
    data["t"] = [-86400, 86400]
    bars = decode(data).bars
    assert str(bars[0].trade_date) == "1969-12-30"
    assert str(bars[1].trade_date) == "1970-01-01"
