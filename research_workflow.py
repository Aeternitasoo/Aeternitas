"""Bounded multi-company research, composed from the existing research modules.

No import-time I/O, database, market-data requirement or automatic model retry.
The price-prediction Agent and the original four-role pipeline stay separate.
"""

from copy import deepcopy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from evidence_repository import digest
from evidence_selection import select_evidence
from research_analysis import ResponsesAnalyst, validate_result
from research_inputs import collect_inputs
from research_observability import Recorder, error_code
from research_pipeline import audit_inputs, _local
from research_report import render_report
from research_runtime import Budget, utc_now
from sources.sec import SourceError


VERSION = "research-workflow-v1"
PROMPT_VERSION = "industry-research-v1"
CLASSIFICATIONS = ("fact", "inference", "assumption", "unknown")
INSTRUCTION = """你是Aeternitas投资研究员。只使用提供的证据，不调用其他工具，不补充记忆中的外部事实。
按输入sections的ID和顺序回答研究问题。每节输出一至四项论点，分类为fact、inference、assumption或unknown。
事实须有原文依据；推断明确说明传导机制而非已经证实的因果；假设写出待验证前提；缺少证据时写unknown。
fact和inference须引用一至三条真实证据ID及逐字摘录。每项论点同一ID只引用一次，摘录十二至三百字符。
正文不要使用数字、金额或中文数量计算，精确数字保留在原始引文和程序财务表。
不要把标题或供应商摘要当作已独立核实的事实，不把公司未来指引写成已经实现的结果。
核对公司与合并范围、财务期间、单位和现金/非现金投入。不同公司不同期间不能直接相加或作客户归因。
区分事实、推断、假设和未知，不强行看涨看跌，不把行业增长等同股票收益。只输出约定JSON。"""
LIMITATIONS = [
    "Single bounded synthesis call, not four independent analyst calls or a queued company-research job.",
    "Evidence selection searches bounded collected filings; it is not comprehensive sector retrieval or RAG.",
    "News is provider summaries, not independently verified article full text.",
    "Exact citations and source hashes do not prove semantic entailment or investment performance.",
    "Date-level research cutoff is not A0/Time Machine strict historical admission.",
]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    """Exclusive creation within a newly reserved run; never overwrite an artifact."""
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False) + "\n").encode()
    with Path(path).open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def config_checked(value):
    required = {"question", "subjects", "as_of_date", "model", "sections", "evidence_kind"}
    optional = {"filing_terms", "news_days", "seconds"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise ValueError("Invalid research configuration fields")
    c = deepcopy(value)
    if not isinstance(c["question"], str) or not 1 <= len(c["question"].strip()) <= 1200:
        raise ValueError("Invalid research question")
    if not isinstance(c["as_of_date"], str) or date.fromisoformat(c["as_of_date"]).isoformat() != c["as_of_date"]:
        raise ValueError("Invalid research date")
    if not isinstance(c["model"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", c["model"]):
        raise ValueError("Invalid configured model")
    if c["evidence_kind"] not in {"live_api", "frozen_live_evidence", "controlled_test"}:
        raise ValueError("Unknown evidence mode")
    if not isinstance(c["subjects"], list) or not 1 <= len(c["subjects"]) <= 6:
        raise ValueError("One to six subjects required")
    symbols, ciks = set(), set()
    for subject in c["subjects"]:
        if (not isinstance(subject, dict) or set(subject) - {
                "symbol", "cik", "report_entity", "accepted_fact_names", "identity_note"}
                or not {"symbol", "cik", "report_entity"} <= set(subject)):
            raise ValueError("Invalid subject fields")
        if not isinstance(subject["symbol"], str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,19}", subject["symbol"]):
            raise ValueError("Invalid subject symbol")
        if not isinstance(subject["cik"], str) or not re.fullmatch(r"[0-9]{10}", subject["cik"]):
            raise ValueError("CIK must be a normalized ten-digit identifier")
        if subject["symbol"] in symbols or subject["cik"] in ciks:
            raise ValueError("Duplicate subject identity")
        symbols.add(subject["symbol"]); ciks.add(subject["cik"])
        if not isinstance(subject["report_entity"], str) or not 1 <= len(subject["report_entity"].strip()) <= 300:
            raise ValueError("Report entity required")
        aliases = subject.get("accepted_fact_names", [])
        if (not isinstance(aliases, list) or len(aliases) > 4
                or any(not isinstance(n, str) or not 1 <= len(n) <= 300 for n in aliases)):
            raise ValueError("Invalid fact entity aliases")
        if aliases and (not isinstance(subject.get("identity_note"), str)
                        or not 1 <= len(subject["identity_note"].strip()) <= 2000):
            raise ValueError("Entity aliases require an explicit review note")
        if "identity_note" in subject and (not isinstance(subject["identity_note"], str)
                                          or not 1 <= len(subject["identity_note"].strip()) <= 2000):
            raise ValueError("Invalid identity review note")
    if not isinstance(c["sections"], list) or not 1 <= len(c["sections"]) <= 8:
        raise ValueError("One to eight research sections required")
    ids = []
    for section in c["sections"]:
        if (not isinstance(section, dict) or set(section) != {"id", "title"}
                or not isinstance(section["id"], str) or not re.fullmatch(r"[a-z_]{1,40}", section["id"])
                or not isinstance(section["title"], str) or not 1 <= len(section["title"].strip()) <= 100):
            raise ValueError("Invalid research section")
        ids.append(section["id"])
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate research section")
    c.setdefault("news_days", 14); c.setdefault("seconds", 300); c.setdefault("filing_terms", [])
    if type(c["news_days"]) is not int or not 1 <= c["news_days"] <= 30:
        raise ValueError("Invalid news window")
    if type(c["seconds"]) is not int or not 1 <= c["seconds"] <= 1800:
        raise ValueError("Invalid deadline")
    if (not isinstance(c["filing_terms"], list) or len(c["filing_terms"]) > 8
            or any(not isinstance(t, str) or not 3 <= len(t.strip()) <= 80 for t in c["filing_terms"])):
        raise ValueError("Invalid filing search terms")
    return c


class FrozenInputs:
    """Read existing company bundles and receipts without changing their directory.

    Hashes must be provided by the caller, not invented on load. Original
    identity/date/question remain intact; a new question belongs to this run.
    """
    def __init__(self, source_root, bundles):
        self.source_root = Path(source_root)
        self.bundles = deepcopy(bundles)

    def __call__(self, root, ticker, cik, cutoff, question, config, **kwargs):
        item = self.bundles[ticker]
        path = Path(item["path"])
        if sha(path) != item["sha256"]:
            raise SourceError("Frozen input bundle fingerprint mismatch")
        bundle = json.loads(path.read_text(encoding="utf-8"))
        if bundle["ticker"] != ticker or bundle["cik"] != cik or bundle["as_of_date"] != cutoff.isoformat():
            raise SourceError("Frozen bundle identity/date mismatch")
        coverage = bundle["news_coverage"]
        days = (date.fromisoformat(coverage["requested_end"]) - date.fromisoformat(coverage["requested_start"])).days + 1
        if days != kwargs["news_days"]:
            raise SourceError("Frozen bundle news window differs from configuration")
        kwargs["budget"].remaining()
        return bundle


def organize(root, bundle, subject, config):
    """Audit first, rank existing evidence, then bounded search of saved full text."""
    audit_inputs(root, bundle)
    if (bundle["ticker"] != subject["symbol"] or bundle["cik"] != subject["cik"]
            or bundle["as_of_date"] != config["as_of_date"]):
        raise SourceError("Collected bundle identity/date mismatch")
    fact_name = bundle["financials"]["company_name"]
    names = [subject["report_entity"], *subject.get("accepted_fact_names", [])]
    if fact_name not in names:
        raise SourceError("Financial entity name requires explicit scope review")
    evidence, manifest = select_evidence("synthesis", subject["symbol"], subject["cik"],
                                         config["as_of_date"], config["question"], bundle["evidence"])
    # Search only full text already reconstructed and hash-checked by audit_inputs.
    extras, searched, matched = [], set(), set()
    for original in bundle["evidence"]:
        if original["kind"] != "filing":
            continue
        loc = original["locator"]
        if loc["normalized_path"] in searched:
            continue
        searched.add(loc["normalized_path"])
        full = _local(root, loc["normalized_path"]).read_text(encoding="utf-8")
        if hashlib.sha256(full.encode()).hexdigest() != loc["normalized_sha256"]:
            raise SourceError("Filing content changed during organization")
        for term in config["filing_terms"]:
            match = re.search(re.escape(term), full, re.IGNORECASE)
            if match is None:
                continue
            pos = match.start()
            matched.add(term)
            if len(extras) >= 4:
                continue
            start, end = max(0, pos - 180), min(len(full), pos + 1000)
            item = deepcopy(original)
            item.update(id="T-" + digest([subject["cik"], original["source"]["sha256"], start, end])[:24],
                        title="Saved filing term: " + term, text=full[start:end])
            item["locator"].update(section="topic_search", search_term=term,
                                   normalized_start=start, normalized_end=end,
                                   selection_version=VERSION)
            if item["id"] not in {e["id"] for e in extras}:
                extras.append(item)
    manifest["topic_search"] = {"terms": config["filing_terms"], "matched_terms": sorted(matched),
                                "selected_ids": [e["id"] for e in extras], "max_extra_excerpts": 4,
                                "scope": "saved_filings_only_first_occurrence_not_semantic_retrieval"}
    result = [{**e, "id": subject["symbol"] + ":" + e["id"],
               "original_evidence_id": e["id"], "symbol": subject["symbol"]} for e in evidence + extras]
    if not result:
        raise SourceError("No usable research evidence")
    manifest["organized_fingerprint"] = digest(result)
    return result, manifest


def schema_for(config):
    cite = {"type": "object", "additionalProperties": False,
            "properties": {"evidence_id": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["evidence_id", "quote"]}
    claim = {"type": "object", "additionalProperties": False,
             "properties": {"classification": {"type": "string", "enum": list(CLASSIFICATIONS)},
                            "text": {"type": "string"}, "citations": {"type": "array", "items": cite}},
             "required": ["classification", "text", "citations"]}
    section = {"type": "object", "additionalProperties": False,
               "properties": {"topic": {"type": "string", "enum": [s["id"] for s in config["sections"]]},
                              "claims": {"type": "array", "items": claim}},
               "required": ["topic", "claims"]}
    return {"type": "object", "additionalProperties": False,
            "properties": {"sections": {"type": "array", "items": section}}, "required": ["sections"]}


def validate_output(result, config, evidence):
    if not isinstance(result, dict) or set(result) != {"sections"} or not isinstance(result["sections"], list):
        raise ValueError("Invalid structured research output")
    if [s.get("topic") if isinstance(s, dict) else None for s in result["sections"]] != [s["id"] for s in config["sections"]]:
        raise ValueError("Research sections differ from configuration")
    for section in result["sections"]:
        if set(section) != {"topic", "claims"} or not isinstance(section["claims"], list) or not 1 <= len(section["claims"]) <= 4:
            raise ValueError("Invalid research claim list")
        for claim in section["claims"]:
            if (not isinstance(claim, dict) or set(claim) != {"classification", "text", "citations"}
                    or claim["classification"] not in CLASSIFICATIONS or not isinstance(claim["citations"], list)):
                raise ValueError("Invalid classified claim")
            if claim["citations"] or claim["classification"] in {"fact", "inference"}:
                validate_result({"claims": [{"text": claim["text"], "stance": "neutral",
                                            "citations": claim["citations"]}], "gaps": []}, evidence)
            else:
                validate_result({"claims": [], "gaps": [claim["text"]]}, evidence)
    return result


def model_evidence(evidence):
    return [{**{k: e[k] for k in ("id", "symbol", "kind", "title", "published_at", "text")},
             "source_version": e["source"]["sha256"], "locator_fingerprint": digest(e["locator"])}
            for e in evidence]


def run_workflow(config, output_root, *, provider_config=None, collector=None,
                 client_factory=None, transports=None, sink=None, run_id=None, profile=None,
                 quality_policy=None, comparison_context=None, observability_context=None):
    """Return a newly published directory. Failures keep state/events, never a commit.

    Live execution uses existing provider config and ResponsesAnalyst. Offline
    callers supply FrozenInputs and/or MockTransport; no implicit mode switch.
    """
    config = config_checked(config)
    if profile is not None:
        from research_contract_profile import checked
        profile = checked(profile)
    provider_config, transports = provider_config or {}, transports or {}
    collector = collector or collect_inputs
    if isinstance(collector, FrozenInputs) != (config["evidence_kind"] == "frozen_live_evidence"):
        raise ValueError("Frozen/live source mode must be explicit and consistent")
    run_id = run_id or uuid4().hex
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid workflow run ID")
    folder = Path(output_root) / run_id
    folder.mkdir(parents=True, exist_ok=False)
    recorder = Recorder(folder / "events.jsonl", run_id, sink, context=observability_context)
    budget = Budget(seconds=config["seconds"], max_calls=1,
                    on_call=lambda role, count: save(folder / "model-attempt.json", {
                        "model": config["model"], "count": count, "role": role, "started_at": utc_now()}))
    source_root = collector.source_root if isinstance(collector, FrozenInputs) else folder / "data"
    state = {"run_id": run_id, "status": "running", "step": "configuration",
             "workflow_version": VERSION, "started_at": utc_now(), "model_attempts": 0}
    stage = "configuration"
    client = None
    try:
        with recorder.span("research", model=config["model"], config_fingerprint=digest(config)) as task:
            save(folder / "config.json", config)
            if profile is not None:
                save(folder / "execution-profile.json", profile)
            if quality_policy is not None:
                save(folder / "quality-policy.json", quality_policy)
            source_paths = [Path(__file__).with_name(n) for n in (
                "research_workflow.py", "research_observability.py", "research_analysis.py", "research_report.py",
                "research_inputs.py", "research_pipeline.py", "evidence_selection.py", "financial_facts.py")]
            if profile is not None:
                source_paths.append(Path(__file__).with_name('research_contract_profile.py'))
            if quality_policy is not None or comparison_context is not None:
                source_paths += [Path(__file__).with_name(n) for n in ('research_quality.py','research_intelligence.py','agent_harness.py','harness_contracts.py')]
            save(folder / "code-version.json", {p.name: sha(p) for p in source_paths})
            bundles, evidence, selections, gaps = {}, [], {}, list(LIMITATIONS)
            for subject in config["subjects"]:
                symbol = subject["symbol"]
                stage = "collection:" + symbol
                with recorder.span("source_collection", symbol=symbol) as span:
                    bundle = collector(source_root, symbol, subject["cik"], date.fromisoformat(config["as_of_date"]),
                                       config["question"], provider_config, refresh=False,
                                       news_days=config["news_days"], budget=budget, transports=transports,
                                       evidence_kind=config["evidence_kind"])
                    save(folder / (symbol + "-bundle.json"), bundle)
                    bundles[symbol] = bundle
                    span.update(source_count=len(bundle["sources"]),
                                request_attempts=0 if isinstance(collector, FrozenInputs) else
                                    sum(s.get("request_attempts", 0) if not s.get("cache_hit") else 0
                                        for s in bundle["sources"]))
                stage = "evidence:" + symbol
                with recorder.span("evidence_organization", symbol=symbol) as span:
                    items, selection = organize(source_root, bundle, subject, config)
                    evidence.extend(items); selections[symbol] = selection
                    span.update(evidence_count=len(items), input_fingerprint=selection["organized_fingerprint"])
                    gaps.extend(bundle["source_gaps"] + bundle["financials"]["limitations"] + bundle["financials"]["warnings"])
                    gaps.append(symbol + ": " + bundle["news_coverage"]["limitation"])
            if len({e["id"] for e in evidence}) != len(evidence):
                raise ValueError("Duplicate workflow evidence ID")
            package=None
            if quality_policy is not None or comparison_context is not None:
                from research_quality import QualityBarrier
                stage='quality:source_and_financial'
                with recorder.span('quality_source_financial'):
                    package=QualityBarrier(quality_policy or comparison_context).call('prepare',
                        root=str(source_root),bundles=bundles,evidence=evidence,
                        policy=quality_policy or comparison_context)
                    evidence=package['evidence']
                    save(folder/'quality-input.json',package)
                    if comparison_context is not None:
                        save(folder/'comparison-context.json',comparison_context)
            payload = {"question": config["question"], "as_of_date": config["as_of_date"],
                       "subjects": config["subjects"], "sections": config["sections"],
                       "limitations": gaps,
                       "evidence": model_evidence(evidence)}
            if package is not None:
                payload['quality_context']=package['context']
            fingerprint = digest(payload)
            schema = schema_for(config)
            instruction, prompt_version = INSTRUCTION, PROMPT_VERSION
            if profile is not None:
                from research_contract_profile import schema as profile_schema
                schema = profile_schema(config, profile)
                instruction, prompt_version = profile['instruction'], profile['prompt_version']
            if quality_policy is not None:
                from research_quality import model_schema,INSTRUCTION as QUALITY_INSTRUCTION
                schema=model_schema(schema)
                instruction+='\n'+QUALITY_INSTRUCTION
            messages = [{"role": "developer", "content": instruction},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
            if len(json.dumps(messages, ensure_ascii=False).encode()) > 90000:
                raise SourceError("Research input exceeds existing model byte budget; narrow scope")
            prompt = {"version": prompt_version, "template_sha256": digest([instruction, schema]),
                      "rendered_sha256": digest(messages)}
            save(folder / "evidence.json", evidence)
            save(folder / "input.json", payload)
            save(folder / "selection.json", selections)
            manifest = {"input_fingerprint": fingerprint, "config_fingerprint": digest(config),
                        "source_root": str(source_root.resolve()),
                        "source_bundle_hashes": {s: sha(folder / (s + "-bundle.json")) for s in bundles},
                        "source_versions": {s: b["sources"] for s, b in bundles.items()},
                        "prompt": prompt, "strict_historical_replay": False}
            save(folder / "input-manifest.json", manifest)
            save(folder / "request.json", {"messages": messages, "schema": schema, "prompt": prompt})
            stage = "model"
            with recorder.span("llm_synthesis", model=config["model"], prompt_version=prompt_version,
                               prompt_sha256=prompt["rendered_sha256"], input_fingerprint=fingerprint,
                               response_id=None, http_status=None, completion_status=None,
                               input_tokens=None, output_tokens=None, total_tokens=None) as span:
                if client_factory is None:
                    billing = provider_config.get("AETERNITAS_AUTH")
                    if billing not in {"chatgpt", "api_key"} or provider_config.get("AETERNITAS_MODEL") != config["model"]:
                        raise ValueError("Existing provider and model configuration must match explicitly")
                    auth = None
                    if billing == "chatgpt":
                        from chatgpt_auth import ChatGPTAuth
                        auth = ChatGPTAuth()
                    client = ResponsesAnalyst(provider_config.get("OPENAI_API_KEY") if billing == "api_key" else None,
                                              config["model"], folder, budget, transport=transports.get("model"), plan_auth=auth)
                else:
                    client = client_factory(folder, budget)
                with client:
                    if not isinstance(client, ResponsesAnalyst) or client.budget is not budget or client.model != config["model"]:
                        raise ValueError("Existing client with this workflow's model and budget required")
                    result, metadata = client.complete_json(messages, schema, name="industry_research")
                # Only known fields from the existing client; never persist arbitrary adapter metadata.
                metadata = {k: metadata.get(k) for k in ("requested_model", "returned_model", "version_authentication",
                                                        "response_id", "http_status", "completion_status", "usage", "cost")}
                save(folder / "raw-response.json", result)
                save(folder / "model-metadata.json", metadata)
                if hasattr(client,'diagnostics'):client.diagnostics.update(response_persisted=True)
                span.update({k: metadata[k] for k in ("response_id", "http_status", "completion_status")})
                span.update(metadata["usage"] or {})
            stage = "validation"
            with recorder.span("output_validation"):
                if metadata["requested_model"] != config["model"]:
                    raise ValueError("Model identity differs from configured request")
                if profile is None:
                    from research_quality import base_result
                    validate_output(base_result(result) if quality_policy is not None else result, config, evidence)
                else:
                    from research_contract_profile import validate_output_profile
                    from research_quality import base_result
                    validate_output_profile(base_result(result) if quality_policy is not None else result, config, evidence, profile)
                if quality_policy is not None:
                    from research_quality import QualityBarrier
                    stage='quality:challenge'
                    with recorder.span('quality_counterevidence_claim_falsification'):
                        quality=QualityBarrier(quality_policy).call('challenge',result=result,evidence=evidence,
                            package=package,schema=schema)
                        save(folder/'quality-evaluation.json',quality)
                budget.remaining()
            output_fingerprint = digest({"input_fingerprint": fingerprint, "prompt": prompt,
                                         "model_identity": metadata, "output": result})
            stage = "publication"
            with recorder.span("report_save", output_fingerprint=output_fingerprint):
                panels = [{"symbol": s["symbol"], "report_entity": s["report_entity"],
                           "summary": bundles[s["symbol"]]["financials"]} for s in config["subjects"]]
                if package is not None:
                    from research_quality import safe_panels
                    save(folder/'unreviewed-financials.json',panels)
                    panels=safe_panels(panels)
                report = {"schema_version": VERSION, "run_id": run_id, "status": "completed_with_gaps",
                          "created_at": utc_now(), "ticker": " / ".join(bundles), "question": config["question"],
                          "as_of_date": config["as_of_date"], "model": config["model"],
                          "billing_mode": "chatgpt_plan" if provider_config.get("AETERNITAS_AUTH") == "chatgpt" else
                                          "api_key" if client_factory is None else "injected_client",
                          "model_execution": "injected_unverified" if client_factory is not None or transports.get("model") is not None else "configured_client",
                          "prompt_version": prompt_version, "model_attempts": budget.calls,
                          "model_metadata": metadata, "prompt": prompt, "financials": panels[0]["summary"],
                          "financial_panels": panels, "research_sections": result["sections"],
                          "section_titles": {s["id"]: s["title"] for s in config["sections"]},
                          "identity_notes": config["subjects"], "evidence": evidence, "limitations": gaps,
                          "news_coverage": {"state": "provider_summaries_only", "selected_rows": sum(e["kind"] == "news" for e in evidence)},
                          "evidence_kind": config["evidence_kind"], "input_fingerprint": fingerprint,
                          "output_fingerprint": output_fingerprint, "strict_historical_replay": False}
                if profile is not None:
                    report['research_contract_version'] = profile['spec']['version']
                    report['execution_profile_fingerprint'] = digest(profile)
                if quality_policy is not None:
                    report.update(status='incomplete_research',quality_barrier=quality,
                        publication_level='incomplete_only',eligible_for_promotion=False)
                    report['limitations'].insert(0,'不完整研究：来源完整性、语义支持与推翻逻辑尚未独立审核；不得作为已验收研究。')
                save(folder / "report.json", report)
                with (folder / "report.html").open("x", encoding="utf-8") as stream:
                    stream.write(render_report(report))
                save(folder / "validation.json", {"structure_and_exact_citations": True,
                     "source_bundles_audited": True, "semantic_support_independently_verified": False,
                     "strict_historical_replay": False})
            task.update(status="completed_with_gaps", input_fingerprint=fingerprint,
                        output_fingerprint=output_fingerprint)
        state.update(status="incomplete_research" if quality_policy is not None else "completed_with_gaps", step="finished", model_attempts=budget.calls,
                     finished_at=utc_now(), observability_export_errors=recorder.export_errors)
        save(folder / "state.json", state)
        files = [p for p in folder.iterdir() if p.is_file()]
        commit = {"workflow_version": VERSION, "run_id": run_id,
             "input_fingerprint": fingerprint, "output_fingerprint": output_fingerprint,
             "files": {p.name: sha(p) for p in files}, "committed_at": utc_now()}
        stage = "publication_verification"
        _verify_artifacts(folder, commit)
        save(folder / "COMMITTED.json", commit)
        if hasattr(client,'diagnostics'):client.diagnostics.update(commit_status=True,phase='committed')
        return folder
    except Exception as exc:
        from research_analysis import FailureDiagnostics
        diag=getattr(client,'diagnostics',None) or FailureDiagnostics(folder)
        cause=diag.state['root_cause']
        if stage=='validation' or stage.startswith('quality:'):cause='output_or_quality_validation_rejected'
        elif stage.startswith('publication'):cause='local_commit_failed'
        diag.fail(stage,type(exc).__name__,cause)
        if stage.startswith('quality:'):
            save(folder/'quality-failure.json',{'stage':stage,'code':getattr(exc,'code','checker_failure'),
                 'error_type':type(exc).__name__,'formal_release_allowed':False,'at':utc_now()})
        if not (folder / "state.json").exists():
            # No exception message or credential-bearing provider payload is persisted.
            save(folder / "state.json", {**state, "status": "failed", "step": stage,
                 "error_type": type(exc).__name__, "error_code": error_code(exc),
                 "model_attempts": budget.calls, "finished_at": utc_now(),
                 "observability_export_errors": recorder.export_errors})
        save(folder / "failure.json", {"status": "failed", "step": stage,
             "error_type": type(exc).__name__, "error_code": error_code(exc),
             "at": utc_now(), "publication_valid": False})
        raise SourceError("Research workflow failed at " + stage + "; inspect " + str(folder)) from None


def verify_workflow(folder):
    """Verify publication, replay source audit and contracts; never execute a model."""
    folder = Path(folder)
    if (folder / "failure.json").exists():
        raise SourceError("Workflow failed; no valid publication")
    commit = json.loads((folder / "COMMITTED.json").read_text())
    return _verify_artifacts(folder, commit)


def _verify_artifacts(folder, commit):
    required = {"config.json", "code-version.json", "evidence.json", "input.json", "selection.json",
                "input-manifest.json", "request.json", "raw-response.json", "model-metadata.json",
                "report.json", "report.html", "validation.json", "state.json", "events.jsonl", "model-attempt.json"}
    names = commit["files"]
    if not required <= set(names):
        raise SourceError("Incomplete workflow publication")
    for name, expected in names.items():
        if not isinstance(name, str) or Path(name).name != name or sha(folder / name) != expected:
            raise SourceError("Workflow artifact fingerprint mismatch")
    c = config_checked(json.loads((folder / "config.json").read_text()))
    manifest = json.loads((folder / "input-manifest.json").read_text())
    payload = json.loads((folder / "input.json").read_text())
    request = json.loads((folder / "request.json").read_text())
    result = json.loads((folder / "raw-response.json").read_text())
    evidence = json.loads((folder / "evidence.json").read_text())
    report = json.loads((folder / "report.json").read_text())
    metadata = json.loads((folder / "model-metadata.json").read_text())
    if manifest["config_fingerprint"] != digest(c) or manifest["input_fingerprint"] != digest(payload):
        raise SourceError("Workflow input identity mismatch")
    if request["prompt"]["rendered_sha256"] != digest(request["messages"]) or json.loads(request["messages"][1]["content"]) != payload:
        raise SourceError("Workflow prompt binding mismatch")
    rebuilt, panels, bundles = [], [], {}
    for subject in c["subjects"]:
        path = folder / (subject["symbol"] + "-bundle.json")
        if subject["symbol"] not in manifest["source_bundle_hashes"] or sha(path) != manifest["source_bundle_hashes"][subject["symbol"]]:
            raise SourceError("Workflow source bundle identity mismatch")
        bundle = json.loads(path.read_text())
        bundles[subject["symbol"]] = bundle
        items, _ = organize(manifest["source_root"], bundle, subject, c)
        rebuilt.extend(items)
        panels.append({"symbol": subject["symbol"], "report_entity": subject["report_entity"],
                       "summary": bundle["financials"]})
    if rebuilt != evidence:
        if not (folder/'quality-input.json').exists():
            raise SourceError("Workflow evidence selection changed")
    quality_policy=None
    if (folder/'quality-input.json').exists():
        from research_quality import prepare_package,safe_panels
        policy_path=folder/('quality-policy.json' if (folder/'quality-policy.json').exists() else 'comparison-context.json')
        for name in ('quality-input.json',policy_path.name,'unreviewed-financials.json'):
            if name not in names:raise SourceError('Unpinned quality barrier')
        policy=json.loads(policy_path.read_text())
        package=prepare_package(root=manifest['source_root'],bundles=bundles,evidence=rebuilt,policy=policy)
        if package!=json.loads((folder/'quality-input.json').read_text()):raise SourceError('Quality input changed')
        rebuilt=package['evidence']
        if rebuilt!=evidence or payload.get('quality_context')!=package['context']:
            raise SourceError('Quality input binding changed')
        if json.loads((folder/'unreviewed-financials.json').read_text())!=panels:raise SourceError('Original financial panel changed')
        panels=safe_panels(panels)
        if policy_path.name=='quality-policy.json':quality_policy=policy
    if (payload["evidence"] != model_evidence(evidence) or payload["question"] != c["question"]
            or payload["subjects"] != c["subjects"] or payload["sections"] != c["sections"]
            or payload["as_of_date"] != c["as_of_date"]
            or report["financial_panels"] != panels or report["financials"] != panels[0]["summary"]
            or report["evidence"] != evidence or report["prompt"] != request["prompt"]
            or report["model_metadata"] != metadata or report["question"] != c["question"]
            or report["model"] != c["model"] or report["strict_historical_replay"] is not False
            or manifest["source_versions"] != {s: b["sources"] for s, b in bundles.items()}):
        raise SourceError("Workflow report/input binding mismatch")
    if 'execution-profile.json' in names:
        from research_contract_profile import checked, schema as profile_schema, validate_output_profile
        profile=checked(json.loads((folder/'execution-profile.json').read_text()))
        instruction=profile['instruction'];expected_schema=profile_schema(c,profile)
        if quality_policy is not None:
            from research_quality import model_schema,INSTRUCTION as QUALITY_INSTRUCTION
            instruction+='\n'+QUALITY_INSTRUCTION;expected_schema=model_schema(expected_schema)
        if (request['messages'][0] != {'role':'developer','content':instruction}
                or request['schema'] != expected_schema
                or request['prompt']['version'] != profile['prompt_version']
                or request['prompt']['template_sha256'] != digest([instruction,request['schema']])
                or report.get('execution_profile_fingerprint') != digest(profile)
                or report.get('research_contract_version') != profile['spec']['version']):
            raise SourceError('Workflow execution profile binding mismatch')
        from research_quality import base_result
        validate_output_profile(base_result(result) if quality_policy is not None else result,c,evidence,profile)
    else:
        from research_quality import base_result
        validate_output(base_result(result) if quality_policy is not None else result, c, evidence)
        if quality_policy is not None:
            from research_quality import model_schema,INSTRUCTION as QUALITY_INSTRUCTION
            if (request['schema']!=model_schema(schema_for(c))
                    or request['messages'][0]!={'role':'developer','content':INSTRUCTION+'\n'+QUALITY_INSTRUCTION}
                    or request['prompt']['template_sha256']!=digest([INSTRUCTION+'\n'+QUALITY_INSTRUCTION,request['schema']])):
                raise SourceError('Quality prompt/schema binding changed')
    if quality_policy is not None:
        from research_quality import challenge
        if 'quality-evaluation.json' not in names:raise SourceError('Missing quality evaluation')
        quality=challenge(result=result,evidence=evidence,package=package,schema=request['schema'])
        if (quality!=json.loads((folder/'quality-evaluation.json').read_text()) or report.get('quality_barrier')!=quality
                or report['status']!='incomplete_research' or report.get('publication_level')!='incomplete_only'
                or report.get('eligible_for_promotion') is not False):raise SourceError('Quality publication bypass')
    expected = digest({"input_fingerprint": digest(payload), "prompt": request["prompt"],
                       "model_identity": metadata, "output": result})
    if (commit["input_fingerprint"] != digest(payload) or commit["output_fingerprint"] != expected
            or report["input_fingerprint"] != digest(payload) or report["output_fingerprint"] != expected
            or report["research_sections"] != result["sections"]):
        raise SourceError("Workflow output identity mismatch")
    return True
