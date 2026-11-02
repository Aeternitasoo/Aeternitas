"""M1's complete synchronous research flow with explicit source/model failures."""

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from chatgpt_auth import ChatGPTAuth
from filing_source import load_filing_evidence
from financial_facts import build_financial_summary
from news_source import load_news, normalize_news
from research_analysis import (PROMPT_VERSION, LEGACY_PROMPT_VERSION, ROLES, ResponsesAnalyst,
                               financial_evidence, validate_result, validate_legacy_result)
from analysis_workflow import (REPORT_SCHEMA_VERSION, build_analysis_plan, analysis_checks,
                               publication_files, verify_analysis_artifacts)
from research_report import render_report
from research_runtime import Budget, WebSources, utc_now
from sources.sec import SecClient, SourceError, write_atomic, write_json


def run_research(root, ticker, cik, cutoff, question, config, *, refresh=False,
                 news_days=14, seconds=300, transports=None, progress=None,
                 evidence_kind="live_api", plan_auth=None, run_id=None, state_observer=None):
    transports = transports or {}
    root = Path(root)
    run_id = run_id or uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Invalid run ID")
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    billing = "chatgpt_plan" if config.get("AETERNITAS_AUTH") == "chatgpt" else "api_key"
    state = {"run_id": run_id, "ticker": ticker, "as_of_date": cutoff.isoformat(), "billing_mode": billing,
             "status": "created", "step": "created", "started_at": utc_now(),
             "model_attempts": 0, "evidence_kind": evidence_kind,
             "model": config["AETERNITAS_MODEL"], "prompt_version": PROMPT_VERSION}

    def save(step, status="running"):
        state.update(step=step, status=status, updated_at=utc_now())
        write_json(run_dir / "state.json", state)
        if state_observer:
            state_observer(dict(state))
        if progress:
            progress(step)

    def attempted(role, count):
        state["model_attempts"] = count
        save(f"model:{role}")

    budget = Budget(seconds=seconds, on_call=attempted)
    analyst = None
    try:
        from research_inputs import collect_inputs
        inputs = collect_inputs(root,ticker,cik,cutoff,question,config,refresh=refresh,news_days=news_days,
                                budget=budget,transports=transports,progress=save,evidence_kind=evidence_kind)
        summary, evidence = inputs["financials"], inputs["evidence"]
        news = [e for e in evidence if e["kind"] == "news"]
        gaps, coverage = inputs["source_gaps"], inputs["news_coverage"]
        write_json(run_dir / "input_bundle.json", inputs)
        audit_inputs(root, inputs)
        if config.get("DATABASE_URL"):
            from evidence_repository import connect, import_bundle
            save("data_import")
            with connect(config["DATABASE_URL"]) as connection:
                state["dataset_id"] = import_bundle(connection,root,inputs)
        selected,plan=build_analysis_plan(inputs,config['AETERNITAS_MODEL'],billing)
        write_json(run_dir/'analysis_plan.json',plan)
        state['workflow_fingerprint']=plan['workflow_fingerprint']
        analyses = {}
        with ResponsesAnalyst(config.get("OPENAI_API_KEY") if billing == "api_key" else None,
                              config["AETERNITAS_MODEL"], run_dir / "steps", budget,
                              transport=transports.get("model"),
                              plan_auth=(plan_auth or ChatGPTAuth()) if billing == "chatgpt_plan" else None) as analyst:
            for role in ROLES:
                save(role)
                allowed = selected[role]
                analyses[role] = analyst.analyze(role, ticker, cutoff.isoformat(), question, allowed, analyses.copy())
                state["model_usage"] = analyst.usage.copy()
        save("validation")
        checks=analysis_checks(analyses,selected)
        limitations = list(summary["limitations"]) + list(summary["warnings"]) + gaps + [
            coverage["limitation"],
            "Filing excerpts cover at most two filings / two selected sections each; not a full company audit.",
            "Citation membership and exact quotation are checked, not semantic entailment; review important claims manually.",
            "Research runs and steps are stored by the local task service when submitted through M3; automatic recovery / retry / cancellation remain M5.",
            "No Arabic digits in AI free prose; exact values are rendered from program tables and quoted evidence.",
        ]
        status = "completed_with_gaps" if gaps or not news or summary["warnings"] or any(a['gaps'] for a in analyses.values()) else "completed"
        report = {**inputs, "schema_version": REPORT_SCHEMA_VERSION, "run_id": run_id,
                  "created_at": utc_now(), "status": status, "model": config["AETERNITAS_MODEL"],
                  "billing_mode": billing, "dataset_id": state.get("dataset_id"),
                  "prompt_version": PROMPT_VERSION, "analyses": analyses, "limitations": limitations,
                  "model_attempts": budget.calls, "model_usage": analyst.usage, "cost": "unknown",
                  "analysis_provenance":plan,"analysis_checks":checks}
        audit_inputs(root, report)
        budget.remaining()
        html = render_report(report).encode()
        write_atomic(run_dir / "report.html", html)
        write_json(run_dir / "report.json", report)
        budget.remaining()
        manifest = {name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
                    for name in publication_files(run_dir)}
        write_json(run_dir / "published.json", {"files": manifest, "published_at": utc_now()})
        budget.remaining()
        state.update(model_usage=analyst.usage, report_html="report.html", report_json="report.json")
        save("finished", status)
        return run_id, report
    except Exception as exc:
        # Do not persist provider exception text, HTTP request headers or credentials.
        message = str(exc) if isinstance(exc, (SourceError, ValueError)) else type(exc).__name__
        for key in ("OPENAI_API_KEY", "FINNHUB_API_KEY", "SEC_CONTACT"):
            secret = config.get(key)
            if secret:
                message = message.replace(secret, "[redacted]")
        state["error"] = message[:500]
        if analyst is not None:
            state["model_usage"] = analyst.usage
        save(state["step"], "failed")
        raise SourceError(f"Research failed at {state['step']}; run {run_id}: {message[:300]}") from None


def _local(root, relative):
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise SourceError("Saved source path escapes data directory")
    return path


def audit_inputs(root, bundle):
    """Verify bytes, normalized spans, exact fact locations and deterministic calculations."""
    decoded = {}
    for source in bundle["sources"]:
        rid = source.get("retrieval_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", rid):
            raise SourceError("Invalid source retrieval identity")
        receipt = json.loads(_local(root, f"retrievals/{rid}.json").read_text(encoding="utf-8"))
        if {k:v for k,v in source.items() if k != "cache_hit"} != {k:v for k,v in receipt.items() if k != "cache_hit"}:
            raise SourceError("Source receipt differs from saved retrieval metadata")
        raw = _local(root, source["snapshot_path"]).read_bytes()
        if len(raw) != source["size_bytes"] or hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise SourceError("Raw source fingerprint mismatch")
        decoded[source["sha256"]] = raw
    financials = bundle["financials"]
    if financials["source"]["source_url"] != f"https://data.sec.gov/api/xbrl/companyfacts/CIK{bundle['cik']}.json":
        raise SourceError("Financial source URL does not match company identity")
    raw = json.loads(decoded[financials["source"]["sha256"]], parse_float=Decimal)
    rebuilt = build_financial_summary(raw=raw, cik=financials["cik"], as_of_date=financials["as_of_date"],
                                      source=financials["source"])
    if rebuilt["annual_periods"] != financials["annual_periods"] or rebuilt["revenue_yoy"] != financials["revenue_yoy"]:
        raise SourceError("Saved financial facts / calculations differ from original source")
    if bundle["cik"] != financials["cik"] or bundle["as_of_date"] != financials["as_of_date"]:
        raise SourceError("Bundle company / cutoff differs from financial selection")
    expected_financial = financial_evidence(rebuilt)
    actual_financial = [e for e in bundle["evidence"] if e["kind"] in {"financial_fact", "calculation"}]
    if expected_financial != actual_financial:
        raise SourceError("Financial evidence differs from verified fact / calculation selection")
    if len({e["id"] for e in bundle["evidence"]}) != len(bundle["evidence"]):
        raise SourceError("Duplicate evidence identity")
    cutoff = date.fromisoformat(bundle["as_of_date"])
    receipts = {s["retrieval_id"]: s for s in bundle["sources"]}
    coverage = bundle["news_coverage"]
    start = date.fromisoformat(coverage["requested_start"])
    if coverage["requested_end"] != bundle["as_of_date"] or not 1 <= (cutoff-start).days+1 <= 30:
        raise SourceError("News requested date window differs from research cutoff")
    news_sources = [s for s in bundle["sources"] if s["source_url"] == "https://finnhub.io/api/v1/company-news"]
    if len(news_sources) != 1:
        raise SourceError("Exactly one company-news retrieval is required")
    news_source = news_sources[0]
    query = news_source["query"]
    if query.get("symbol") != bundle["ticker"] or query.get("to") != bundle["as_of_date"] or date.fromisoformat(query["from"]) > start:
        raise SourceError("News source query belongs to another company / window")
    raw_news = json.loads(decoded[news_source["sha256"]])
    selected_news = [e for e in bundle["evidence"] if e["kind"] == "news"]
    if not isinstance(raw_news,list) or coverage["provider_rows"] != len(raw_news) or coverage["selected_rows"] != len(selected_news):
        raise SourceError("News coverage counts differ from saved response / selection")
    if coverage["state"] != ("results" if selected_news else "no_results_returned"):
        raise SourceError("News coverage state conflicts with the evidence")
    for evidence in bundle["evidence"]:
        if receipts.get(evidence["source"]["retrieval_id"]) != evidence["source"]:
            raise SourceError("Evidence retrieval is outside the source bundle")
        if evidence["company_cik"] != financials["cik"]:
            raise SourceError("Evidence company mismatch")
        if evidence["kind"] == "filing":
            from filing_source import extract_sections
            locator = evidence["locator"]
            text = _local(root, locator["normalized_path"]).read_text(encoding="utf-8")
            if hashlib.sha256(text.encode()).hexdigest() != locator["normalized_sha256"]:
                raise SourceError("Normalized filing fingerprint mismatch")
            if text[locator["normalized_start"]:locator["normalized_end"]] != evidence["text"]:
                raise SourceError("Filing quotation does not match normalized source span")
            original_text, _ = extract_sections(decoded[evidence["source"]["sha256"]], locator["form"])
            if original_text != text:
                raise SourceError("Normalized filing differs from original HTML")
            if evidence["published_at"] != locator["filed_at"] or date.fromisoformat(locator["filed_at"]) > cutoff:
                raise SourceError("Filing date / cutoff mismatch")
            expected_url = (f"https://www.sec.gov/Archives/edgar/data/{int(bundle['cik'])}/"
                            f"{locator['accession'].replace('-', '')}/{locator['document']}")
            if evidence["source_url"] != expected_url or evidence["source"]["source_url"] != expected_url:
                raise SourceError("Filing URL does not match its company / accession")
        elif evidence["kind"] == "news":
            raw_news = json.loads(decoded[evidence["source"]["sha256"]])
            row = raw_news[evidence["locator"]["response_index"]]
            if evidence["text"] != f"{row['headline'][:400]}\n{row.get('summary', '')[:1400]}":
                raise SourceError("News evidence differs from raw provider response")
            stamp = datetime.fromtimestamp(row["datetime"], timezone.utc)
            from zoneinfo import ZoneInfo
            day = stamp.astimezone(ZoneInfo("Asia/Shanghai")).date()
            start = date.fromisoformat(bundle["news_coverage"]["requested_start"])
            if not start <= day <= cutoff or evidence["published_at"] != stamp.isoformat():
                raise SourceError("News publication date / cutoff mismatch")
            if evidence["source_url"] != row["url"]:
                raise SourceError("News URL differs from original response")
            related = row.get("related")
            if related and bundle["ticker"] not in {x.strip().upper() for x in str(related).split(",")}:
                raise SourceError("News is attributed to a different company")
            if evidence["locator"].get("parser_version"):
                days = (cutoff - start).days + 1
                expected_news = [{**n, "source": evidence["source"]} for n in
                                 normalize_news(raw_news,bundle["ticker"],bundle["cik"],cutoff,days)[:10]]
                if evidence not in expected_news:
                    raise SourceError("News selection / canonical identity differs from parser")
    return True


def saved_report(root, run_id):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Invalid run ID")
    folder = Path(root) / "runs" / run_id
    return validate_report_directory(root, folder, run_id)


def validate_report_directory(root, folder, run_id):
    """The same publication checks also validate an isolated owner generation before promotion."""
    folder = Path(folder)
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    if state["status"] not in {"completed", "completed_with_gaps"}:
        raise SourceError("This run has no completed report; inspect state.json")
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    if state.get('prompt_version')!=report.get('prompt_version'):
        raise SourceError('Saved state and report prompt versions differ')
    for key in ('run_id','ticker','as_of_date','model','billing_mode','model_attempts'):
        if state.get(key)!=report.get(key):
            raise SourceError('Saved state and report identity differ')
    m4=report.get('schema_version')==REPORT_SCHEMA_VERSION
    legacy=(report.get('schema_version')=='aeternitas-report-m1-v1' and report.get('prompt_version')==LEGACY_PROMPT_VERSION)
    if not m4 and not legacy:
        raise SourceError('Unknown saved report / prompt version; verification refused')
    if legacy and (any(k in report for k in ('analysis_provenance','analysis_checks'))
                   or 'workflow_fingerprint' in state or (folder/'analysis_plan.json').exists()):
        raise SourceError('M4 metadata cannot be verified as a legacy report')
    manifest = json.loads((folder / "published.json").read_text(encoding="utf-8"))
    expected=set(publication_files(folder)) if m4 else {"report.html", "report.json", "input_bundle.json"}
    if set(manifest["files"]) != expected:
        raise SourceError("Invalid report manifest")
    for name, expected in manifest["files"].items():
        if not (folder/name).resolve().is_relative_to(folder.resolve()):
            raise SourceError('Published artifact path escapes run directory')
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
            raise SourceError("Published report fingerprint mismatch")
    if report["run_id"] != run_id:
        raise SourceError("Report identity mismatch")
    bundle=json.loads((folder/'input_bundle.json').read_text())
    if any(report.get(k)!=v for k,v in bundle.items()):
        raise SourceError('Saved input bundle differs from report inputs')
    if m4 and set(bundle)!={'ticker','cik','as_of_date','question','financials','evidence','sources',
                            'news_coverage','source_gaps','evidence_kind'}:
        raise SourceError('Unexpected M4 input bundle fields')
    audit_inputs(root, report)
    if m4:
        from analysis_contracts import SUPPORTED_PROMPT_VERSIONS
        if report.get('prompt_version') not in SUPPORTED_PROMPT_VERSIONS:
            raise SourceError('M4 prompt version differs from its report contract')
        if state.get('workflow_fingerprint')!=report.get('analysis_provenance',{}).get('workflow_fingerprint'):
            raise SourceError('M4 state and report workflow fingerprint differ')
        try:
            verify_analysis_artifacts(folder,report)
        except (ValueError,KeyError,TypeError,OSError):
            raise SourceError('M4 analysis provenance / actual input / check trace validation failed') from None
        return folder,report
    for role in ROLES:
        allowed = report["evidence"]
        if role == "fundamentals":
            allowed = [e for e in allowed if e["kind"] != "news"]
        elif role == "events":
            allowed = [e for e in allowed if e["kind"] == "news"]
        validate_legacy_result(report["analyses"][role], allowed)
    return folder, report
