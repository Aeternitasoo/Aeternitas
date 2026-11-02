"""Local, content-free workflow events with an optional callable export sink.

An adapter receives an allowlisted event, never credentials, prompts,
source text or raw exception messages. Export failure cannot change research.
"""

from contextlib import contextmanager
from copy import deepcopy
import json
import re
import time
from uuid import uuid4

from research_runtime import utc_now


EVENT_VERSION = "research-events-v1"
FIELDS = {
    "symbol", "model", "prompt_version", "prompt_sha256", "input_fingerprint",
    "output_fingerprint", "config_fingerprint", "evidence_count", "source_count",
    "request_attempts", "response_id", "http_status", "completion_status",
    "input_tokens", "output_tokens", "total_tokens", "error_code", "error_type",
    "billing_mode", "status", "elapsed_seconds",
    "parent_run_id", "harness_version", "registry_version", "round_number",
    "tool_name", "call_id", "arguments_sha256", "tool_version", "cached",
    "skill_name", "skill_version", "skill_sha256",
    "candidate_version", "evaluation_id", "dataset_fingerprint", "memory_ids_sha256",
    "retry_count", "memory_count",
}


def error_code(exc):
    code = getattr(exc, 'code', None)
    if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_:]{0,120}', code):
        return code
    name = type(exc).__name__
    message = str(exc).lower()  # Classify locally; never persist this text.
    if "budget exhausted" in message:
        return "budget_exhausted"
    if "deadline" in message or "timeout" in name.lower():
        return "timeout_or_deadline"
    return "stage_failed"


class Recorder:
    def __init__(self, path, run_id, sink=None, context=None):
        self.path, self.run_id, self.sink = path, run_id, sink
        self.context = deepcopy(context or {})
        if set(self.context)-FIELDS:
            raise ValueError('Unapproved observability context')
        self.events, self.export_errors = [], 0
        self.stack = []
        self.sequence = 0

    def emit(self, name, span_id, parent_id, phase, **fields):
        fields={**self.context,**fields}
        fields={k:(re.sub(r'(?i)(Bearer\s+\S+|sk-[a-z0-9_-]{20,}|eyJ[a-zA-Z0-9_-]{20,}\.)','[redacted]',v)
                   if isinstance(v,str) else v) for k,v in fields.items()}
        if set(fields) - FIELDS:
            raise ValueError("Unapproved observability fields")
        self.sequence += 1
        event = {"schema_version": EVENT_VERSION, "trace_id": self.run_id,
                 "sequence": self.sequence, "span_id": span_id,
                 "parent_span_id": parent_id, "name": name, "phase": phase,
                 "at": utc_now(), **fields}
        # Events are append-only in an exclusively created run directory.
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        self.events.append(event)
        if self.sink:
            try:
                self.sink(deepcopy(event))
            except Exception:
                self.export_errors += 1  # Never persist sink exception payloads.

    @contextmanager
    def span(self, name, **fields):
        span_id = uuid4().hex
        parent_id = self.stack[-1] if self.stack else None
        self.emit(name, span_id, parent_id, "start", **fields)
        self.stack.append(span_id)
        started = time.monotonic()
        result = {}
        try:
            yield result
        except Exception as exc:
            # Error types are local class names, not arbitrary provider messages.
            result.update(status="failed", error_type=type(exc).__name__, error_code=error_code(exc))
            raise
        finally:
            self.stack.pop()
            # Duration is a locally measured value, not provider latency.
            result.setdefault("status", "completed")
            self.emit(name, span_id, parent_id, "end", **{
                **fields, **result, "elapsed_seconds": max(0, time.monotonic() - started)})
