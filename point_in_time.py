"""Conservative, offline timing checks for archived research inputs.

This is an input gate, not historical vendor-vintage certification or a forecast.
Current SEC dates are conservatively available after the entire New York day.
Late snapshots are excluded even if the document claims an older publication.
"""

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import re
from zoneinfo import ZoneInfo

from research_pipeline import saved_report


POLICY_VERSION = "archived-input-timing-v1"
SEC_TIMEZONE = ZoneInfo("America/New_York")
KINDS = {"financial_fact", "filing", "news", "calculation"}
LIVE_EVIDENCE_MODES = {"live_api", "frozen_live_evidence"}


def parse_timestamp(value):
    """Accept only explicit, timezone-aware ISO timestamps; never guess a zone."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value
    ):
        raise ValueError("Timestamp must include seconds and a timezone, e.g. 2026-10-24T12:00:00Z")
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _iso(value):
    return value.isoformat() if value is not None else None


def time_boundary_issues(available, retrieved, cutoff, *, check_order=True):
    """Shared conservative A0 rule for already parsed, aware timestamps.

    A capture timestamp is not a publication timestamp. Both must be known and
    no later than cutoff; source-specific date rounding happens before here.
    """
    for value in (available, retrieved, cutoff):
        if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
            raise ValueError("Time boundaries require timezone-aware datetimes")
    if cutoff is None:
        raise ValueError("A cutoff is required")
    issues = []
    if available is None:
        issues.append("availability_time_unknown")
    elif available > cutoff:
        issues.append("availability_after_cutoff")
    if retrieved is None:
        issues.append("retrieval_time_unresolved")
    elif retrieved > cutoff:
        issues.append("snapshot_after_cutoff")
    if check_order and available is not None and retrieved is not None and retrieved < available:
        issues.append("retrieval_precedes_availability")
    return issues


def _publication_boundary(evidence):
    precision = evidence.get("time_precision")
    value = evidence.get("published_at")
    if precision == "second":
        return parse_timestamp(value), "provider_timestamp"
    if precision == "date" and evidence.get("kind") in {"financial_fact", "filing", "calculation"}:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Invalid SEC publication date")
        next_day = date.fromisoformat(value) + timedelta(days=1)
        return datetime.combine(next_day, time.min, SEC_TIMEZONE).astimezone(timezone.utc), "sec_date_end_conservative"
    raise ValueError("Unknown publication precision or date timezone")


def assess_inputs(report, cutoff):
    """Pure timing assessment. Caller must separately verify archive integrity."""
    cutoff_at = parse_timestamp(cutoff)
    entries = report.get("evidence", [])
    if not isinstance(entries, list):
        raise ValueError("Report evidence must be a list")
    ids = [entry["id"] for entry in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate evidence IDs")
    results = []
    by_id = {}
    fact_ids = {}
    for entry in entries:
        source = entry.get("source", {})
        locator = entry.get("locator", {})
        issues = []
        publication = retrieval = None
        policy = None
        if report.get("evidence_kind") not in LIVE_EVIDENCE_MODES or source.get("data_mode") != "live":
            issues.append("non_live_evidence")
        if entry.get("kind") not in KINDS:
            issues.append("unsupported_evidence_kind")
        if not source.get("snapshot_path") or not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")):
            issues.append("missing_snapshot_identity")
        try:
            publication, policy = _publication_boundary(entry)
        except (ValueError, TypeError, OverflowError):
            issues.append("publication_time_unresolved")
        try:
            retrieval = parse_timestamp(source.get("fetched_at"))
        except (ValueError, TypeError, OverflowError):
            issues.append("retrieval_time_unresolved")
        aliases = {"availability_time_unknown": "publication_time_unresolved",
                   "availability_after_cutoff": "publication_after_cutoff",
                   "retrieval_precedes_availability": "retrieval_precedes_publication"}
        for issue in time_boundary_issues(publication, retrieval, cutoff_at,
                                         check_order=entry.get("time_precision") == "second"):
            issue = aliases.get(issue, issue)
            if issue not in issues:
                issues.append(issue)
        available = max(publication, retrieval) if publication and retrieval else None
        row = {
            "evidence_id": entry["id"], "kind": entry.get("kind"),
            "issuer_cik": entry.get("company_cik"), "security_id": None,
            "source_url": entry.get("source_url"), "snapshot_path": source.get("snapshot_path"),
            "snapshot_sha256": source.get("sha256"), "retrieval_id": source.get("retrieval_id"),
            "publication_at": entry.get("published_at"), "publication_precision": entry.get("time_precision"),
            "publication_policy": policy, "publication_upper_bound": _iso(publication),
            "retrieved_at": _iso(retrieval), "observation_start": locator.get("period_start"),
            "observation_end": locator.get("period_end"), "available_not_before": _iso(available),
            "issues": issues,
        }
        results.append(row)
        by_id[entry["id"]] = row
        if entry.get("kind") == "financial_fact":
            fact_id = locator.get("fact_id")
            if not fact_id or fact_id in fact_ids:
                raise ValueError("Missing or duplicate financial fact identity")
            fact_ids[fact_id] = row
    # Resolve full fact IDs, independent of entry ordering. Derived values cannot
    # inherit an earlier timestamp or become available without their inputs.
    for entry in entries:
        if entry.get("kind") != "calculation":
            continue
        row = by_id[entry["id"]]
        calculation = entry.get("locator", {}).get("calculation", {})
        inputs = calculation.get("input_fact_ids", [])
        row["input_fact_ids"] = inputs
        if calculation.get("value") is None:
            row["issues"].append("calculation_value_unavailable")
        if not inputs or any(fact_id not in fact_ids for fact_id in inputs):
            row["issues"].append("calculation_inputs_unresolved")
        else:
            dependencies = [fact_ids[fact_id] for fact_id in inputs]
            if any(item["issues"] for item in dependencies):
                row["issues"].append("calculation_input_excluded")
            times = [row["available_not_before"], *(item["available_not_before"] for item in dependencies)]
            row["available_not_before"] = _iso(max(map(parse_timestamp, times))) if all(times) else None
    for row in results:
        row["eligible"] = not row["issues"]
    counts = Counter(issue for row in results for issue in row["issues"])
    return {
        "cutoff": cutoff_at.isoformat(), "policy_version": POLICY_VERSION,
        "status": "eligible" if results and not counts else "blocked",
        "evidence_count": len(results), "eligible_count": sum(row["eligible"] for row in results),
        "issue_counts": dict(sorted(counts.items())), "inputs": results,
    }


def audit_saved_run(root, run_id, cutoff):
    """Verify a completed archive before a read-only, zero-network assessment."""
    parse_timestamp(cutoff)
    folder, report = saved_report(root, run_id)
    assessment = assess_inputs(report, cutoff)
    try:
        generated = parse_timestamp(report.get("created_at"))
        research_timing = "generated_after_cutoff" if generated > parse_timestamp(cutoff) else "generated_by_cutoff"
    except ValueError:
        research_timing = "generation_time_unknown"
    return {
        "schema_version": "aeternitas-readiness-v1", "run_id": run_id,
        "ticker": report["ticker"], "research_as_of_date": report["as_of_date"],
        "evidence_mode": report["evidence_kind"], "archive_verification": "passed",
        "report_sha256": hashlib.sha256((folder / "report.json").read_bytes()).hexdigest(),
        "research_timing": research_timing,
        "study_classification": "saved_research_only_not_a_registered_forward_prediction",
        "timing_check": assessment,
        "prediction_readiness": {
            "status": "not_implemented", "prediction_created": False,
            "missing_capabilities": ["historical_security_identity", "market_and_benchmark_snapshots",
                                     "locked_prediction_registry", "deterministic_outcome_evaluator"],
        },
        "scope": [
            "Timing eligibility checks archived input availability only; it does not certify an entire historical backtest.",
            "Snapshot hashes identify captured bytes, not a vendor-certified publication or revision history.",
            "Late snapshots are excluded; no validated historical-vintage override is implemented.",
            "SEC date-only publication uses next midnight America/New_York, not an invented intraday release time.",
            "No new model or data-source requests; the original report and research workflow are unchanged.",
        ],
    }
