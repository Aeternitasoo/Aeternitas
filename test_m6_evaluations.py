"""Deterministic M6 development evaluation; the holdout is never discovered here."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from evaluation_cases import evaluate_case, evaluate_directory, load_cases


ROOT = Path(__file__).resolve().parent
DEVELOPMENT = ROOT / "evals" / "m6" / "development"


def test_eight_frozen_development_business_cases_pass_and_keep_semantic_boundary(tmp_path):
    result = evaluate_directory(DEVELOPMENT, split="development", output=tmp_path / "result.json")
    assert result["case_count"] == 8
    assert result["hard_checks_passed"], [
        (case["id"], [(scenario["id"], scenario["failures"]) for scenario in case["scenarios"]
                      if not scenario["hard_checks_passed"]])
        for case in result["cases"] if not case["hard_checks_passed"]
    ]
    assert result["live_model_requests"] == result["live_source_requests"] == 0
    assert result["semantic_certified"] is False
    assert all(Path(case["artifact"]["path"]).is_file() for case in result["cases"])
    semantic = next(case for case in result["cases"] if case["id"] == "D07")
    review = next(row for row in semantic["scenarios"] if "manual_review" in row)
    assert review["manual_review"]["record"]["verdict"] == "unsupported"
    assert review["manual_review"]["automated_entailment_verdict"] is False
    assert review["actual"]["value"]["semantic_support"] == "human_review_required"


def test_frozen_fixture_tamper_is_refused_before_execution(tmp_path):
    cases, _ = load_cases(DEVELOPMENT, split="development")
    case, metadata = cases[0]
    name = Path(metadata["path"]).name
    original = Path(metadata["path"]).read_bytes()
    (tmp_path / name).write_bytes(original + b" ")
    manifest = {"case_version": "m6-case-v1", "split": "development", "case_count": 1,
                "files": {name: hashlib.sha256(original).hexdigest()}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Frozen case hash differs"):
        load_cases(tmp_path, split="development")


def test_wrong_expected_value_produces_a_recorded_failure_not_a_false_pass():
    cases, _ = load_cases(DEVELOPMENT, split="development")
    case, metadata = cases[0]
    changed = deepcopy(case)
    changed["scenarios"][0]["expected"]["matches"]["/cik"] = "0000000000"
    result = evaluate_case(changed, metadata)
    assert result["hard_checks_passed"] is False
    assert "projection differs: /cik" in result["scenarios"][0]["failures"]


def test_requested_split_must_match_manifest_without_loading_other_directory():
    with pytest.raises(ValueError, match="split / version mismatch"):
        load_cases(DEVELOPMENT, split="holdout")


def test_programming_error_cannot_count_as_expected_business_rejection():
    case = {"case_version": "m6-case-v1", "id": "controlled-runner-defect", "split": "development",
            "material": {"mode": "synthetic"}, "scenarios": [
                {"id": "missing-function-input", "operation": "financial_summary", "input": {},
                 "expected": {"outcome": "rejected"}}
            ]}
    result = evaluate_case(case, {"path": "controlled", "sha256": "0" * 64})
    assert result["hard_checks_passed"] is False
    assert result["scenarios"][0]["actual"]["outcome"] == "runner_error"
