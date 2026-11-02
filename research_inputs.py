"""Shared bounded source collection for research and M2 data ingestion."""

from filing_source import load_filing_evidence
from financial_facts import build_financial_summary
from news_source import load_news
from research_analysis import financial_evidence
from research_runtime import WebSources
from sources.sec import SecClient, SourceError


def collect_inputs(root, ticker, cik, cutoff, question, config, *, refresh, news_days, budget,
                   transports=None, progress=lambda step: None, evidence_kind="live_api"):
    transports = transports or {}
    progress("financials")
    with SecClient(root, config["SEC_CONTACT"], transport=transports.get("sec"),
                   elapsed_budget_seconds=min(60, budget.remaining())) as sec:
        snapshot = sec.companyfacts(cik, refresh=refresh)
    budget.remaining()
    summary = build_financial_summary(raw=snapshot.data, cik=cik, as_of_date=cutoff.isoformat(), source=snapshot.reference())
    progress("filing_text")
    latest = summary["annual_periods"][0]["facts"]["revenue"] if summary["annual_periods"] else None
    if latest is None:
        raise SourceError("No annual revenue data before cutoff; cannot issue a normal research report")
    with WebSources(root, budget, transport=transports.get("web")) as web:
        filings, directories, gaps = load_filing_evidence(
            web, cik, cutoff, config["SEC_CONTACT"], latest["accession"], refresh,
        )
        progress("company_news")
        news, news_receipt, coverage = load_news(web, ticker, cik, cutoff, config["FINNHUB_API_KEY"],
                                                  days=news_days, refresh=refresh)
    evidence = financial_evidence(summary) + filings + news
    if len({e["id"] for e in evidence}) != len(evidence):
        raise SourceError("Duplicate evidence IDs require review")
    receipts = {s["retrieval_id"]: s for s in [snapshot.reference(), *directories, news_receipt,
                                                     *(e["source"] for e in filings)]}
    inputs = {"ticker": ticker, "cik": cik, "as_of_date": cutoff.isoformat(), "question": question,
              "financials": summary, "evidence": evidence, "sources": list(receipts.values()),
              "news_coverage": coverage, "source_gaps": gaps, "evidence_kind": evidence_kind}
    return inputs
