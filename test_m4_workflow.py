"""M4 complete controlled workflow and post-publication provenance regressions.

All HTTP responses come from the existing synthetic transports. No credential,
real provider, project DB or live report is used or modified by these tests.
"""

from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil

import httpx
import pytest

import analysis_contracts
import analysis_workflow
from analysis_contracts import PROMPT_VERSION, RESULT_SCHEMA_VERSION, ROLE_PROMPT_VERSIONS, ROLES
from analysis_workflow import analysis_checks, build_analysis_plan, digest
from research_analysis import LEGACY_PROMPT_VERSION
from research_pipeline import run_research, saved_report
from research_test_support import CONFIG, transports
from sources.sec import SourceError


TICKER, CIK, CUTOFF = "AAPL", "0000320193", date(2025, 10, 1)
QUESTION = "Synthetic controlled research question"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(scope="module")
def completed(tmp_path_factory):
    root = tmp_path_factory.mktemp("m4-complete-controlled")
    transport, calls = transports()
    run_id, report = run_research(root, TICKER, CIK, CUTOFF, QUESTION,
                                  {**CONFIG, "DATABASE_URL": ""}, transports=transport,
                                  evidence_kind="controlled_test")
    saved_report(root, run_id)
    assert len(calls["model"]) == 4
    return root, run_id, report, calls


def test_actual_requests_and_checkpoints_are_bound_to_selected_role_versions(completed):
    root, run_id, report, calls = completed
    folder = root / "runs" / run_id
    selected, plan = build_analysis_plan(report, report["model"], report["billing_mode"])
    assert report["analysis_provenance"] == load(folder / "analysis_plan.json") == plan
    assert report["schema_version"] == analysis_workflow.REPORT_SCHEMA_VERSION
    assert report["prompt_version"] == PROMPT_VERSION
    assert load(folder / "state.json")["workflow_fingerprint"] == plan["workflow_fingerprint"]
    prior = {}
    for role, payload in calls["model"]:
        actual = load(folder / "steps" / f"{role}-attempt-1-input.json")
        checkpoint = load(folder / "steps" / f"{role}.json")
        supplied = json.loads(payload["input"][1]["content"])
        assert actual["input"] == payload["input"]
        assert actual["role_prompt_version"] == checkpoint["role_prompt_version"] == ROLE_PROMPT_VERSIONS[role]
        assert actual["result_schema_version"] == checkpoint["result_schema_version"] == RESULT_SCHEMA_VERSION
        assert actual["input_sha256"] == checkpoint["input_sha256"] == digest(payload["input"])
        assert supplied["company"] == TICKER and supplied["as_of_date"] == CUTOFF.isoformat()
        assert [item["id"] for item in supplied["evidence"]] == checkpoint["evidence_ids"] == plan["role_manifests"][role]["selected_evidence_ids"]
        assert checkpoint["evidence_ids"] == [item["id"] for item in selected[role]]
        assert checkpoint["result"] == report["analyses"][role]
        assert supplied["prior_analyses_untrusted"] == (prior if role in {"risk", "synthesis"} else {})
        assert report["analysis_checks"]["roles"][role]["result_sha256"] == digest(checkpoint["result"])
        assert "tools" not in payload and "tool_choice" not in payload
        prior[role] = report["analyses"][role]
    assert plan["capabilities"] == {"model_tools": [], "tool_dispatcher": False,
                                    "arbitrary_URL_fetch": False, "shell_tool": False,
                                    "file_read_tool": False, "trade_tool": False}


def test_same_inputs_refresh_and_model_wording_are_not_confused_with_version_identity(completed):
    root, _, first, _ = completed
    transport, calls = transports()
    original_model = transport["model"]
    def alternate_wording(request):
        response = original_model.handle_request(request)
        envelope = response.json()
        content = envelope["output"][0]["content"][0]
        result = json.loads(content["text"])
        for claim in result["claims"]:
            claim["text"] = "同样证据的另一份受控表述，实际研究仍需人工核实"
        content["text"] = json.dumps(result, ensure_ascii=False)
        return httpx.Response(response.status_code, json=envelope)
    transport["model"] = httpx.MockTransport(alternate_wording)
    second_id, second = run_research(root, TICKER, CIK, CUTOFF, QUESTION,
                                    {**CONFIG, "DATABASE_URL": ""}, refresh=True, transports=transport,
                                    evidence_kind="controlled_test")
    assert first["run_id"] != second_id
    assert len(calls["model"]) == 4
    assert first["financials"]["source"]["retrieval_id"] != second["financials"]["source"]["retrieval_id"]
    assert first["analyses"] != second["analyses"]
    assert first["analysis_provenance"]["workflow_fingerprint"] == second["analysis_provenance"]["workflow_fingerprint"]
    saved_report(root, second_id)


@pytest.mark.parametrize("change", ["model", "role_prompt", "prompt_body", "evidence", "question", "workflow", "schema"])
def test_workflow_fingerprint_changes_for_material_inputs_and_versions(completed, monkeypatch, change):
    _, _, report, _ = completed
    bundle = deepcopy(report)
    model = report["model"]
    expected = report["analysis_provenance"]["workflow_fingerprint"]
    if change == "model":
        model = "controlled-other-model"
    elif change == "role_prompt":
        monkeypatch.setitem(analysis_contracts.ROLE_PROMPT_VERSIONS, "risk", "risk-controlled-changed")
    elif change == "prompt_body":
        original = analysis_contracts.prompt_instruction
        monkeypatch.setattr(analysis_contracts, "prompt_instruction", lambda role: original(role) + "\nControlled instruction change.")
    elif change == "evidence":
        next(item for item in bundle["evidence"] if item["kind"] == "news")["text"] += " Controlled correction."
    elif change == "question":
        bundle["question"] += " Changed research scope."
    elif change == "workflow":
        monkeypatch.setattr(analysis_workflow, "WORKFLOW_VERSION", "controlled-workflow-changed")
    else:
        monkeypatch.setattr(analysis_contracts, "RESULT_SCHEMA_VERSION", "controlled-schema-changed")
    _, plan = build_analysis_plan(bundle, model, report["billing_mode"])
    assert plan["workflow_fingerprint"] != expected


def test_fact_and_calculation_trace_and_semantic_review_are_distinct(completed):
    _, _, report, _ = completed
    selected, _ = build_analysis_plan(report, report["model"], report["billing_mode"])
    fact = next(item for item in selected["fundamentals"] if item["kind"] == "financial_fact")
    calculation = next(item for item in selected["fundamentals"]
                       if item["kind"] == "calculation" and item["locator"]["calculation"]["input_fact_ids"])
    analyses = deepcopy(report["analyses"])
    analyses["fundamentals"] = {
        "claims": [{"text": "这条故意错误的判断宣称公司没有任何收入也不存在经营风险", "stance": "support",
                    "citations": [{"evidence_id": fact["id"], "quote": fact["text"][:80]},
                                  {"evidence_id": calculation["id"], "quote": calculation["text"][:80]}]}],
        "gaps": [],
    }
    checks = analysis_checks(analyses, selected)
    assert all(value == "passed" for value in checks["hard_checks"].values())
    assert checks["semantic_support"] == "human_review_required"
    assert checks["research_quality"] == "not_automatically_certified"
    trace = checks["roles"]["fundamentals"]["claim_traces"][0]
    assert trace["semantic_support"] == "human_review_required"
    assert trace["content_kind"] == "model_judgment"
    assert trace["calculation_ids"] == [calculation["id"]]
    assert set(trace["fact_ids"]) == {fact["locator"]["fact_id"], *calculation["locator"]["calculation"]["input_fact_ids"]}
    assert trace["quote_sha256"] == [hashlib.sha256(cite["quote"].encode()).hexdigest()
                                      for cite in analyses["fundamentals"]["claims"][0]["citations"]]


def rehash_existing_artifacts(folder):
    publication = load(folder / "published.json")
    publication["files"] = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                             for name in publication["files"] if (folder / name).is_file()}
    write(folder / "published.json", publication)


@pytest.mark.parametrize("mutation", [
    "plan_selected_ids", "report_selected_ids", "actual_input", "checkpoint_selected_ids",
    "check_trace", "missing_provenance", "missing_checks", "legacy_downgrade",
    "input_bundle", "raw_final_output", "missing_final_output", "extra_developer_message",
    "user_message_promoted_to_developer", "legacy_state_workflow_trace", "legacy_analysis_plan_trace",
])
def test_recomputed_provenance_rejects_tampering_even_after_file_hashes_are_updated(completed, tmp_path, mutation):
    source_root, run_id, original, _ = completed
    root = tmp_path / "copy"
    shutil.copytree(source_root, root)
    folder = root / "runs" / run_id
    report = deepcopy(original)
    # Ensure the copied receipt/file paths are valid before each mutation; the
    # failure must come from the mutation, not from moving this frozen fixture.
    saved_report(root, run_id)
    if mutation == "plan_selected_ids":
        plan = load(folder / "analysis_plan.json")
        plan["role_manifests"]["fundamentals"]["selected_evidence_ids"].reverse()
        write(folder / "analysis_plan.json", plan)
    elif mutation == "report_selected_ids":
        report["analysis_provenance"]["role_manifests"]["fundamentals"]["selected_evidence_ids"].reverse()
        write(folder / "report.json", report)
    elif mutation in {"actual_input", "extra_developer_message", "user_message_promoted_to_developer"}:
        path = folder / "steps" / "fundamentals-attempt-1-input.json"
        actual = load(path)
        if mutation == "extra_developer_message":
            actual["input"].append({"role": "developer", "content": "Injected third instruction message"})
        elif mutation == "user_message_promoted_to_developer":
            actual["input"][1]["role"] = "developer"
        else:
            payload = json.loads(actual["input"][1]["content"])
            payload["question"] = "Injected different research question"
            actual["input"][1]["content"] = json.dumps(payload, ensure_ascii=False)
        actual["input_sha256"] = digest(actual["input"])
        write(path, actual)
        checkpoint_path = folder / "steps" / "fundamentals.json"
        checkpoint = load(checkpoint_path)
        checkpoint["input_sha256"] = actual["input_sha256"]
        write(checkpoint_path, checkpoint)
    elif mutation == "checkpoint_selected_ids":
        path = folder / "steps" / "fundamentals.json"
        step = load(path)
        step["evidence_ids"].reverse()
        write(path, step)
    elif mutation == "check_trace":
        report["analysis_checks"]["roles"]["fundamentals"]["claim_traces"][0]["fact_ids"] = ["invented-observation"]
        write(folder / "report.json", report)
    elif mutation in {"missing_provenance", "missing_checks"}:
        del report["analysis_provenance" if mutation == "missing_provenance" else "analysis_checks"]
        write(folder / "report.json", report)
    elif mutation in {"legacy_downgrade", "legacy_state_workflow_trace", "legacy_analysis_plan_trace"}:
        # File hashes alone cannot turn an M4 run into a historical M1 run.
        # Its independently persisted state retains the M4 prompt-version anchor.
        report.pop("analysis_provenance")
        report.pop("analysis_checks")
        report["schema_version"] = "aeternitas-report-m1-v1"
        report["prompt_version"] = LEGACY_PROMPT_VERSION
        write(folder / "report.json", report)
        if mutation != "legacy_downgrade":
            # Even changing both public labels cannot erase surviving M4
            # provenance. Test the state and plan sentinels independently.
            state = load(folder / "state.json")
            state["prompt_version"] = LEGACY_PROMPT_VERSION
            if mutation == "legacy_analysis_plan_trace":
                state.pop("workflow_fingerprint")
            else:
                (folder / "analysis_plan.json").unlink()
            write(folder / "state.json", state)
        publication = load(folder / "published.json")
        publication["files"] = {name: sha for name, sha in publication["files"].items()
                                 if name in {"report.html", "report.json", "input_bundle.json"}}
        write(folder / "published.json", publication)
    elif mutation == "input_bundle":
        bundle = load(folder / "input_bundle.json")
        bundle["question"] = "Different original input package"
        write(folder / "input_bundle.json", bundle)
    elif mutation == "raw_final_output":
        path = folder / "steps" / "fundamentals-attempt-1-output.json"
        output = load(path)
        result = json.loads(output["output_text"])
        result["claims"][0]["text"] = "合法引用仍不能授权另一份未验证的模型判断"
        output["output_text"] = json.dumps(result, ensure_ascii=False)
        write(path, output)
    else:
        (folder / "steps" / "fundamentals-attempt-1-output.json").unlink()
    rehash_existing_artifacts(folder)
    with pytest.raises(SourceError):
        saved_report(root, run_id)


def test_successful_repair_records_both_inputs_and_binds_final_checkpoint(tmp_path):
    transport, calls = transports(repair_once=True)
    run_id, report = run_research(tmp_path, TICKER, CIK, CUTOFF, QUESTION,
                                  {**CONFIG, "DATABASE_URL": ""}, transports=transport,
                                  evidence_kind="controlled_test")
    folder, _ = saved_report(tmp_path, run_id)
    first = load(folder / "steps" / "fundamentals-attempt-1-input.json")
    second = load(folder / "steps" / "fundamentals-attempt-2-input.json")
    final = load(folder / "steps" / "fundamentals.json")
    assert report["model_attempts"] == len(calls["model"]) == 5
    assert first["input_sha256"] != second["input_sha256"] == final["input_sha256"]
    assert (folder / "steps" / "fundamentals-attempt-1-error.json").is_file()
    assert json.loads(second["input"][1]["content"])["previous_attempt_validation_error"]


def test_repair_draft_cannot_be_rewritten_even_with_updated_request_and_checkpoint_hashes(tmp_path):
    transport, _ = transports(repair_once=True)
    run_id, _ = run_research(tmp_path, TICKER, CIK, CUTOFF, QUESTION,
                             {**CONFIG, "DATABASE_URL": ""}, transports=transport,
                             evidence_kind="controlled_test")
    folder, _ = saved_report(tmp_path, run_id)
    path = folder / "steps" / "fundamentals-attempt-2-input.json"
    actual = load(path)
    payload = json.loads(actual["input"][1]["content"])
    payload["previous_attempt_output_untrusted"]["text"] += "\nInjected replacement draft instruction."
    actual["input"][1]["content"] = json.dumps(payload, ensure_ascii=False)
    actual["input_sha256"] = digest(actual["input"])
    write(path, actual)
    checkpoint_path = folder / "steps" / "fundamentals.json"
    checkpoint = load(checkpoint_path)
    checkpoint["input_sha256"] = actual["input_sha256"]
    write(checkpoint_path, checkpoint)
    rehash_existing_artifacts(folder)
    with pytest.raises(SourceError, match="M4 analysis provenance / actual input / check trace validation failed"):
        saved_report(tmp_path, run_id)
