"""Fixed local M5 measurement: real isolated PostgreSQL, synthetic providers.

No .env, OAuth store or local API token is read. The generated local API token
is confined to this process. Only the optional, already saved public source
bundle is read from data/live. No production table is modified.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import secrets
import socket
import statistics
import sys
import threading
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
import uvicorn

import evidence_repository as domain
import run_repository as tasks
from local_api import create_app
from research_pipeline import audit_inputs
from research_analysis import ROLE_TASKS
from research_runtime import utc_now
from research_service import ResearchService
from research_test_support import CONFIG, transports
from sources.sec import write_json


PROTOCOL = {
    "version": "m5-fixed-local-load-v2",
    "revision_reason": "v1 incorrectly required zero instantaneous idle-in-transaction, including normal pauses between short SQL statements. v2 preserves that raw peak and checks idle lasting over 100ms plus zero current-worker DB connections at model entry.",
    "scope": "Real PostgreSQL and loopback HTTP; synthetic source/model transports",
    "runs": 10,
    "worker_concurrency": 2,
    "query_requests": 100,
    "query_concurrency": 2,
    "query_mix": ["list", "status", "report_json"],
    "fixed_model_delay_seconds": 0.35,
    "run_deadline_seconds": 60,
    "conditions": {
        "all_ten_runs_complete_once": True,
        "normal_model_calls_per_run": 4,
        "maximum_model_calls_per_run": 8,
        "normal_attempt_ledger_complete": True,
        "maximum_parallel_workers": 2,
        "maximum_parallel_model_calls": 2,
        "all_100_queries_overlap_running_work": True,
        "query_errors": 0,
        "unexpected_http_5xx": 0,
        "query_p95_seconds_strictly_less_than": 1.0,
        "application_connection_peak_at_most": 8,
        "idle_in_transaction_over_100ms_peak": 0,
        "worker_connections_at_model_entry": 0,
        "external_model_requests": 0,
    },
    "explain_rows": 20000,
    "explain_distribution": "4 percent AAPL and 96 percent MSFT failed background rows; selective ticker/status listing. The v1 all-AAPL comparison is preserved separately.",
    "explain_acceptance": "Same actual listing SQL before/after index; no fabricated speedup requirement",
    "real_source_competition": "Saved live source bytes audited and imported into the isolated schema; no new HTTP",
    "limitations": [
        "The two workers are controlled executor threads using the production claim/service/pipeline functions.",
        "Synthetic model delay and usage do not estimate provider throughput, quality or billing.",
        "No internet scale SLA or external-call exactly-once guarantee is implied.",
        "Connection peaks are sampled from pg_stat_activity and also counted at connection acquisition.",
    ],
}


def distribution(values):
    """Nearest-rank percentiles; retain count/min/max for small samples."""
    values = sorted(values)
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
    return {"count": len(values), "min": round(values[0], 6),
            "p50": round(statistics.median(values), 6),
            "p95": round(values[max(0, math.ceil(len(values) * .95) - 1)], 6),
            "max": round(values[-1], 6)}


class Meter:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = {"workers": 0, "models": 0, "connections": 0}
        self.peak = dict(self.active)
        self.model_calls = []
        self.source_calls = []
        self.question_to_run = {}
        self.denied_http = []
        self.connections_by_thread = {}
        self.run_seconds = []
        self.run_sources = {}
        self.worker_errors = []
        self.queries = []
        self.db_samples = []
        self.long_idle_samples = []
        self.stop = threading.Event()

    @contextmanager
    def acquire(self, kind):
        with self.lock:
            self.active[kind] += 1
            self.peak[kind] = max(self.peak[kind], self.active[kind])
            if kind == "connections":
                thread = threading.get_ident()
                self.connections_by_thread[thread] = self.connections_by_thread.get(thread, 0) + 1
        try:
            yield
        finally:
            with self.lock:
                self.active[kind] -= 1
                if kind == "connections":
                    self.connections_by_thread[thread] -= 1


@contextmanager
def isolated_connections(dsn, schema, app_name, meter):
    """Route every domain connection, including pipeline imports, to our schema.

    This is explicit test instrumentation, not an alternate production DB
    implementation. SQL, transactions, constraints and PostgreSQL are real.
    """
    original = domain.connect

    @contextmanager
    def instrumented(request_dsn=domain.DEFAULT_DATABASE, schema="public"):
        if schema not in {"public", isolated_schema}:
            raise ValueError("Measurement attempted another database schema")
        with meter.acquire("connections"), original(dsn, schema=isolated_schema) as connection:
            connection.execute("SELECT set_config('application_name',%s,false)", (app_name,))
            yield connection

    isolated_schema = schema
    domain.connect = instrumented
    try:
        yield
    finally:
        domain.connect = original


@contextmanager
def loopback_http_only(meter):
    """A missed synthetic transport must fail before any external request."""
    original = httpx.HTTPTransport.handle_request

    def guarded(transport, request):
        if request.url.host not in {"127.0.0.1", "localhost", "::1"}:
            with meter.lock:
                meter.denied_http.append({"method": request.method, "host": request.url.host})
            raise RuntimeError("External HTTP is disabled in the measurement process")
        return original(transport, request)

    httpx.HTTPTransport.handle_request = guarded
    try:
        yield
    finally:
        httpx.HTTPTransport.handle_request = original


@contextmanager
def local_http(service, token):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(create_app(service, token=token), log_level="error",
                                          access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        until = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > until:
                raise RuntimeError("Controlled local API failed to start")
            time.sleep(.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("Controlled local API failed to stop")


def client(url, token):
    return httpx.Client(base_url=url, trust_env=False, timeout=15,
                        headers={"X-Aeternitas-Token": token})


def require(response, status=200):
    if response.status_code != status:
        raise AssertionError(f"Controlled HTTP response was {response.status_code}, expected {status}")
    return response.json()


def controlled_transports(meter):
    transport, calls = transports()
    original = transport["model"]

    def delayed(request):
        payload = json.loads(request.content)
        question = json.loads(payload["input"][1]["content"])["question"]
        role = next(role for role, task in ROLE_TASKS.items() if task in payload["input"][0]["content"])
        with meter.lock:
            rid = meter.question_to_run[question]
            worker_connections = meter.connections_by_thread.get(threading.get_ident(), 0)
        started = time.monotonic()
        with meter.acquire("models"):
            time.sleep(PROTOCOL["fixed_model_delay_seconds"])
            result = original.handle_request(request)
        with meter.lock:
            meter.model_calls.append({"run_id": rid, "role": role,
                                      "worker_connections_at_model_entry": worker_connections,
                                      "seconds": time.monotonic() - started})
        return result

    transport["model"] = httpx.MockTransport(delayed)
    for name in ("sec", "web"):
        source_handler = transport[name]

        def source(request, handler=source_handler, source_kind=name):
            started = time.monotonic()
            response = handler.handle_request(request)
            with meter.lock:
                meter.source_calls.append({"kind": source_kind, "host": request.url.host,
                                           "seconds": time.monotonic() - started})
            return response

        transport[name] = httpx.MockTransport(source)
    return transport, calls


def sample_database(dsn, app_name, meter):
    try:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("SELECT set_config('application_name',%s,false)", (app_name + "_monitor",))
            while not meter.stop.is_set():
                # Explicitly refresh activity snapshots. A polling monitor must
                # not age an earlier observation as if that session remained
                # idle continuously.
                connection.execute("SELECT pg_stat_clear_snapshot()")
                sessions = connection.execute("""SELECT pid,state,
                  extract(epoch FROM clock_timestamp()-state_change)::float8,
                  wait_event_type,wait_event,left(query,120)
                  FROM pg_stat_activity WHERE datname=current_database() AND application_name=%s""",
                                              (app_name,)).fetchall()
                row = (len(sessions), sum(s[1] == 'idle in transaction' for s in sessions),
                       sum(s[1] == 'active' for s in sessions),
                       sum(s[1] == 'idle in transaction' and s[2] > .1 for s in sessions))
                with meter.lock:
                    meter.db_samples.append({"connections": row[0], "idle_in_transaction": row[1],
                                             "active": row[2], "idle_over_100ms": row[3]})
                    if row[3] and len(meter.long_idle_samples) < 30:
                        meter.long_idle_samples.extend({"pid":s[0], "idle_seconds":round(s[2],6),
                            "wait_type":s[3], "wait_event":s[4], "controlled_sql_prefix":s[5]}
                            for s in sessions if s[1] == 'idle in transaction' and s[2] > .1)
                meter.stop.wait(.02)
    except Exception as exc:
        with meter.lock:
            meter.worker_errors.append({"phase": "database_monitor", "type": type(exc).__name__})


def run_load(service, dsn, app_name, meter, url, token):
    ids = []
    with client(url, token) as http:
        for number in range(PROTOCOL["runs"]):
            question = f"Synthetic fixed measurement run {number}; not investment research"
            row = require(http.post("/research-runs", json={
                "ticker": "AAPL", "as_of_date": "2025-10-01",
                "question": question,
                "refresh": True, "news_days": 14, "deadline": PROTOCOL["run_deadline_seconds"]},
                headers={"Idempotency-Key": f"measurement-{number}"}), 202)
            ids.append(row["run_id"])
            meter.question_to_run[question] = row["run_id"]

    monitor = threading.Thread(target=sample_database, args=(dsn, app_name, meter), daemon=True)
    monitor.start()

    def worker(number):
        try:
            while True:
                with service.connection() as connection:
                    task = tasks.claim(connection, service.root, f"measurement-worker-{number}")
                    if connection.info.transaction_status.name != "IDLE":
                        raise AssertionError("Claim left a transaction open")
                if task is None:
                    return
                started = time.monotonic()
                with meter.acquire("workers"):
                    service.execute(task)
                with meter.lock:
                    meter.run_seconds.append(time.monotonic() - started)
        except Exception as exc:
            with meter.lock:
                meter.worker_errors.append({"phase": "worker", "type": type(exc).__name__})

    try:
        with ThreadPoolExecutor(max_workers=PROTOCOL["worker_concurrency"]) as workers:
            futures = [workers.submit(worker, number) for number in range(2)]
            until = time.monotonic() + 90
            report_id = None
            with client(url, token) as http:
                while report_id is None:
                    rows = require(http.get("/research-runs"))["items"]
                    report_id = next((row["run_id"] for row in rows
                                      if row["status"] in {"completed", "completed_with_gaps"}), None)
                    if all(f.done() for f in futures) or time.monotonic() > until:
                        if report_id is None:
                            raise AssertionError("No controlled report became queryable")
                        break
                    time.sleep(.02)

            def query(number):
                with client(url, token) as http:
                    for offset in range(number, PROTOCOL["query_requests"], 2):
                        kind = PROTOCOL["query_mix"][offset % 3]
                        path = ("/research-runs" if kind == "list" else
                                f"/research-runs/{ids[offset % len(ids)]}" if kind == "status" else
                                f"/research-runs/{report_id}/report.json")
                        with meter.lock:
                            active_start = meter.active["workers"]
                        started = time.monotonic()
                        status, error = None, None
                        try:
                            response = http.get(path)
                            status = response.status_code
                            require(response)
                        except Exception as exc:
                            error = type(exc).__name__
                        with meter.lock:
                            meter.queries.append({"index": offset, "kind": kind, "status": status,
                                "error": error, "seconds": time.monotonic() - started,
                                "active_workers_start": active_start,
                                "active_workers_end": meter.active["workers"]})

            with ThreadPoolExecutor(max_workers=2) as queries:
                list(queries.map(query, range(2)))
            for future in futures:
                future.result(timeout=120)
        with client(url, token) as http:
            final = [require(http.get(f"/research-runs/{rid}")) for rid in ids]
    finally:
        meter.stop.set()
        monitor.join(5)
    return final


def real_source_competition(service, source_root, run_id):
    """Frozen live bytes, real domain import, two independent DB claim sessions."""
    if not run_id or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise ValueError("Real source run ID must be lowercase hexadecimal")
    source_root = Path(source_root).resolve()
    bundle = json.loads((source_root / "runs" / run_id / "input_bundle.json").read_text())
    if bundle.get("evidence_kind") != "live_api":
        raise ValueError("Competition requires previously saved live source bytes")
    audit_inputs(source_root, bundle)
    with service.connection() as connection:
        dataset = domain.import_bundle(connection, source_root, bundle)
    ids = []
    for number in range(2):
        row = service.create(ticker=bundle["ticker"], as_of_date=bundle["as_of_date"],
            question=f"Frozen live evidence claim competition {number}; do not execute",
            refresh=False, news_days=14, deadline=60, idempotency_key=f"source-competition-{number}")
        ids.append(row["run_id"])
    with service.connection() as connection:
        connection.execute("UPDATE research_runs SET dataset_id=%s WHERE run_id=ANY(%s)", (dataset, ids))
    barrier = threading.Barrier(2)

    def claim(number):
        with service.connection() as connection:
            pid = connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            barrier.wait(timeout=10)
            started = time.monotonic()
            task = tasks.claim(connection, service.root, f"live-source-session-{number}")
            return {"session": number, "backend_pid": pid, "run_id": task["run_id"] if task else None,
                    "seconds": round(time.monotonic() - started, 6),
                    "transaction_after_claim": connection.info.transaction_status.name}

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(claim, range(2)))
    if {row["run_id"] for row in claimed} != set(ids):
        raise AssertionError("Concurrent sessions duplicated or lost a queued run")
    if any(row["transaction_after_claim"] != "IDLE" for row in claimed):
        raise AssertionError("Competition left a transaction open")
    return {"saved_live_run_id": run_id, "dataset_id": dataset, "source_count": len(bundle["sources"]),
            "fact_count": sum(f is not None for p in bundle["financials"]["annual_periods"]
                              for f in p["facts"].values()), "sessions": claimed,
            "distinct_claims": True, "new_source_http_requests": 0, "model_requests": 0,
            "scope": "Frozen live source import plus actual claim race; analysis is not re-run"}


def explain_listing(service, rows):
    """Use the production listing SELECT and parameters in an isolated schema."""
    with service.connection() as connection:
        connection.execute("""INSERT INTO research_runs(run_id,ticker,cik,as_of_date,question,options,
          data_root,billing_mode,model,status,step,created_at,finished_at)
          SELECT md5('m5-explain-'||n::text),CASE WHEN n%%25=0 THEN 'AAPL' ELSE 'MSFT' END,
          CASE WHEN n%%25=0 THEN '0000320193' ELSE '0000789019' END,'2025-10-01',
          'Synthetic EXPLAIN background row','{}'::jsonb,%s,'api_key','synthetic-test-model',
          'failed','financials',now()-n*interval '1 second',now()
          FROM generate_series(1,%s) AS n""", (str(service.root), rows))
        connection.execute("ANALYZE research_runs")
        indexes = connection.execute("""SELECT indexname,indexdef FROM pg_indexes
          WHERE schemaname=current_schema() AND tablename='research_runs'""").fetchall()
        candidates = [row for row in indexes
                      if "(data_root, ticker, status, created_at" in row["indexdef"]]
        # The measurement refuses to silently alter unrelated indexes. A local
        # comparison index is used only when the migration has none yet.
        for row in candidates:
            connection.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(row["indexname"])))
        captured = {}
        class Capture:
            def execute(self, query, parameters):
                captured.update(query=query, parameters=parameters)
                return connection.execute(query, parameters)
        tasks.listing(Capture(), root=service.root, ticker="AAPL", status="failed", limit=50, offset=0)
        query, params = captured["query"], captured["parameters"]

        def explain():
            return connection.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params).fetchone()["QUERY PLAN"][0]

        before = explain()
        if candidates:
            for row in candidates:
                connection.execute(row["indexdef"])
            index = [{"name": row["indexname"], "definition": row["indexdef"]} for row in candidates]
        else:
            connection.execute("CREATE INDEX m5_measure_listing ON research_runs(data_root,ticker,status,created_at DESC,run_id DESC)")
            index = [{"name": "m5_measure_listing", "definition":
                      "CREATE INDEX m5_measure_listing ON research_runs(data_root,ticker,status,created_at DESC,run_id DESC)",
                      "note": "Isolated comparison index; migration index absent"}]
        after = explain()
        return {"rows_added": rows, "actual_listing_sql": query, "parameters": params,
                "indexes_compared": index, "without_index": before, "with_index": after,
                "explanation": "Equality filters plus descending pagination can use the compound index; plans/times are recorded without forcing planner choices."}


def conditions(final, meter):
    calls = {row["run_id"]: sum(c["run_id"] == row["run_id"] for c in meter.model_calls) for row in final}
    latency = distribution([row["seconds"] for row in meter.queries])
    return {
        "all_ten_runs_complete_once": len(final) == 10 and len({r["run_id"] for r in final}) == 10
            and all(r["status"] in {"completed", "completed_with_gaps"} for r in final),
        "normal_model_calls_per_run": all(calls[r["run_id"]] == r["model_attempts"] == 4 for r in final),
        "maximum_model_calls_per_run": all(r["model_attempts"] <= 8 for r in final),
        "normal_attempt_ledger_complete": all(
            len(r.get("attempts", [])) == 4 and
            {a["role"] for a in r["attempts"]} == set(ROLE_TASKS) and
            all(a["status"] == "completed" and a["role_attempt"] == 1 for a in r["attempts"])
            for r in final),
        "maximum_parallel_workers": meter.peak["workers"] <= 2,
        "maximum_parallel_model_calls": meter.peak["models"] <= 2,
        "all_100_queries_overlap_running_work": len(meter.queries) == 100 and
            all(r["active_workers_start"] > 0 and r["active_workers_end"] > 0 for r in meter.queries),
        "query_errors": not meter.worker_errors and not any(r["error"] for r in meter.queries),
        "unexpected_http_5xx": not any(r["status"] is not None and r["status"] >= 500 for r in meter.queries),
        "query_p95_seconds_strictly_less_than": latency["p95"] is not None and latency["p95"] < 1,
        "application_connection_peak_at_most": meter.peak["connections"] <= 8 and
            max((s["connections"] for s in meter.db_samples), default=0) <= 8,
        "idle_in_transaction_over_100ms_peak": bool(meter.db_samples) and
            max(s["idle_over_100ms"] for s in meter.db_samples) == 0,
        "worker_connections_at_model_entry": all(c.get("worker_connections_at_model_entry", 1) == 0
                                                 for c in meter.model_calls),
        "external_model_requests": not meter.denied_http,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=os.environ.get("AETERNITAS_TEST_DATABASE", domain.DEFAULT_DATABASE),
                        help="Explicit local test DSN; never read from the project .env")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data/live/m5/measurement.json")
    parser.add_argument("--real-source-run", default="6e5d6cd6990c47e29a9a963949209772")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1] / "data/live")
    args = parser.parse_args(argv)
    connection_options = conninfo_to_dict(args.database)
    host = connection_options.get("host", "")
    if connection_options.get("service") or connection_options.get("hostaddr", "127.0.0.1") not in {"127.0.0.1", "::1"} or (host and host not in {"localhost", "127.0.0.1", "::1"} and not host.startswith("/")):
        raise ValueError("Measurement requires an explicit local PostgreSQL destination")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    protocol_path = output.with_suffix(".protocol.json")
    if output.exists():
        previous = json.loads(output.read_text())
        suffix = previous.get("started_at", utc_now()).replace(":", "").replace(".", "")
        write_json(output.with_name(output.stem + "-previous-" + suffix + ".json"), previous)
        if protocol_path.exists():
            write_json(protocol_path.with_name(protocol_path.stem + "-previous-" + suffix + ".json"),
                       json.loads(protocol_path.read_text()))
    protocol = {**PROTOCOL, "declared_at": utc_now(), "environment": {
        "python": platform.python_version(), "system": platform.system(), "release": platform.release(),
        "architecture": platform.machine(), "cpu_count": os.cpu_count(), "postgresql": "captured at connection"}}
    write_json(protocol_path, protocol)
    record = {"schema_version": "m5-measurement-v1", "started_at": utc_now(), "status": "running",
              "protocol_path": str(protocol_path), "protocol_sha256": hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
              "external_model_requests": 0, "provider_cost": "not applicable; all provider transports synthetic"}
    project = Path(__file__).resolve().parents[1]
    measured_files = ["scripts/measure_m5.py", "recoverable_pipeline.py", "run_repository.py", "research_service.py",
                      "research_analysis.py", "research_pipeline.py", "local_api.py", "evidence_repository.py"]
    record["code_sha256_at_start"] = {name:hashlib.sha256((project / name).read_bytes()).hexdigest()
                                      for name in measured_files}
    write_json(output, record)
    schema = "m5_measure_" + uuid4().hex
    app_name = schema
    data_dir = output.parent / ("controlled-data-" + uuid4().hex)
    data_dir.mkdir(mode=0o700)
    meter = Meter()
    created = False
    started = time.monotonic()
    try:
        with psycopg.connect(args.database, autocommit=True) as connection:
            record["postgresql_version"] = connection.execute("SHOW server_version").fetchone()[0]
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            created = True
        with isolated_connections(args.database, schema, app_name, meter), loopback_http_only(meter):
            with domain.connect(args.database, schema=schema) as connection:
                domain.migrate(connection)
            transport, calls = controlled_transports(meter)
            service = ResearchService(data_dir, {**CONFIG, "DATABASE_URL": args.database}, schema=schema,
                                      transports=transport)
            token = secrets.token_urlsafe(32)
            with (output.parent / "measurement-worker.log").open("w") as log, redirect_stdout(log):
                with local_http(service, token) as url:
                    final = run_load(service, args.database, app_name, meter, url, token)
            # Freeze load peaks before the separate race/EXPLAIN experiments.
            checks = conditions(final, meter)
            record.update(checks=checks, load_elapsed_seconds=round(time.monotonic() - started, 6),
                runs=[{k:r.get(k) for k in ("run_id", "status", "step", "model_attempts", "dataset_id", "model_usage", "attempts", "steps", "workflow_config_fingerprint")} for r in final],
                query_seconds=distribution([q["seconds"] for q in meter.queries]),
                query_seconds_by_kind={k:distribution([q["seconds"] for q in meter.queries if q["kind"] == k]) for k in PROTOCOL["query_mix"]},
                queries=sorted(meter.queries, key=lambda q:q["index"]),
                worker_errors=meter.worker_errors, run_seconds=distribution(meter.run_seconds),
                model_call_seconds=distribution([c["seconds"] for c in meter.model_calls]), model_calls=meter.model_calls,
                source_call_seconds=distribution([c["seconds"] for c in meter.source_calls]),
                provider_calls={"sec":len(calls["sec"]), "web":len(calls["web"]), "model":len(calls["model"])},
                denied_external_http=meter.denied_http, observed_peak=dict(meter.peak),
                pg_stat_activity={"samples":len(meter.db_samples),
                    "long_idle_samples":meter.long_idle_samples,
                    **{f"peak_{key}":max((s[key] for s in meter.db_samples), default=0)
                       for key in ("connections", "idle_in_transaction", "active", "idle_over_100ms")}})
            write_json(output, record)
            record["real_source_claim_competition"] = real_source_competition(service, args.source_root, args.real_source_run)
            record["listing_explain"] = explain_listing(service, PROTOCOL["explain_rows"])
            record["status"] = "passed" if all(checks.values()) else "failed"
    except Exception as exc:
        record.update(status="failed", failure_type=type(exc).__name__)
    finally:
        meter.stop.set()
        removed = False
        if created:
            try:
                with psycopg.connect(args.database, autocommit=True) as connection:
                    connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                removed = True
            except Exception as exc:
                record.update(status="failed", cleanup_failure_type=type(exc).__name__)
        record.update(finished_at=utc_now(), elapsed_seconds=round(time.monotonic() - started, 6),
                      isolated_schema=schema, isolated_schema_removed=removed,
                      controlled_artifacts=str(data_dir), production_tables_modified=False)
        write_json(output, record)
    print(f"M5 controlled measurement {record['status']}: {output}")
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
