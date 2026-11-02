"""Precise feedback, bounded untrusted drafts and unchanged output guards."""

import copy
import json

import httpx
import pytest

from research_analysis import ResponsesAnalyst, prompt_for
from research_runtime import Budget
from sources.sec import SourceError


EVIDENCE = [{"id": "E-valid", "kind": "filing_text", "title": "Synthetic source",
             "published_at": "2025-01-01", "text": "Exact supplied source sentence for this test."}]
VALID = {"claims": [{"text": "受控测试论点", "stance": "neutral", "citations": [
    {"evidence_id": "E-valid", "quote": EVIDENCE[0]["text"]}]}], "gaps": []}


class Plan:
    def token(self):
        return "synthetic-plan-secret"


def response_for(text, plan):
    data = {"id": "synthetic-response", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]}]}
    if plan:
        wire = ("data: " + json.dumps({"type": "response.completed", "response": data}) + "\n\n").encode()
        return httpx.Response(200, content=wire)
    return httpx.Response(200, json=data)


def invalid_output(case):
    data = copy.deepcopy(VALID)
    if case == "json":
        return '```json\n{}\n```', "JSON syntax invalid"
    if case == "schema":
        del data["gaps"]
        expected = "Output must contain only claims and gaps"
    elif case == "numbers":
        data["claims"][0]["text"] = "收入增长10%"
        expected = "claims[0].text: Put numbers"
    elif case == "id":
        data["claims"][0]["citations"][0]["evidence_id"] = "invented-id"
        expected = "claims[0].citations[0].evidence_id"
    elif case == "quote":
        data["claims"][0]["citations"][0]["quote"] = "This quotation is not in the evidence."
        expected = "claims[0].citations[0].quote: Citation quote is not an exact"
    elif case == "short_quote":
        data["claims"][0]["citations"][0]["quote"] = "Exact"
        expected = "claims[0].citations[0].quote: Citation quote must contain"
    elif case == "too_many_cites":
        data["claims"][0]["citations"] *= 4
        expected = "claims[0].citations: Each claim needs one to three citations"
    elif case == "gap":
        data["gaps"] = ["需要补充2026年材料"]
        expected = "gaps[0]"
    return json.dumps(data, ensure_ascii=False), expected


@pytest.mark.parametrize("plan", [False, True])
@pytest.mark.parametrize("case", ["json", "schema", "numbers", "id", "quote", "short_quote", "too_many_cites", "gap"])
def test_precise_feedback_and_previous_draft_allow_targeted_repair(tmp_path, plan, case):
    invalid, expected = invalid_output(case)
    calls = []

    def model(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return response_for(invalid, plan)
        feedback = json.loads(payload["input"][1]["content"])
        assert expected in feedback["previous_attempt_validation_error"]
        assert feedback["previous_attempt_output_untrusted"]["text"] == invalid
        assert feedback["evidence"][0]["text"] == EVIDENCE[0]["text"]
        return response_for(json.dumps(VALID), plan)

    with ResponsesAnalyst(None if plan else "synthetic-key", "account-model", tmp_path, Budget(),
                          transport=httpx.MockTransport(model), plan_auth=Plan() if plan else None) as analyst:
        actual = analyst.analyze("fundamentals", "AAPL", "2025-10-01", "Question", EVIDENCE, {})
    assert actual == VALID and len(calls) == 2
    diagnostic = json.loads((tmp_path / "fundamentals-attempt-1-error.json").read_text())
    assert expected in diagnostic["error"]
    assert diagnostic["phase"] == ("analysis_json" if case == "json" else "analysis_validation")
    stored = json.loads((tmp_path / diagnostic["output_file"]).read_text())
    assert stored["evidence_kind"] == "unvalidated_model_output" and stored["output_text"] == invalid


def test_failed_repair_preserves_reason_without_publishing(tmp_path):
    invalid, expected = invalid_output("numbers")
    calls = []

    def model(request):
        calls.append(request)
        return response_for(invalid, True)

    with ResponsesAnalyst(None, "account-model", tmp_path, Budget(),
                          transport=httpx.MockTransport(model), plan_auth=Plan()) as analyst:
        with pytest.raises(SourceError, match=r"claims\[0\]\.text"):
            analyst.analyze("fundamentals", "AAPL", "2025-10-01", "Question", EVIDENCE, {})
    assert len(calls) == 2 and not (tmp_path / "fundamentals.json").exists()
    assert expected in json.loads((tmp_path / "fundamentals-attempt-2-error.json").read_text())["error"]


def test_repair_and_diagnostic_output_are_bounded(tmp_path):
    calls = []

    def model(request):
        calls.append(json.loads(request.content))
        return response_for("错" * 30000, False)

    with ResponsesAnalyst("synthetic-key", "account-model", tmp_path, Budget(),
                          transport=httpx.MockTransport(model)) as analyst:
        with pytest.raises(SourceError, match="JSON syntax invalid"):
            analyst.analyze("fundamentals", "AAPL", "2025-10-01", "Question", EVIDENCE, {})
    draft = json.loads(calls[1]["input"][1]["content"])["previous_attempt_output_untrusted"]
    assert len(draft["text"].encode()) <= 12000 and draft["truncated"]
    stored = json.loads((tmp_path / "fundamentals-attempt-1-output.json").read_text())
    assert len(stored["output_text"].encode()) <= 64000 and stored["output_truncated"]


def test_repair_preserves_evidence_if_draft_exceeds_input_budget():
    evidence = copy.deepcopy(EVIDENCE)
    evidence[0]["text"] = "x" * 87000
    messages = prompt_for("fundamentals", "AAPL", "2025-10-01", "Question", evidence, {},
                          "Specific validation failure", {"text": "x" * 12000})
    payload = json.loads(messages[1]["content"])
    assert len(messages[1]["content"].encode()) <= 90000
    assert payload["evidence"][0]["text"] == evidence[0]["text"]
    assert payload["previous_attempt_validation_error"] == "Specific validation failure"
    assert "previous_attempt_output_omitted" in payload
    assert "previous_attempt_output_untrusted" not in payload
