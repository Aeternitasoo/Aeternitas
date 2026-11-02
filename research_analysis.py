"""Four bounded Responses calls; citations must quote actual supplied evidence."""

import hashlib
import json
import re
import time

import httpx

from chatgpt_stream import plan_error, read_plan_response
from sources.sec import SourceError, write_json


PROMPT_VERSION = "evidence-roles-m1-v3"
ROLES = ("fundamentals", "events", "risk", "synthesis")
ROLE_TASKS = {
    "fundamentals": "解释经营变化，仅根据财务事实、程序计算和财报片段。区分事实与可能解释。",
    "events": "整理本次新闻摘要中的事件，区分已经发生的事实与媒体判断；没有新闻则报告缺口。",
    "risk": "审阅已有分析及原始证据，指出冲突、口径、时间和信息缺口，提出有依据的反方判断。",
    "synthesis": "回答用户问题，综合支持与反对证据、主要风险和仍需研究的问题；不能新增外部事实。",
}
RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "claims": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "text": {"type": "string"},
                "stance": {"type": "string", "enum": ["support", "challenge", "neutral"]},
                "citations": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"evidence_id": {"type": "string"}, "quote": {"type": "string"}},
                    "required": ["evidence_id", "quote"],
                }},
            }, "required": ["text", "stance", "citations"],
        }},
        "gaps": {"type": "array", "items": {"type": "string"}},
    }, "required": ["claims", "gaps"],
}
LEGACY_PROMPT_VERSION = PROMPT_VERSION
from analysis_contracts import (PROMPT_VERSION, ROLE_PROMPT_VERSIONS, RESULT_SCHEMA_VERSION,
                                RESULT_SCHEMA, MODEL_RESULT_SCHEMA, prompt_instruction)


def financial_evidence(summary):
    source = summary["source"]
    evidence = []
    for annual in summary["annual_periods"]:
        for metric, fact in annual["facts"].items():
            if fact is None:
                continue
            evidence.append({"id": f"V-{fact['fact_id'][:16]}", "kind": "financial_fact",
                             "company_cik": summary["cik"], "title": metric,
                             "text": f"{metric}: USD {fact['value']}; period {fact['period_start']} to {fact['period_end']}; filed {fact['filed_at']}",
                             "source_url": source["source_url"], "source": source,
                             "published_at": fact["filed_at"], "time_precision": "date",
                             "locator": fact})
        for name in ("operating_margin", "net_margin"):
            calculation = annual[name]
            key = f"{name}:{annual['period_end']}"
            evidence.append(_calculation_evidence(key, calculation, summary))
    evidence.append(_calculation_evidence("revenue_yoy", summary["revenue_yoy"], summary))
    return evidence


def _calculation_evidence(key, calculation, summary):
    digest = hashlib.sha256(json.dumps([key, calculation], sort_keys=True).encode()).hexdigest()[:16]
    input_ids = set(calculation["input_fact_ids"])
    filed_dates = [fact["filed_at"] for period in summary["annual_periods"] for fact in period["facts"].values()
                   if fact is not None and fact["fact_id"] in input_ids]
    return {"id": f"C-{digest}", "kind": "calculation", "company_cik": summary["cik"],
            "title": key, "text": f"{key}: {json.dumps(calculation, ensure_ascii=False, sort_keys=True)}",
            "source_url": summary["source"]["source_url"], "source": summary["source"],
            "published_at": max(filed_dates) if filed_dates else summary["as_of_date"], "time_precision": "date",
            "derived_as_of": summary["as_of_date"],
            "locator": {"calculation": calculation, "formula_version": calculation["formula_version"]}}


def validate_legacy_result(result, evidence):
    if not isinstance(result, dict) or set(result) != {"claims", "gaps"}:
        raise ValueError("Output must contain only claims and gaps")
    claims, gaps = result["claims"], result["gaps"]
    if not isinstance(claims, list) or len(claims) > 6 or not isinstance(gaps, list) or len(gaps) > 6:
        raise ValueError("Output list limit exceeded")
    if not claims and not gaps:
        raise ValueError("Empty analysis needs an explicit information gap")
    by_id = {e["id"]: e for e in evidence}
    for claim_index, claim in enumerate(claims):
        if not isinstance(claim, dict) or set(claim) != {"text", "stance", "citations"}:
            raise ValueError(f"claims[{claim_index}]: Invalid claim structure")
        text = claim["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError(f"claims[{claim_index}].text: Claim text limit exceeded")
        if re.search(r"[0-9]", text):
            raise ValueError(f"claims[{claim_index}].text: Put numbers in deterministic tables / original quotations, not free prose")
        if claim["stance"] not in {"support", "challenge", "neutral"}:
            raise ValueError(f"claims[{claim_index}].stance: Invalid stance")
        citations = claim["citations"]
        if not isinstance(citations, list) or not 1 <= len(citations) <= 3:
            raise ValueError(f"claims[{claim_index}].citations: Each claim needs one to three citations")
        for cite_index, cite in enumerate(citations):
            location = f"claims[{claim_index}].citations[{cite_index}]"
            if not isinstance(cite, dict) or set(cite) != {"evidence_id", "quote"}:
                raise ValueError(f"{location}: Invalid citation structure")
            eid, quote = cite["evidence_id"], cite["quote"]
            if not isinstance(eid, str) or eid not in by_id:
                raise ValueError(f"{location}.evidence_id: Citation is outside this role's evidence set")
            if not isinstance(quote, str) or not 12 <= len(quote) <= 300:
                raise ValueError(f"{location}.quote: Citation quote must contain twelve to three hundred characters")
            if quote not in by_id[eid]["text"]:
                raise ValueError(f"{location}.quote: Citation quote is not an exact supplied source substring")
    for gap_index, gap in enumerate(gaps):
        if not isinstance(gap, str) or not gap.strip() or len(gap) > 600 or re.search(r"[0-9]", gap):
            raise ValueError(f"gaps[{gap_index}]: Gap must be concise text without unverified numbers")
    return result


def validate_result(result, evidence):
    from analysis_contracts import validate_analysis
    return validate_analysis(result,evidence)


def prompt_for(role, company, cutoff, question, evidence, previous, repair=None, repair_output=None,
               *, prompt_version=None):
    from analysis_contracts import HISTORICAL_PROMPT_VERSION
    version = prompt_version or PROMPT_VERSION
    instruction = prompt_instruction(role) if version == PROMPT_VERSION else prompt_instruction(role, version)
    payload = {"company": company, "as_of_date": cutoff, "question": question,
               "evidence": [{"id": e["id"], "kind": e["kind"], "title": e["title"],
                             "published_at": e["published_at"], "text": e["text"]} for e in evidence],
               "prior_analyses_untrusted": previous if role in {"risk", "synthesis"} else {}}
    if version != HISTORICAL_PROMPT_VERSION:
        kinds = {'fundamentals':['financial_fact','calculation','filing'], 'events':['news'],
                 'risk':['financial_fact','calculation','filing','news'],
                 'synthesis':['financial_fact','calculation','filing','news']}
        payload['input_evidence_scope'] = {'role':role, 'provided_kinds':sorted({e['kind'] for e in evidence}),
                                           'coverage':'this_role_only_not_global_company_coverage'}
        payload['prior_analysis_scopes'] = {name:{'allowed_evidence_kinds':kinds[name],
                       'coverage':'this_role_only_not_global_company_coverage'}
                       for name in payload['prior_analyses_untrusted'] if name in kinds}
    if repair:
        payload["previous_attempt_validation_error"] = repair
    if repair_output is not None:
        payload["previous_attempt_output_untrusted"] = repair_output
    serialized = json.dumps(payload, ensure_ascii=False)
    if len(serialized.encode()) > 90000 and "previous_attempt_output_untrusted" in payload:
        del payload["previous_attempt_output_untrusted"]
        payload["previous_attempt_output_omitted"] = "Input byte budget; repair using the validation error and evidence."
        serialized = json.dumps(payload, ensure_ascii=False)
    if len(serialized.encode()) > 90000:
        raise SourceError("Selected evidence exceeds model input byte budget")
    return [{"role": "developer", "content": instruction}, {"role": "user", "content": serialized}]



class FailureDiagnostics:
    """Append-only safe status observations, using existing run IDs and Recorder."""
    def __init__(self,root):
        from pathlib import Path
        self.path=Path(root)/'diagnostics'
        self.path.mkdir(parents=True,exist_ok=True)
        self.state={'failure_stage':None,'error_type':None,'root_cause':None,
          'request_attempted':False,'request_sent':False,'http_status':None,
          'response_completed':None,'response_persisted':False,'commit_status':False,
          'retry_count':0,'token_usage':None,'recovery_action':'none','phase':'created'}
        self.record()
    def record(self):
        import os
        from research_runtime import utc_now
        index=len(list(self.path.glob('event-*.json')))+1
        with (self.path/('event-'+str(index).zfill(4)+'.json')).open('x') as f:
            json.dump({'at':utc_now(),**self.state},f,sort_keys=True)
    def update(self,**fields):
        self.state.update(fields);self.record()
    def request(self,request):
        self.update(request_attempted=True,request_sent=None,phase='awaiting_http')
    def response(self,response):
        self.update(request_sent=True,http_status=response.status_code,phase='reading_response')
    def fail(self,stage,error_type,cause=None):
        self.update(failure_stage=stage,error_type=error_type,root_cause=cause,
          recovery_action='stop_no_automatic_retry')


class ResponsesAnalyst:
    def __init__(self, key, model, root, budget, *, transport=None, plan_auth=None,
                 usage_observer=None, fault_hook=None):
        if (not key and plan_auth is None) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", model):
            raise ValueError("Missing API key or invalid model identifier")
        self.key, self.model, self.root, self.budget = key, model, root, budget
        self.plan_auth = plan_auth
        self.http = httpx.Client(transport=transport, follow_redirects=False)
        self.usage = []
        self.usage_observer = usage_observer
        self.fault_hook = fault_hook or (lambda event, **details: None)

    def record_usage(self, usage):
        self.usage.append(usage)
        if self.usage_observer:
            self.usage_observer(usage)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    def complete_json(self, inputs, schema, *, name="agent_prediction"):
        """One bounded structured call using this client's existing auth/SSE/budget.

        No automatic retries, tool execution, disk writes or API-key fallback.
        Caller validates domain fields and citations before publishing anything.
        """
        self.diagnostics=FailureDiagnostics(self.root)
        self.http.event_hooks['request'].append(self.diagnostics.request)
        self.http.event_hooks['response'].append(self.diagnostics.response)
        if not re.fullmatch(r"[a-z_]{1,64}", name):
            raise ValueError("Invalid schema name")
        if len(json.dumps(inputs, ensure_ascii=False).encode()) > 90000:
            raise SourceError("Agent input exceeds local byte budget")
        self.budget.model_attempt(name)
        body = {"model": self.model, "store": False, "input": inputs,
                "text": {"format": {"type": "json_schema", "name": name,
                                    "strict": True, "schema": schema}}}
        self.diagnostics.update(phase='authorization')
        try:
            token = self.plan_auth.token() if self.plan_auth is not None else self.key
        except Exception as exc:
            self.diagnostics.fail('authorization',type(exc).__name__,'authorization_unavailable')
            raise
        if self.plan_auth is not None:
            body["stream"] = True
        else:
            body["max_output_tokens"] = 2200
        try:
            with self.http.stream("POST", "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {token}"}, json=body,
                    timeout=httpx.Timeout(min(180, self.budget.remaining()),
                                          connect=min(90, self.budget.remaining()))) as response:
                if response.status_code != 200:
                    self.diagnostics.update(root_cause='http_status_rejected')
                    raise SourceError(f"Agent model HTTP {response.status_code}; no retry or fallback")
                media = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if self.plan_auth is not None:
                    if media and media != "text/event-stream":
                        raise SourceError("Agent expected SSE")
                    data = read_plan_response(response, self.budget)
                else:
                    if media != "application/json":
                        raise SourceError("Agent expected JSON envelope")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        self.budget.remaining()
                        if len(raw)+len(chunk) > 1024*1024:
                            raise SourceError("Agent response exceeds byte budget")
                        raw.extend(chunk)
                    data = json.loads(raw)
            self.diagnostics.update(response_completed=isinstance(data,dict) and data.get('status')=='completed',
                phase='response_validation')
            if not isinstance(data, dict) or data.get("status") != "completed":
                raise SourceError("Agent response not completed")
            observed_usage=data.get('usage')
            if isinstance(observed_usage,dict):
                self.diagnostics.update(token_usage={k:observed_usage[k] for k in
                    ('input_tokens','output_tokens','total_tokens')
                    if type(observed_usage.get(k)) is int and observed_usage[k]>=0} or None)
            texts = []
            for item in data.get("output", []):
                if item.get("type") not in {"message", "reasoning"}:
                    raise SourceError("Agent unconfigured tool/output rejected")
                for part in item.get("content", []) if item.get("type") == "message" else []:
                    if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                        raise SourceError("Agent refusal or invalid output")
                    texts.append(part["text"])
            text = "".join(texts)
            if not text.strip() or len(text.encode()) > 24000:
                raise SourceError("Agent empty or excessive output")
            for secret in (self.key, token):
                if secret and secret in text:
                    raise SourceError("Agent credential echo rejected")
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result: raise ValueError("Duplicate output key")
                    result[key] = value
                return result
            result = json.loads(text, object_pairs_hook=unique,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            self.budget.remaining()
            version = data.get("model")
            version = version if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", version) else None
            if version and any(secret and secret in version for secret in (self.key, token)):
                version = None
            self.record_usage({"role": name, "status": "completed"})
            response_id = data.get("id")
            if (not isinstance(response_id, str)
                    or not re.fullmatch(r"resp_[A-Za-z0-9_-]{1,150}", response_id)
                    or any(secret and secret in response_id for secret in (self.key, token))):
                response_id = None
            supplied_usage = data.get("usage")
            usage = ({key: supplied_usage[key] for key in
                      ("input_tokens", "output_tokens", "total_tokens")
                      if type(supplied_usage.get(key)) is int and supplied_usage[key] >= 0}
                     if isinstance(supplied_usage, dict) else {})
            self.diagnostics.update(token_usage=usage or None,phase='parsed')
            return result, {"requested_model": self.model, "returned_model": version,
                            "version_authentication": "provider_declaration_only",
                            "response_id": response_id, "http_status": response.status_code,
                            "completion_status": data["status"], "usage": usage or None,
                            "cost": None}
        except httpx.TransportError as exc:
            self.diagnostics.fail(self.diagnostics.state['phase'],type(exc).__name__,'transport_error_remote_outcome_unknown')
            raise SourceError("Agent transport failed; remote outcome unknown; no automatic retry") from None
        except SourceError as exc:
            if str(exc) in {'ChatGPT stream ended without response.completed',
                            'ChatGPT stream interrupted before response.completed; no successful analysis'}:
                self.diagnostics.update(response_completed=False,root_cause='incomplete_response')
            self.diagnostics.fail(self.diagnostics.state['phase'],type(exc).__name__,
                self.diagnostics.state['root_cause'] or 'provider_or_completion_rejected')
            raise SourceError("Agent provider/completion rejected; no automatic retry") from None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.diagnostics.fail('response_validation',type(exc).__name__,'structured_response_invalid')
            raise SourceError("Agent response JSON/envelope invalid") from None

    def analyze(self, role, company, cutoff, question, evidence, previous, *, attempt_start=0):
        from analysis_workflow import digest
        error = None
        repair_output = None
        if attempt_start:
            old_error = json.loads((self.root / f'{role}-attempt-{attempt_start}-error.json').read_text())
            error = old_error['error']
            old_output = self.root / f'{role}-attempt-{attempt_start}-output.json'
            if old_output.exists():
                draft = json.loads(old_output.read_text())
                prefix = draft['output_text'].encode()[:12000].decode('utf-8', errors='ignore')
                repair_output = {'text': prefix, 'truncated': draft['output_truncated'] or len(prefix)<len(draft['output_text'])}
        for attempt in range(attempt_start+1, 3):
            inputs = prompt_for(role, company, cutoff, question, evidence, previous, error, repair_output)
            write_json(self.root / f"{role}-attempt-{attempt}-input.json", {
                "model": self.model, "prompt_version": PROMPT_VERSION,
                "role_prompt_version": ROLE_PROMPT_VERSIONS[role],"result_schema_version": RESULT_SCHEMA_VERSION,
                "input_sha256": digest(inputs),"input": inputs,
            })
            self.budget.model_attempt(role)
            self.fault_hook('before_request', role=role, attempt=attempt)
            retry_delay = 0.5
            phase = "response_envelope"
            output_path = None
            model_text = None
            started = time.monotonic()
            try:
                body = {"model": self.model, "store": False, "input": inputs,
                        "text": {"format": {"type": "json_schema", "name": "research_claims",
                                            "strict": True, "schema": MODEL_RESULT_SCHEMA}}}
                if self.plan_auth is not None:
                    body["stream"] = True
                    token = self.plan_auth.token()
                else:
                    body["max_output_tokens"] = 2200
                    token = self.key
                with self.http.stream(
                    "POST", "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {token}"}, json=body,
                    timeout=httpx.Timeout(min(180, self.budget.remaining()),
                                          connect=min(90, self.budget.remaining())),
                ) as response:
                    self.fault_hook('after_request', role=role, attempt=attempt)
                    if self.plan_auth is not None and response.status_code != 200:
                        raw = bytearray()
                        for chunk in response.iter_bytes():
                            self.budget.remaining()
                            if len(raw) + len(chunk) > 65536:
                                raise SourceError("ChatGPT error response exceeds local size limit")
                            raw.extend(chunk)
                        try:
                            diagnostic = json.loads(raw)
                        except ValueError:
                            diagnostic = {}
                        detail = diagnostic.get('error', {}) if isinstance(diagnostic,dict) else {}
                        permanent_quota = (isinstance(detail,dict) and detail.get('code') in {
                            'subscription_sharing_usage_limit_exceeded','insufficient_quota','billing_hard_limit_reached'})
                        if response.status_code not in {429,500,502,503,504} or permanent_quota:
                            self.record_usage({"role": role, "attempt": attempt, "status": "plan_error", "usage": None})
                            raise plan_error(response.status_code, diagnostic, response.headers.get("x-request-id"))
                    if response.status_code in {429, 500, 502, 503, 504}:
                        hint = response.headers.get("Retry-After")
                        if hint:
                            try:
                                retry_delay = float(hint)
                            except ValueError:
                                raise SourceError("Model requested a later retry") from None
                            if not 0 <= retry_delay <= 5:
                                raise SourceError("Model Retry-After exceeds local wait limit")
                        error = f"Transient model HTTP {response.status_code}"
                        self.record_usage({"role": role, "attempt": attempt, "status": "provider_error", "usage": None})
                        if attempt == 1:
                            write_json(self.root / f"{role}-attempt-{attempt}-error.json", {
                                "error":error,"phase":"provider_retry","output_file":None})
                            self.budget.pause(retry_delay)
                            continue
                        raise SourceError(error)
                    if response.status_code != 200:
                        self.record_usage({'role':role,'attempt':attempt,'status':'provider_error',
                                           'usage':None,'http_status':response.status_code})
                        raise SourceError(f"Model HTTP {response.status_code}; check local credentials / model access")
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if self.plan_auth is not None:
                        # Some live responses omit Content-Type while still carrying
                        # valid SSE. Only an absent header gets this compatibility
                        # path; the event decoder must still prove completion.
                        if content_type and content_type != "text/event-stream":
                            raise SourceError("ChatGPT response media type is not text/event-stream")
                        data = read_plan_response(response, self.budget)
                    else:
                        if content_type != "application/json":
                            raise SourceError("API response media type is not application/json")
                        raw = bytearray()
                        for chunk in response.iter_bytes():
                            self.budget.remaining()
                            if len(raw) + len(chunk) > 1024 * 1024:
                                raise SourceError("Model response exceeds size limit")
                            raw.extend(chunk)
                        data = json.loads(raw)
                self.record_usage({"role": role, "attempt": attempt, "response_id": data.get("id"),
                                   "usage": data.get("usage"), "status": data.get("status"),
                                   "response_content_type": content_type or None,
                                   "elapsed_seconds": round(time.monotonic() - started, 3),
                                   "stream_output_reconstructed": data.get("stream_output_reconstructed", False),
                                   "stream_header_missing": self.plan_auth is not None and not content_type})
                self.budget.remaining()
                self.fault_hook('after_response', role=role, attempt=attempt)
                if data.get("status") != "completed":
                    raise SourceError("Model response incomplete / failed; cannot publish as success")
                texts = []
                for item in data.get("output", []):
                    if item.get("type") not in {"message", "reasoning"}:
                        raise SourceError("Model returned an unconfigured output capability; no execution permitted")
                    if item.get("type") != "message":
                        continue
                    for part in item.get("content", []):
                        if part.get("type") == "refusal":
                            raise SourceError("Model refused this analysis")
                        if part.get("type") == "output_text":
                            if not isinstance(part.get("text"), str):
                                raise SourceError("Model output_text field is not text")
                            texts.append(part["text"])
                model_text = "".join(texts)
                output_path = self.root / f"{role}-attempt-{attempt}-output.json"
                encoded = model_text.encode()
                write_json(output_path, {
                    "model": self.model, "prompt_version": PROMPT_VERSION,
                    "response_id": data.get("id"), "evidence_kind": "unvalidated_model_output",
                    "output_text": encoded[:64000].decode("utf-8", errors="ignore"),
                    "output_truncated": len(encoded) > 64000,
                })
                phase = "analysis_json"
                result = json.loads(model_text)
                phase = "analysis_validation"
                validate_result(result, evidence)
                write_json(self.root / f"{role}.json", {
                    "role": role, "model": self.model, "prompt_version": PROMPT_VERSION,
                    "role_prompt_version": ROLE_PROMPT_VERSIONS[role],"result_schema_version": RESULT_SCHEMA_VERSION,
                    "input_sha256": digest(inputs),
                    "evidence_ids": [e["id"] for e in evidence], "result": result,
                })
                return result
            except SourceError as exc:
                message=str(exc)
                for secret in (self.key,locals().get('token')):
                    if secret: message=message.replace(secret,'[redacted]')
                if not any(u.get('role')==role and u.get('attempt')==attempt for u in self.usage):
                    self.record_usage({'role':role,'attempt':attempt,'status':'unconfirmed_error','usage':None,
                                       'elapsed_seconds':round(time.monotonic()-started,3)})
                write_json(self.root/f'{role}-attempt-{attempt}-error.json',{
                    'error':message[:500],'phase':phase,'output_file':output_path.name if output_path else None})
                raise
            except httpx.TransportError as exc:
                # Exception class is useful; exception text can expose request details.
                error = f"Model network / timeout ({type(exc).__name__}); whether the provider charged this call is unknown"
                self.record_usage({"role": role, "attempt": attempt, "status": "network_unknown", "usage": None,
                                   "transport_error_type": type(exc).__name__,
                                   "elapsed_seconds": round(time.monotonic() - started, 3)})
                if not getattr(self.budget, 'allow_uncertain_retry', True):
                    write_json(self.root / f'{role}-attempt-{attempt}-error.json', {
                        'error': error, 'phase': 'network_unknown', 'output_file': None})
                    raise SourceError(error) from None
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                if phase == "analysis_json" and isinstance(exc, json.JSONDecodeError):
                    error = f"Model output JSON syntax invalid at line {exc.lineno}, column {exc.colno}"
                elif phase == "analysis_validation" and isinstance(exc, ValueError):
                    error = f"Model output validation failed: {exc}"
                else:
                    error = f"Model response envelope invalid ({type(exc).__name__})"
                if model_text is not None:
                    prefix = model_text.encode()[:12000].decode("utf-8", errors="ignore")
                    repair_output = {"text": prefix, "truncated": len(prefix) < len(model_text)}
            write_json(self.root / f"{role}-attempt-{attempt}-error.json", {
                "error": error, "phase": phase,
                "output_file": output_path.name if output_path is not None else None,
            })
            if attempt == 1:
                self.budget.pause(retry_delay)
        raise SourceError(error or "Model validation / retry budget exhausted")
