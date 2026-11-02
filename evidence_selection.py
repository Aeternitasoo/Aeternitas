"""Deterministic selection over an already collected, audited evidence bundle.

This module has no filesystem, network or model access. ``audit_inputs`` remains
responsible for comparing receipts and hashes with original bytes. Here we bind
the selected package to validated company/date/source metadata and rank only the
filing excerpts that the existing collector supplied; this is not full-text SEC
search. Retrieval time/cache/path changes do not create a new evidence version.
"""

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from models import normalize_cik
from sources.sec import SourceError


SELECTION_VERSION = "keyword-metadata-selection-m4-v1"
ROLES = ("fundamentals", "events", "risk", "synthesis")
KINDS = ("financial_fact", "calculation", "filing", "news")
MAX_CANDIDATES = 64
MAX_FILING_EXCERPTS = 4
MAX_NEWS = 10
MAX_SELECTED_TEXT_BYTES = 45000
_SHA = re.compile(r"[0-9a-f]{64}")
_TOPICS = (
    (("revenue", "sales", "收入", "营收", "销售"), ("revenue", "sales")),
    (("profit", "margin", "盈利", "利润", "毛利"), ("profit", "margin", "income")),
    (("risk", "风险", "不确定"), ("risk", "uncertainty", "competition")),
    (("growth", "增长", "成长"), ("growth", "increase", "demand")),
    (("cash", "现金", "流动性"), ("cash", "liquidity")),
    (("fundamental", "基本面", "经营"), ("operating", "business", "management")),
)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _date(value, label):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise SourceError(f"Evidence selection: invalid {label}")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise SourceError(f"Evidence selection: invalid {label}") from None


def _identity(ticker, cik, cutoff, question):
    if not isinstance(ticker, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,19}", ticker):
        raise SourceError("Evidence selection: invalid ticker")
    try:
        cik = normalize_cik(cik)
    except ValueError:
        raise SourceError("Evidence selection: invalid company CIK") from None
    cutoff = _date(cutoff, "cutoff")
    if not isinstance(question, str) or not question.strip() or len(question) > 1200:
        raise SourceError("Evidence selection: invalid question")
    return ticker, cik, cutoff, question


def query_tokens(question):
    """Small transparent lexical baseline, including explicit bilingual aliases."""
    lowered = question.casefold()
    tokens = set(re.findall(r"[a-z][a-z0-9_-]{1,39}", lowered))
    for span in re.findall(r"[\u3400-\u9fff]+", lowered):
        tokens.update(span[i:i + 2] for i in range(len(span) - 1))
    for triggers, expansion in _TOPICS:
        if any(trigger in lowered for trigger in triggers):
            tokens.update(expansion)
    return sorted(tokens)


def _publication(item, cutoff):
    published = item.get("published_at")
    if item["kind"] == "news":
        if not isinstance(published, str):
            raise SourceError("Evidence selection: invalid publication timestamp")
        try:
            stamp = datetime.fromisoformat(published)
            if stamp.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise SourceError("Evidence selection: publication timestamp requires timezone") from None
        day = stamp.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if item.get("time_precision") != "second":
            raise SourceError("Evidence selection: invalid news time precision")
    else:
        day = _date(published, "publication date")
        if item.get("time_precision") != "date":
            raise SourceError("Evidence selection: invalid publication precision")
    if day > cutoff:
        raise SourceError("Evidence selection: publication is after research cutoff")


def _source(item, ticker, cik, cutoff):
    source = item.get("source")
    if not isinstance(source, dict):
        raise SourceError("Evidence selection: missing source receipt")
    if not isinstance(source.get("retrieval_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", source["retrieval_id"]):
        raise SourceError("Evidence selection: invalid source retrieval identity")
    if not isinstance(source.get("sha256"), str) or not _SHA.fullmatch(source["sha256"]):
        raise SourceError("Evidence selection: invalid source fingerprint")
    if type(source.get("size_bytes")) is not int or source["size_bytes"] <= 0:
        raise SourceError("Evidence selection: invalid source byte size")
    source_url = source.get("source_url")
    if not isinstance(source_url, str) or len(source_url) > 2048:
        raise SourceError("Evidence selection: invalid source URL")
    parts = urlsplit(source_url)
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment:
        raise SourceError("Evidence selection: invalid source URL")
    try:
        fetched = datetime.fromisoformat(source.get("fetched_at", ""))
        if fetched.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        raise SourceError("Evidence selection: invalid retrieval timestamp") from None
    path = source.get("snapshot_path")
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")) or "\\" in path or ".." in path.split("/") or ":" in path:
        raise SourceError("Evidence selection: invalid snapshot path")
    kind, locator = item["kind"], item["locator"]
    if kind in {"financial_fact", "calculation"}:
        expected = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        if source_url != expected or item.get("source_url") != expected or source.get("cik") != cik:
            raise SourceError("Evidence selection: financial source company mismatch")
        if kind == "financial_fact" and locator.get("filed_at") != item["published_at"]:
            raise SourceError("Evidence selection: financial publication date mismatch")
        if kind == "calculation" and item.get("derived_as_of") != cutoff:
            raise SourceError("Evidence selection: calculation cutoff mismatch")
    elif kind == "filing":
        accession, document = locator.get("accession"), locator.get("document")
        if (not isinstance(accession, str) or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession)
                or not isinstance(document, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.html?", document)):
            raise SourceError("Evidence selection: invalid filing locator")
        expected = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"
        if source_url != expected or item.get("source_url") != expected:
            raise SourceError("Evidence selection: filing source company / accession mismatch")
        if locator.get("filed_at") != item["published_at"]:
            raise SourceError("Evidence selection: filing publication date mismatch")
        if locator.get("section") not in {"risk_factors", "management_discussion"}:
            raise SourceError("Evidence selection: unsupported filing section")
    else:
        query = source.get("query")
        if (source_url != "https://finnhub.io/api/v1/company-news" or not isinstance(query, dict)
                or query.get("symbol") != ticker or query.get("to") != cutoff):
            raise SourceError("Evidence selection: news source company / cutoff mismatch")
        if _date(query.get("from"), "news query start") > cutoff:
            raise SourceError("Evidence selection: invalid news query window")
        if item.get("text_level") != "provider_summary":
            raise SourceError("Evidence selection: news must retain provider summary limitation")
        if locator.get("company_match") not in {"related_field", "provider_query_only"}:
            raise SourceError("Evidence selection: news must retain company association level")
        article_url = item.get("source_url")
        if not isinstance(article_url, str) or len(article_url) > 2048:
            raise SourceError("Evidence selection: invalid news article URL")
        article = urlsplit(article_url)
        if article.scheme not in {"http", "https"} or not article.hostname or article.username or article.password:
            raise SourceError("Evidence selection: invalid news article URL")
    # Keep receipt diagnostics for inspection but exclude them from version identity.
    receipt = {key: deepcopy(source[key]) for key in (
        "retrieval_id", "fetched_at", "snapshot_path", "cache_hit", "request_attempts", "forced_refresh"
    ) if key in source}
    stable = {key: deepcopy(source[key]) for key in (
        "source_url", "sha256", "size_bytes", "cik", "query", "schema_version", "data_mode"
    ) if key in source}
    return stable, receipt


def _validate(item, ticker, cik, cutoff):
    if not isinstance(item, dict) or item.get("kind") not in KINDS:
        raise SourceError("Evidence selection: invalid evidence kind")
    if item.get("company_cik") != cik:
        raise SourceError("Evidence selection: evidence company mismatch")
    if not isinstance(item.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", item["id"]):
        raise SourceError("Evidence selection: invalid evidence identity")
    if not isinstance(item.get("text"), str) or not item["text"].strip() or len(item["text"].encode("utf-8")) > 8000:
        raise SourceError("Evidence selection: evidence text byte budget exceeded")
    if not isinstance(item.get("title"), str) or len(item["title"].encode("utf-8")) > 1600:
        raise SourceError("Evidence selection: evidence title byte budget exceeded")
    if not isinstance(item.get("locator"), dict):
        raise SourceError("Evidence selection: missing evidence locator")
    try:
        if len(_encoded(item["locator"])) > 16000:
            raise SourceError("Evidence selection: locator byte budget exceeded")
    except (TypeError, ValueError):
        raise SourceError("Evidence selection: locator must be finite JSON metadata") from None
    _publication(item, cutoff)
    stable_source, receipt = _source(item, ticker, cik, cutoff)
    stable_locator = {key: deepcopy(value) for key, value in item["locator"].items()
                      if key != "normalized_path"}
    identity = {key: deepcopy(item[key]) for key in (
        "id", "kind", "company_cik", "title", "text", "source_url", "published_at",
        "time_precision", "derived_as_of", "text_level"
    ) if key in item}
    identity.update(source=stable_source, locator=stable_locator)
    return identity, receipt


def _rank(item, role, tokens):
    if item["kind"] != "filing":
        return 0, ["all_verified_financial_evidence" if item["kind"] in {"financial_fact", "calculation"}
                   else "provider_summary_latest_first"]
    haystack = (item["title"] + "\n" + item["text"]).casefold()
    matches = [token for token in tokens if token in haystack]
    score, reasons = len(matches) * 2, [f"keyword:{token}" for token in matches]
    section = item["locator"]["section"]
    preferred = {"fundamentals": "management_discussion", "risk": "risk_factors"}.get(role)
    if section == preferred:
        score += 3
        reasons.append(f"role_section:{section}")
    else:
        reasons.append(f"collected_section:{section}")
    return score, reasons


def _published_order(row):
    if row["kind"] == "news":
        return datetime.fromisoformat(row["published_at"]).astimezone(timezone.utc).isoformat()
    return row["published_at"]


def select_evidence(role, ticker, cik, cutoff, question, evidence):
    """Return fresh evidence copies and an explainable, replayable role manifest.

    Bad candidates are rejected even when a role would not select their kind.
    Ranking never expands source coverage and does not score factual support.
    """
    if role not in ROLES:
        raise SourceError("Evidence selection: unknown analysis role")
    ticker, cik, cutoff, question = _identity(ticker, cik, cutoff, question)
    if not isinstance(evidence, list) or len(evidence) > MAX_CANDIDATES:
        raise SourceError("Evidence selection: candidate count budget exceeded")
    tokens, rows, by_id, receipts = query_tokens(question), [], {}, {}
    for item in evidence:
        identity, receipt = _validate(item, ticker, cik, cutoff)
        eid = item["id"]
        if eid in by_id:
            raise SourceError("Evidence selection: duplicate evidence identity")
        rid = receipt["retrieval_id"]
        receipt_identity = _encoded({"source": identity["source"], "retrieval": receipt})
        if rid in receipts and receipts[rid] != receipt_identity:
            raise SourceError("Evidence selection: conflicting metadata for source retrieval identity")
        receipts[rid] = receipt_identity
        by_id[eid] = item
        score, reasons = _rank(item, role, tokens)
        rows.append({"evidence_id": eid, "kind": item["kind"], "published_at": item["published_at"],
                     "identity": identity, "identity_sha256": _digest(identity),
                     "content_sha256": hashlib.sha256(item["text"].encode("utf-8")).hexdigest(),
                     "locator_sha256": _digest(identity["locator"]),
                     "locator": identity["locator"],
                     "source": identity["source"], "retrieval": receipt,
                     "score": score, "reasons": reasons})
    if sum(len(item["text"].encode("utf-8")) for item in evidence) > 256000:
        raise SourceError("Evidence selection: candidate text byte budget exceeded")
    # Stable sorts make ties independent of collector/input order. Newer filings
    # win equal keyword/section scores; finance facts retain a transparent ID tie.
    rows.sort(key=lambda row: row["evidence_id"])
    rows.sort(key=_published_order, reverse=True)
    rows.sort(key=lambda row: row["score"], reverse=True)
    rows.sort(key=lambda row: KINDS.index(row["kind"]))
    counts = {kind: 0 for kind in KINDS}
    chosen = []
    eligible = ({"financial_fact", "calculation", "filing"} if role == "fundamentals"
                else {"news"} if role == "events" else set(KINDS))
    for row in rows:
        kind = row["kind"]
        counts[kind] += 1
        row["rank_in_kind"] = counts[kind]
        limit = MAX_FILING_EXCERPTS if kind == "filing" else MAX_NEWS if kind == "news" else MAX_CANDIDATES
        row["selected"] = kind in eligible and counts[kind] <= limit
        row["selection_reason"] = ("selected" if row["selected"] else
                                   "outside_role_scope" if kind not in eligible else "role_count_budget")
        if row["selected"]:
            chosen.append(deepcopy(by_id[row["evidence_id"]]))
    if sum(len(item["text"].encode("utf-8")) for item in chosen) > MAX_SELECTED_TEXT_BYTES:
        raise SourceError("Evidence selection: selected text byte budget exceeded")
    # Keep source text in the actual package, not redundantly in the manifest.
    stable_rows = []
    for row in rows:
        row.pop("identity")
        stable_rows.append({key: value for key, value in row.items() if key != "retrieval"})
    manifest = {"selection_version": SELECTION_VERSION, "role": role, "ticker": ticker,
                "cik": cik, "as_of_date": cutoff, "question": question, "query_tokens": tokens,
                "policy": {"search_scope": "already_collected_filing_excerpts",
                           "max_filing_excerpts": MAX_FILING_EXCERPTS, "max_news_summaries": MAX_NEWS,
                           "max_selected_text_bytes": MAX_SELECTED_TEXT_BYTES,
                           "ranking_is_support_validation": False},
                "candidate_count": len(rows), "selected_count": len(chosen),
                "selected_evidence_ids": [item["id"] for item in chosen], "candidates": rows}
    manifest["evidence_fingerprint"] = _digest({**manifest, "candidates": stable_rows})
    return chosen, manifest


def workflow_fingerprint(*, ticker, cik, cutoff, question, model, billing,
                         prompt_versions, result_schema_version, role_manifests,
                         workflow_version):
    """Bind deterministic inputs/versions; model prose itself is not deterministic."""
    ticker, cik, cutoff, question = _identity(ticker, cik, cutoff, question)
    if not isinstance(role_manifests, dict) or set(role_manifests) != set(ROLES):
        raise SourceError("Workflow fingerprint requires all role manifests")
    if not isinstance(prompt_versions, dict) or set(prompt_versions) != set(ROLES):
        raise SourceError("Workflow fingerprint requires all role prompt versions")
    for value in (model, billing, result_schema_version, workflow_version, *prompt_versions.values()):
        if not isinstance(value, str) or not value or len(value) > 200:
            raise SourceError("Workflow fingerprint requires explicit bounded versions")
    fingerprints = {}
    for role in ROLES:
        manifest = role_manifests[role]
        if (not isinstance(manifest, dict) or manifest.get("role") != role or manifest.get("ticker") != ticker
                or manifest.get("cik") != cik or manifest.get("as_of_date") != cutoff or manifest.get("question") != question):
            raise SourceError("Workflow fingerprint role identity mismatch")
        expected = manifest.get("evidence_fingerprint")
        if not isinstance(expected, str) or not _SHA.fullmatch(expected):
            raise SourceError("Workflow fingerprint missing evidence version")
        stable = {key: deepcopy(value) for key, value in manifest.items() if key != "evidence_fingerprint"}
        candidates = stable.get("candidates")
        if (not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES
                or any(not isinstance(row, dict) for row in candidates)):
            raise SourceError("Workflow fingerprint invalid evidence manifest")
        stable["candidates"] = [{key: value for key, value in row.items() if key != "retrieval"}
                                for row in candidates]
        try:
            digest = _digest(stable)
        except (TypeError, ValueError):
            raise SourceError("Workflow fingerprint invalid evidence manifest") from None
        if digest != expected:
            raise SourceError("Workflow fingerprint evidence manifest mismatch")
        fingerprints[role] = expected
    return _digest({"ticker": ticker, "cik": cik, "as_of_date": cutoff, "question": question,
                    "model": model, "billing": billing, "prompt_versions": prompt_versions,
                    "result_schema_version": result_schema_version,
                    "workflow_version": workflow_version, "role_evidence_fingerprints": fingerprints})
