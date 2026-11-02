"""Select annual facts and calculate metrics; no networking or file writes."""

import hashlib
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from models import normalize_cik


PARSER_VERSION = "annual-us-gaap-usd-v1"
METRIC_TAGS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss",),
}


def _date(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field} must be an ISO date")
    return date.fromisoformat(value)


def _annual_facts(raw, metric, cutoff, source_sha256):
    selected = {}
    concepts = raw["facts"]["us-gaap"]
    for priority, tag in enumerate(METRIC_TAGS[metric]):
        concept = concepts.get(tag)
        if concept is None:
            continue
        if not isinstance(concept, dict) or not isinstance(concept.get("units"), dict):
            raise ValueError(f"Invalid concept: {tag}")
        observations = concept["units"].get("USD", [])
        if not isinstance(observations, list):
            raise ValueError(f"{tag}.units.USD must be a list")
        for index, observation in enumerate(observations):
            if not isinstance(observation, dict):
                raise ValueError(f"Invalid observation: {tag}[{index}]")
            if observation.get("form") not in {"10-K", "10-K/A"}:
                continue
            filed = _date(observation.get("filed"), "filed")
            if filed > cutoff:
                continue
            # Instant values and quarterly comparatives inside annual filings
            # do not represent annual income statement durations.
            if "start" not in observation:
                continue
            start = _date(observation["start"], "start")
            end = _date(observation.get("end"), "end")
            if start >= end or end > filed:
                raise ValueError(f"Invalid fact period: {tag}[{index}]")
            duration = (end - start).days + 1
            if not 330 <= duration <= 400:
                continue
            value = observation.get("val")
            if type(value) not in (int, Decimal):
                raise ValueError(f"{tag}[{index}].val must be an exact JSON number")
            value = Decimal(value)
            if not value.is_finite():
                raise ValueError("Financial values must be finite")
            accession = observation.get("accn", "")
            if not isinstance(accession, str) or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
                raise ValueError(f"Invalid accession: {tag}[{index}]")
            pointer = f"/facts/us-gaap/{tag}/units/USD/{index}"
            fact = {
                "fact_id": hashlib.sha256(f"{source_sha256}:{pointer}".encode()).hexdigest(),
                "metric": metric,
                "taxonomy": "us-gaap",
                "tag": tag,
                "unit": "USD",
                "value": str(value),
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "duration_days": duration,
                "filed_at": filed.isoformat(),
                "accession": accession,
                "form": observation["form"],
                "source_json_pointer": pointer,
            }
            key = (start.isoformat(), end.isoformat())
            # fy is the filing's fiscal year, not necessarily the fact's year.
            rank = (filed.isoformat(), -priority, accession)
            previous = selected.get(key)
            if previous is not None:
                old_rank, old_fact = previous
                if rank[:2] == old_rank[:2] and Decimal(old_fact["value"]) != value:
                    raise ValueError(f"Conflicting same-date annual facts: {tag}, {key}")
                if rank <= old_rank:
                    continue
            selected[key] = (rank, fact)
    return {period: fact for period, (_, fact) in selected.items()}


def _ratio(numerator, denominator, formula):
    inputs = [fact["fact_id"] for fact in (numerator, denominator) if fact is not None]
    reason = None
    value = None
    if numerator is None or denominator is None:
        reason = "missing_input"
    elif Decimal(denominator["value"]) == 0:
        reason = "zero_denominator"
    elif Decimal(denominator["value"]) < 0:
        reason = "negative_denominator"
    else:
        with localcontext() as context:
            context.prec = max(50, len(numerator["value"]) + len(denominator["value"]) + 10)
            value = str((Decimal(numerator["value"]) / Decimal(denominator["value"]) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
    return {
        "formula": formula, "formula_version": "percent-v1",
        "input_fact_ids": inputs, "value": value, "unit": "percent",
        "unavailable_reason": reason, "rounding": "0.01, ROUND_HALF_EVEN",
    }


def build_financial_summary(raw, cik, as_of_date, source):
    """Return up to two newest revenue-anchored annual periods as of a date.

    Missing operating / net income is explicit; never silently fall back to an
    older complete year. This is a bounded USD annual selector, not a general
    XBRL interpretation or a guarantee of comparable accounting policies.
    """
    cik = normalize_cik(cik)
    if normalize_cik(raw.get("cik")) != cik:
        raise ValueError("Company identity mismatch")
    cutoff = _date(as_of_date, "as_of_date")
    if not isinstance(raw.get("entityName"), str) or not raw["entityName"].strip():
        raise ValueError("Missing entityName")
    facts = raw.get("facts")
    if not isinstance(facts, dict) or not isinstance(facts.get("us-gaap"), dict):
        raise ValueError("Missing facts.us-gaap")
    series = {
        metric: _annual_facts(raw, metric, cutoff, source["sha256"])
        for metric in METRIC_TAGS
    }
    revenue = series["revenue"]
    if not revenue:
        raise ValueError("No supported annual USD revenue facts before cutoff; check coverage / tags")
    # Reject ambiguous durations instead of guessing between two starts for one end.
    ends = {}
    for start, end in revenue:
        if end in ends and ends[end] != start:
            raise ValueError(f"Ambiguous annual period ending {end}")
        ends[end] = start
    periods = sorted(revenue, key=lambda period: period[1], reverse=True)[:2]
    warnings = []
    annual = []
    for period in periods:
        metrics = {metric: series[metric].get(period) for metric in METRIC_TAGS}
        missing = [metric for metric, value in metrics.items() if value is None]
        if missing:
            warnings.append(f"{period[1]}: missing annual USD facts: {', '.join(missing)}")
        record = {
            "period_start": period[0], "period_end": period[1],
            "facts": metrics, "missing_metrics": missing,
            "operating_margin": _ratio(metrics["operating_income"], metrics["revenue"], "operating_income / revenue * 100"),
            "net_margin": _ratio(metrics["net_income"], metrics["revenue"], "net_income / revenue * 100"),
        }
        for key in ("operating_margin", "net_margin"):
            numerator = metrics["operating_income" if key == "operating_margin" else "net_income"]
            if numerator is not None and numerator["accession"] != metrics["revenue"]["accession"]:
                record[key]["value"] = None
                record[key]["unavailable_reason"] = "mixed_filing_versions_require_review"
            if record[key]["unavailable_reason"]:
                warnings.append(f"{period[1]}: {key}: {record[key]['unavailable_reason']}")
        annual.append(record)
    if len(annual) < 2:
        warnings.append("Only one annual revenue period available; two-period comparison unavailable")
        growth = _ratio(None, None, "(current_revenue - prior_revenue) / prior_revenue * 100")
        growth["unavailable_reason"] = "missing_prior_period"
    else:
        current = annual[0]["facts"]["revenue"]
        prior = annual[1]["facts"]["revenue"]
        growth = _ratio(current, prior, "(current_revenue - prior_revenue) / prior_revenue * 100")
        consecutive = (_date(current["period_start"], "start") - _date(prior["period_end"], "end")).days == 1
        if not consecutive or abs(current["duration_days"] - prior["duration_days"]) > 7:
            growth["value"] = None
            growth["unavailable_reason"] = "nonconsecutive_or_different_duration"
        elif current["tag"] != prior["tag"]:
            growth["value"] = None
            growth["unavailable_reason"] = "different_revenue_tags_require_review"
        elif growth["value"] is not None:
            with localcontext() as context:
                context.prec = max(50, len(current["value"]) + len(prior["value"]) + 10)
                growth["value"] = str(((Decimal(current["value"]) - Decimal(prior["value"])) / Decimal(prior["value"]) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
        if abs(current["duration_days"] - prior["duration_days"]) > 1:
            warnings.append("Annual durations differ; review 52/53-week and accounting comparability")
    if growth["unavailable_reason"]:
        warnings.append(f"Revenue growth unavailable: {growth['unavailable_reason']}")
    return {
        "schema_version": "financial-input-v1", "parser_version": PARSER_VERSION,
        "status": "financial_data_with_gaps" if warnings else "financial_data_ready_for_review",
        "cik": cik, "company_name": raw["entityName"],
        "as_of_date": cutoff.isoformat(), "as_of_timezone": "Asia/Shanghai",
        "source": source, "annual_periods": annual, "revenue_yoy": growth,
        "warnings": warnings,
        "limitations": [
            "Annual us-gaap USD facts only; quarter, TTM and custom tags are not supported yet.",
            "filed date filter is date-level; not a certified point-in-time reconstruction.",
            "Same tag / unit / duration does not prove unchanged accounting policies; filing review is still required.",
        ],
    }
