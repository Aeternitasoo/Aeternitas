"""Synthetic financial data only. No fixture here is an actual company report."""

from copy import deepcopy
from decimal import Decimal

import pytest

from financial_facts import METRIC_TAGS, build_financial_summary
from sources.sec import SourceError, decode_companyfacts


SOURCE = {"sha256": "a" * 64, "retrieval_id": "synthetic-test-only"}
REVENUE = METRIC_TAGS["revenue"][0]


def make_facts(cik=320193):
    raw = {"cik": cik, "entityName": "Synthetic Example (NOT real financial data)", "facts": {"us-gaap": {}}}
    for metric, values in (("revenue", [100, 120]), ("operating_income", [0, 30]), ("net_income", [10, -6])):
        rows = []
        for year, value in zip([2023, 2024], values):
            rows.append({
                "start": f"{year}-01-01", "end": f"{year}-12-31",
                "val": value, "accn": f"{cik:010d}-25-000001",
                "form": "10-K", "filed": "2025-02-01", "fy": 2024,
            })
        raw["facts"]["us-gaap"][METRIC_TAGS[metric][0]] = {"units": {"USD": rows}}
    return raw


def summarize(raw):
    return build_financial_summary(raw, str(raw["cik"]), "2025-10-01", SOURCE)


def rows(raw, metric="revenue"):
    return raw["facts"]["us-gaap"][METRIC_TAGS[metric][0]]["units"]["USD"]


def test_annual_selection_uses_period_not_filing_fy_and_retains_zero_and_loss():
    result = summarize(make_facts())
    current, prior = result["annual_periods"]
    assert [current["period_end"], prior["period_end"]] == ["2024-12-31", "2023-12-31"]
    assert prior["facts"]["operating_income"]["value"] == "0"
    assert current["net_margin"]["value"] == "-5.00"
    assert result["revenue_yoy"]["value"] == "20.00"
    assert current["operating_margin"]["value"] == "25.00"
    assert current["facts"]["revenue"]["source_json_pointer"].endswith("/USD/1")


@pytest.mark.parametrize("cik", [320193, 789019])
def test_company_identity_is_not_hardcoded(cik):
    result = summarize(make_facts(cik))
    assert result["cik"] == f"{cik:010d}"


def test_cutoff_ignores_future_restatement_and_uses_latest_known_observation():
    raw = make_facts()
    known = {**rows(raw)[1], "filed": "2025-05-01", "val": 130}
    future = {**rows(raw)[1], "filed": "2025-11-01", "val": 999}
    rows(raw).extend([future, known])  # order must not decide the answer
    assert summarize(raw)["annual_periods"][0]["facts"]["revenue"]["value"] == "130"


def test_quarter_inside_annual_filing_is_not_selected_as_an_annual_fact():
    raw = make_facts()
    rows(raw).append({**rows(raw)[1], "start": "2024-10-01", "val": 999})
    assert summarize(raw)["annual_periods"][0]["facts"]["revenue"]["value"] == "120"


def test_missing_latest_metric_stays_missing_instead_of_selecting_old_complete_year():
    raw = make_facts()
    rows(raw, "net_income").pop()
    current = summarize(raw)["annual_periods"][0]
    assert current["period_end"] == "2024-12-31"
    assert current["facts"]["net_income"] is None
    assert current["net_margin"]["unavailable_reason"] == "missing_input"


@pytest.mark.parametrize("prior,reason", [(0, "zero_denominator"), (-100, "negative_denominator")])
def test_growth_does_not_fabricate_zero_or_negative_base_percentage(prior, reason):
    raw = make_facts()
    rows(raw)[0]["val"] = prior
    result = summarize(raw)
    assert result["revenue_yoy"]["value"] is None
    assert result["revenue_yoy"]["unavailable_reason"] == reason
    assert result["status"] == "financial_data_with_gaps"


def test_zero_current_revenue_marks_margin_unavailable():
    raw = make_facts()
    rows(raw)[1]["val"] = 0
    result = summarize(raw)
    assert result["annual_periods"][0]["operating_margin"]["value"] is None
    assert result["revenue_yoy"]["value"] == "-100.00"
    assert result["status"] == "financial_data_with_gaps"


def test_nonconsecutive_years_do_not_become_yoy():
    raw = make_facts()
    rows(raw)[0].update(start="2022-01-01", end="2022-12-31")
    assert summarize(raw)["revenue_yoy"]["unavailable_reason"] == "nonconsecutive_or_different_duration"


def test_tag_fallback_is_visible_and_unit_is_not_silently_converted():
    raw = make_facts()
    concepts = raw["facts"]["us-gaap"]
    concepts["Revenues"] = concepts.pop(REVENUE)
    assert summarize(raw)["annual_periods"][0]["facts"]["revenue"]["tag"] == "Revenues"
    concepts["Revenues"]["units"]["EUR"] = concepts["Revenues"]["units"].pop("USD")
    with pytest.raises(ValueError, match="No supported annual USD"):
        summarize(raw)


def test_different_revenue_tags_are_not_assumed_comparable():
    raw = make_facts()
    prior = rows(raw).pop(0)
    raw["facts"]["us-gaap"]["Revenues"] = {"units": {"USD": [prior]}}
    assert summarize(raw)["revenue_yoy"]["unavailable_reason"] == "different_revenue_tags_require_review"


def test_margin_does_not_mix_filing_versions_without_review():
    raw = make_facts()
    rows(raw, "operating_income")[1]["accn"] = "0000320193-25-000002"
    margin = summarize(raw)["annual_periods"][0]["operating_margin"]
    assert margin["value"] is None
    assert margin["unavailable_reason"] == "mixed_filing_versions_require_review"


def test_decimal_is_kept_at_json_boundary_and_in_result():
    blob = b'{"cik":320193,"entityName":"Synthetic","facts":{"us-gaap":{"x":{"val":0.1}}}}'
    decoded = decode_companyfacts(blob, "0000320193")
    assert decoded["facts"]["us-gaap"]["x"]["val"] == Decimal("0.1")
    raw = make_facts()
    rows(raw)[1]["val"] = Decimal("120.01")
    assert summarize(raw)["annual_periods"][0]["facts"]["revenue"]["value"] == "120.01"


@pytest.mark.parametrize("value", [True, "120", 120.1, Decimal("NaN")])
def test_bad_numeric_types_are_rejected(value):
    raw = make_facts()
    rows(raw)[1]["val"] = value
    with pytest.raises(ValueError):
        summarize(raw)


def test_same_date_conflicting_observations_are_rejected():
    raw = make_facts()
    rows(raw).append({**rows(raw)[1], "val": 999})
    with pytest.raises(ValueError, match="Conflicting"):
        summarize(raw)


def test_equivalent_decimal_formatting_is_not_a_conflict():
    raw = make_facts()
    rows(raw).append({**rows(raw)[1], "val": Decimal("120.00")})
    result = summarize(raw)
    assert result["revenue_yoy"]["value"] == "20.00"
    assert result["status"] == "financial_data_ready_for_review"


def test_single_annual_period_is_explicitly_partial():
    raw = make_facts()
    rows(raw).pop(0)
    result = summarize(raw)
    assert result["revenue_yoy"]["unavailable_reason"] == "missing_prior_period"
    assert result["status"] == "financial_data_with_gaps"


def test_build_does_not_mutate_source_and_rejects_wrong_company():
    raw = make_facts()
    before = deepcopy(raw)
    summarize(raw)
    assert raw == before
    with pytest.raises(ValueError, match="identity"):
        build_financial_summary(raw, "789019", "2025-10-01", SOURCE)


@pytest.mark.parametrize("payload", [b"[]", b"<html>blocked</html>", b'{"x":NaN}'])
def test_invalid_api_payload_is_not_empty_success(payload):
    with pytest.raises(SourceError):
        decode_companyfacts(payload, "0000320193")
