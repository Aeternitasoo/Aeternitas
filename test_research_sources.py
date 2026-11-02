import json
from datetime import date, datetime, timezone

import httpx
import pytest

from filing_source import directory_rows, extract_sections
from news_source import load_news
from research_runtime import Budget, WebSources
from research_test_support import filing_html
from sources.sec import SourceError


def test_filing_extraction_prefers_body_and_retains_original_span():
    text, sections = extract_sections(filing_html(), "10-K")
    assert len(sections) == 2
    for section in sections:
        assert section["text"] == text[section["normalized_start"]:section["normalized_end"]]
        assert len(section["text"]) >= 200 and "Synthetic" in section["text"]


def test_sec_rate_limit_page_is_not_a_filing():
    with pytest.raises(SourceError): extract_sections(b"Request Rate Threshold Exceeded", "10-K")


def test_directory_rejects_column_misalignment():
    with pytest.raises(SourceError, match="different lengths"):
        directory_rows({"accessionNumber": [], "form": ["10-K"], "filingDate": [], "primaryDocument": []}, date(2025, 1, 1))


@pytest.mark.parametrize("url", ["http://www.sec.gov/Archives/edgar/data/1/1/a.htm", "https://127.0.0.1/x",
                                "https://www.sec.gov/Archives/edgar/data/1/000032019325000001/../../x.htm",
                                "https://finnhub.io.evil.invalid/api/v1/company-news"])
def test_source_allowlist_blocks_arbitrary_or_traversal_urls(tmp_path, url):
    calls = []
    def never_fetch(request):
        calls.append(request)
        raise AssertionError("Rejected URLs must not reach the transport")
    with WebSources(tmp_path, Budget(), transport=httpx.MockTransport(never_fetch)) as web:
        with pytest.raises(SourceError, match="allowlist"): web.get(url)
    assert calls == []


@pytest.mark.parametrize("status,headers,body", [(302, {"location": "https://elsewhere.invalid"}, b""),
                                               (200, {"content-type": "text/html"}, b"<html>error</html>"),
                                               (200, {"content-type": "application/json"}, b"x" * 100)])
def test_source_rejects_redirect_content_type_and_oversize(tmp_path, status, headers, body):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, headers=headers, content=body))
    with WebSources(tmp_path, Budget(), transport=transport) as web:
        with pytest.raises(SourceError): web.get("https://finnhub.io/api/v1/company-news", max_bytes=50)


def test_long_retry_after_does_not_hammer_provider(tmp_path):
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})
    with WebSources(tmp_path, Budget(), transport=httpx.MockTransport(response)) as web:
        with pytest.raises(SourceError, match="Retry-After"): web.get("https://finnhub.io/api/v1/company-news")
    assert len(calls) == 1


def test_news_filters_exact_timestamps_in_research_timezone_and_preserves_zero_results(tmp_path):
    rows = [{"id": 1, "datetime": int(datetime(2025, 10, 1, 17, tzinfo=timezone.utc).timestamp()),
             "headline": "Already tomorrow in Shanghai", "summary": "Not eligible", "url": "https://example.invalid/x"}]
    with WebSources(tmp_path, Budget(), transport=httpx.MockTransport(lambda req: httpx.Response(200, json=rows))) as web:
        news, source, coverage = load_news(web, "AAPL", "0000320193", date(2025, 10, 1), "synthetic")
    assert news == [] and coverage["state"] == "no_results_returned"
    assert "synthetic" not in json.dumps(source)


def test_news_api_error_object_is_not_an_empty_success(tmp_path):
    with WebSources(tmp_path, Budget(), transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"error": "not authorized"}))) as web:
        with pytest.raises(SourceError, match="Invalid Finnhub"):
            load_news(web, "AAPL", "0000320193", date(2025, 10, 1), "synthetic")
