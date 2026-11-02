"""Frozen M6 business cases over production functions, without live credentials.

The runner measures hard contracts. Exact quotation and a passing case never
certify a claim's meaning. Human review notes are retained separately.
"""

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time

from sources.sec import SourceError, write_json


CASE_VERSION = "m6-case-v1"
RESULT_VERSION = "m6-evaluation-result-v1"
OPERATIONS = frozenset({"financial_summary", "normalize_news", "select_evidence",
                        "validate_analysis", "analysis_checks", "source_failure", "prompt_scope"})
IMPLEMENTATION_FILES = ("evaluation_cases.py", "financial_facts.py", "news_source.py",
                        "evidence_selection.py", "analysis_contracts.py", "analysis_workflow.py",
                        "research_inputs.py", "research_pipeline.py", "research_runtime.py",
                        "research_analysis.py", "sources/sec.py")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_json(path):
    blob = Path(path).read_bytes()
    if len(blob) > 1024 * 1024:
        raise ValueError("Evaluation case exceeds one MiB")
    return json.loads(blob, parse_float=Decimal,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite case JSON")))


def _json_value(value):
    """Preserve Decimal's exact value in records, including diagnostic failures."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str, allow_nan=False))


def _pointer(value, pointer):
    if pointer == "":
        return value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("Expected projection must use a JSON pointer")
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def load_cases(directory, *, split, manifest_path=None):
    """Load only the explicitly requested split and verify frozen bytes first."""
    if split not in {"development", "holdout"}:
        raise ValueError("Unknown evaluation split")
    folder = Path(directory)
    manifest_path = Path(manifest_path) if manifest_path else folder / "manifest.json"
    manifest = _read_json(manifest_path)
    if manifest.get("split") != split or manifest.get("case_version") != CASE_VERSION:
        raise ValueError("Evaluation manifest split / version mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Evaluation manifest needs frozen file hashes")
    cases, identities = [], set()
    for name, expected in sorted(files.items()):
        if (not isinstance(name, str) or Path(name).name != name or not name.endswith(".json")
                or name == "manifest.json" or not isinstance(expected, str)
                or not re.fullmatch("[0-9a-f]{64}", expected)):
            raise ValueError("Invalid case manifest member")
        path = folder / name
        if sha256_file(path) != expected:
            raise ValueError(f"Frozen case hash differs: {name}")
        case = _read_json(path)
        if (case.get("case_version") != CASE_VERSION or case.get("split") != split
                or not isinstance(case.get("id"), str)
                or not re.fullmatch("[A-Za-z0-9_.-]{1,100}", case["id"])
                or case["id"] in identities):
            raise ValueError("Invalid / duplicate case identity")
        if case.get("material", {}).get("mode") not in {"synthetic", "derived_public_snapshot", "mixed"}:
            raise ValueError("Case must state actual material provenance")
        scenarios = case.get("scenarios")
        if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 32:
            raise ValueError("Case needs one to thirty two business scenarios")
        scenario_ids = set()
        for scenario in scenarios:
            if (not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str)
                    or not re.fullmatch("[A-Za-z0-9_.-]{1,100}", scenario["id"])
                    or scenario["id"] in scenario_ids or scenario.get("operation") not in OPERATIONS
                    or not isinstance(scenario.get("input"), dict)
                    or not isinstance(scenario.get("expected"), dict)
                    or scenario["expected"].get("outcome") not in {"accepted", "rejected"}):
                raise ValueError("Invalid case scenario")
            scenario_ids.add(scenario["id"])
        identities.add(case["id"])
        cases.append((case, {"path": str(path), "sha256": expected}))
    declared = manifest.get("case_count")
    if declared is not None and declared != len(cases):
        raise ValueError("Case count differs from frozen manifest")
    return cases, {"path": str(manifest_path), "sha256": sha256_file(manifest_path),
                   "case_count": len(cases), "split": split}


def _source_failure(options):
    """Run the real orchestration failure boundary using in-memory HTTP only."""
    import httpx
    from research_pipeline import run_research

    count = {"source_requests": 0, "model_requests": 0}

    def source(request):
        count["source_requests"] += 1
        return httpx.Response(options["status_code"], json={"error": "controlled source rejection"})

    def model(request):
        count["model_requests"] += 1
        raise AssertionError("Source failure must not reach a model")

    ticker = options.get("ticker", "AAPL")
    cik = options.get("cik", "0000320193" if ticker == "AAPL" else "0000789019")
    run_id = "e" * 32
    config = {"SEC_CONTACT": "controlled-evaluation@example.invalid",
              "FINNHUB_API_KEY": "synthetic-unused", "OPENAI_API_KEY": "synthetic-unused",
              "AETERNITAS_AUTH": "api_key", "AETERNITAS_MODEL": "controlled-no-model"}
    with tempfile.TemporaryDirectory(prefix="aeternitas-m6-source-") as temporary:
        root = Path(temporary)
        try:
            run_research(root, ticker, cik, date.fromisoformat(options.get("as_of_date", "2025-10-01")),
                         "Controlled upstream failure, not actual company research", config,
                         transports={"sec": httpx.MockTransport(source), "web": httpx.MockTransport(source),
                                     "model": httpx.MockTransport(model)},
                         evidence_kind="controlled_evaluation", seconds=30, run_id=run_id)
        except SourceError as exc:
            error = str(exc).replace(run_id, "[controlled-run-id]")
        else:
            raise AssertionError("A failing upstream unexpectedly produced a report")
        folder = root / "runs" / run_id
        state = json.loads((folder / "state.json").read_text())
        return {"status": state["status"], "step": state["step"],
                "model_attempts": state["model_attempts"], **count,
                "report_exists": (folder / "report.json").exists() or (folder / "report.html").exists(),
                "published_exists": (folder / "published.json").exists(), "error": error,
                "transport": "httpx.MockTransport", "live_requests": 0}


def execute_operation(operation, options):
    """The allowlist intentionally excludes eval/exec, arbitrary imports and HTTP."""
    options = deepcopy(options)
    if operation == "financial_summary":
        from financial_facts import build_financial_summary
        return build_financial_summary(options["raw"], options["cik"], options["as_of_date"], options["source"])
    if operation == "normalize_news":
        from news_source import normalize_news
        return normalize_news(options["rows"], options["ticker"], options["cik"],
                              date.fromisoformat(options["as_of_date"]), options["days"])
    if operation == "select_evidence":
        from evidence_selection import select_evidence
        chosen, manifest = select_evidence(options["role"], options["ticker"], options["cik"],
                                           options["as_of_date"], options["question"], options["evidence"])
        return {"selected": chosen, "manifest": manifest}
    if operation == "validate_analysis":
        from analysis_contracts import validate_analysis
        return validate_analysis(options["result"], options["evidence"])
    if operation == "analysis_checks":
        from analysis_workflow import analysis_checks
        return analysis_checks(options["analyses"], options["chosen"])
    if operation == "source_failure":
        return _source_failure(options)
    if operation == "prompt_scope":
        from research_analysis import prompt_for
        messages = prompt_for(options["role"], options["ticker"], options["as_of_date"],
                              options["question"], options["evidence"], options["previous"])
        return {"messages": messages, "payload": json.loads(messages[1]["content"])}
    raise ValueError("Operation is outside the frozen evaluation allowlist")


def evaluate_case(case, metadata):
    records = []
    started = time.monotonic()
    for scenario in case["scenarios"]:
        expected = scenario["expected"]
        try:
            value = execute_operation(scenario["operation"], scenario["input"])
            actual = {"outcome": "accepted", "value": _json_value(value)}
        except (ValueError, SourceError) as exc:
            actual = {"outcome": "rejected", "error_type": type(exc).__name__, "error": str(exc)}
        except Exception as exc:
            actual = {"outcome": "runner_error", "error_type": type(exc).__name__, "error": str(exc)}
        failures = []
        if actual["outcome"] != expected["outcome"]:
            failures.append("outcome differs")
        if "error_contains" in expected and expected["error_contains"] not in actual.get("error", ""):
            failures.append("expected rejection reason absent")
        projections = {}
        for pointer, target in expected.get("matches", {}).items():
            try:
                observed = _pointer(actual.get("value"), pointer)
                projections[pointer] = {"expected": _json_value(target), "actual": observed}
                # bool and int compare equal in Python; business contracts do not.
                if type(observed) is not type(target) or observed != target:
                    failures.append(f"projection differs: {pointer}")
            except (KeyError, IndexError, TypeError, ValueError):
                failures.append(f"projection absent: {pointer}")
        for pointer in expected.get("absent", []):
            try:
                _pointer(actual.get("value"), pointer)
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            failures.append(f"unexpected field present: {pointer}")
        record = {"id": scenario["id"], "operation": scenario["operation"],
                  "expected": _json_value(expected), "actual": actual,
                  "projections": projections, "hard_checks_passed": not failures, "failures": failures}
        if "manual_review" in scenario:
            record["manual_review"] = {"record": scenario["manual_review"],
                                       "automated_entailment_verdict": False,
                                       "independent_user_review": False}
        records.append(record)
    return {"id": case["id"], "case_version": case["case_version"], "split": case["split"],
            "company": case.get("company"), "description": case.get("description"),
            "material": case["material"], "fixture": metadata,
            "hard_checks_passed": all(row["hard_checks_passed"] for row in records),
            "semantic_certified": False, "elapsed_seconds": round(time.monotonic() - started, 6),
            "scenarios": records}


def evaluate_directory(directory, *, split, output, artifacts=None, manifest_path=None):
    cases, manifest = load_cases(directory, split=split, manifest_path=manifest_path)
    repo = Path(__file__).resolve().parent
    names = [*IMPLEMENTATION_FILES,
             *(str(path.relative_to(repo)) for path in sorted((repo / "prompts").rglob("*.md")))]
    fingerprints = {name: sha256_file(repo / name) for name in names}
    rows = [evaluate_case(case, metadata) for case, metadata in cases]
    artifact_dir = Path(artifacts) if artifacts else Path(output).parent / f"{split}-cases"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        path = artifact_dir / f"{row['id']}.json"
        write_json(path, _json_value(row))
        row["artifact"] = {"path": str(path), "sha256": sha256_file(path)}
    result = {"schema_version": RESULT_VERSION, "split": split,
              "evaluated_at": datetime.now(timezone.utc).isoformat(), "manifest": manifest,
              "implementation_sha256": fingerprints, "case_count": len(rows),
              "passed_case_count": sum(row["hard_checks_passed"] for row in rows),
              "hard_checks_passed": all(row["hard_checks_passed"] for row in rows),
              "live_source_requests": 0, "live_model_requests": 0,
              "semantic_certified": False, "human_review_required": True,
              "limitations": ["A small project-specific regression set is not a market accuracy estimate.",
                              "Quotation membership does not prove semantic support.",
                              "Synthetic HTTP failures do not constitute live upstream validation."],
              "cases": rows}
    write_json(output, _json_value(result))
    return result
