"""Time Machine contract tests: fixed synthetic CSV, no network or database."""

import copy
import csv
import hashlib
import json
from pathlib import Path

import pytest

import aeternitas
from time_machine import TimeMachineError, run, save_manifest, verify_manifest


FIXTURE = Path(__file__).parent / "tests/fixtures/market_daily_synthetic_v1.csv"
CALENDAR = FIXTURE.with_name("market_calendar_synthetic_v1.json")
LATE = "2025-03-03T12:00:00Z"


def snapshot(path=FIXTURE, **kwargs):
    return run(path, **{"symbol": "DEMO", "start": "2025-01-01", "end": "2025-02-28",
                       "cutoff": LATE, "calendar_path": CALENDAR, **kwargs})


def csv_copy(tmp_path, change):
    with FIXTURE.open() as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    change(rows)
    path = tmp_path / "input.csv"
    with path.open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def content(**kwargs):
    return snapshot(**kwargs)["content"]


def test_strict_manifest_contains_only_admitted_inputs_and_exact_identity():
    manifest = snapshot()
    c = manifest["content"]
    assert c["admission_status"] == "ready" and c["strict_timing_passed"]
    assert c["summary"] == {"candidate_count": 6, "admitted_daily_count": 6,
        "rejected_candidate_count": 0, "output_record_count": 6, "diagnostic_count": 0, "issue_counts": {}}
    assert c["dataset"]["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert c["calendar"]["version"] == "synthetic-calendar-v1"
    assert c["research_inputs"][3]["record"]["volume"] == 0
    assert all(i["record"]["adjustment"] == "raw" for i in c["research_inputs"])
    assert not c["prediction_created"] and verify_manifest(manifest)


def test_timezone_normalization_and_repeatability_separate_run_metadata(tmp_path):
    first = snapshot()
    relocated = tmp_path / "same.csv"
    relocated.write_bytes(FIXTURE.read_bytes())
    second = snapshot(relocated, cutoff="2025-03-03T20:00:00+08:00", data_types=["1d"])
    assert first["run_metadata"] != second["run_metadata"]
    assert first["content"] == second["content"]
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["manifest_fingerprint"] == second["manifest_fingerprint"]


@pytest.mark.parametrize("cutoff,count", [("2025-01-03T05:00:59Z", 0), ("2025-01-03T05:01:00Z", 1),
                                         ("2025-01-04T05:01:00Z", 2)])
def test_a0_exact_boundaries_and_traceable_rejections(cutoff, count):
    c = content(cutoff=cutoff)
    assert len(c["research_inputs"]) == count
    assert c["summary"]["rejected_candidate_count"] == 6 - count
    assert c["summary"]["admitted_daily_count"] + c["summary"]["rejected_candidate_count"] == 6
    assert c["admission_status"] == ("limited" if count else "blocked")
    assert all(e["reasons"] for e in c["excluded_candidates"])
    assert c["summary"]["issue_counts"]["snapshot_after_cutoff"] > 0
    assert all("open" not in entry for entry in c["candidates"])


def test_unknown_publication_is_blocked_or_explicitly_assumed_without_invention(tmp_path):
    path = csv_copy(tmp_path, lambda rows: [r.update(available_at="", availability_basis="unknown",
                                                   adjustment="split_adjusted") for r in rows])
    strict = snapshot(path)["content"]
    assert strict["admission_status"] == "blocked"
    assert strict["summary"]["issue_counts"]["availability_time_unknown"] == 6
    assert strict["summary"]["issue_counts"]["adjustment_vintage_not_verified"] == 6
    assumed = snapshot(path, mode="assumed", assumptions=["Use the captured split-adjusted version as a retrospective scenario."])["content"]
    assert assumed["classification"] == "assumption_based_simulation" and not assumed["strict_timing_passed"]
    assert all(i["record"]["available_at"] is None and i["record"]["adjustment"] == "split_adjusted" and
               not i["record"]["strict_replay_eligible"] for i in assumed["research_inputs"])
    early = snapshot(path, mode="assumed", assumptions=["Test assumption"], cutoff="2025-01-03T05:00:59Z")["content"]
    assert early["admission_status"] == "blocked" and early["summary"]["issue_counts"]["snapshot_after_cutoff"] == 6


def test_assumptions_cannot_override_known_future_publication():
    c = content(mode="assumed", assumptions=["Do not override known timestamps"], cutoff="2025-01-03T04:59:59Z")
    assert not c["research_inputs"]
    assert c["summary"]["issue_counts"]["availability_after_cutoff"] == 6


@pytest.mark.parametrize("kwargs", [{"mode": "assumed"}, {"assumptions": ["Not allowed in strict"]},
    {"mode": "other"}, {"assumptions": "text"}, {"data_types": []}, {"data_types": ["news"]},
    {"data_types": ["1d", "1d"]}, {"cutoff": "2025-03-03T12:00:00"}, {"symbol": "demo"},
    {"start": "2025-03-01"}, {"run_id": "../bad"}, {"created_at": "yesterday"}])
def test_invalid_configuration_fails_before_publication(kwargs):
    with pytest.raises(ValueError): snapshot(**kwargs)


@pytest.mark.parametrize("change", [lambda rows: rows[0].update(close="11.5"),
                                    lambda rows: rows[0].update(source_version="fixture-2"),
                                    lambda rows: rows[0].update(provider="another-source")])
def test_content_and_source_changes_are_identified_and_version_pins_fail(tmp_path, change):
    baseline = snapshot()
    path = csv_copy(tmp_path, change)
    changed = snapshot(path)
    assert baseline["input_fingerprint"] != changed["input_fingerprint"]
    assert baseline["content"]["candidates"][0]["record_id"] != changed["content"]["candidates"][0]["record_id"]
    with pytest.raises(TimeMachineError, match="dataset_version_mismatch"):
        snapshot(path, expected_dataset_sha256=baseline["content"]["dataset"]["sha256"])


def test_calendar_version_is_pinned_and_bound_into_fingerprint(tmp_path):
    original = snapshot()
    changed = json.loads(CALENDAR.read_text())
    changed["version"] = "different-calendar-version"
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(changed))
    assert snapshot(calendar_path=path)["input_fingerprint"] != original["input_fingerprint"]
    with pytest.raises(TimeMachineError, match="calendar_version_mismatch"):
        snapshot(calendar_path=path, expected_calendar_sha256=original["content"]["calendar"]["sha256"])


def test_expected_versions_can_be_pinned_without_changing_input_identity():
    original = snapshot()
    pinned = snapshot(expected_dataset_sha256=original["content"]["dataset"]["sha256"],
                      expected_calendar_sha256=original["content"]["calendar"]["sha256"])
    assert pinned["input_fingerprint"] == original["input_fingerprint"]


def test_calendar_from_after_cutoff_blocks_all_outputs(tmp_path):
    calendar = json.loads(CALENDAR.read_text())
    calendar.update(available_at="2026-01-01T00:00:00Z", retrieved_at="2026-01-01T00:01:00Z")
    path = tmp_path / "future-calendar.json"
    path.write_text(json.dumps(calendar))
    c = content(calendar_path=path, data_types=["1d", "1w", "1mo"])
    assert c["admission_status"] == "blocked" and not c["research_inputs"]
    assert "calendar_not_known_by_cutoff" in c["summary"]["issue_counts"]


def test_missing_capture_cannot_be_overridden_by_simulation(tmp_path):
    path = csv_copy(tmp_path, lambda rows: [r.update(retrieved_at="") for r in rows])
    c = snapshot(path, mode="assumed", assumptions=["A declared publication time exists"])["content"]
    assert not c["research_inputs"] and c["summary"]["issue_counts"]["retrieval_time_unresolved"] == 6


def test_mixed_adjustments_are_blocked_without_leaking_partial_data(tmp_path):
    path = csv_copy(tmp_path, lambda rows: rows[0].update(adjustment="split_adjusted"))
    c = snapshot(path, mode="assumed", assumptions=["Use observed versions"])["content"]
    assert not c["research_inputs"] and c["summary"]["rejected_candidate_count"] == 6
    assert c["summary"]["issue_counts"]["mixed_market_identity_currency_provider_or_adjustment"] == 1


def test_query_range_and_cutoff_are_part_of_input_identity():
    original = snapshot()
    assert snapshot(start="2025-01-02")["input_fingerprint"] != original["input_fingerprint"]
    assert snapshot(cutoff="2025-03-03T12:00:01Z")["input_fingerprint"] != original["input_fingerprint"]


def test_source_changes_after_read_do_not_mutate_saved_input_payload(tmp_path):
    path = csv_copy(tmp_path, lambda rows: None)
    original = snapshot(path)
    original_input = original["content"]["research_inputs"][0]["record"]["close"]
    path.write_bytes(path.read_bytes().replace(b",11,100,", b",11.5,100,", 1))
    assert verify_manifest(original)
    assert original["content"]["research_inputs"][0]["record"]["close"] == original_input
    with pytest.raises(TimeMachineError, match="version_mismatch"):
        snapshot(path, expected_dataset_sha256=original["content"]["dataset"]["sha256"])


@pytest.mark.parametrize("interval,count", [("1d", 6), ("1w", 3), ("1mo", 2)])
def test_interval_queries_retain_daily_dependency_hashes_and_period_checks(interval, count):
    c = content(data_types=[interval])
    assert c["admission_status"] == "ready" and len(c["research_inputs"]) == count
    candidate_ids = {c["record_id"] for c in c["candidates"]}
    assert {i for r in c["research_inputs"] for i in r["daily_input_ids"]} == candidate_ids
    assert all(r["record"]["complete"] for r in c["research_inputs"])
    if interval == "1mo":
        first = c["research_inputs"][0]["record"]
        assert (first["open"], first["high"], first["low"], first["close"], first["volume"]) == ("10", "15", "9", "14", 600)


def test_combined_types_and_request_order_are_reproducible():
    a = snapshot(data_types=["1d", "1w", "1mo"])
    b = snapshot(data_types=["1mo", "1w", "1d"])
    assert a["input_fingerprint"] == b["input_fingerprint"]
    assert a["manifest_fingerprint"] == b["manifest_fingerprint"]
    assert a["content"]["summary"]["output_record_count"] == 11
    assert a["content"]["summary"]["admitted_daily_count"] == 6


def test_missing_daily_record_and_entire_period_are_limited_not_success(tmp_path):
    path = csv_copy(tmp_path, lambda rows: rows.pop(1))
    c = snapshot(path, data_types=["1d", "1w", "1mo"])["content"]
    assert c["admission_status"] == "limited" and not c["strict_timing_passed"]
    assert "missing_daily_session" in c["summary"]["issue_counts"]
    assert c["results_by_type"]["1mo"]["output_record_count"] == 1


def test_entire_missing_month_is_reported(tmp_path):
    path = csv_copy(tmp_path, lambda rows: rows.__delitem__(slice(4, None)))
    c = snapshot(path, data_types=["1mo"])["content"]
    assert c["admission_status"] == "limited" and len(c["research_inputs"]) == 1
    assert c["summary"]["issue_counts"]["no_admissible_daily_sessions"] == 1
    assert c["results_by_type"]["1mo"]["coverage"]["missing_source_dates"] == ["2025-02-03", "2025-02-04"]


@pytest.mark.parametrize("types,calendar,status", [(["1d"], None, "limited"), (["1w"], None, "blocked"),
                                                   (["1mo"], None, "blocked")])
def test_unverified_calendar_never_silently_claims_complete_range(types, calendar, status):
    c = content(data_types=types, calendar_path=calendar)
    assert c["admission_status"] == status and not c["strict_timing_passed"]
    assert "session_coverage_unverified" in c["summary"]["issue_counts"]


def test_partial_month_or_unclosed_week_is_not_exposed():
    c = content(data_types=["1mo"], cutoff="2025-01-08T06:00:00Z")
    assert not c["research_inputs"] and "period_not_closed" in c["summary"]["issue_counts"]
    c = content(data_types=["1w"], start="2025-01-03", end="2025-01-03")
    assert not c["research_inputs"] and "missing_daily_sessions" in c["summary"]["issue_counts"]


def test_absent_symbol_generates_blocked_manifest():
    c = content(symbol="OTHER")
    assert c["admission_status"] == "blocked" and c["summary"]["candidate_count"] == 0
    assert c["summary"]["issue_counts"]["no_data"] == 1


@pytest.mark.parametrize("change", [lambda rows: rows.append(rows[0].copy()),
                                    lambda rows: rows[0].update(available_at="not-a-timestamp"),
                                    lambda rows: rows[0].update(close="")])
def test_invalid_source_has_no_partial_snapshot(tmp_path, change):
    with pytest.raises(ValueError): snapshot(csv_copy(tmp_path, change))


def test_snapshot_integrity_and_exclusive_persistence(tmp_path):
    original = snapshot()
    target = tmp_path / "manifest.json"
    save_manifest(original, target)
    assert verify_manifest(json.loads(target.read_text()))
    with pytest.raises(FileExistsError): save_manifest(original, target)
    altered = copy.deepcopy(original)
    altered["content"]["research_inputs"][0]["record"]["close"] = "999"
    with pytest.raises(TimeMachineError, match="integrity"): verify_manifest(altered)
    with pytest.raises(TimeMachineError): save_manifest(altered, tmp_path / "bad.json")
    assert not (tmp_path / "bad.json").exists()


def test_cli_is_offline_help_no_config_and_writes_only_explicit_path(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(aeternitas, "load_config", lambda *a: pytest.fail("Must not read credentials"))
    with pytest.raises(SystemExit) as done: aeternitas.main(["time-machine", "--help"])
    assert done.value.code == 0
    capsys.readouterr()
    args = ["time-machine", "DEMO", "--csv", str(FIXTURE), "--start", "2025-01-01", "--end", "2025-02-28",
            "--cutoff", LATE, "--calendar", str(CALENDAR)]
    assert aeternitas.main(args) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert verify_manifest(manifest)
    out = tmp_path / "cli.json"
    assert aeternitas.main([*args, "--output", str(out)]) == 0
    assert aeternitas.main([*args, "--output", str(out)]) == 1
    assert aeternitas.main([*args, "--cutoff", "2025-01-03T05:00:59Z"]) == 2
