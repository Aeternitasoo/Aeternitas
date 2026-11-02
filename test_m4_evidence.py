"""Controlled evidence selection tests; no external request or model call."""

from copy import deepcopy
from datetime import date
import hashlib
import random

import pytest

from evidence_selection import ROLES, select_evidence, workflow_fingerprint
from research_inputs import collect_inputs
from research_runtime import Budget
from research_test_support import CONFIG, transports
from sources.sec import SourceError


TICKER, CIK, CUTOFF = "AAPL", "0000320193", "2025-10-01"
QUESTION = "经营增长与风险来自哪里？"


@pytest.fixture
def package(tmp_path):
    transport, calls = transports()
    result = collect_inputs(tmp_path, TICKER, CIK, date.fromisoformat(CUTOFF), QUESTION, CONFIG,
                            refresh=False, news_days=14, budget=Budget(), transports=transport,
                            evidence_kind="controlled_test")
    assert calls["model"] == []
    return result["evidence"]


def select(package, role="fundamentals", **overrides):
    return select_evidence(role, overrides.get("ticker", TICKER), overrides.get("cik", CIK),
                           overrides.get("cutoff", CUTOFF), overrides.get("question", QUESTION), package)


def test_role_scope_selection_has_actual_different_inputs_and_replayable_rank(package):
    picked = {}
    for role in ROLES:
        picked[role], manifest = select(package, role)
        assert manifest["selected_evidence_ids"] == [item["id"] for item in picked[role]]
        assert len(manifest["evidence_fingerprint"]) == 64
        assert manifest["policy"]["search_scope"] == "already_collected_filing_excerpts"
        filings = [row for row in manifest["candidates"] if row["kind"] == "filing"]
        assert [row["rank_in_kind"] for row in filings] == [1, 2, 3, 4]
        assert all(row["reasons"] for row in filings)
        assert all(row["content_sha256"] and row["locator_sha256"] and row["source"]["sha256"] for row in filings)
    assert all(item["kind"] != "news" for item in picked["fundamentals"])
    assert all(item["kind"] == "news" for item in picked["events"])
    assert {item["id"] for item in picked["risk"]} == {item["id"] for item in package}
    assert picked["fundamentals"] != picked["events"]
    # The selector returns copies; callers cannot mutate the audited input in place.
    picked["risk"][0]["text"] = "changed only in selected package"
    assert all(item["text"] != "changed only in selected package" for item in package)


def test_input_permutation_does_not_change_selection_manifest_or_fingerprint(package):
    expected = select(package, "risk")
    random.Random(49).shuffle(package)
    assert select(package, "risk") == expected


def test_keyword_and_role_metadata_ranking_is_explained_and_bounded(package):
    base = next(item for item in package if item["kind"] == "filing")
    financial = [item for item in package if item["kind"] in {"financial_fact", "calculation"}]
    extras = []
    for index in range(7):
        item = deepcopy(base)
        item["id"] = f"F-selection-{index}"
        item["title"] = "Collected filing section"
        item["text"] = "Unrelated collected material." if index < 6 else "Revenue sales growth cash liquidity risk."
        item["locator"]["section"] = "management_discussion" if index == 6 else "risk_factors"
        extras.append(item)
    selected, manifest = select(financial + extras, question="收入和现金增长")
    selected_filings = [item for item in selected if item["kind"] == "filing"]
    assert len(selected_filings) == 4 and selected_filings[0]["id"] == "F-selection-6"
    assert len([item for item in selected if item["kind"] in {"financial_fact", "calculation"}]) == len(financial)
    best = next(row for row in manifest["candidates"] if row["evidence_id"] == "F-selection-6")
    assert "keyword:cash" in best["reasons"] and "role_section:management_discussion" in best["reasons"]
    omitted = [row for row in manifest["candidates"] if row["selection_reason"] == "role_count_budget"]
    assert len(omitted) == 3


def test_summary_association_is_retained_without_claiming_full_article(package):
    item = next(item for item in package if item["kind"] == "news")
    item["locator"]["company_match"] = "provider_query_only"
    selected, manifest = select(package, "events")
    assert selected[0]["text_level"] == "provider_summary"
    assert selected[0]["locator"]["company_match"] == "provider_query_only"
    assert manifest["policy"]["ranking_is_support_validation"] is False


@pytest.mark.parametrize("mutation,match", [
    ("company", "company mismatch"), ("future", "after research cutoff"),
    ("source_company", "source company mismatch"), ("source_hash", "fingerprint"),
    ("receipt", "retrieval identity"), ("source_path", "snapshot path"),
    ("filed", "publication date mismatch"), ("news_symbol", "news source company"),
    ("news_cutoff", "news source company"), ("summary", "summary limitation"),
    ("association", "association level"), ("text", "text byte budget"),
    ("duplicate", "duplicate evidence"), ("unknown_kind", "evidence kind"),
])
def test_bad_evidence_rejected_even_when_outside_role_scope(package, mutation, match):
    fact = next(item for item in package if item["kind"] == "financial_fact")
    news = next(item for item in package if item["kind"] == "news")
    if mutation == "company": fact["company_cik"] = "0000789019"
    elif mutation == "future": fact["published_at"] = "2025-10-02"
    elif mutation == "source_company": fact["source"]["cik"] = "0000789019"
    elif mutation == "source_hash": fact["source"]["sha256"] = "unverifiable"
    elif mutation == "receipt": fact["source"]["retrieval_id"] = "../outside"
    elif mutation == "source_path": fact["source"]["snapshot_path"] = "../../outside"
    elif mutation == "filed": fact["locator"]["filed_at"] = "2025-01-01"
    elif mutation == "news_symbol": news["source"]["query"]["symbol"] = "MSFT"
    elif mutation == "news_cutoff": news["source"]["query"]["to"] = "2025-10-02"
    elif mutation == "summary": news["text_level"] = "full_article"
    elif mutation == "association": news["locator"]["company_match"] = "unverified_full_company_match"
    elif mutation == "text": fact["text"] = "大" * 3000
    elif mutation == "duplicate": package.append(deepcopy(fact))
    elif mutation == "unknown_kind": fact["kind"] = "model_guess"
    with pytest.raises(SourceError, match=match):
        select(package, "events")


def test_cutoff_uses_shanghai_publication_date_and_not_retrieval_time(package):
    news = next(item for item in package if item["kind"] == "news")
    news["published_at"] = "2025-10-01T15:59:59+00:00"
    select(package, "events")
    news["published_at"] = "2025-10-01T16:00:00+00:00"
    with pytest.raises(SourceError, match="after research cutoff"):
        select(package, "events")


def test_refreshing_identical_bytes_changes_receipt_but_not_evidence_version(package):
    selected, original = select(package, "risk")
    changed_receipts = {}
    for item in package:
        # Shared receipts can appear in many evidence rows.
        original_id = item["source"]["retrieval_id"]
        item["source"]["retrieval_id"] = changed_receipts.setdefault(original_id, hashlib.md5(original_id.encode()).hexdigest())
        item["source"]["fetched_at"] = "2026-10-22T00:00:00+00:00"
        item["source"]["cache_hit"] = True
        item["source"]["forced_refresh"] = True
        item["source"]["snapshot_path"] = "snapshots/rearranged-copy.bin"
        if item["kind"] == "filing":
            item["locator"]["normalized_path"] = "extracted/relocated.txt"
    refreshed, new = select(package, "risk")
    assert original["candidates"][0]["retrieval"] != new["candidates"][0]["retrieval"]
    assert original["evidence_fingerprint"] == new["evidence_fingerprint"]
    assert [item["id"] for item in refreshed] == [item["id"] for item in selected]


def test_a_retrieval_identity_cannot_describe_two_source_versions(package):
    copied = deepcopy(package[0])
    copied["id"] += "-conflicting"
    copied["source"]["sha256"] = "b" * 64
    with pytest.raises(SourceError, match="conflicting metadata"):
        select(package + [copied], "risk")


def test_news_limit_uses_actual_timestamp_order_with_offsets(package):
    template = next(item for item in package if item["kind"] == "news")
    news = []
    for index in range(12):
        item = deepcopy(template)
        item["id"] = f"N-bounded-{index:02}"
        item["published_at"] = f"2025-09-30T{index:02}:00:00+00:00"
        news.append(item)
    # This wall-clock hour looks latest, but represents midnight UTC.
    news[0]["published_at"] = "2025-09-30T13:00:00+13:00"
    selected, manifest = select(news, "events")
    assert len(selected) == 10
    assert selected[0]["id"] == "N-bounded-11"
    assert {item["id"] for item in selected} == {f"N-bounded-{index:02}" for index in range(2, 12)}
    assert len([row for row in manifest["candidates"] if row["selection_reason"] == "role_count_budget"]) == 2


@pytest.mark.parametrize("mutation", ["text", "source_bytes", "locator", "published", "question", "company"])
def test_semantic_change_changes_evidence_fingerprint(package, mutation):
    _, before = select(package, "risk")
    fact = next(item for item in package if item["kind"] == "financial_fact")
    options = {}
    if mutation == "text": fact["text"] += " Source correction."
    elif mutation == "source_bytes": fact["source"]["sha256"] = hashlib.sha256(b"different source bytes").hexdigest()
    elif mutation == "locator": fact["locator"]["source_json_pointer"] += "-other"
    elif mutation == "published":
        fact["published_at"] = fact["locator"]["filed_at"] = "2025-02-02"
    elif mutation == "question": options["question"] = QUESTION + "现金呢？"
    else:
        for item in package:
            item["company_cik"] = "0000789019"
            if item["kind"] in {"financial_fact", "calculation"}:
                item["source"]["cik"] = "0000789019"
                item["source"]["source_url"] = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"
                item["source_url"] = item["source"]["source_url"]
            elif item["kind"] == "filing":
                item["source"]["source_url"] = item["source"]["source_url"].replace("/320193/", "/789019/")
                item["source_url"] = item["source"]["source_url"]
        options["cik"] = "0000789019"
    _, after = select(package, "risk", **options)
    assert before["evidence_fingerprint"] != after["evidence_fingerprint"]


def workflow_options(package):
    return dict(ticker=TICKER, cik=CIK, cutoff=CUTOFF, question=QUESTION,
                model="controlled-model", billing="controlled_test", workflow_version="workflow-test-v1",
                result_schema_version="schema-test-v1", prompt_versions={role: f"{role}-test-v1" for role in ROLES},
                role_manifests={role: select(package, role)[1] for role in ROLES})


def test_workflow_fingerprint_binds_all_versions_and_detects_manifest_tampering(package):
    options = workflow_options(package)
    expected = workflow_fingerprint(**options)
    assert workflow_fingerprint(**deepcopy(options)) == expected
    for key in ("model", "billing", "workflow_version", "result_schema_version"):
        changed = {**options, key: options[key] + "-changed"}
        assert workflow_fingerprint(**changed) != expected
    changed = deepcopy(options)
    changed["prompt_versions"]["risk"] += "-changed"
    assert workflow_fingerprint(**changed) != expected
    changed = deepcopy(options)
    changed["role_manifests"]["risk"]["selected_evidence_ids"].reverse()
    with pytest.raises(SourceError, match="manifest mismatch"):
        workflow_fingerprint(**changed)
    changed = deepcopy(options)
    del changed["role_manifests"]["events"]
    with pytest.raises(SourceError, match="all role manifests"):
        workflow_fingerprint(**changed)


def test_valid_empty_news_is_an_explicit_empty_role_package(package):
    news_free = [item for item in package if item["kind"] != "news"]
    selected, manifest = select(news_free, "events")
    assert selected == [] and manifest["selected_count"] == 0
    assert manifest["candidate_count"] == len(news_free)
    assert manifest["selected_evidence_ids"] == []
