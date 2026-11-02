"""M6 source/update campaign: saved live evidence plus explicit controlled HTTP.

No source or model network request is made. The update exercise uses the real
collectors/parsers/auditor/importer, MockTransport, and a disposable PG schema.
Saved live claims are reviewed by the assistant, not independently by the user.
"""

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
from html import unescape
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import evidence_repository as repository
from filing_fact_audit import InlineFacts
from filing_source import load_filing_evidence
from financial_facts import METRIC_TAGS
from news_source import load_news
from research_inputs import collect_inputs
from research_pipeline import audit_inputs, saved_report
from research_runtime import Budget, WebSources, utc_now
from research_test_support import CONFIG, filing_html
from sources.sec import SourceError, write_json
from test_financial_facts import make_facts


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "data/live"
AAPL = "379073942801443698b541bb7c913124"
MSFT = "b2fc146160954ef2854b11287ef36e2f"
CUTOFF = date(2025, 10, 1)


def need(condition, label):
    if not condition:
        raise AssertionError(label)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pointer(value, location):
    for part in location.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class ControlledProvider:
    """Only synthetic response bodies; counters count transport requests."""
    def __init__(self):
        self.version = 1
        self.bad = None
        self.calls = []
        self.transports = {name: httpx.MockTransport(self.respond) for name in ("sec", "web")}

    def respond(self, request):
        self.calls.append({"host": request.url.host, "path": request.url.path})
        if request.url.host == "data.sec.gov" and "/companyfacts/" in request.url.path:
            raw = make_facts()
            if self.bad == "financial_fields":
                return httpx.Response(200, json={"cik": 320193, "entityName": "Synthetic"})
            if self.version >= 2:
                for metric, value in (("revenue", 130), ("operating_income", 35), ("net_income", -5)):
                    rows = raw["facts"]["us-gaap"][METRIC_TAGS[metric][0]]["units"]["USD"]
                    rows.extend([{**rows[-1], "val": value, "accn": "0000320193-25-000003", "filed": "2025-05-01", "form": "10-K/A"},
                                 {**rows[-1], "val": 9999, "accn": "0000320193-25-000004", "filed": "2025-11-01", "form": "10-K/A"}])
            return httpx.Response(200, json=raw)
        if request.url.host == "data.sec.gov":
            accessions = ["0000320193-25-000001", "0000320193-25-000002"]
            forms, dates, docs, periods = ["10-K", "10-Q"], ["2025-02-01", "2025-08-01"], ["base-k.htm", "q.htm"], ["2024-12-31", "2025-06-30"]
            if self.version >= 2:
                accessions += ["0000320193-25-000003", "0000320193-25-000004"]
                forms += ["10-K/A", "10-K/A"]
                dates += ["2025-05-01", "2025-11-01"]
                docs += ["revised-k.htm", "late-k.htm"]
                periods += ["2024-12-31", "2024-12-31"]
            directory = dict(zip(("accessionNumber", "form", "filingDate", "primaryDocument", "reportDate"),
                                 (accessions, forms, dates, docs, periods)))
            return httpx.Response(200, json={"cik": 320193, "filings": {"recent": directory, "files": []}})
        if request.url.host == "www.sec.gov":
            need("late-k" not in request.url.path, "late_filing_was_fetched")
            body = filing_html("10-Q" if request.url.path.endswith("q.htm") else "10-K")
            if "revised-k" in request.url.path:
                body = body.replace(b"Synthetic", b"Revised synthetic")
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})
        need(request.url.host == "finnhub.io", "unexpected_host_in_controlled_transport")
        if self.bad == "news_http":
            return httpx.Response(403, json={"error": "synthetic unavailable"})
        if self.bad == "news_shape":
            return httpx.Response(200, json={"error": "synthetic error object"})
        rows = [{"id": 1, "datetime": int(datetime(2025, 9, 30, 12, tzinfo=timezone.utc).timestamp()),
                 "related": "AAPL", "headline": "Synthetic original announcement", "summary": "Controlled version one; no actual company news.",
                 "url": "https://example.invalid/original"}]
        if self.version >= 2:
            rows.extend([{"id": 2, "datetime": int(datetime(2025, 10, 1, 12, tzinfo=timezone.utc).timestamp()),
                          "related": "AAPL", "headline": "Synthetic newly disclosed announcement", "summary": "Controlled version two; not actual news.",
                          "url": "https://example.invalid/new"},
                         {"id": 3, "datetime": int(datetime(2025, 10, 1, 17, tzinfo=timezone.utc).timestamp()),
                          "related": "AAPL", "headline": "Future in Shanghai", "url": "https://example.invalid/future"}])
        return httpx.Response(200, json=rows)


def collect(root, provider, refresh=False):
    result = collect_inputs(root, "AAPL", "0000320193", CUTOFF, "Synthetic update campaign", CONFIG,
                            refresh=refresh, news_days=14, budget=Budget(), transports=provider.transports,
                            evidence_kind="controlled_test")
    audit_inputs(root, result)
    return result


def revenue(bundle):
    return bundle["financials"]["annual_periods"][0]["facts"]["revenue"]["value"]


def controlled_campaign(root, connection=None):
    root = Path(root)
    provider = ControlledProvider()
    stages = []
    def stage(name, bundle, before):
        stages.append({"name": name, "transport_requests": len(provider.calls)-before,
                       "retrieval_ids": [s["retrieval_id"] for s in bundle["sources"]],
                       "cache_hits": sum(s["cache_hit"] for s in bundle["sources"]),
                       "source_count": len(bundle["sources"]), "revenue": revenue(bundle),
                       "news_count": sum(e["kind"] == "news" for e in bundle["evidence"])})
    first = collect(root, provider)
    stage("first_fetch", first, 0)
    need(len(provider.calls) == 5 and not any(s["cache_hit"] for s in first["sources"]), "initial_fetch_not_upstream")
    snapshots = {s["snapshot_path"]: (root/s["snapshot_path"]).read_bytes() for s in first["sources"]}
    start = len(provider.calls)
    cached = collect(root, provider)
    stage("cache_hit", cached, start)
    need(len(provider.calls) == start and all(s["cache_hit"] for s in cached["sources"]), "cache_reissued_request")
    # Expire only disposable fixture metadata; production caches are untouched.
    for path in [*(root/"cache").glob("*.json"), *(root/"retrievals").glob("*.json")]:
        meta = load(path)
        meta["fetched_at"] = "2000-01-01T00:00:00+00:00"
        write_json(path, meta)
    start = len(provider.calls)
    expired = collect(root, provider)
    stage("expired_cache_fetch", expired, start)
    need(len(provider.calls)-start == 5 and not any(s["cache_hit"] for s in expired["sources"]), "expired_cache_not_refetched")
    need({s["sha256"] for s in first["sources"]} == {s["sha256"] for s in expired["sources"]}, "unchanged_bytes_changed")
    start = len(provider.calls)
    refreshed = collect(root, provider, True)
    stage("forced_refresh_unchanged_bytes", refreshed, start)
    need(len(provider.calls)-start == 5 and all(s["forced_refresh"] for s in refreshed["sources"]), "refresh_not_requested")
    need(not ({s["retrieval_id"] for s in expired["sources"]} & {s["retrieval_id"] for s in refreshed["sources"]}), "refresh_receipts_reused")
    old_dataset = repository.import_bundle(connection, root, refreshed) if connection else None
    old_sql_facts = repository.facts(connection, "AAPL", old_dataset) if connection else None
    old_sql_news = {e["id"]: repository.evidence(connection, old_dataset, e["id"]) for e in refreshed["evidence"] if e["kind"] == "news"} if connection else None
    provider.version = 2
    start = len(provider.calls)
    changed = collect(root, provider, True)
    stage("new_filing_and_news", changed, start)
    need(revenue(refreshed) == "120" and revenue(changed) == "130", "new_filing_not_selected")
    need(sum(e["kind"] == "news" for e in changed["evidence"]) == 2, "new_news_not_selected")
    need(all(e.get("locator", {}).get("accession") != "0000320193-25-000004" for e in changed["evidence"]), "late_filing_leaked")
    need(all(e["source_url"] != "https://example.invalid/future" for e in changed["evidence"]), "future_news_leaked")
    need(all((root/path).read_bytes() == body for path,body in snapshots.items()), "old_source_bytes_overwritten")
    database = {"used": connection is not None}
    if connection:
        current = repository.import_bundle(connection, root, changed)
        before = connection.execute("SELECT count(*) AS n FROM fact_observations").fetchone()["n"]
        need(repository.import_bundle(connection, root, changed) == current, "duplicate_import_changed_dataset")
        need(connection.execute("SELECT count(*) AS n FROM fact_observations").fetchone()["n"] == before, "duplicate_import_added_observations")
        need(repository.facts(connection,"AAPL",old_dataset) == old_sql_facts, "historical_facts_polluted")
        need(all(repository.evidence(connection,old_dataset,eid) == e for eid,e in old_sql_news.items()), "historical_news_polluted")
        vals = {f["value"] for f in repository.facts(connection,"AAPL",current) if f["metric"] == "revenue"}
        need(Decimal(130) in vals and Decimal(9999) not in vals, "updated_SQL_values_wrong")
        need(len(repository.list_datasets(connection)) == 2 and current != old_dataset, "new_dataset_not_retained")
        database.update({"old_dataset":old_dataset,"updated_dataset":current,"fact_observations":before,
                         "duplicate_import_idempotent":True,"old_facts_and_news_unchanged":True})
    failures = []
    for kind in ("financial_fields", "news_http", "news_shape"):
        provider.bad = kind
        try:
            collect(root, provider, True)
        except SourceError as error:
            failures.append({"kind":kind,"status":"rejected","reason":str(error)})
        else:
            raise AssertionError("corrupt_or_failed_source_reported_success")
    return {"status":"passed", "source_mode":"synthetic_only_MockTransport_real_collectors",
            "external_source_requests":0,"external_model_requests":0,"stages":stages,"failures":failures,
            "future_restatement_and_next_Shanghai_day_news_excluded":True,
            "immutable_old_source_bytes_preserved":True,"database":database}


def history_coverage_campaign(root):
    calls = []
    def directory(acc, form, filed, doc):
        return dict(zip(("accessionNumber","form","filingDate","primaryDocument"), ([acc],[form],[filed],[doc])))
    def provider(request):
        calls.append(request.url.path)
        if request.url.host == "data.sec.gov":
            if "submissions-001" in request.url.path:
                return httpx.Response(200,json=directory("0000320193-24-000001","10-K","2024-02-01","old-k.htm"))
            return httpx.Response(200,json={"cik":320193,"filings":{
                "recent":directory("0000320193-25-000002","10-Q","2025-08-01","q.htm"),
                "files":[{"name":"CIK0000320193-submissions-001.json","filingFrom":"2023-01-01","filingTo":"2024-12-31"}]}})
        return httpx.Response(200,content=filing_html("10-Q" if request.url.path.endswith("q.htm") else "10-K"),headers={"content-type":"text/html"})
    with WebSources(root, Budget(),transport=httpx.MockTransport(provider)) as web:
        found,_,gaps = load_filing_evidence(web,"0000320193",CUTOFF,"test@example.invalid","0000320193-24-000001")
        _,_,missing = load_filing_evidence(web,"0000320193",CUTOFF,"test@example.invalid","0000320193-22-000001")
    need(not gaps and any(e["locator"]["accession"] == "0000320193-24-000001" for e in found),"history_page_not_used")
    need("Annual fact accession not found within bounded directory coverage" in missing,"incomplete_coverage_hidden")
    with WebSources(Path(root)/"empty",Budget(),transport=httpx.MockTransport(lambda request:httpx.Response(200,json=[]))) as web:
        rows,_,coverage = load_news(web,"AAPL","0000320193",CUTOFF,"synthetic")
    need(rows == [] and coverage["state"] == "no_results_returned","zero_results_not_explicit")
    return {"status":"passed","historical_directory_requested":any("submissions-001" in p for p in calls),
            "insufficient_coverage_gaps":missing,"zero_news_state":coverage["state"],
            "coverage_limitation":coverage["limitation"],"no_exhaustive_news_claim":True,
            "stale_replay_behavior":"No stale fallback on HTTP/shape errors; immutable old reports remain inspectable, not reclassified as fresh."}


def original_support(report, citation, connection):
    evidence = next(e for e in report["evidence"] if e["id"] == citation["evidence_id"])
    need(citation["quote"] in evidence["text"], "review_quote_not_in_evidence")
    out = {"evidence_id":evidence["id"],"kind":evidence["kind"],"quote":citation["quote"],
           "evidence_text":evidence["text"],"source_url":evidence["source_url"],"published_at":evidence["published_at"],
           "locator":evidence["locator"],"source":evidence["source"]}
    raw_path = ROOT/evidence["source"]["snapshot_path"]
    need(sha(raw_path) == evidence["source"]["sha256"],"review_source_hash_differs")
    if evidence["kind"] == "filing":
        raw = raw_path.read_bytes()
        quote = citation["quote"].encode()
        start = raw.find(quote)
        need(start >= 0,"review_quote_not_literal_in_original_HTML")
        need(evidence["locator"]["accession"].replace("-","") in evidence["source_url"],"review_URL_accession_differs")
        out["original_byte_span"] = [start,start+len(quote)]
        out["original_bytes_utf8"] = raw[start:start+len(quote)].decode()
    elif evidence["kind"] == "news":
        rows=load(raw_path); row=rows[evidence["locator"]["response_index"]]
        need(row["headline"][:400]+"\n"+row.get("summary","")[:1400] == evidence["text"] and row["url"] == evidence["source_url"],"review_news_original_differs")
        out["original_response_row"] = row
        out["source_authority_limit"] = "Provider summary only; no issuer confirmation or full article was fetched."
    elif evidence["kind"] == "calculation":
        calc=next(c for c in repository.calculations(connection,report["dataset_id"]) if c["calculation_id"] == evidence["id"])
        facts={f["fact_id"]:f for f in repository.facts(connection,report["ticker"],report["dataset_id"])}
        out["stored_calculation"] = json.loads(json.dumps(calc,default=str))
        out["input_originals"] = []
        for fid in calc["input_fact_ids"]:
            fact=facts[fid]
            value=pointer(json.loads(raw_path.read_text(),parse_float=Decimal),fact["json_pointer"])
            need(Decimal(str(value["val"])) == fact["value"],"review_calculation_input_differs")
            out["input_originals"].append({"fact_id":fid,"value":str(fact["value"]),"original_json_pointer":fact["json_pointer"],"original_object":json.loads(json.dumps(value,default=str))})
        values=[facts[fid]["value"] for fid in calc["input_fact_ids"]]
        if calc["title"] == "revenue_yoy":
            recomputed=(values[0]-values[1])/values[1]*100
        else:
            need(calc["title"].startswith(("operating_margin:","net_margin:")),"unexpected_review_calculation")
            recomputed=values[0]/values[1]*100
        recomputed=recomputed.quantize(Decimal("0.01"),rounding=ROUND_HALF_EVEN)
        need(recomputed == calc["value"],"independent_calculation_differs")
        out["independently_recomputed_value"] = str(recomputed)
    return out


def compare_annuals(report, connection):
    """Recheck six values/context bindings; this is not a policy audit."""
    facts={f["fact_id"]:f for f in repository.facts(connection,report["ticker"],report["dataset_id"])}
    original=json.loads((ROOT/report["financials"]["source"]["snapshot_path"]).read_text(),parse_float=Decimal)
    parsers={}
    locations={}
    for evidence in report["evidence"]:
        if evidence["kind"] != "filing" or evidence["locator"]["accession"] in parsers:
            continue
        accession=evidence["locator"]["accession"]
        parser=InlineFacts(); parser.feed((ROOT/evidence["source"]["snapshot_path"]).read_text())
        parsers[accession]=parser;locations[accession]=evidence["source"]
    checked=[]
    for annual in report["financials"]["annual_periods"]:
        for metric,fact in annual["facts"].items():
            need(fact is not None,"annual_metric_missing_in_selected_live_reports")
            value=pointer(original,fact["source_json_pointer"])
            need(Decimal(str(value["val"])) == Decimal(fact["value"]) == facts[fact["fact_id"]]["value"],"annual_JSON_SQL_value_differs")
            matches=parsers[fact["accession"]].match(fact,report["cik"])
            need(matches,"annual_inline_context_not_matched")
            checked.append({"metric":metric,"fact_id":fact["fact_id"],"value":fact["value"],"tag":fact["tag"],
                            "period_start":fact["period_start"],"period_end":fact["period_end"],"accession":fact["accession"],
                            "filed_at":fact["filed_at"],"raw_json_pointer":fact["source_json_pointer"],
                            "original_filing":locations[fact["accession"]],"inline_XBRL":matches[0],"SQL_NUMERIC_matched":True})
    need(len(checked) == 6,"two_annuals_three_metrics_required")
    return {"facts":checked,"code_calculations":[json.loads(json.dumps(c,default=str)) for c in repository.calculations(connection,report["dataset_id"])],
            "policy_review":"limited: matched tag, annual duration, USD, consolidated inline-XBRL context and filing version; did not establish complete accounting-policy or corporate-events comparability"}


def byte_excerpt(raw, first, last, *, after=0):
    """A bounded, reproducible slice of saved HTML; never store its full body."""
    start=raw.find(first.encode(),after)
    end=raw.find(last.encode(),start) if start >= 0 else -1
    need(start >= 0 and end >= start,"comparability_original_phrase_missing")
    end+=len(last.encode())
    need(end-start <= 5000,"comparability_excerpt_not_bounded")
    body=raw[start:end]
    rendered=" ".join(unescape(re.sub(r"<[^>]+>","",body.decode())).split())
    return {"raw_byte_span":[start,end],"raw_span_sha256":hashlib.sha256(body).hexdigest(),"text":rendered}


def comparability_review(report):
    annuals=report["financials"]["annual_periods"]
    accession=annuals[0]["facts"]["revenue"]["accession"]
    evidence=next(e for e in report["evidence"] if e["kind"] == "filing" and e["locator"]["accession"] == accession)
    source=evidence["source"]; raw=(ROOT/source["snapshot_path"]).read_bytes()
    need(sha(ROOT/source["snapshot_path"]) == source["sha256"],"comparability_source_bytes_changed")
    parser=InlineFacts();parser.feed(raw.decode())
    candidates=[];bindings=[]
    for annual in annuals:
        for metric,fact in annual["facts"].items():
            need(fact["accession"] == accession,"selected_annual_values_not_same_disclosure")
            locations={}
            for match in parser.match(fact,report["cik"]):
                marker=('id="'+match["html_fact_id"]+'"').encode()
                position=raw.find(marker)
                table_start=raw.rfind(b"<table",0,position)
                table_end=raw.find(b"</table>",position)+len(b"</table>")
                if position >= 0 and 0 <= table_start < position < table_end:
                    locations[(table_start,table_end)]={"html_fact_id":match["html_fact_id"],"fact_id_attribute_byte_span":[position,position+len(marker)],
                                                       "context_ref":match["context_ref"],"unit":match["unit"],"consolidated_context":match["consolidated_context"]}
            candidates.append(set(locations));bindings.append((metric,fact,locations))
    common=set.intersection(*candidates)
    need(len(common) == 1,"six_annual_values_not_found_in_one_comparative_statement")
    table_start,table_end=next(iter(common))
    table_text=" ".join(unescape(re.sub(r"<[^>]+>"," ",raw[table_start:table_end].decode())).split())
    preceding_text=" ".join(unescape(re.sub(r"<[^>]+>"," ",raw[max(0,table_start-5000):table_start].decode())).split())
    statement_header=preceding_text[-450:]+" "+table_text[:400]
    years=[f["period_end"][:4] for f in annuals]
    need(all(year in statement_header for year in years) and "millions" in statement_header,"statement_years_or_units_missing")
    selected=[]
    for metric,fact,locations in bindings:
        match=locations[(table_start,table_end)]
        need(match["consolidated_context"] and match["unit"] == "USD","statement_scope_or_currency_mismatch")
        selected.append({"metric":metric,"period_start":fact["period_start"],"period_end":fact["period_end"],"value_usd":fact["value"],
                         "duration_days":(date.fromisoformat(fact["period_end"])-date.fromisoformat(fact["period_start"])).days+1,**match})
    if report["ticker"] == "AAPL":
        specs=[
            ("consolidation","The consolidated financial statements include the accounts of Apple Inc.","wholly owned subsidiaries."),
            ("preparation","The preparation of these consolidated financial statements","use of management estimates."),
            ("prior_period_reclassification","Certain prior period amounts in the notes","presentation."),
            ("annual_lengths","The Company&#8217;s fiscal years 2025 and 2024 spanned","fiscal year 2023 spanned 53 weeks."),
            ("tax_driver","The Company&#8217;s effective tax rate for 2025 was lower compared to 2024","higher effective tax rate on foreign earnings."),
            ("one_off_tax","the Company recorded a one-time income tax charge of $","billion, net,"),
        ]
        judgment="The two selected annual values are presented together in the 2025 consolidated comparative statement, each for 52 weeks. Note 1 flags reclassifications of prior-period note amounts; this review does not certify that every underlying note/policy or corporate event is unchanged. The disclosed State Aid tax effect materially limits attributing the net-margin increase to core operations: the filing says tax provision decreased $10.7 billion year over year and 2024 included a $10.2 billion net one-time tax charge. Compare as-disclosed GAAP historical values only; no normalized-earnings calculation was performed."
    else:
        specs=[
            ("preparation","Our consolidated financial statements and accompanying notes are prepared in accordance with accounting principles","United States of America."),
            ("consolidation","The consolidated financial statements include the accounts of Microsoft Corporation","Intercompany transactions and balances have been eliminated."),
            ("prior_period_recast","We have recast certain prior period amounts on our consolidated cash flows statements","our consolidated cash flows statements."),
            ("investment_driver","Current year net income and diluted EPS were positively impacted by net gains from investments in OpenAI,","$3.6 billion and $0.49, respectively."),
            ("tax_driver","Our effective tax rate for fiscal years 2026 and 2025 was 19% and 18%, respectively.","between the U.S. and foreign countries."),
        ]
        judgment="The selected 2026 and 2025 years are each July 1 through June 30 (365 days), in one consolidated comparative income statement in USD. Note 1 says the cash-flow presentation recast had no effect on consolidated income statements. Net margin is affected by non-operating investment gains/losses: the filing attributes a $5.0 billion current-year net-income increase and a $3.6 billion prior-year decrease to OpenAI investments; tax rates are 19%/18%. These are as-disclosed historical GAAP comparisons, not evidence that the net-margin change was caused solely by core operating efficiency. No complete policy, transaction or normalized-earnings audit was performed."
    excerpts=[{"topic":topic,**byte_excerpt(raw,first,last,after=raw.find(b"Basis of Presentation")
                                           if report["ticker"] == "AAPL" and topic == "annual_lengths" else 0)}
              for topic,first,last in specs]
    need(all(350 <= item["duration_days"] <= 371 for item in selected),"selected_duration_not_annual")
    return {"run_id":report["run_id"],"source":source,"source_url":evidence["source_url"],"accession":accession,
            "comparative_statement":{"raw_byte_span":[table_start,table_end],"raw_span_sha256":hashlib.sha256(raw[table_start:table_end]).hexdigest(),
                                     "header_text_excerpt":statement_header,"all_six_values_in_same_table":True,"selected_values":selected},
            "policy_and_special_item_excerpts":excerpts,"judgment":"historical_as_disclosed_comparison_supported_with_limits","reason":judgment,
            "review_scope":"Read selected statement, Note 1 preparation/consolidation/reclassification and selected tax/investment discussion in saved full 10-K. No conclusion that all accounting policies or corporate events were unchanged; no independent source authenticity guarantee.",
            "model_visibility":"These additional full-filing paragraphs were read by the assistant for this limited comparability review; they were not added to the frozen model evidence. A model's scoped statement that its selected evidence lacks tax/other-income explanation remains valid.",
            "full_text_copied_into_record":False,"user_independent_acceptance":"not_assessed"}


def review_reports(connection):
    _,msft=saved_report(ROOT,MSFT)
    judgments = [
        ("synthesis",0,"supported_with_stated_limits","17.79% is recalculable from the two annual revenue facts; the saved MD&A says Cloud revenue increased 27%. The claim does not equate Cloud with AI or extrapolate quarterly acceleration."),
        ("synthesis",1,"supported_with_stated_limits","Annual operating margins are 46.78% and 45.62%; the direction is correct. The claim explicitly leaves drivers and persistence unresolved."),
        ("synthesis",2,"supported_with_stated_limits","The original risk section describes continuing investment and profitability risk using may. The claim correctly treats this as forward-looking, not an observed deterioration."),
        ("synthesis",3,"supported_as_provider_summary_not_independently_confirmed","The saved summary says results will be reported October 28, after the report cutoff October 21. The claim preserves its prospective nature; issuer confirmation remains missing."),
    ]
    claims=[]
    for role,index,judgment,reason in judgments:
        claim=msft["analyses"][role]["claims"][index]
        claims.append({"role":role,"claim_index":index,"claim_text":claim["text"],"citations":claim["citations"],
                       "judgment":judgment,"reason":reason,
                       "original_support":[original_support(msft,c,connection) for c in claim["citations"]]})
    aapl_path=ROOT/"m5/semantic-review.json"; aapl=load(aapl_path)
    _,aapl_report=saved_report(ROOT,AAPL)
    for claim in aapl["claims"]:
        need(claim["claim_text"] == aapl_report["analyses"][claim["role"]]["claims"][claim["claim_index"]]["text"],"AAPL_review_stale")
    need(sum(c["judgment"] == "minor_scope_misread_follow_up_M6" for c in aapl["claims"]) == 1,"AAPL_scope_issue_hidden")
    return {"reviewer":"assistant_source_review_not_user_independent_acceptance",
            "MSFT":{"run_id":MSFT,"question":msft["question"],"as_of_date":msft["as_of_date"],"claims":claims,"annual_source_checks":compare_annuals(msft,connection)},
            "AAPL":{"run_id":AAPL,"reused_review":"data/live/m5/semantic-review.json","review_sha256":sha(aapl_path),"claim_count":len(aapl["claims"]),"minor_scope_misread_count":1,"all_claims_certified":False,"annual_source_checks":compare_annuals(aapl_report,connection)},
            "comparability_review":{"AAPL":comparability_review(aapl_report),"MSFT":comparability_review(msft)},
            "scope":"Selected important claims only. Hashes establish byte consistency, not independent source authenticity. The separate comparability review adds bounded statement/policy/special-item context from saved full filings; no full accounting-policy or corporate-events audit.",
            "user_independent_acceptance":"not_assessed"}


def use_records():
    m3=load(ROOT/"m3/acceptance.json"); m5=load(ROOT/"m5/acceptance.json")
    records=[]
    for module,rid in (("M2","8cf4e9e4087044c09f576d7f40f9b386"),("M2",MSFT),("M3","6e5d6cd6990c47e29a9a963949209772"),("M5",AAPL)):
        folder,report=saved_report(ROOT,rid); state=load(folder/"state.json")
        start,finish=state["started_at"],state["updated_at"]
        elapsed=(datetime.fromisoformat(finish)-datetime.fromisoformat(start)).total_seconds()
        if module == "M5":
            elapsed=m5["live"]["elapsed_seconds"]
            live=load(ROOT/"m5/live-acceptance.json")
            start,finish=live["final_state"]["started_at"],live["final_state"]["finished_at"]
        records.append({"module":module,"run_id":rid,"ticker":report["ticker"],"as_of_date":report["as_of_date"],
                        "question":report["question"],"status":report["status"],"model":report["model"],"billing_mode":report["billing_mode"],
                        "started_at":start,"finished_at":finish,"elapsed_seconds":round(elapsed,3),
                        "elapsed_basis":"durable DB first claim through final finish, including crash/recovery" if module == "M5" else "saved pipeline start through final state update; excludes CLI/catalog startup",
                        "model_response_elapsed_seconds":[u.get("elapsed_seconds") for u in report["model_usage"]],
                        "model_attempts":report["model_attempts"],"unknown_attempt_outcomes":sum(u.get("status") in ("unknown","network_unknown") for u in report["model_usage"]),
                        "known_token_usage":{k:sum(u.get("usage",{}).get(k,0) for u in report["model_usage"] if isinstance(u.get("usage"),dict)) for k in ("input_tokens","output_tokens","total_tokens")},
                        "cost":"unknown","source_receipts":[{k:s.get(k) for k in ("retrieval_id","fetched_at","source_url","sha256","forced_refresh","cache_hit","request_attempts")} for s in report["sources"]],
                        "report_json_sha256":sha(folder/"report.json"),"saved_report_verified_now":True})
    need(m3["checks"]["after_real_API_worker_restart"].startswith("completed") and m3["checks"]["real_process_restart_logs"]["API_start_count"] >= 2,"two_sessions_restart_not_recorded")
    return {"live_reports":records,"source_records":[{"path":f"data/live/{m}/acceptance.json","sha256":sha(ROOT/m/"acceptance.json")} for m in ("m2","m3","m5")],
            "two_startup_sessions_and_intermediate_restart":{"prior_record":"data/live/m3/acceptance.json","checks":m3["checks"]["real_process_restart_logs"],"history_after_restart":m3["checks"]["after_real_API_worker_restart"]},
            "later_real_refresh":{"run_id":AAPL,"forced_refresh_sources":m5["live"]["forced_refresh_sources"],"four_real_process_crashes":len(m5["live"]["four_real_process_crashes"])},
            "additional_live_model_or_source_requests_this_campaign":0,"live_records_reused_not_reexecuted":True}


def main():
    target=ROOT/"m6"; target.mkdir(parents=True,exist_ok=True)
    schema="m6_sources_"+uuid4().hex
    dsn="dbname=aeternitas host=/var/run/postgresql"
    with psycopg.connect(dsn,autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with repository.connect(dsn,schema=schema) as connection:
            repository.migrate(connection)
            controlled=controlled_campaign(target/("controlled-sources-"+uuid4().hex),connection)
        with repository.connect(dsn) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            review=review_reports(connection)
        history=history_coverage_campaign(target/("controlled-history-"+uuid4().hex))
        repeated=use_records()
    finally:
        with psycopg.connect(dsn,autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    record={"schema_version":"m6-source-campaign-v1","status":"passed","checked_at":utc_now(),
            "controlled_update_campaign":controlled,"history_and_coverage":history,"semantic_source_review":review,
            "repeated_use":repeated,"isolated_schema_removed":True,"production_DB_mutated":False,
            "remaining_limits":["Annual rather than quarterly/TTM analysis.","No complete accounting-policy audit.","News provider summaries are not exhaustive and are not issuer-confirmed.","Legacy AAPL M1 lacks an explicit news parser_version; historical output is retained rather than rewritten.","Known AAPL M5 role-scope misreading is preserved; M6 evaluation/fix must address it separately.","No independent-user capability or acceptance inferred."]}
    write_json(target/"source-campaign.json",record)
    print("M6 source campaign passed: data/live/m6/source-campaign.json")


if __name__ == "__main__":
    main()
