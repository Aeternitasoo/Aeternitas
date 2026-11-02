"""The measurement must reject breaches instead of reporting a false pass."""

import copy

import httpx
import pytest

from research_analysis import ROLES
from scripts.measure_m5 import Meter, conditions, distribution, loopback_http_only


def observations():
    meter = Meter()
    rows = [{"run_id": str(n), "status": "completed_with_gaps", "model_attempts": 4,
             "attempts": [{"role": role, "role_attempt": 1, "status": "completed"} for role in ROLES]}
            for n in range(10)]
    meter.model_calls = [{"run_id": row["run_id"], "role": role, "worker_connections_at_model_entry": 0}
                         for row in rows for role in ROLES]
    meter.queries = [{"seconds": .01, "active_workers_start": 2, "active_workers_end": 2,
                      "error": None, "status": 200} for _ in range(100)]
    meter.peak = {"workers": 2, "models": 2, "connections": 6}
    meter.db_samples = [{"connections": 5, "idle_in_transaction": 1, "idle_over_100ms": 0, "active": 2}]
    return rows, meter


def test_nearest_rank_latency_and_empty_measurements():
    assert distribution([]) == {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
    assert distribution(list(range(1, 101))) == {"count": 100, "min": 1, "p50": 50.5, "p95": 95, "max": 100}


@pytest.mark.parametrize("breach,check", [
    ("unfinished", "all_ten_runs_complete_once"), ("duplicate", "all_ten_runs_complete_once"),
    ("budget", "maximum_model_calls_per_run"), ("ledger", "normal_attempt_ledger_complete"),
    ("late_query", "all_100_queries_overlap_running_work"),
    ("slow_query", "query_p95_seconds_strictly_less_than"),
    ("connections", "application_connection_peak_at_most"),
    ("long_transaction", "idle_in_transaction_over_100ms_peak"),
    ("network_transaction", "worker_connections_at_model_entry"),
    ("external", "external_model_requests"),
])
def test_measurement_rejects_predeclared_limit_breaches(breach, check):
    rows, meter = observations()
    assert all(conditions(rows, meter).values())
    if breach == "unfinished": rows[0]["status"] = "failed"
    elif breach == "duplicate": rows[0] = copy.deepcopy(rows[1])
    elif breach == "budget": rows[0]["model_attempts"] = 9
    elif breach == "ledger": rows[0]["attempts"][0]["status"] = "unknown"
    elif breach == "late_query": meter.queries[0]["active_workers_end"] = 0
    elif breach == "slow_query":
        for query in meter.queries: query["seconds"] = 1.001
    elif breach == "connections": meter.peak["connections"] = 9
    elif breach == "long_transaction": meter.db_samples[0]["idle_over_100ms"] = 1
    elif breach == "network_transaction": meter.model_calls[0]["worker_connections_at_model_entry"] = 1
    elif breach == "external": meter.denied_http.append({"host": "example.invalid"})
    assert conditions(rows, meter)[check] is False


def test_missing_mock_is_blocked_before_external_http(monkeypatch):
    reached = []
    def original(self, request):
        reached.append(request.url.host)
        return httpx.Response(200)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", original)
    meter = Meter()
    transport = httpx.HTTPTransport()
    try:
        with loopback_http_only(meter):
            with pytest.raises(RuntimeError, match="External HTTP"):
                transport.handle_request(httpx.Request("GET", "https://example.invalid/?secret=not-recorded"))
            transport.handle_request(httpx.Request("GET", "http://127.0.0.1:1234/health"))
        assert reached == ["127.0.0.1"]
        assert meter.denied_http == [{"method": "GET", "host": "example.invalid"}]
        assert httpx.HTTPTransport.handle_request is original
    finally:
        transport.close()
