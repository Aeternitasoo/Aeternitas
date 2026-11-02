"""Date/identity/update regressions; all HTTP responses here are synthetic."""

import copy
import json
import hashlib
from pathlib import Path
from datetime import date, datetime, timezone

import httpx
import pytest

from filing_source import directory_rows, load_filing_evidence
from news_source import canonical_news_url, load_news
from research_inputs import collect_inputs
from research_pipeline import audit_inputs
from research_runtime import Budget, WebSources
from research_test_support import CONFIG, filing_html, transports
from sources.sec import SourceError


def test_frozen_public_sec_subset_preserves_two_company_annual_contract():
    from financial_facts import build_financial_summary
    fixture=json.loads((Path(__file__).parent/"tests/fixtures/m2_sec_annual_v1.json").read_text())
    assert fixture["schema_version"]=="m2-sec-annual-subset-v1"
    for company in fixture["companies"]:
        raw=company["raw_subset"]
        sha=hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest()
        result=build_financial_summary(raw,company["cik"],company["as_of_date"],{"sha256":sha})
        for period,expected in zip(result["annual_periods"],company["expected_periods"]):
            assert period["period_start"]==expected["period_start"] and period["period_end"]==expected["period_end"]
            assert {k:f["value"] for k,f in period["facts"].items()}==expected["values"]
            assert period["operating_margin"]["value"]==expected["operating_margin"]
            assert period["net_margin"]["value"]==expected["net_margin"]
        assert result["revenue_yoy"]["value"]==company["expected_revenue_yoy"]


def news_row(identity, url, hour=12, **extra):
    return {"id": identity, "url": url, "related": "AAPL", "headline": "Synthetic company announcement",
            "summary": "Synthetic summary for a contract regression.",
            "datetime": int(datetime(2025,10,1,hour,tzinfo=timezone.utc).timestamp()), **extra}


def test_news_deduplicates_canonical_urls_and_provider_ids_using_latest_observation(tmp_path):
    rows = [news_row(1,"https://EXAMPLE.invalid:443/story?id=9&utm_source=test#section"),
            news_row(2,"https://example.invalid/story?id=9",13,summary="Updated synthetic summary."),
            news_row(2,"https://example.invalid/other",11),
            news_row(3,"https://example.invalid/story?id=10"),
            news_row(4,"https://example.invalid/unrelated",related="MSFT"),
            news_row(5,"https://example.invalid/future",17)]
    with WebSources(tmp_path,Budget(),transport=httpx.MockTransport(lambda _:httpx.Response(200,json=rows))) as web:
        selected,_,coverage=load_news(web,"AAPL","0000320193",date(2025,10,1),"synthetic")
    assert len(selected)==2 and coverage["eligible_unique_rows"]==2
    assert selected[0]["locator"]["response_index"]==1
    assert selected[0]["text"].endswith("Updated synthetic summary.")
    assert coverage["newest_returned_eligible"]==selected[0]["published_at"]
    assert canonical_news_url(rows[0]["url"])==rows[1]["url"]
    assert canonical_news_url(rows[3]["url"])!=rows[1]["url"]


def test_new_provider_response_enters_selection_without_local_file_edits(tmp_path):
    calls=[]
    def provider(request):
        calls.append(request)
        rows=[news_row(1,"https://example.invalid/first")]
        if len(calls)>1: rows.append(news_row(2,"https://example.invalid/new",13))
        return httpx.Response(200,json=rows)
    with WebSources(tmp_path,Budget(),transport=httpx.MockTransport(provider)) as web:
        first,source1,_=load_news(web,"AAPL","0000320193",date(2025,10,1),"synthetic")
        cached,receipt,_=load_news(web,"AAPL","0000320193",date(2025,10,1),"synthetic")
        updated,source2,_=load_news(web,"AAPL","0000320193",date(2025,10,1),"synthetic",refresh=True)
    assert len(calls)==2 and len(first)==len(cached)==1 and receipt["cache_hit"]
    assert len(updated)==2 and source1["sha256"]!=source2["sha256"]


def test_history_budget_skips_future_pages_and_can_find_relevant_page_after_four_future_entries(tmp_path):
    calls=[]
    preferred="0000320193-24-000001"
    def directory(accessions,forms,dates,documents):
        return dict(zip(("accessionNumber","form","filingDate","primaryDocument"),
                        (accessions,forms,dates,documents)))
    def provider(request):
        calls.append(str(request.url))
        if str(request.url).endswith("CIK0000320193.json"):
            pages=[{"name":f"CIK0000320193-submissions-{i:03}.json","filingFrom":"2026-01-01","filingTo":"2026-12-31"} for i in range(1,5)]
            pages.append({"name":"CIK0000320193-submissions-005.json","filingFrom":"2023-01-01","filingTo":"2024-12-31"})
            return httpx.Response(200,json={"cik":320193,"filings":{"recent":directory(
                ["0000320193-25-000002"],["10-Q"],["2025-08-01"],["q.htm"]),"files":pages}})
        if "submissions-005" in str(request.url):
            return httpx.Response(200,json=directory([preferred],["10-K"],["2024-02-01"],["k.htm"]))
        return httpx.Response(200,content=filing_html("10-Q" if "q.htm" in str(request.url) else "10-K"),headers={"content-type":"text/html"})
    with WebSources(tmp_path,Budget(),transport=httpx.MockTransport(provider)) as web:
        evidence,_,gaps=load_filing_evidence(web,"0000320193",date(2025,10,1),"test@example.invalid",preferred)
    assert not gaps and any(e["locator"]["accession"]==preferred for e in evidence)
    assert sum("submissions-" in url for url in calls)==1


def test_filing_eligibility_uses_filing_date_not_report_period():
    rows={"accessionNumber":["0000320193-25-000001"],"form":["10-K"],
          "filingDate":["2025-02-01"],"reportDate":["2024-12-31"],"primaryDocument":["k.htm"]}
    assert directory_rows(rows,date(2025,1,31))==[]
    assert directory_rows(rows,date(2025,2,1))[0]["period_end"]=="2024-12-31"


def test_incomplete_directory_coverage_is_reported_as_specific_gaps(tmp_path):
    def provider(request):
        if request.url.host=="data.sec.gov":
            return httpx.Response(200,json={"cik":320193,"filings":{"recent":{
                "accessionNumber":["0000320193-25-000002"],"form":["10-Q"],
                "filingDate":["2025-08-01"],"primaryDocument":["q.htm"]},"files":[]}})
        return httpx.Response(200,content=filing_html("10-Q"),headers={"content-type":"text/html"})
    with WebSources(tmp_path,Budget(),transport=httpx.MockTransport(provider)) as web:
        evidence,_,gaps=load_filing_evidence(web,"0000320193",date(2025,10,1),"test@example.invalid","0000320193-25-000001")
    assert len(evidence)==2
    assert "Annual fact accession not found within bounded directory coverage" in gaps
    assert "Only one filing covered before cutoff" in gaps


@pytest.mark.parametrize("mutation",["financial_text","news_date","news_url","filing_date","company"])
def test_import_audit_rejects_semantic_provenance_mutations_before_sql(tmp_path,mutation):
    transport,calls=transports()
    bundle=collect_inputs(tmp_path,"AAPL","0000320193",date(2025,10,1),"Synthetic",CONFIG,
                          refresh=True,news_days=14,budget=Budget(),transports=transport,evidence_kind="controlled_test")
    assert calls["model"]==[] and audit_inputs(tmp_path,bundle)
    broken=copy.deepcopy(bundle)
    kind={"financial_text":"financial_fact","news_date":"news","news_url":"news","filing_date":"filing","company":"news"}[mutation]
    item=next(e for e in broken["evidence"] if e["kind"]==kind)
    if mutation=="financial_text":item["text"]="Invented financial value"
    elif mutation=="news_date":item["published_at"]="2025-10-02T00:00:00+00:00"
    elif mutation=="news_url":item["source_url"]="https://example.invalid/invented"
    elif mutation=="filing_date":item["published_at"]="2024-01-01"
    else:item["company_cik"]="0000789019"
    with pytest.raises(SourceError):audit_inputs(tmp_path,broken)
