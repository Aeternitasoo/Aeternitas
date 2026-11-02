"""Offline, version-pinned market inputs. No forecast, network or database I/O.

Reuse the market/A0 gate; a manifest references existing source bytes instead of
creating a second raw snapshot store. Only research_inputs may enter a task.
"""

from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
from uuid import uuid4

from evidence_repository import digest
from market_data import (MarketError, SessionCalendar, VERSION as MARKET_VERSION,
                         iso_day, load_csv, query)
from point_in_time import POLICY_VERSION, parse_timestamp


SCHEMA_VERSION = "time-machine-manifest-v1"
POLICY = "time-machine-market-gate-v1"
INTERVALS = {"1d", "1w", "1mo"}


class TimeMachineError(ValueError):
    """Invalid configuration/source integrity; no partial manifest is published."""


def _plain(value):
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise TimeMachineError("timestamp_requires_timezone")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date): return value.isoformat()
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, dict): return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)): return sorted(_plain(v) for v in value)
    return value


def _version(actual, expected, label):
    if not isinstance(actual, str) or not re.fullmatch(r"[0-9a-f]{64}", actual):
        raise TimeMachineError(f"{label}_sha256_invalid")
    if expected is not None and expected != actual:
        raise TimeMachineError(f"{label}_version_mismatch")


def _input_identity(content):
    return {k: content[k] for k in ("query", "policy", "dataset", "calendar", "candidates")}


def run(csv_path, *, symbol, start, end, cutoff, data_types=("1d",),
        calendar_path=None, expected_dataset_sha256=None, expected_calendar_sha256=None,
        mode="strict", assumptions=(), run_id=None, created_at=None):
    """Read one immutable CSV version and return a JSON-ready input manifest.

    ``assumed`` explicitly reuses market ``captured`` policy; it never invents a
    publication time or overrides a known future timestamp. Completeness remains
    enforced. CSV declarations are not independent vendor-vintage certification.
    """
    cutoff_at = parse_timestamp(cutoff)
    if iso_day(start) > iso_day(end): raise TimeMachineError("invalid_date_range")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.:-]{0,31}", symbol):
        raise TimeMachineError("invalid_symbol")
    if (not isinstance(data_types, (tuple, list)) or not data_types or
            any(not isinstance(t, str) or t not in INTERVALS for t in data_types) or len(set(data_types)) != len(data_types)):
        raise TimeMachineError("unsupported_or_duplicate_data_types")
    if mode not in {"strict", "assumed"}: raise TimeMachineError("invalid_mode")
    if (not isinstance(assumptions, (tuple, list)) or
            any(not isinstance(a, str) or not a.strip() or len(a) > 2000 for a in assumptions)):
        raise TimeMachineError("invalid_assumptions")
    assumptions = sorted(set(a.strip() for a in assumptions))
    if (mode == "assumed") != bool(assumptions):
        raise TimeMachineError("assumed_mode_requires_explicit_assumptions_only")
    if expected_calendar_sha256 is not None and calendar_path is None:
        raise TimeMachineError("calendar_version_requires_calendar")
    stamp = parse_timestamp(created_at) if created_at is not None else datetime.now(timezone.utc)
    run_id = uuid4().hex if run_id is None else run_id
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise TimeMachineError("invalid_run_id")

    dataset = load_csv(csv_path)  # Bounded read; hashes the same bytes it parses.
    _version(dataset.source["sha256"], expected_dataset_sha256, "dataset")
    calendar = SessionCalendar.load(calendar_path) if calendar_path is not None else None
    if calendar:
        _version(calendar.sha256, expected_calendar_sha256, "calendar")
    selected = sorted((b for b in dataset.bars if b.symbol == symbol and start <= str(b.trade_date) <= end),
                      key=lambda b: (b.exchange, b.trade_date))
    candidates = [{"record_id": digest(_plain(asdict(b))), "trade_date": str(b.trade_date),
                   "source_id": b.source_id, "source_version": b.source_version,
                   "available_at": _plain(b.available_at), "retrieved_at": _plain(b.retrieved_at),
                   "availability_basis": b.availability_basis, "adjustment": b.adjustment}
                  for b in selected]
    by_date = {c["trade_date"]: c["record_id"] for c in candidates}
    inputs, diagnostics, results, admitted_dates = [], [], {}, set()
    for interval in sorted(data_types):
        try:
            result = query(dataset, symbol=symbol, start=start, end=end, cutoff=cutoff,
                           interval=interval, time_policy="strict" if mode == "strict" else "captured",
                           calendar=calendar, include_incomplete=False)
        except MarketError as error:
            # No partial output on ambiguous identities/adjustments. Other query
            # errors are configuration failures and must not be relabelled safe.
            code = str(error).split(":", 1)[0]
            if code not in {"no_data", "mixed_market_identity_currency_provider_or_adjustment", "calendar_identity_mismatch"}:
                raise
            result = {"records": [], "rejected": [{"reasons": [code]}],
                      "coverage": {"calendar_coverage_declared": False, "missing_source_dates": []}}
        dates = set()
        for record in result["records"]:
            record = _plain(record)
            if mode == "assumed": record["strict_replay_eligible"] = False
            ids = [by_date[d] for d in record["input_dates"]]
            inputs.append({"data_type": interval, "record_id": digest(record), "record": record,
                           "daily_input_ids": ids})
            dates.update(record["input_dates"])
        admitted_dates.update(dates)
        issues = [{"data_type": interval, **_plain(r)} for r in result["rejected"]]
        # Missing calendar knowledge is explicit even for daily queries; actual
        # records can pass timing while range completeness remains unverified.
        if not result["coverage"]["calendar_coverage_declared"]:
            issues.append({"data_type": interval, "reasons": ["session_coverage_unverified"]})
        diagnostics.extend(issues)
        results[interval] = {"output_record_count": len(result["records"]),
                             "admitted_daily_count": len(dates),
                             "excluded_candidate_count": len(selected) - len(dates),
                             "coverage": result["coverage"]}
    excluded = []
    for candidate in candidates:
        if candidate["trade_date"] in admitted_dates: continue
        reasons = set()
        for diagnostic in diagnostics:
            day = candidate["trade_date"]
            if ("trade_date" not in diagnostic and "period_start" not in diagnostic or
                    diagnostic.get("trade_date") == day or
                    diagnostic.get("period_start", "~") <= day <= diagnostic.get("period_end", "")):
                reasons.update(diagnostic["reasons"])
        excluded.append({"record_id": candidate["record_id"], "trade_date": candidate["trade_date"],
                         "reasons": sorted(reasons or {"no_admissible_output"})})
    counts = Counter(reason for diagnostic in diagnostics for reason in diagnostic["reasons"])
    status = "blocked" if not inputs else "limited" if diagnostics else "ready"
    content = _plain({
        "query": {"symbol": symbol, "start": start, "end": end, "cutoff": cutoff_at,
                  "data_types": sorted(data_types)},
        "policy": {"version": POLICY, "a0_version": POLICY_VERSION, "market_version": MARKET_VERSION,
                   "mode": mode, "market_time_policy": "strict" if mode == "strict" else "captured",
                   "assumptions": assumptions, "include_incomplete": False},
        "dataset": {"source_id": dataset.source["source_id"], "sha256": dataset.source["sha256"],
                    "adapter_version": dataset.source["adapter_version"],
                    "source_versions": sorted({b.source_version for b in selected})},
        "calendar": asdict(calendar) if calendar else None,
        "candidates": candidates, "research_inputs": inputs, "excluded_candidates": excluded,
        "diagnostics": diagnostics, "results_by_type": results,
        "summary": {"candidate_count": len(candidates), "admitted_daily_count": len(admitted_dates),
                    "rejected_candidate_count": len(excluded), "output_record_count": len(inputs),
                    "diagnostic_count": len(diagnostics), "issue_counts": dict(sorted(counts.items()))},
        "admission_status": status,
        "strict_timing_passed": mode == "strict" and status == "ready",
        "classification": "strict_declared_input_timing" if mode == "strict" else "assumption_based_simulation",
        "prediction_created": False,
        "limitations": ["Source times and raw adjustment labels are declarations, not independent vendor-vintage certification.",
                        "A hash identifies input content; it does not authenticate publication history.",
                        "Only research_inputs are admitted data; candidates and diagnostics are audit metadata.",
                        "No registered prediction, outcome evaluation or return backtest is performed."],
    })
    return {"schema_version": SCHEMA_VERSION,
            "run_metadata": {"run_id": run_id, "created_at": _plain(stamp),
                             "source_path": str(Path(csv_path).resolve()),
                             "calendar_path": str(Path(calendar_path).resolve()) if calendar_path else None},
            "input_fingerprint": digest(_input_identity(content)),
            "manifest_fingerprint": digest(content), "content": content}


def verify_manifest(manifest):
    """Check content hashes offline; metadata is deliberately outside fingerprints.

    This is checksum verification, not a signature, source authentication or a
    fresh read of original files. Re-run with pinned source hashes to compare.
    """
    try:
        if manifest["schema_version"] != SCHEMA_VERSION: raise ValueError()
        content = manifest["content"]
        if (manifest["input_fingerprint"] != digest(_input_identity(content)) or
                manifest["manifest_fingerprint"] != digest(content)):
            raise ValueError()
        for item in content["research_inputs"]:
            if item["record_id"] != digest(item["record"]): raise ValueError()
        return True
    except (KeyError, TypeError, ValueError):
        raise TimeMachineError("manifest_integrity_mismatch") from None


def save_manifest(manifest, path):
    verify_manifest(manifest)
    # Explicit path only, exclusive create. No default project writes or raw
    # source copies; paths/receipts retain the existing SHA-256 snapshot model.
    with Path(path).open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def cli(args):
    if args.output and (args.output.exists() or args.output.is_symlink()):
        raise TimeMachineError("output_destination_exists")
    manifest = run(args.csv, symbol=args.symbol, start=args.start, end=args.end, cutoff=args.cutoff,
                   data_types=args.types, calendar_path=args.calendar,
                   expected_dataset_sha256=args.dataset_sha256, expected_calendar_sha256=args.calendar_sha256,
                   mode=args.mode, assumptions=args.assumption)
    if args.output: save_manifest(manifest, args.output)
    else: print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["content"]["admission_status"] == "ready" else 2
