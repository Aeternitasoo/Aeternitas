"""Transport-controlled source tests; these are NOT live SEC access evidence."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sources.sec import SecClient, SourceError
from test_financial_facts import make_facts


def client(tmp_path, handler, **kwargs):
    return SecClient(tmp_path, "test@example.com", transport=httpx.MockTransport(handler), **kwargs)


def test_cache_refresh_and_changed_response_keep_old_snapshot(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.scheme == "https" and request.url.host == "data.sec.gov"
        assert request.headers["User-Agent"].endswith("test@example.com")
        raw = make_facts()
        raw["entityName"] += str(len(requests))
        return httpx.Response(200, json=raw)

    with client(tmp_path, handler) as source:
        first = source.companyfacts("320193")
        cached = source.companyfacts("320193")
        refreshed = source.companyfacts("320193", refresh=True)
    assert len(requests) == 2
    assert not first.cache_hit and cached.cache_hit and not refreshed.cache_hit
    assert cached.metadata["retrieval_id"] == first.metadata["retrieval_id"]
    assert refreshed.metadata["retrieval_id"] != first.metadata["retrieval_id"]
    assert refreshed.metadata["sha256"] != first.metadata["sha256"]
    assert (tmp_path / first.metadata["snapshot_path"]).exists()
    assert len(list((tmp_path / "retrievals").glob("*.json"))) == 2
    assert "test@example.com" not in json.dumps(first.metadata)


def test_unchanged_refresh_still_records_new_request(tmp_path):
    with client(tmp_path, lambda request: httpx.Response(200, json=make_facts())) as source:
        first = source.companyfacts(320193)
        second = source.companyfacts(320193, refresh=True)
    assert first.metadata["sha256"] == second.metadata["sha256"]
    assert first.metadata["retrieval_id"] != second.metadata["retrieval_id"]
    assert second.metadata["forced_refresh"] is True


def test_failed_refresh_does_not_replace_good_cache_or_serve_it_as_live(tmp_path):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=make_facts()) if count == 1 else httpx.Response(403)

    with client(tmp_path, handler) as source:
        first = source.companyfacts(320193)
        with pytest.raises(SourceError, match="403"):
            source.companyfacts(320193, refresh=True)
        cached = source.companyfacts(320193)
    assert count == 2
    assert cached.cache_hit
    assert cached.metadata["retrieval_id"] == first.metadata["retrieval_id"]


def test_expired_cache_fetches_again_and_failure_does_not_serve_stale(tmp_path):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=make_facts()) if count == 1 else httpx.Response(403)

    with client(tmp_path, handler) as source:
        first = source.companyfacts(320193)
        receipt = tmp_path / "retrievals" / f"{first.metadata['retrieval_id']}.json"
        metadata = json.loads(receipt.read_text())
        metadata["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        receipt.write_text(json.dumps(metadata))
        with pytest.raises(SourceError, match="403"):
            source.companyfacts(320193)
    assert count == 2
    assert (tmp_path / first.metadata["snapshot_path"]).exists()


def test_corrupt_cache_is_detected_not_silently_accepted(tmp_path):
    with client(tmp_path, lambda request: httpx.Response(200, json=make_facts())) as source:
        saved = source.companyfacts(320193)
        (tmp_path / saved.metadata["snapshot_path"]).write_bytes(b"tampered")
        with pytest.raises(SourceError, match="hash"):
            source.companyfacts(320193)


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}),
    httpx.Response(200, text="<html>blocked</html>"),
    httpx.Response(200, json=make_facts(789019)),
])
def test_redirect_html_or_wrong_company_never_publish_success(tmp_path, response):
    requests = []

    def handler(request):
        requests.append(request)
        return response

    with client(tmp_path, handler) as source:
        with pytest.raises(SourceError):
            source.companyfacts(320193)
    assert len(requests) == 1
    assert not list(tmp_path.rglob("*.json"))


def test_response_size_is_limited(tmp_path):
    with client(tmp_path, lambda request: httpx.Response(200, json=make_facts()), max_bytes=20) as source:
        with pytest.raises(SourceError, match="byte limit"):
            source.companyfacts(320193)
    assert not list(tmp_path.rglob("*.json"))


def test_transient_failure_is_retried_but_attempts_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("sources.sec.time.sleep", lambda seconds: None)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(503) if count == 1 else httpx.Response(200, json=make_facts())

    with client(tmp_path, handler) as source:
        assert source.companyfacts(320193).metadata["request_attempts"] == 2
    count = 0

    def unavailable(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("synthetic timeout")

    with client(tmp_path / "failed", unavailable) as source:
        with pytest.raises(SourceError, match="exhausted"):
            source.companyfacts(320193)
    assert count == 3


def test_long_retry_after_is_not_ignored(tmp_path):
    with client(tmp_path, lambda request: httpx.Response(429, headers={"Retry-After": "60"})) as source:
        with pytest.raises(SourceError, match="wait budget"):
            source.companyfacts(320193)


def test_no_arbitrary_cik_url_path_is_accepted(tmp_path):
    def forbidden(request):
        pytest.fail("Invalid CIK must be rejected before HTTP")
    with client(tmp_path, forbidden) as source:
        with pytest.raises(ValueError):
            source.companyfacts("../private")
