"""Offline regressions for archived input timing, not prediction accuracy.

All provider responses and financial values are explicitly synthetic. These
tests never read project credentials, a live archive, or an integration DB.
"""

from copy import deepcopy
from datetime import date
import hashlib
import json
import shutil

import pytest

import aeternitas
from point_in_time import assess_inputs, audit_saved_run, parse_timestamp
from research_pipeline import run_research
from research_test_support import CONFIG, transports
from sources.sec import SourceError


CUTOFF = "2026-10-24T12:00:00Z"


def source(fetched="2026-10-24T10:00:00Z"):
    return {"data_mode": "live", "fetched_at": fetched,
            "sha256": "a" * 64, "snapshot_path": "snapshots/synthetic.json",
            "retrieval_id": "b" * 32}


def news(eid="N-synthetic", *, published="2026-10-23T09:00:00Z", fetched=None):
    return {"id": eid, "kind": "news", "company_cik": "0000000001",
            "source_url": "https://example.invalid/synthetic",
            "published_at": published, "time_precision": "second",
            "source": source(fetched or "2026-10-24T10:00:00Z"), "locator": {}}


def fact(letter="c", *, published="2026-02-27", fetched=None):
    return {"id": "V-" + letter * 16, "kind": "financial_fact",
            "company_cik": "0000000001", "source_url": "https://example.invalid/synthetic",
            "published_at": published, "time_precision": "date",
            "source": source(fetched or "2026-10-24T10:00:00Z"),
            "locator": {"fact_id": letter * 64, "period_start": "2025-01-01",
                        "period_end": "2025-12-31", "value": "0"}}


def calculation(inputs=None, *, value="0"):
    item = fact("e")
    item.update(id="C-synthetic", kind="calculation")
    item["locator"] = {"calculation": {
        "input_fact_ids": ["c" * 64, "d" * 64] if inputs is None else inputs,
        "value": value, "formula_version": "synthetic-v1"}}
    return item


def report(*entries, mode="live_api"):
    return {"evidence_kind": mode, "evidence": list(entries)}


@pytest.mark.parametrize("value", [
    "2026-10-24", "2026-10-24T12:00:00", "2026-10-24 12:00:00Z",
    "2026-10-24T12:00Z", "2026-02-30T12:00:00Z", "2026-10-24T25:00:00Z",
    "2026-10-24T12:00:00+25:00", "2026-10-24T12:00:00+00:99",
    "2026-10-24T12:00:00-04:60", None,
])
def test_cutoff_requires_valid_explicit_seconds_and_timezone(value):
    with pytest.raises(ValueError):
        parse_timestamp(value)


def test_same_instant_in_different_timezones_has_identical_assessment():
    data = report(news())
    assert assess_inputs(data, CUTOFF) == assess_inputs(data, "2026-10-24T20:00:00+08:00")


@pytest.mark.parametrize("fetched,eligible", [
    ("2026-10-24T11:59:59.999999Z", True),
    ("2026-10-24T12:00:00Z", True),
    ("2026-10-24T12:00:00.000001Z", False),
])
def test_snapshot_capture_cutoff_is_inclusive(fetched, eligible):
    row = assess_inputs(report(news(fetched=fetched)), CUTOFF)["inputs"][0]
    assert row["eligible"] is eligible
    assert ("snapshot_after_cutoff" in row["issues"]) is (not eligible)


@pytest.mark.parametrize("published,before,boundary", [
    ("2026-03-08", "2026-03-09T03:59:59Z", "2026-03-09T04:00:00Z"),
    ("2026-11-01", "2026-11-02T04:59:59Z", "2026-11-02T05:00:00Z"),
])
def test_sec_date_waits_until_next_new_york_midnight_across_dst(published, before, boundary):
    data = report(fact(published=published, fetched=published + "T18:00:00Z"))
    early = assess_inputs(data, before)["inputs"][0]
    ready = assess_inputs(data, boundary)["inputs"][0]
    assert early["issues"] == ["publication_after_cutoff"]
    assert ready["eligible"] is True
    assert ready["publication_upper_bound"] == parse_timestamp(boundary).isoformat()
    assert ready["publication_policy"] == "sec_date_end_conservative"


def test_old_publication_does_not_justify_later_retrieved_revision():
    row = assess_inputs(report(news(published="2020-01-01T00:00:00Z",
                                   fetched="2026-10-25T00:00:00Z")), CUTOFF)["inputs"][0]
    assert row["eligible"] is False
    assert row["issues"] == ["snapshot_after_cutoff"]


def test_future_publication_and_contradictory_capture_time_are_flagged():
    row = assess_inputs(report(news(published="2026-10-25T00:00:00Z")), CUTOFF)["inputs"][0]
    assert {"publication_after_cutoff", "retrieval_precedes_publication"} <= set(row["issues"])


def test_cached_snapshot_uses_original_capture_time_without_mutating_input():
    item = news()
    item["source"]["cache_hit"] = True
    data = report(item)
    before = deepcopy(data)
    row = assess_inputs(data, CUTOFF)["inputs"][0]
    assert row["eligible"] is True
    assert row["retrieved_at"] == "2026-10-24T10:00:00+00:00"
    assert data == before


@pytest.mark.parametrize("mode,source_mode", [("controlled_test", "live"), ("live_api", "fixture")])
def test_synthetic_evidence_cannot_pass_as_live_even_with_capture_timestamps(mode, source_mode):
    item = news()
    item["source"]["data_mode"] = source_mode
    result = assess_inputs(report(item, mode=mode), CUTOFF)
    assert result["status"] == "blocked"
    assert "non_live_evidence" in result["inputs"][0]["issues"]


def test_date_only_news_does_not_inherit_sec_timezone_policy():
    item = news(published="2026-10-23")
    item["time_precision"] = "date"
    row = assess_inputs(report(item), CUTOFF)["inputs"][0]
    assert "publication_time_unresolved" in row["issues"]


def test_calculation_uses_full_fact_ids_regardless_of_evidence_order_and_preserves_zero():
    data = report(calculation(value="0"), fact("d"), fact("c"))
    result = assess_inputs(data, CUTOFF)
    assert result["status"] == "eligible"
    assert result["inputs"][0]["input_fact_ids"] == ["c" * 64, "d" * 64]
    assert result["eligible_count"] == 3


def test_excluded_dependency_blocks_calculation_and_moves_availability_forward():
    late = "2026-10-25T10:00:00Z"
    result = assess_inputs(report(calculation(), fact("c"), fact("d", fetched=late)), CUTOFF)
    derived = result["inputs"][0]
    assert "calculation_input_excluded" in derived["issues"]
    assert derived["eligible"] is False
    assert derived["available_not_before"] == parse_timestamp(late).isoformat()


def test_missing_calculation_is_not_zero_or_vacuously_valid():
    result = assess_inputs(report(calculation([], value=None)), CUTOFF)
    assert result["status"] == "blocked"
    assert {"calculation_value_unavailable", "calculation_inputs_unresolved"} <= set(result["inputs"][0]["issues"])


def test_truncated_evidence_id_cannot_substitute_for_full_fact_identity():
    data = report(calculation(["V-" + "c" * 16]), fact("c"))
    assert "calculation_inputs_unresolved" in assess_inputs(data, CUTOFF)["inputs"][0]["issues"]


def test_empty_archive_does_not_report_eligible():
    result = assess_inputs(report(), CUTOFF)
    assert result["status"] == "blocked"
    assert result["eligible_count"] == 0


def hash_files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.fixture(scope="module")
def controlled_archive(tmp_path_factory):
    root = tmp_path_factory.mktemp("readiness-controlled")
    mock_transports, calls = transports()
    run_id, data = run_research(root, "AAPL", "0000320193", date(2025, 10, 1),
                                "Synthetic timing gate regression", {**CONFIG, "DATABASE_URL": ""},
                                transports=mock_transports, evidence_kind="controlled_test")
    return root, run_id, data, calls


def test_real_archive_verifier_is_reused_read_only_without_new_requests(controlled_archive):
    root, run_id, _, calls = controlled_archive
    before = hash_files(root)
    requests = {key: len(value) for key, value in calls.items()}
    result = audit_saved_run(root, run_id, "2099-01-01T00:00:00Z")
    assert result["archive_verification"] == "passed"
    assert result["timing_check"]["status"] == "blocked"
    assert result["prediction_readiness"]["prediction_created"] is False
    assert hash_files(root) == before
    assert requests == {key: len(value) for key, value in calls.items()}


@pytest.mark.parametrize("target", ["snapshot", "receipt"])
def test_source_tampering_is_rejected_before_timing_assessment(controlled_archive, tmp_path, target):
    original, run_id, data, _ = controlled_archive
    root = tmp_path / "copy"
    shutil.copytree(original, root)
    source_record = data["sources"][0]
    if target == "snapshot":
        path = root / source_record["snapshot_path"]
        path.write_bytes(path.read_bytes() + b" ")
    else:
        path = root / "retrievals" / (source_record["retrieval_id"] + ".json")
        receipt = json.loads(path.read_text())
        receipt["fetched_at"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(receipt))
    with pytest.raises(SourceError):
        audit_saved_run(root, run_id, "2099-01-01T00:00:00Z")


def test_cli_does_not_read_local_credentials(controlled_archive, monkeypatch, capsys):
    root, run_id, _, _ = controlled_archive
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline audit must not load credentials")
    monkeypatch.setattr(aeternitas, "load_config", forbidden)
    assert aeternitas.main(["--data-dir", str(root), "audit-readiness", run_id,
                           "--cutoff", "2099-01-01T00:00:00Z"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["run_id"] == run_id
    assert result["archive_verification"] == "passed"
    assert result["prediction_readiness"]["prediction_created"] is False


def test_cli_invalid_cutoff_fails_before_archive_access(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid cutoff must not access archive or credentials")
    import point_in_time
    monkeypatch.setattr(point_in_time, "saved_report", forbidden)
    monkeypatch.setattr(aeternitas, "load_config", forbidden)
    assert aeternitas.main(["audit-readiness", "a" * 32, "--cutoff", "2026-10-24"]) == 1
    assert "timezone" in capsys.readouterr().err
