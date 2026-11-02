"""One subscription-only request; save text/shape diagnostics, never credentials."""

import collections
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from chatgpt_auth import ChatGPTAuth
from chatgpt_stream import plan_error, read_plan_response
from research_runtime import Budget, utc_now
from sources.sec import SourceError, write_json


def main():
    root = Path(__file__).resolve().parents[1]
    destination = root / "data/live/diagnostics" / (sys.argv[1] if len(sys.argv) > 1 else "minimal-model.json")
    auth = ChatGPTAuth()
    model = auth.choose_model("gpt-6.1-sol")
    budget = Budget(seconds=120, max_calls=1)
    budget.model_attempt("smoke")
    started = time.monotonic()
    diagnostic = {"started_at": utc_now(), "billing_mode": "chatgpt_plan", "model": model,
                  "event_counts": {}, "text_events": [], "completion_shape": None}
    pending = b""
    counts = collections.Counter()

    class Observed(httpx.SyncByteStream):
        def __init__(self, source):
            self.source = source

        def __iter__(self):
            nonlocal pending
            for chunk in self.source.iter_bytes():
                pending += chunk
                pending = pending.replace(b"\r\n", b"\n")
                while b"\n\n" in pending:
                    block, pending = pending.split(b"\n\n", 1)
                    parts = [line[5:].lstrip() for line in block.split(b"\n") if line.startswith(b"data:")]
                    if not parts or b"\n".join(parts) == b"[DONE]":
                        continue
                    event = json.loads(b"\n".join(parts))
                    kind = event.get("type")
                    counts[kind] += 1
                    if kind in {"response.output_text.delta", "response.output_text.done"}:
                        diagnostic["text_events"].append({k: event[k] for k in
                            ("type", "output_index", "content_index", "delta", "text") if k in event})
                    if kind == "response.completed":
                        result = event["response"]
                        diagnostic["completion_shape"] = {"keys": list(result), "status": result.get("status"),
                            "output": result.get("output"), "usage": {k: result.get("usage", {}).get(k)
                            for k in ("input_tokens", "output_tokens", "total_tokens")}}
                yield chunk

    body = {"model": model, "store": False, "stream": True,
            "input": [{"role": "user", "content": 'Return only this JSON object: {"ok":true}'}],
            "text": {"format": {"type": "json_schema", "name": "smoke", "strict": True,
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                           "required": ["ok"], "additionalProperties": False}}}}
    try:
        with httpx.Client(follow_redirects=False) as client:
            with client.stream("POST", "https://api.openai.com/v1/responses",
                               headers={"Authorization": "Bearer " + auth.token()}, json=body,
                               timeout=min(90, budget.remaining())) as response:
                diagnostic.update(http_status=response.status_code, content_type=response.headers.get("content-type"))
                if response.status_code != 200:
                    raise plan_error(response.status_code, {}, response.headers.get("x-request-id"))
                observed = httpx.Response(200, headers=response.headers, stream=Observed(response))
                try:
                    result = read_plan_response(observed, budget)
                finally:
                    observed.close()
                text = "".join(p.get("text", "") for item in result.get("output", [])
                               if item.get("type") == "message" for p in item.get("content", [])
                               if p.get("type") == "output_text")
                diagnostic["parsed_text"] = text
                diagnostic["passed"] = json.loads(text) == {"ok": True}
    except (SourceError, ValueError, httpx.TransportError) as exc:
        diagnostic.update(passed=False, error_type=type(exc).__name__)
    finally:
        diagnostic.update(event_counts=dict(counts), elapsed_seconds=round(time.monotonic() - started, 2))
        write_json(destination, diagnostic)
    print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
    return 0 if diagnostic["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
