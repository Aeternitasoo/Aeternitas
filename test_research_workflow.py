"""Synthetic offline workflow integration; every HTTP request uses MockTransport."""

from copy import deepcopy
from datetime import date
import json

import httpx
import pytest

from research_analysis import ResponsesAnalyst
from research_inputs import collect_inputs
from research_observability import Recorder
from research_runtime import Budget
from research_test_support import CONFIG, filing_html
from test_financial_facts import make_facts
import research_workflow as workflow
from sources.sec import SourceError


NAME = "Synthetic Example (NOT real financial data)"


def config(symbols=("AAPL",), question="研究经营变化及风险"):
    ciks = {"AAPL": "0000320193", "OTHER": "0000001234"}
    return {"question": question,
            "subjects": [{"symbol": s, "cik": ciks[s], "report_entity": NAME} for s in symbols],
            "as_of_date": "2025-10-01", "model": "mock-research",
            "sections": [{"id": "business", "title": "经营证据"}, {"id": "gaps", "title": "研究边界"}],
            "filing_terms": ["operating discussion"], "evidence_kind": "controlled_test"}


def sources(*, failed=False, empty_news=False):
    def sec(request):
        cik = request.url.path.rsplit("CIK", 1)[1].split(".")[0]
        return httpx.Response(200, json=make_facts(cik=int(cik)))

    def web(request):
        if request.url.host == "data.sec.gov":
            cik = request.url.path.rsplit("CIK", 1)[1].split(".")[0]
            return httpx.Response(200, json={"cik": cik, "filings": {"recent": {
                "accessionNumber": ["0000320193-25-000001", "0000320193-25-000002"],
                "form": ["10-K", "10-Q"], "filingDate": ["2025-02-01", "2025-08-01"],
                "primaryDocument": ["synthetic-k.htm", "synthetic-q.htm"]}, "files": []}})
        if request.url.host == "www.sec.gov":
            return httpx.Response(200, content=filing_html("10-Q" if "q.htm" in request.url.path else "10-K"),
                                  headers={"content-type": "text/html"})
        if failed:
            return httpx.Response(403, json={"error": CONFIG["FINNHUB_API_KEY"]})
        from datetime import datetime, timezone
        rows = [] if empty_news else [{"id": 1, "datetime": int(datetime(2025, 9, 30, tzinfo=timezone.utc).timestamp()),
                 "headline": "Synthetic company disclosure", "summary": "Synthetic summary, not real news.",
                 "related": request.url.params["symbol"], "url": "https://example.invalid/synthetic"}]
        return httpx.Response(200, json=rows)
    return {"sec": httpx.MockTransport(sec), "web": httpx.MockTransport(web)}


def output(payload):
    evidence = payload["evidence"][0]
    return {"sections": [{"topic": s["id"], "claims": [
        {"classification": "fact", "text": "本项仅用于核验合成证据与流程",
         "citations": [{"evidence_id": evidence["id"], "quote": evidence["text"][:80]}]},
        {"classification": "unknown", "text": "当前资料没有覆盖完整产业信息", "citations": []}]} for s in payload["sections"]]}


def factory(calls, mutate=None, *, sse=False, usage=True, failure=None):
    def create(folder, budget):
        def model(request):
            body = json.loads(request.content)
            payload = json.loads(body["input"][1]["content"])
            calls.append(payload)
            if failure:
                raise httpx.ReadTimeout("do-not-persist " + CONFIG["OPENAI_API_KEY"], request=request)
            result = output(payload)
            if mutate:
                mutate(result)
            envelope = {"id": "resp_synthetic", "status": "completed", "model": "mock-research-version",
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]}
            if usage:
                envelope["usage"] = {"input_tokens": 101, "output_tokens": 55, "total_tokens": 156,
                                     "sensitive": "never-export"}
            if sse:
                return httpx.Response(200, content=("data: " + json.dumps({"type": "response.completed", "response": envelope}) + "\n\n").encode())
            return httpx.Response(200, json=envelope)
        class Plan:
            def token(self):
                return "synthetic-plan-secret"
        return ResponsesAnalyst(None if sse else CONFIG["OPENAI_API_KEY"], "mock-research", folder, budget,
                                transport=httpx.MockTransport(model), plan_auth=Plan() if sse else None)
    return create


def run(tmp_path, *, c=None, calls=None, **options):
    calls = [] if calls is None else calls
    return workflow.run_workflow(c or config(), tmp_path, provider_config=CONFIG,
                                 transports=sources(), client_factory=factory(calls, **options))


def read(folder, name):
    return json.loads((folder / name).read_text())


@pytest.mark.parametrize("symbols,question", [(("AAPL",), "研究现金流"), (("OTHER",), "研究供给风险"),
                                               (("AAPL", "OTHER"), "跨企业经营传导关系")])
def test_configurable_subjects_question_and_publication(tmp_path, symbols, question):
    calls = []
    folder = run(tmp_path, c=config(symbols, question), calls=calls)
    assert len(calls) == 1
    assert calls[0]["question"] == question
    assert [s["symbol"] for s in calls[0]["subjects"]] == list(symbols)
    assert {e["symbol"] for e in calls[0]["evidence"]} == set(symbols)
    report = read(folder, "report.json")
    assert report["strict_historical_replay"] is False
    assert report["model_attempts"] == 1 and report["model_metadata"]["response_id"] == "resp_synthetic"
    assert report["model_metadata"]["usage"] == {"input_tokens": 101, "output_tokens": 55, "total_tokens": 156}
    assert report["model_metadata"]["cost"] is None
    assert report["prompt"]["rendered_sha256"] == workflow.digest(read(folder, "request.json")["messages"])
    assert read(folder, "input-manifest.json")["input_fingerprint"] == report["input_fingerprint"]
    assert read(folder, "model-attempt.json")["count"] == 1
    assert workflow.verify_workflow(folder)
    html = (folder / "report.html").read_text()
    for symbol in symbols:
        assert f"{symbol} · USD" in html
    for term in ("事实", "未知", "注入模型响应"):
        assert term in html
    for e in report["evidence"]:
        assert f'id="{e["id"]}"' in html
    assert any(e["locator"].get("selection_version") == workflow.VERSION for e in report["evidence"])
    events = [json.loads(s) for s in (folder / "events.jsonl").read_text().splitlines()]
    assert events[0]["name"] == "research" and events[-1]["phase"] == "end"
    assert events[-1]["parent_span_id"] is None
    assert all(e["trace_id"] == folder.name for e in events)
    assert all(e["elapsed_seconds"] >= 0 for e in events if e["phase"] == "end")
    assert not any(k in str(events) for k in (CONFIG["OPENAI_API_KEY"], CONFIG["FINNHUB_API_KEY"], "never-export", "messages"))


@pytest.mark.parametrize("sse,usage", [(True, True), (False, False)])
def test_existing_client_exposes_only_actual_metadata(tmp_path, sse, usage):
    folder = run(tmp_path, sse=sse, usage=usage)
    meta = read(folder, "model-metadata.json")
    assert meta["completion_status"] == "completed" and meta["http_status"] == 200
    assert (meta["usage"] is None) == (not usage)


def test_sink_mutation_or_failure_cannot_change_research(tmp_path):
    transport = sources()
    common = dict(provider_config=CONFIG, transports=transport, client_factory=factory([]))
    first = workflow.run_workflow(config(), tmp_path, **common)
    # Reuse the exact frozen input bytes/metadata from the first run.
    c = config(); c["evidence_kind"] = "frozen_live_evidence"
    frozen = workflow.FrozenInputs(first / "data", {"AAPL": {"path": str(first / "AAPL-bundle.json"),
                                                            "sha256": workflow.sha(first / "AAPL-bundle.json")}})
    def bad_sink(event):
        event["input_fingerprint"] = "mutated"
        raise RuntimeError("secret-export-error")
    one = workflow.run_workflow(c, tmp_path, collector=frozen, client_factory=factory([]))
    two = workflow.run_workflow(c, tmp_path, collector=frozen, client_factory=factory([]), sink=bad_sink)
    for name in ("input.json", "raw-response.json", "model-metadata.json", "request.json"):
        assert read(one, name) == read(two, name)
    assert read(one, "report.json")["output_fingerprint"] == read(two, "report.json")["output_fingerprint"]
    assert read(two, "state.json")["observability_export_errors"] > 0
    events = [json.loads(line) for line in (two / "events.jsonl").read_text().splitlines()]
    assert all(e["request_attempts"] == 0 for e in events if e["name"] == "source_collection" and e["phase"] == "end")
    assert "secret-export-error" not in (two / "events.jsonl").read_text()
    assert workflow.verify_workflow(two)


@pytest.mark.parametrize("mutation", ["unknown_id", "fake_quote", "missing_cite", "extra_key", "wrong_section", "number", "duplicate_cite"])
def test_invalid_response_never_commits_and_retains_raw(tmp_path, mutation):
    def change(result):
        claim = result["sections"][0]["claims"][0]
        if mutation == "unknown_id": claim["citations"][0]["evidence_id"] = "missing"
        if mutation == "fake_quote": claim["citations"][0]["quote"] = "An invented source quotation."
        if mutation == "missing_cite": claim["citations"] = []
        if mutation == "extra_key": claim["unapproved"] = True
        if mutation == "wrong_section": result["sections"][0]["topic"] = "other"
        if mutation == "number": claim["text"] = "增长99%"
        if mutation == "duplicate_cite": claim["citations"] *= 2
    calls = []
    with pytest.raises(SourceError, match="validation"):
        run(tmp_path, calls=calls, mutate=change)
    folder = next(tmp_path.iterdir())
    assert len(calls) == 1 and (folder / "raw-response.json").exists()
    assert read(folder, "state.json")["status"] == "failed"
    assert not (folder / "COMMITTED.json").exists() and not (folder / "report.html").exists()


def test_data_source_failure_is_recorded_without_call_or_secret(tmp_path):
    calls = []
    with pytest.raises(SourceError, match="collection"):
        workflow.run_workflow(config(), tmp_path, provider_config=CONFIG, transports=sources(failed=True),
                              client_factory=factory(calls))
    folder = next(tmp_path.iterdir())
    assert calls == [] and not (folder / "COMMITTED.json").exists()
    state = read(folder, "state.json")
    assert state["step"] == "collection:AAPL" and state["status"] == "failed"
    assert all("secret" not in p.read_text() for p in folder.glob("*.json*"))


def test_empty_news_is_explicit_gap(tmp_path):
    folder = workflow.run_workflow(config(), tmp_path, provider_config=CONFIG, transports=sources(empty_news=True),
                                   client_factory=factory([]))
    assert not any(e["kind"] == "news" for e in read(folder, "input.json")["evidence"])
    assert read(folder, "report.json")["status"] == "completed_with_gaps"


def test_transport_failure_is_single_attempt_and_sanitized(tmp_path):
    calls = []
    with pytest.raises(SourceError, match="model"):
        run(tmp_path, calls=calls, failure=True)
    folder = next(tmp_path.iterdir())
    assert len(calls) == 1 and read(folder, "state.json")["model_attempts"] == 1
    assert not (folder / "COMMITTED.json").exists()
    assert CONFIG["OPENAI_API_KEY"] not in (folder / "state.json").read_text()


def test_entity_alias_requires_review_and_is_preserved(tmp_path):
    c = config(); c["subjects"][0]["report_entity"] = "Reviewed parent"
    calls = []
    with pytest.raises(SourceError, match="evidence"):
        run(tmp_path / "blocked", c=c, calls=calls)
    assert not calls
    c["subjects"][0].update(accepted_fact_names=[NAME], identity_note="Synthetic alias reviewed, not independent source certification")
    folder = run(tmp_path / "reviewed", c=c)
    assert read(folder, "AAPL-bundle.json")["financials"]["company_name"] == NAME
    assert "Synthetic alias reviewed" in (folder / "report.html").read_text()


def test_no_overwrite_and_published_tamper_detected(tmp_path):
    folder = workflow.run_workflow(config(), tmp_path, provider_config=CONFIG, transports=sources(),
                                   client_factory=factory([]), run_id="fixed")
    before = workflow.sha(folder / "report.json")
    with pytest.raises(FileExistsError):
        workflow.run_workflow(config(), tmp_path, client_factory=factory([]), run_id="fixed")
    assert workflow.sha(folder / "report.json") == before
    with (folder / "report.html").open("a") as stream:
        stream.write("changed")
    with pytest.raises(SourceError, match="fingerprint"):
        workflow.verify_workflow(folder)


def test_missing_evidence_and_frozen_hash_fail_before_model(tmp_path):
    calls = []
    def no_evidence(*args, **kwargs):
        bundle = collect_inputs(*args, **kwargs)
        bundle["evidence"] = []
        return bundle
    with pytest.raises(SourceError, match="evidence"):
        workflow.run_workflow(config(), tmp_path / "empty", provider_config=CONFIG, collector=no_evidence,
                              transports=sources(), client_factory=factory(calls))
    assert not calls
    c = config(); c["evidence_kind"] = "frozen_live_evidence"
    path = tmp_path / "wrong.json"; path.write_text("{}")
    frozen = workflow.FrozenInputs(tmp_path, {"AAPL": {"path": str(path), "sha256": "0" * 64}})
    with pytest.raises(SourceError, match="collection"):
        workflow.run_workflow(c, tmp_path / "hash", collector=frozen, client_factory=factory(calls))
    assert not calls


def test_observability_budget_and_timeout_classification(tmp_path):
    recorder = Recorder(tmp_path / "events.jsonl", "synthetic")
    for message in ("Research model call budget exhausted", "Research deadline exceeded"):
        with pytest.raises(SourceError):
            with recorder.span("step"):
                raise SourceError(message)
    assert [e["error_code"] for e in recorder.events if e["phase"] == "end"] == ["budget_exhausted", "timeout_or_deadline"]


def test_report_save_failure_cannot_publish(tmp_path, monkeypatch):
    def fail(_):
        raise OSError("synthetic disk error with sensitive details")
    monkeypatch.setattr(workflow, "render_report", fail)
    with pytest.raises(SourceError, match="publication"):
        run(tmp_path)
    folder = next(tmp_path.iterdir())
    assert read(folder, "state.json")["step"] == "publication"
    assert not (folder / "COMMITTED.json").exists()
    assert "sensitive" not in (folder / "state.json").read_text()


def test_final_verification_failure_is_not_committed(tmp_path, monkeypatch):
    def fail(*args):
        raise ValueError("synthetic verification failure")
    monkeypatch.setattr(workflow, "_verify_artifacts", fail)
    with pytest.raises(SourceError, match="publication_verification"):
        run(tmp_path)
    folder = next(tmp_path.iterdir())
    assert not (folder / "COMMITTED.json").exists()
    assert read(folder, "failure.json")["publication_valid"] is False
    with pytest.raises(SourceError, match="failed"):
        workflow.verify_workflow(folder)
