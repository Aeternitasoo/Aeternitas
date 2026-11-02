"""Offline-first daily market records, A0 admission and calendar-based aggregation.

Finnhub access is explicit. Importing this module never performs I/O. No price
series is relabelled raw, and captured observations are never certified PIT.
"""

from collections import defaultdict
import csv
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from point_in_time import POLICY_VERSION, parse_timestamp, time_boundary_issues
from research_runtime import WebSources
from sources.sec import SourceError


VERSION = "market-daily-v1"
CANDLES_URL = "https://finnhub.io/api/v1/stock/candle"
ADJUSTMENTS = {"raw", "split_adjusted", "total_return_adjusted", "unknown"}
CSV_FIELDS = {"symbol", "exchange", "timezone", "trade_date", "open", "high", "low", "close", "volume",
              "currency", "provider", "source_version", "available_at", "availability_basis", "retrieved_at",
              "adjustment", "data_mode"}


class MarketError(ValueError):
    """Diagnostic codes contain no provider body, headers or credential values."""


def iso_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise MarketError("invalid_date: use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise MarketError("invalid_date") from None


def zone(name):
    try:
        return ZoneInfo(name)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise MarketError("invalid_timezone") from None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise MarketError("invalid_numeric_type")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise MarketError("invalid_numeric_value") from None
    if not result.is_finite() or len(result.as_tuple().digits) > 34 or result and not -24 <= result.adjusted() <= 24:
        raise MarketError("numeric_value_out_of_bounds")
    return result


def _stamp(value):
    return parse_timestamp(value) if value else None


def _json(value):
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, (date, datetime)): return value.isoformat()
    if isinstance(value, frozenset): return sorted(value)
    raise TypeError("Unsupported market serialization type")


def dumps(value):
    return json.dumps(value, default=_json, ensure_ascii=False, indent=2) + "\n"


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    exchange: str
    timezone: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    currency: str
    provider: str
    source_version: str
    source_id: str
    available_at: datetime | None
    availability_basis: str
    retrieved_at: datetime | None
    adjustment: str
    data_mode: str
    trading_timestamp: int | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.:-]{0,31}", self.symbol):
            raise MarketError("invalid_symbol")
        if not re.fullmatch(r"[A-Z0-9]{4}", self.exchange) or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise MarketError("invalid_exchange_or_currency")
        zone(self.timezone)
        if type(self.trade_date) is not date:
            raise MarketError("invalid_trade_date")
        prices = [self.open, self.high, self.low, self.close]
        if any(not isinstance(p, Decimal) or number(p) <= 0 for p in prices):
            raise MarketError("prices_must_be_positive_decimal")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise MarketError("invalid_ohlc_relationship")
        if type(self.volume) is not int or not 0 <= self.volume <= 10 ** 24:
            raise MarketError("invalid_volume")
        if self.adjustment not in ADJUSTMENTS:
            raise MarketError("invalid_adjustment")
        if self.availability_basis not in {"source_timestamp", "declared", "unknown"}:
            raise MarketError("invalid_availability_basis")
        if (self.available_at is None) != (self.availability_basis == "unknown"):
            raise MarketError("availability_basis_mismatch")
        for stamp in (self.available_at, self.retrieved_at):
            if stamp is not None and (not isinstance(stamp, datetime) or stamp.utcoffset() is None):
                raise MarketError("timestamp_requires_timezone")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in
               (self.provider, self.source_version, self.source_id, self.data_mode)):
            raise MarketError("missing_source_identity")
        # An EOD bar cannot be published on a preceding local trading date.
        if self.available_at and self.available_at.astimezone(zone(self.timezone)).date() < self.trade_date:
            raise MarketError("availability_precedes_trading_date")
        if self.retrieved_at and self.retrieved_at.astimezone(zone(self.timezone)).date() < self.trade_date:
            raise MarketError("retrieval_precedes_trading_date")


@dataclass(frozen=True)
class MarketDataset:
    bars: tuple[DailyBar, ...]
    source: dict

    def __post_init__(self):
        if not self.bars:
            raise MarketError("no_data: source contains no daily bars")
        seen = set()
        for bar in self.bars:
            if not isinstance(bar, DailyBar): raise MarketError("invalid_daily_record")
            key = (bar.symbol, bar.exchange, bar.trade_date)
            if key in seen: raise MarketError("duplicate_daily_record")
            seen.add(key)


def _read_bytes(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024: raise MarketError("source_size_limit")
    return raw, hashlib.sha256(raw).hexdigest()


def load_csv(path):
    raw, sha = _read_bytes(path)
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)) or set(reader.fieldnames) != CSV_FIELDS:
            raise MarketError("csv_columns_mismatch")
        bars = []
        for line, row in enumerate(reader, 2):
            if line > 20001: raise MarketError("row_limit")
            try:
                if None in row or any(v is None for v in row.values()): raise MarketError("csv_row_width")
                volume = number(row["volume"])
                if volume != volume.to_integral_value(): raise MarketError("fractional_volume")
                bars.append(DailyBar(symbol=row["symbol"], exchange=row["exchange"], timezone=row["timezone"],
                    trade_date=iso_day(row["trade_date"]), open=number(row["open"]), high=number(row["high"]),
                    low=number(row["low"]), close=number(row["close"]), volume=int(volume), currency=row["currency"],
                    provider=row["provider"], source_version=row["source_version"], source_id=f"csv:{sha}",
                    available_at=_stamp(row["available_at"]), availability_basis=row["availability_basis"],
                    retrieved_at=_stamp(row["retrieved_at"]), adjustment=row["adjustment"], data_mode=row["data_mode"]))
            except (ValueError, TypeError, InvalidOperation):
                raise MarketError(f"invalid_csv_row: line {line}; no partial dataset returned") from None
    except (UnicodeError, csv.Error):
        raise MarketError("invalid_csv_encoding_or_syntax") from None
    return MarketDataset(tuple(bars), {"source_id": f"csv:{sha}", "sha256": sha,
                                      "path": str(Path(path).resolve()), "adapter_version": VERSION})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise MarketError("duplicate_json_key")
        result[key] = value
    return result


def normalize_finnhub(raw, source, *, symbol, exchange, timezone_name, currency, data_mode="live_api"):
    try:
        if source.get("sha256") != hashlib.sha256(raw).hexdigest():
            raise MarketError("source_fingerprint_mismatch")
        data = json.loads(raw, parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                          object_pairs_hook=_pairs)
        if not isinstance(data, dict): raise ValueError()
        if data.get("s") == "no_data": raise MarketError("no_data: Finnhub returned no_data")
        if data.get("s") != "ok": raise ValueError()
        arrays = [data.get(k) for k in ("o", "h", "l", "c", "v", "t")]
        if any(not isinstance(a, list) for a in arrays): raise ValueError()
        if not arrays[0]: raise MarketError("no_data: Finnhub returned empty arrays")
        if len(arrays[0]) > 20000 or len({len(a) for a in arrays}) != 1: raise ValueError()
        bars = []
        for o, h, l, c, v, t in zip(*arrays):
            if type(t) is not int: raise ValueError()
            if any(type(value) not in (int, Decimal) for value in (o, h, l, c, v)): raise ValueError()
            volume = number(v)
            if volume != volume.to_integral_value(): raise ValueError()
            bars.append(DailyBar(symbol, exchange, timezone_name,
                datetime.fromtimestamp(t, timezone.utc).astimezone(zone(timezone_name)).date(),
                number(o), number(h), number(l), number(c), int(volume), currency, "finnhub",
                source["sha256"], source["sha256"], None, "unknown", parse_timestamp(source["fetched_at"]),
                "split_adjusted", data_mode, t))
        return MarketDataset(tuple(bars), {**source, "adapter_version": VERSION,
            "timestamp_date_basis": "provider_epoch_converted_to_user_supplied_exchange_timezone",
            "availability_statement": "Provider supplies candle timestamps, not historical publication/vintage timestamps",
            "adjustment_statement": "Official Stock Candles spec: daily data adjusted for splits; no total-return claim"})
    except MarketError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        raise MarketError("invalid_finnhub_candles_response") from None


def fetch_finnhub(root, key, *, symbol, start, end, exchange, timezone_name, currency, budget, transport=None,
                  data_mode="live_api"):
    start, end = iso_day(start), iso_day(end)
    if start > end: raise MarketError("invalid_date_range")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.:-]{0,31}", symbol): raise MarketError("invalid_symbol")
    tz = zone(timezone_name)
    if not re.fullmatch(r"[A-Z0-9]{4}", exchange) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise MarketError("invalid_exchange_or_currency")
    if not isinstance(key, str) or not key or "\n" in key or "\r" in key:
        raise MarketError("FINNHUB_API_KEY_missing_or_invalid")
    params = {"symbol": symbol, "resolution": "D", "from": int(datetime.combine(start, time.min, tz).timestamp()),
              "to": int(datetime.combine(end + timedelta(days=1), time.min, tz).timestamp()) - 1}
    def validate(raw):
        if key.encode() in raw: raise MarketError("credential_echo_rejected")
        normalize_finnhub(raw, {"sha256": hashlib.sha256(raw).hexdigest(),
            "fetched_at": datetime.now(timezone.utc).isoformat()}, symbol=symbol, exchange=exchange,
            timezone_name=timezone_name, currency=currency, data_mode=data_mode)
    try:
        with WebSources(root, budget, transport=transport) as web:
            raw, source = web.get(CANDLES_URL, params=params, headers={"X-Finnhub-Token": key}, refresh=True,
                                  max_bytes=5 * 1024 * 1024, validate_response=validate)
    except SourceError as error:
        # Existing client errors omit provider bodies and transport exception text.
        raise MarketError(str(error).replace(key, "[redacted]")) from None
    dataset = normalize_finnhub(raw, source, symbol=symbol, exchange=exchange, timezone_name=timezone_name,
                               currency=currency, data_mode=data_mode)
    if any(not start <= bar.trade_date <= end for bar in dataset.bars):
        raise MarketError("provider_returned_out_of_range_dates")
    return dataset


@dataclass(frozen=True)
class SessionCalendar:
    exchange: str
    timezone: str
    coverage_start: date
    coverage_end: date
    sessions: frozenset[date]
    available_at: datetime
    retrieved_at: datetime
    source: str
    version: str
    sha256: str

    def __post_init__(self):
        if self.coverage_start > self.coverage_end or any(not self.coverage_start <= d <= self.coverage_end for d in self.sessions):
            raise MarketError("invalid_calendar_coverage")
        zone(self.timezone)
        if not re.fullmatch(r"[A-Z0-9]{4}", self.exchange): raise MarketError("invalid_calendar_exchange")
        if any(not isinstance(t, datetime) or t.utcoffset() is None for t in (self.available_at, self.retrieved_at)):
            raise MarketError("invalid_calendar_timestamps")

    @classmethod
    def load(cls, path):
        raw, sha = _read_bytes(path)
        try:
            data = json.loads(raw, object_pairs_hook=_pairs)
            start, end = iso_day(data["coverage_start"]), iso_day(data["coverage_end"])
            sessions = [iso_day(d) for d in data["sessions"]]
            if start > end or len(sessions) != len(set(sessions)) or any(not start <= d <= end for d in sessions):
                raise ValueError()
            if not re.fullmatch(r"[A-Z0-9]{4}", data["exchange"]): raise ValueError()
            zone(data["timezone"])
            if any(not isinstance(data[k], str) or not data[k].strip() for k in ("source", "version")): raise ValueError()
            return cls(data["exchange"], data["timezone"], start, end, frozenset(sessions),
                       parse_timestamp(data["available_at"]), parse_timestamp(data["retrieved_at"]),
                       data["source"], data["version"], sha)
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise MarketError("invalid_session_calendar") from None


def _period(day, interval):
    if interval == "1d": return day, day
    if interval == "1w":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    start = day.replace(day=1)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, next_month - timedelta(days=1)


def query(dataset, *, symbol, start, end, cutoff, interval="1d", time_policy="strict", calendar=None,
          include_incomplete=False):
    start, end, cutoff_at = iso_day(start), iso_day(end), parse_timestamp(cutoff)
    if start > end or interval not in {"1d", "1w", "1mo"} or time_policy not in {"strict", "captured"}:
        raise MarketError("invalid_query")
    selected = sorted((b for b in dataset.bars if b.symbol == symbol and start <= b.trade_date <= end),
                      key=lambda b: b.trade_date)
    if not selected: raise MarketError("no_data: requested symbol/range has no records")
    if len({(b.exchange, b.timezone, b.currency, b.adjustment, b.provider, b.data_mode) for b in selected}) != 1:
        raise MarketError("mixed_market_identity_currency_provider_or_adjustment")
    first = selected[0]
    if calendar and (calendar.exchange, calendar.timezone) != (first.exchange, first.timezone):
        raise MarketError("calendar_identity_mismatch")
    calendar_issues = time_boundary_issues(calendar.available_at, calendar.retrieved_at, cutoff_at) if calendar else []
    allowed, rejected = [], []
    for bar in selected:
        strict_issues = time_boundary_issues(bar.available_at, bar.retrieved_at, cutoff_at)
        if bar.adjustment != "raw": strict_issues.append("adjustment_vintage_not_verified")
        issues = [i for i in strict_issues if time_policy == "strict" or i not in
                  {"availability_time_unknown", "adjustment_vintage_not_verified"}]
        # Conservative EOD closure: wait through the entire local trade date.
        if datetime.combine(bar.trade_date + timedelta(days=1), time.min, zone(bar.timezone)) > cutoff_at:
            issues.append("daily_period_not_closed")
        if calendar_issues:
            issues.append("calendar_not_known_by_cutoff")
        if calendar and not calendar.coverage_start <= bar.trade_date <= calendar.coverage_end:
            issues.append("calendar_coverage_missing")
        elif calendar and bar.trade_date not in calendar.sessions:
            issues.append("not_a_declared_trading_session")
        if issues:
            rejected.append({"trade_date": bar.trade_date.isoformat(), "reasons": issues})
        else:
            allowed.append((bar, strict_issues))
    grouped = defaultdict(list)
    for bar, warnings in allowed: grouped[_period(bar.trade_date, interval)].append((bar, warnings))
    expected_days = {d for d in calendar.sessions if start <= d <= end} if calendar and not calendar_issues else set()
    missing_days = expected_days - {b.trade_date for b in selected}
    for day in sorted(missing_days):
        rejected.append({"trade_date": day.isoformat(), "reasons": ["missing_daily_session"]})
    if interval != "1d":
        for day in expected_days:
            grouped.setdefault(_period(day, interval), [])
    records = []
    for (period_start, period_end), items in sorted(grouped.items()):
        bars = [b for b, _ in items]
        if not bars:
            rejected.append({"period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
                             "reasons": ["no_admissible_daily_sessions"]})
            continue
        reasons = []
        if interval != "1d":
            if not calendar: reasons.append("calendar_required_for_completeness")
            else:
                if calendar_issues: reasons.append("calendar_not_known_by_cutoff")
                if not calendar.coverage_start <= period_start <= period_end <= calendar.coverage_end:
                    reasons.append("calendar_coverage_missing")
                expected = {d for d in calendar.sessions if period_start <= d <= period_end}
                if expected != {b.trade_date for b in bars}: reasons.append("missing_daily_sessions")
            if datetime.combine(period_end + timedelta(days=1), time.min, zone(first.timezone)) > cutoff_at:
                reasons.append("period_not_closed")
        if reasons and not include_incomplete:
            rejected.append({"period_start": period_start.isoformat(), "period_end": period_end.isoformat(), "reasons": reasons})
            continue
        available = max((b.available_at for b in bars), default=None) if all(b.available_at for b in bars) else None
        retrieved = max(b.retrieved_at for b in bars)
        warnings = sorted({w for _, ws in items for w in ws} | set(reasons))
        record = asdict(bars[0])
        closed_at = datetime.combine(period_end + timedelta(days=1), time.min, zone(first.timezone))
        record.update(trade_date=bars[-1].trade_date, interval=interval, period_start=period_start, period_end=period_end,
            open=bars[0].open, high=max(b.high for b in bars), low=min(b.low for b in bars), close=bars[-1].close,
            volume=sum(b.volume for b in bars), available_at=available, retrieved_at=retrieved,
            trading_timestamp=bars[0].trading_timestamp if interval == "1d" else None,
            complete=not reasons, strict_replay_eligible=not warnings, quality_checks="passed",
            warnings=warnings, input_dates=[b.trade_date.isoformat() for b in bars],
            input_sources=[{"source_id": b.source_id, "source_version": b.source_version} for b in bars])
        if interval != "1d":
            identity = hashlib.sha256(dumps({"version": VERSION, "period": [period_start, period_end],
                "inputs": [asdict(b) for b in bars], "calendar_sha256": calendar.sha256 if calendar else None}).encode()).hexdigest()
            record.update(source_id=identity, source_version=VERSION, availability_basis="declared" if available else "unknown")
            if calendar:
                record["available_at"] = max(available, calendar.available_at) if available else None
                record["retrieved_at"] = max(retrieved, calendar.retrieved_at)
        record["period_closed_at"] = closed_at
        record["eligible_not_before"] = max(record["available_at"], record["retrieved_at"], closed_at) if record["available_at"] else None
        records.append(record)
    return {"schema_version": VERSION, "a0_policy_version": POLICY_VERSION, "symbol": symbol, "interval": interval,
            "start": start.isoformat(), "end": end.isoformat(), "cutoff": cutoff_at.isoformat(), "time_policy": time_policy,
            "status": "ok" if records else "unavailable", "records": records, "rejected": rejected,
            "source": dataset.source, "calendar": asdict(calendar) if calendar else None, "prediction_created": False,
            "coverage": {"calendar_coverage_declared": bool(calendar and not calendar_issues and
                calendar.coverage_start <= start <= end <= calendar.coverage_end),
                "missing_source_dates": [d.isoformat() for d in sorted(missing_days)]},
            "limitations": ["CSV timing is a source declaration, not independent publication certification.",
                "Captured policy permits inspection of already captured versions, not strict historical backtests.",
                "Unknown historical publication and adjusted-series vintage remain explicit.",
                "EOD/weekly/monthly closure uses next local calendar boundary conservatively."]}


def cli(args, config=None):
    """Called only after explicit CLI dispatch; no key needed for CSV queries."""
    if iso_day(args.start) > iso_day(args.end): raise MarketError("invalid_date_range")
    if args.command == "market": parse_timestamp(args.cutoff)
    if args.output and (args.output.exists() or args.output.is_symlink()): raise MarketError("output_destination_exists")
    if args.command == "market" and args.csv:
        dataset = load_csv(args.csv)
    else:
        from research_runtime import Budget
        dataset = fetch_finnhub(args.data_dir, (config or {}).get("FINNHUB_API_KEY"), symbol=args.symbol,
            start=args.start, end=args.end, exchange=args.exchange, timezone_name=args.timezone,
            currency=args.currency, budget=Budget(seconds=60))
    if args.command == "market-check":
        result = {"status": "online_response_received", "daily_count": len(dataset.bars), "source": dataset.source,
                  "adjustment": "split_adjusted", "historical_availability": "unknown",
                  "strict_replay_eligible": False, "prediction_created": False}
    else:
        result = query(dataset, symbol=args.symbol, start=args.start, end=args.end, cutoff=args.cutoff,
            interval=args.interval, time_policy=args.time_policy,
            calendar=SessionCalendar.load(args.calendar) if args.calendar else None,
            include_incomplete=args.include_incomplete)
    output = dumps(result)
    if args.output:
        # Explicit destination only; never overwrite an existing report or data file.
        with args.output.open("x", encoding="utf-8") as stream: stream.write(output)
        print(f"Market result saved: {args.output}")
    else:
        print(output, end="")
    return 2 if result["status"] == "unavailable" else 0
