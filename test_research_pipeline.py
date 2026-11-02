"""Business / failure integration tests. All data and model responses are synthetic."""

import json
from datetime import date

import pytest

from aeternitas import load_config, main
from research_analysis import ResponsesAnalyst, validate_result
from research_pipeline import run_research, saved_report
from research_runtime import Budget
from research_test_support import CONFIG, transports
from sources.sec import SourceError


def run(tmp_path, **options):
    transport, calls = transports(**options)
    run_id, report = run_research(tmp_path, "AAPL", "0000320193", date(2025, 10, 1),
                                    "Synthetic test question", CONFIG, transports=transport,
                                    evidence_kind="controlled_test")
    return run_id, report, calls


def test_complete_controlled_flow_and_saved_report_integrity(tmp_path):
    run_id, report, calls = run(tmp_path)
    assert report["model_attempts"] == 4
    assert [r for r, _ in calls["model"]] == ["fundamentals", "events", "risk", "synthesis"]
    assert report["financials"]["revenue_yoy"]["value"] == "20.00"
    assert report["news_coverage"]["selected_rows"] == 1
    folder, reloaded = saved_report(tmp_path, run_id)
    html = (folder / "report.html").read_text()
    assert "受控测试 / 回放样例，非真实 AI 研究" in html
    assert reloaded["evidence_kind"] == "controlled_test"
    assert "2000" not in report["analyses"]["synthesis"]["claims"][0]["text"]
    for secret in CONFIG.values():
        if "secret" in secret or "@" in secret:
            assert all(secret not in p.read_text(errors="replace") for p in (tmp_path / "runs").rglob("*.json"))
    assert all("OPENAI_API_KEY" not in str(payload) for _, payload in calls["model"])


def test_second_run_reuses_data_but_calls_real_model_interface_again(tmp_path):
    transport, calls = transports()
    options = dict(transports=transport, evidence_kind="controlled_test")
    first, _ = run_research(tmp_path, "AAPL", "0000320193", date(2025, 10, 1), "Question", CONFIG, **options)
    second, report = run_research(tmp_path, "AAPL", "0000320193", date(2025, 10, 1), "Question", CONFIG, **options)
    assert first != second
    assert len(calls["sec"]) == 1 and len(calls["web"]) == 4
    assert len(calls["model"]) == 8
    assert all(source["cache_hit"] for source in report["sources"])


def test_refresh_creates_new_retrieval_even_when_bytes_unchanged(tmp_path):
    run_id, report, _ = run(tmp_path)
    transport, calls = transports()
    _, refreshed = run_research(tmp_path, "AAPL", "0000320193", date(2025, 10, 1), "Question", CONFIG,
                                 transports=transport, refresh=True, evidence_kind="controlled_test")
    assert report["financials"]["source"]["sha256"] == refreshed["financials"]["source"]["sha256"]
    assert report["financials"]["source"]["retrieval_id"] != refreshed["financials"]["source"]["retrieval_id"]
    assert len(calls["web"]) == 4 and not refreshed["sources"][0]["cache_hit"]
    saved_report(tmp_path, run_id)


@pytest.mark.parametrize("options,stage", [({"news_failure": True}, "company_news"),
                                          ({"model_failure": "auth"}, "model:fundamentals"),
                                          ({"model_failure": "incomplete"}, "model:fundamentals"),
                                          ({"model_failure": "network"}, "model:fundamentals")])
def test_upstream_failures_are_saved_without_a_success_report(tmp_path, options, stage):
    transport, calls = transports(**options)
    with pytest.raises(SourceError):
        run_research(tmp_path, "AAPL", "0000320193", date(2025, 10, 1), "Question", CONFIG,
                     transports=transport, evidence_kind="controlled_test")
    folder = next((tmp_path / "runs").iterdir())
    state_text = (folder / "state.json").read_text()
    state = json.loads(state_text)
    assert state["status"] == "failed" and state["step"] == stage
    assert not (folder / "published.json").exists() and not (folder / "report.html").exists()
    assert CONFIG["OPENAI_API_KEY"] not in state_text
    assert CONFIG["FINNHUB_API_KEY"] not in state_text
    if options.get("news_failure"):
        assert calls["model"] == []
    if options.get("model_failure") == "network":
        assert state["model_attempts"] == 2
        assert all(u["status"] == "network_unknown" for u in state["model_usage"])
        assert all(u["transport_error_type"] == "ReadTimeout" and u["elapsed_seconds"] >= 0
                   for u in state["model_usage"])


def test_invalid_citation_is_repaired_before_later_roles(tmp_path):
    run_id, report, calls = run(tmp_path, repair_once=True)
    assert len(calls["model"]) == 5 and report["model_attempts"] == 5
    assert [role for role, _ in calls["model"]][:3] == ["fundamentals", "fundamentals", "events"]
    saved_report(tmp_path, run_id)


def test_zero_news_is_explicit_gap_not_a_provider_failure(tmp_path):
    _, report, _ = run(tmp_path, news_empty=True)
    assert report["status"] == "completed_with_gaps"
    assert report["news_coverage"]["state"] == "no_results_returned"
    assert report["analyses"]["events"]["claims"] == []


def test_untrusted_html_is_escaped_and_no_article_url_is_fetched(tmp_path):
    run_id, report, calls = run(tmp_path, malicious_news=True)
    html = (tmp_path / "runs" / run_id / "report.html").read_text()
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert all(request.url.host in {"data.sec.gov", "www.sec.gov", "finnhub.io"} for request in calls["web"])


@pytest.mark.parametrize("which", ["raw", "normalized", "report"])
def test_source_or_report_tampering_is_detected(tmp_path, which):
    run_id, report, _ = run(tmp_path)
    if which == "raw":
        path = tmp_path / report["sources"][0]["snapshot_path"]
    elif which == "normalized":
        evidence = next(e for e in report["evidence"] if e["kind"] == "filing")
        path = tmp_path / evidence["locator"]["normalized_path"]
    else:
        path = tmp_path / "runs" / run_id / "report.html"
    path.write_bytes(path.read_bytes() + b"TAMPERED")
    with pytest.raises(SourceError, match="fingerprint"):
        saved_report(tmp_path, run_id)


@pytest.mark.parametrize("mutation", ["missing_id", "fake_quote", "no_citation", "number", "extra_key", "too_many"])
def test_claim_guard_rejects_unknown_sources_and_unverified_numbers(mutation):
    evidence = [{"id": "E", "text": "This is a genuine supplied test quotation."}]
    result = {"claims": [{"text": "仅作测试观点", "stance": "neutral",
                          "citations": [{"evidence_id": "E", "quote": evidence[0]["text"]}]}], "gaps": []}
    if mutation == "missing_id": result["claims"][0]["citations"][0]["evidence_id"] = "other"
    elif mutation == "fake_quote": result["claims"][0]["citations"][0]["quote"] = "A quotation that was never supplied."
    elif mutation == "no_citation": result["claims"][0]["citations"] = []
    elif mutation == "number": result["claims"][0]["text"] = "Profit grew 99%"
    elif mutation == "extra_key": result["invented"] = "value"
    else: result["claims"] *= 7
    with pytest.raises(ValueError): validate_result(result, evidence)


def test_call_budget_prevents_second_paid_attempt(tmp_path):
    transport, calls = transports(repair_once=True)
    with ResponsesAnalyst(CONFIG["OPENAI_API_KEY"], CONFIG["AETERNITAS_MODEL"], tmp_path,
                          Budget(max_calls=1), transport=transport["model"]) as client:
        with pytest.raises(SourceError, match="budget exhausted"):
            client.analyze("fundamentals", "AAPL", "2025-10-01", "Question",
                           [{"id": "E", "kind": "filing", "title": "Test", "published_at": "2025-01-01",
                             "text": "A sufficiently long synthetic source quotation."}], {})
    assert len(calls["model"]) == 1


def test_late_model_response_cannot_publish_or_start_next_role(tmp_path):
    import httpx
    budget = Budget()
    calls = []
    def late_response(request):
        calls.append(request)
        budget.deadline = 0
        return httpx.Response(200, json={"status": "completed", "output": []})
    with ResponsesAnalyst(CONFIG["OPENAI_API_KEY"], CONFIG["AETERNITAS_MODEL"], tmp_path,
                          budget, transport=httpx.MockTransport(late_response)) as client:
        with pytest.raises(SourceError, match="deadline exceeded"):
            client.analyze("fundamentals", "AAPL", "2025-10-01", "Question", [], {})
    assert len(calls) == 1 and not (tmp_path / "fundamentals.json").exists()


def test_config_is_data_not_shell_code_and_env_precedence(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('OPENAI_API_KEY="literal$(do-not-run)"\nAETERNITAS_MODEL="test-model"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "environment-value")
    assert load_config(env)["OPENAI_API_KEY"] == "environment-value"
    assert load_config(env)["AETERNITAS_MODEL"] == "test-model"


def test_invalid_run_id_rejected_without_reading_other_paths(tmp_path):
    with pytest.raises(ValueError): saved_report(tmp_path, "../../elsewhere")


def test_cli_rejects_malformed_company_before_network():
    assert main(["run", "../NOT-A-TICKER"]) == 1


def test_cli_run_automatically_audits_the_saved_report(tmp_path, monkeypatch, capsys):
    import aeternitas
    original_run = run_research
    original_saved = saved_report
    transport, calls = transports()
    verified = []
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "")  # Offline CLI tests never use the local project DB.
    def controlled_run(*args, **kwargs):
        return original_run(*args, **kwargs, transports=transport, evidence_kind="controlled_test")
    def observed_verify(root, run_id):
        result = original_saved(root, run_id)
        verified.append(run_id)
        return result
    monkeypatch.setattr(aeternitas, "run_research", controlled_run)
    monkeypatch.setattr(aeternitas, "saved_report", observed_verify)
    assert main(["--data-dir", str(tmp_path), "run-local", "AAPL", "--as-of", "2025-10-01"]) == 0
    assert len(verified) == 1 and len(calls["model"]) == 4
    assert "已自动完成保存后的来源" in capsys.readouterr().out
