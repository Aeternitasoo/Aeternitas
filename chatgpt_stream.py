"""Bounded SSE decoding for official ChatGPT-plan Responses requests."""

import json
import re

from sources.sec import SourceError


class StreamText:
    """Retain bounded text when the terminal response omits its output items."""

    def __init__(self):
        self.parts = {}
        self.bytes = 0

    def put(self, output_index, content_index, text, *, delta=False):
        if any(type(i) is not int or not 0 <= i < 128 for i in (output_index, content_index)):
            raise SourceError("ChatGPT stream text indices are invalid")
        if not isinstance(text, str):
            raise SourceError("ChatGPT stream text must be text")
        key = (output_index, content_index)
        prior = self.parts.get(key)
        if delta:
            value = (prior or "") + text
        else:
            if prior is not None and prior != text:
                raise SourceError("ChatGPT stream text disagrees with completed text")
            value = text
        self.bytes += len(value.encode()) - len((prior or "").encode())
        if self.bytes > 24000:
            raise SourceError("ChatGPT output exceeds local text budget; request closed, usage may continue")
        self.parts[key] = value

    def observe(self, event):
        kind = event["type"]
        if ("_call" in kind or "function_call_arguments" in kind or
            (kind in {"response.output_item.added", "response.output_item.done"}
             and "call" in event.get("item", {}).get("type", ""))):
            raise SourceError("Model attempted an unconfigured tool; no execution permitted")
        if kind in {"response.refusal.delta", "response.refusal.done"}:
            raise SourceError("Model refused this analysis")
        if kind in {"response.output_text.delta", "response.output_text.done"}:
            delta = kind.endswith(".delta")
            self.put(event.get("output_index"), event.get("content_index"),
                     event.get("delta" if delta else "text"), delta=delta)
        elif kind == "response.content_part.done":
            part = event.get("part", {})
            if part.get("type") == "refusal":
                raise SourceError("Model refused this analysis")
            if part.get("type") == "output_text":
                self.put(event.get("output_index"), event.get("content_index"), part.get("text"))
        elif kind == "response.output_item.done":
            item = event.get("item", {})
            if item.get("type") == "message":
                for index, part in enumerate(item.get("content", [])):
                    if part.get("type") == "refusal":
                        raise SourceError("Model refused this analysis")
                    if part.get("type") == "output_text":
                        self.put(event.get("output_index"), index, part.get("text"))

    def complete(self, result):
        output = result.get("output", [])
        if not isinstance(output, list):
            raise SourceError("ChatGPT completed output must be a list")
        if any(item.get("type") not in {"message", "reasoning"} for item in output):
            raise SourceError("Model returned an unconfigured output capability; no execution permitted")
        # A populated terminal snapshot must agree with any streamed text.
        if output:
            terminal = {}
            for oi, item in enumerate(output):
                if item.get("type") == "message":
                    for ci, part in enumerate(item.get("content", [])):
                        if part.get("type") == "refusal":
                            raise SourceError("Model refused this analysis")
                        if part.get("type") == "output_text":
                            terminal[(oi, ci)] = part.get("text")
            if any(terminal.get(key) != text for key, text in self.parts.items()):
                raise SourceError("ChatGPT terminal output disagrees with streamed text")
            for (oi, ci), text in terminal.items():
                self.put(oi, ci, text)
        if not any(text.strip() for text in self.parts.values()):
            raise SourceError("ChatGPT completed without output text; no successful analysis")
        if not output:
            messages = {}
            for (oi, ci), text in sorted(self.parts.items()):
                messages.setdefault(oi, []).append({"type": "output_text", "text": text})
            result = {**result, "output": [{"type": "message", "role": "assistant", "content": parts}
                                           for parts in messages.values()]}
        return {**result, "stream_output_reconstructed": not bool(output)}


def plan_error(status, data, request_id=None):
    error = data.get("error", {}) if isinstance(data, dict) else {}
    if not isinstance(error, dict):
        error = {}
    code = error.get("code", "unknown")
    code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,120}", code) else "unknown"
    param = error.get("param")
    param = param if isinstance(param, str) and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,100}", param) else "unknown"
    rid = request_id if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,150}", request_id) else "unknown"
    shape = "detail" if isinstance(data, dict) and "detail" in data else "error" if error else "other"
    return SourceError(f"ChatGPT plan HTTP {status}; shape={shape}; code={code}; param={param}; request_id={rid}. "
                       "Check ChatGPT settings / Usage or login permission; no API-key fallback.")


def read_plan_response(response, budget):
    pending = b""
    received = 0
    text = StreamText()
    for chunk in response.iter_bytes():
        budget.remaining()
        received += len(chunk)
        if received > 2 * 1024 * 1024:
            raise SourceError("ChatGPT stream exceeds local byte budget; remote billing / usage may continue")
        pending += chunk
        pending = pending.replace(b"\r\n", b"\n")
        while b"\n\n" in pending:
            block, pending = pending.split(b"\n\n", 1)
            if len(block) > 131072:
                raise SourceError("ChatGPT stream event exceeds local size budget")
            parts = [line[5:].lstrip(b" ") for line in block.split(b"\n") if line.startswith(b"data:")]
            if not parts:
                continue
            payload = b"\n".join(parts)
            if payload == b"[DONE]":
                raise SourceError("ChatGPT stream ended without response.completed")
            try:
                data = json.loads(payload)
            except (ValueError, UnicodeError):
                raise SourceError("ChatGPT stream contains invalid event JSON") from None
            if not isinstance(data, dict):
                raise SourceError("ChatGPT stream event must be an object")
            kind = data.get("type")
            if not isinstance(kind, str) or not (kind.startswith("response.") or kind == "error"):
                raise SourceError("ChatGPT stream event type is invalid")
            text.observe(data)
            if kind in {"response.failed", "response.incomplete", "error"}:
                detail = data.get("response", data)
                raise plan_error("stream", detail, response.headers.get("x-request-id"))
            if kind == "response.completed":
                result = data.get("response")
                if not isinstance(result, dict) or result.get("status") != "completed":
                    raise SourceError("ChatGPT completion event is invalid")
                if len(json.dumps(result.get("output", []), ensure_ascii=False).encode()) > 64000:
                    raise SourceError("ChatGPT completed output exceeds local budget")
                budget.remaining()
                return text.complete(result)
        if len(pending) > 131072:
            raise SourceError("Unterminated ChatGPT stream event exceeds local size budget")
    raise SourceError("ChatGPT stream interrupted before response.completed; no successful analysis")
