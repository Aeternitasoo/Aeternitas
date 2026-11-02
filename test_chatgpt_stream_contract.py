"""Regression for live SSE with a missing Content-Type; no external calls."""

import json

import httpx
import pytest

from chatgpt_stream import read_plan_response
from research_analysis import ResponsesAnalyst
from research_runtime import Budget
from sources.sec import SourceError


class Plan:
    def token(self):
        return "synthetic-plan-secret"


def completion(result):
    response = {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(result)}]}]}
    return ("data: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode()


@pytest.mark.parametrize("wire", [
    b"", b"<html>gateway page</html>", b'{"status":"completed","output":[]}',
    b"data: [DONE]\n\n", b"data: not-json\n\n", b"data: []\n\n",
    b'data: {"type":null}\n\n',
    b'data: {"type":"response.output_text.delta","delta":42}\n\n',
    b'data: {"type":"response.completed","response":{"status":"incomplete"}}\n\n',
])
def test_headerless_invalid_body_stops_without_repair_or_success(tmp_path, wire):
    calls = []

    def model(request):
        calls.append(request)
        return httpx.Response(200, content=wire)

    with ResponsesAnalyst(None, "account-model", tmp_path, Budget(),
                          transport=httpx.MockTransport(model), plan_auth=Plan()) as analyst:
        with pytest.raises(SourceError):
            analyst.analyze("fundamentals", "AAPL", "2025-10-01", "Question", [], {})
    assert len(calls) == 1
    assert not (tmp_path / "fundamentals.json").exists()


def test_explicit_wrong_media_type_is_not_treated_as_missing(tmp_path):
    wire = completion({"claims": [], "gaps": ["缺少资料"]})
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, content=wire, headers={"content-type": "text/html"}))
    with ResponsesAnalyst(None, "account-model", tmp_path, Budget(),
                          transport=transport, plan_auth=Plan()) as analyst:
        with pytest.raises(SourceError, match="media type"):
            analyst.analyze("fundamentals", "AAPL", "2025-10-01", "Question", [], {})
    assert not (tmp_path / "fundamentals.json").exists()


def test_headerless_completed_stream_still_rejects_invented_citation(tmp_path):
    result = {"claims": [{"text": "测试论点", "stance": "neutral", "citations": [
        {"evidence_id": "invented", "quote": "This is an invented citation."}]}], "gaps": []}
    calls = []

    def model(request):
        calls.append(request)
        return httpx.Response(200, content=completion(result))

    with ResponsesAnalyst(None, "account-model", tmp_path, Budget(),
                          transport=httpx.MockTransport(model), plan_auth=Plan()) as analyst:
        with pytest.raises(SourceError, match="Citation is outside"):
            analyst.analyze("fundamentals", "AAPL", "2025-10-01", "Question", [], {})
    assert len(calls) == 2  # Only actual analysis validation gets a bounded repair.
    assert not (tmp_path / "fundamentals.json").exists()


def test_valid_stream_survives_split_event_fields():
    expected = {"claims": [], "gaps": ["缺少资料"]}
    wire = completion(expected)

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(wire), 7):
                yield wire[offset:offset + 7]

    response = httpx.Response(200, stream=Chunks())
    try:
        actual = read_plan_response(response, Budget())
    finally:
        response.close()
    assert actual["status"] == "completed"
    assert json.loads(actual["output"][0]["content"][0]["text"]) == expected


def sse(events):
    return b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)


def sparse_completion():
    # Sanitized shape observed on the real subscription endpoint 2026-10-21:
    # deltas and done contain JSON, response.completed has output=[] + usage.
    return {"type": "response.completed", "response": {
        "status": "completed", "output": [], "usage": {"output_tokens": 13}}}


def text_event(kind, text, oi=0, ci=0):
    return {"type": "response.output_text." + kind, "output_index": oi,
            "content_index": ci, "delta" if kind == "delta" else "text": text}


def test_live_sparse_completion_shape_reaches_validated_analysis(tmp_path):
    expected = {"claims": [], "gaps": ["本次缺少可用证据"]}
    text = json.dumps(expected, ensure_ascii=False)
    events = [text_event("delta", text[:8]), text_event("delta", text[8:]),
              text_event("done", text),
              {"type": "response.output_item.done", "output_index": 0, "item": {
                  "type": "message", "content": [{"type": "output_text", "text": text}]}},
              sparse_completion()]
    calls = []

    def model(request):
        calls.append(request)
        return httpx.Response(200, content=sse(events))

    with ResponsesAnalyst(None, "account-model", tmp_path, Budget(),
                          transport=httpx.MockTransport(model), plan_auth=Plan()) as analyst:
        assert analyst.analyze("fundamentals", "AAPL", "2025-10-01", "Question", [], {}) == expected
        assert analyst.usage[0]["stream_output_reconstructed"] is True
    assert len(calls) == 1
    assert json.loads((tmp_path / "fundamentals-attempt-1-output.json").read_text())["output_text"] == text


def test_indexed_parts_reconstruct_in_output_order_without_duplicate_done_text():
    events = [text_event("delta", "second", 2), text_event("done", "second", 2),
              text_event("done", "first", 1), sparse_completion()]
    response = httpx.Response(200, content=sse(events))
    result = read_plan_response(response, Budget())
    assert [item["content"][0]["text"] for item in result["output"]] == ["first", "second"]


@pytest.mark.parametrize("events, message", [
    ([sparse_completion()], "without output text"),
    ([text_event("delta", "partial")], "interrupted"),
    ([text_event("delta", "partial"), {"type": "response.failed", "response": {"error": {"code": "failed"}}}], "HTTP stream"),
    ([text_event("delta", "a"), text_event("done", "b"), sparse_completion()], "disagrees"),
    ([text_event("delta", "a"), json.loads(completion({"claims": [], "gaps": ["缺少资料"]})[6:].strip())], "disagrees"),
    ([text_event("done", "x" * 24001), sparse_completion()], "text budget"),
    ([text_event("delta", "x", -1), sparse_completion()], "indices"),
    ([text_event("delta", "x", True), sparse_completion()], "indices"),
    ([{"type": "response.refusal.done", "refusal": "refused"}, sparse_completion()], "refused"),
    ([{"type": "response.content_part.done", "part": {"type": "refusal", "refusal": "refused"}}, sparse_completion()], "refused"),
])
def test_sparse_completion_preserves_failure_consistency_and_budget_guards(events, message):
    with pytest.raises(SourceError, match=message):
        read_plan_response(httpx.Response(200, content=sse(events)), Budget())
