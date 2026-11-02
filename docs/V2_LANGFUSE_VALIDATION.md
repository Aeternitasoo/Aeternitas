# V2 Stage 1: Langfuse remote acceptance

This stage preserves the production Recorder/Adapter and locked Langfuse 5.0.4.
No Memory policy, RSI candidate, authorization or license change is included.

## Acceptance path

The explicit smoke command uses the production Recorder → sanitize → Mapper →
official SDK path, with one synthetic infrastructure root, a child Generation,
and a deliberately failed synthetic tool span. The same local events remain
available even if export or readback fails.

The verifier checks server observation IDs, trace/parent relationships, types,
model, Prompt version, candidate/evaluation/fingerprint metadata, duration,
tool identity and error state. It requests core/basic/metadata/model/usage fields
from Observations API v2. Names or flush success alone cannot pass.

Generation usage 7/3/10 is a **synthetic transport fixture**. It is not actual model
usage. The model name is synthetic-no-model-call. Actual model requests are zero;
actual model tokens and monetary cost remain null. Do not mix these records into
research quality or cost statistics.

Readback is bounded to ten API queries, two seconds per query without SDK read
retries, with two seconds between queries. The entire child has a 45-second
execution boundary. Unobserved ingestion within that budget is not success.
Read attempts are appended before each query; no response IO or arbitrary server
metadata is saved. Shutdown is attempted even when reads fail.
The process boundary terminates hung SDK work; local records remain authoritative.

## Run explicitly

Configure these through the local environment or an existing private env file:
LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL (LANGFUSE_HOST alias
supported). Choose the actual project region or existing self-host endpoint.
No default cloud project, account creation, subscription or secret is supplied.
Do not paste credentials into chat or commit the file.

~~~bash
python -B scripts/check_agent_infrastructure.py \
  --env /absolute/private.env \
  --output-root /absolute/new-telemetry-checks
~~~

Each run creates a new UUID directory containing events.jsonl and
langfuse-status.json. Configured exports also save synthetic-events.jsonl and
readback-attempts.jsonl; neither contains credentials or research bodies.
A failed child leaves its completed local events and attempted read count.

Only server_readback_verified with remote_delivery_verified=true proves the
specific synthetic remote check. Inspect that trace in your own Langfuse console
using the returned trace_id; automated API verification is distinct from a
human console review. Missing credentials return Requires Credentials with no
remote request. Export/readback errors never trigger model requests.

## Current verification and boundaries

Offline tests cover metadata corruption, wrong parent/trace/type, missing end
time, wrong usage/model/version, lost ERROR state, ingestion lag, timeout,
shutdown and missing credentials. Existing actual SDK in-memory tests and
bounded-process Recorder fallback tests remain in place.

No credentials were configured during implementation. Remote ingestion,
readback and console inspection remain **Requires Credentials / Not Yet
Validated**. Mem0 semantics, memory ablation and RSI V2 are subsequent stages,
not completed by this patch.

Official references:
- [Python v4 to v5](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v4-to-v5)
- [Public API](https://langfuse.com/docs/api-and-data-platform/features/public-api)
- [Python SDK reference](https://python.reference.langfuse.com/langfuse)

These documents and installed SDK signatures were checked; no SDK upgrade was
performed. Asynchronous ingestion requires independent reads after flush.
