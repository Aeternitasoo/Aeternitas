"""Explicitly synthetic responses for offline tests; never used by the live CLI."""

import json
from datetime import datetime, timezone

import httpx

from research_analysis import ROLE_TASKS
from test_financial_facts import make_facts


CONFIG = {"SEC_CONTACT": "test@example.invalid", "FINNHUB_API_KEY": "synthetic-news-secret",
          "AETERNITAS_AUTH": "api_key",
          "OPENAI_API_KEY": "synthetic-model-secret", "AETERNITAS_MODEL": "synthetic-test-model"}


def filing_html(form="10-K"):
    management_item = "7" if form == "10-K" else "2"
    return ("<html><body><p>Item 1A. Risk Factors</p><p>TOC</p>"
            f"<p>Item {management_item}. Management Discussion</p><p>TOC</p>"
            "<p>Item 1A. Risk Factors</p><p>" + "Synthetic business risk, not a real filing. " * 40 + "</p>"
            f"<p>Item {management_item}. Management Discussion</p><p>"
            + "Synthetic operating discussion, not real financial data. " * 40
            + "</p><p>Item 9. Other Information</p></body></html>").encode()


def transports(*, news_empty=False, news_failure=False, model_failure=None,
               repair_once=False, malicious_news=False):
    calls = {"sec": [], "web": [], "model": []}

    def sec(request):
        calls["sec"].append(request)
        return httpx.Response(200, json=make_facts())

    def web(request):
        calls["web"].append(request)
        if request.url.host == "data.sec.gov":
            return httpx.Response(200, json={"cik": "320193", "filings": {"recent": {
                "accessionNumber": ["0000320193-25-000001", "0000320193-25-000002"],
                "form": ["10-K", "10-Q"], "filingDate": ["2025-02-01", "2025-08-01"],
                "primaryDocument": ["synthetic-k.htm", "synthetic-q.htm"],
            }, "files": []}})
        if request.url.host == "www.sec.gov":
            return httpx.Response(200, content=filing_html("10-Q" if "q.htm" in request.url.path else "10-K"),
                                  headers={"content-type": "text/html"})
        if news_failure:
            return httpx.Response(403, json={"error": "synthetic entitlement failure"})
        summary = '<script>alert("x")</script> Synthetic provider summary.' if malicious_news else "Synthetic provider summary; not actual company news."
        rows = [] if news_empty else [{"id": 1, "datetime": int(datetime(2025, 9, 30, 12, tzinfo=timezone.utc).timestamp()),
                                      "headline": "Synthetic announcement (NOT real news)", "summary": summary,
                                      "related": "AAPL", "url": "https://example.invalid/synthetic-news"}]
        return httpx.Response(200, json=rows)

    def model(request):
        payload = json.loads(request.content)
        role = next(k for k, task in ROLE_TASKS.items() if task in payload["input"][0]["content"])
        calls["model"].append((role, payload))
        if model_failure == "auth":
            return httpx.Response(401, json={"error": {"message": CONFIG["OPENAI_API_KEY"]}})
        if model_failure == "network":
            raise httpx.ReadTimeout("contains " + CONFIG["OPENAI_API_KEY"], request=request)
        evidence = json.loads(payload["input"][1]["content"])["evidence"]
        result = {"claims": [], "gaps": ["这是受控测试材料，不能形成真实投资判断"]}
        if evidence:
            result["claims"] = [{"text": "本条仅验证流程与引用，不能当作实际研究结论", "stance": "neutral",
                                  "citations": [{"evidence_id": evidence[0]["id"], "quote": evidence[0]["text"][:80]}]}]
        if repair_once and len(calls["model"]) == 1:
            result["claims"][0]["citations"][0]["evidence_id"] = "DOES-NOT-EXIST"
        return httpx.Response(200, json={"id": "synthetic-response", "status": "incomplete" if model_failure == "incomplete" else "completed",
                                        "usage": {"input_tokens": 100, "output_tokens": 100},
                                        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]})

    return {"sec": httpx.MockTransport(sec), "web": httpx.MockTransport(web), "model": httpx.MockTransport(model)}, calls
