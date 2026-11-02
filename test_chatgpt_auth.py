"""Local OAuth and stream tests: generated identities, no real OpenAI sessions."""

import base64
import hashlib
import json
import threading
import time
from datetime import date
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from aeternitas import configure, load_config
from chatgpt_auth import ChatGPTAuth, DIRECT, ISSUER, RESOURCE, TOKEN, checked_callback, private_json
from chatgpt_stream import read_plan_response
from research_analysis import ResponsesAnalyst
from research_pipeline import run_research, saved_report
from research_runtime import Budget
from research_test_support import CONFIG, transports
from sources.sec import SourceError


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def provider(signing_key, *, scopes=None, claims_change=None, refresh_failure=False):
    seen = {"requests": [], "nonce": "test-nonce", "tokens": 0}
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update(kid="test-key", use="sig", alg="RS256")
    def handler(request):
        seen["requests"].append(request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": ISSUER + "/.well-known/jwks.json"})
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [jwk]})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"models": [{"slug": "account-model", "display_name": "Synthetic model", "visibility": "list"},
                                                         {"slug": "hidden-model", "visibility": "hidden"}]})
        assert str(request.url) == TOKEN
        values = parse_qs(request.content.decode())
        seen["last_form"] = values
        if refresh_failure and values["grant_type"] == ["refresh_token"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        seen["tokens"] += 1
        claims = {"sub": "synthetic-user", "iss": ISSUER, "aud": "oaiapp_synthetic",
                  "exp": int(time.time()) + 3600, "iat": int(time.time()), "nonce": seen["nonce"]}
        claims.update(claims_change or {})
        identity = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-key"})
        return httpx.Response(200, json={"access_token": "synthetic-access-secret-" + str(seen["tokens"]),
                                         "refresh_token": "synthetic-refresh-secret-" + str(seen["tokens"]),
                                         "id_token": identity, "token_type": "Bearer", "expires_in": 3600,
                                         "scope": scopes if scopes is not None else "openid profile offline_access " + DIRECT})
    return httpx.MockTransport(handler), seen


def login(auth, seen, *, new=False):
    threads = []
    def open_local(url):
        query = parse_qs(urlsplit(url).query)
        seen["authorization"] = query
        seen["nonce"] = query["nonce"][0]
        callback = query["redirect_uri"][0]
        def deliver():
            with httpx.Client(trust_env=False) as client:
                response = client.get(callback + "?" + urlencode({"code": "synthetic-code", "state": query["state"][0],
                                                                    "client_id": "oaiapp_synthetic"}))
                assert response.status_code == 200
        worker = threading.Thread(target=deliver)
        threads.append(worker)
        worker.start()
    try:
        return auth.login(timeout=5, opener=open_local, announce=lambda _: None, new_account=new)
    finally:
        for worker in threads:
            worker.join(timeout=5)


def test_official_pkce_login_and_private_storage(tmp_path, signing_key):
    transport, seen = provider(signing_key)
    auth = ChatGPTAuth(tmp_path, transport=transport)
    status = login(auth, seen)
    assert status["connected"] and status["plan_permission"]
    query, form = seen["authorization"], seen["last_form"]
    assert query["client_id"] == ["dynamic_agent_client"] and query["agent_name_hint"] == ["Aeternitas"]
    assert form["client_id"] == ["oaiapp_synthetic"] and form["resource"] == [RESOURCE]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).decode().rstrip("=")
    assert challenge == query["code_challenge"][0]
    assert query["redirect_uri"] == form["redirect_uri"]
    assert urlsplit(query["redirect_uri"][0]).hostname == "127.0.0.1"
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.rglob("*.json"))
    assert "access_token" not in json.dumps(status)
    assert auth.token() == "synthetic-access-secret-1"
    login(auth, seen)
    assert seen["authorization"]["client_id"] == ["oaiapp_synthetic"]
    assert "agent_name_hint" not in seen["authorization"]
    assert len(list((tmp_path / "profiles").glob("*.json"))) == 1


@pytest.mark.parametrize("query,match", [
    ("code=x&state=wrong&client_id=oaiapp_test", "state mismatch"),
    ("code=x&state=s", "issued client"),
    ("code=x&state=s&state=s&client_id=oaiapp_test", "Duplicate"),
    ("error=access_denied&state=s", "declined"),
    ("code=x&state=s&client_id=oaiapp_changed", "changed"),
])
def test_bad_callback_is_rejected(query, match):
    selected = "oaiapp_original" if match == "changed" else None
    with pytest.raises(SourceError, match=match):
        checked_callback("/auth/callback?" + query, "s", selected)


@pytest.mark.parametrize("change,match", [
    ({"iss": "https://untrusted.invalid"}, "validation"),
    ({"aud": "another-client"}, "validation"),
    ({"exp": 1}, "validation"),
    ({"nonce": "another-attempt"}, "nonce mismatch"),
])
def test_unverified_identity_never_becomes_active(tmp_path, signing_key, change, match):
    transport, seen = provider(signing_key, claims_change=change)
    auth = ChatGPTAuth(tmp_path, transport=transport)
    with pytest.raises(SourceError, match=match):
        login(auth, seen)
    assert auth.active() is None


def test_wrong_signature_is_rejected(tmp_path, signing_key):
    transport, _ = provider(signing_key)
    claims = {"sub": "x", "iss": ISSUER, "aud": "oaiapp_synthetic", "exp": int(time.time()) + 60, "iat": int(time.time())}
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(claims, other, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(SourceError, match="validation"):
        ChatGPTAuth(tmp_path, transport=transport).validate_identity(token, "oaiapp_synthetic")


def test_identity_without_plan_permission_is_retained_but_cannot_infer(tmp_path, signing_key):
    transport, seen = provider(signing_key, scopes="openid profile offline_access")
    auth = ChatGPTAuth(tmp_path, transport=transport)
    with pytest.raises(SourceError, match="not authorized"):
        login(auth, seen)
    assert auth.status()["connected"] and not auth.status()["plan_permission"]
    with pytest.raises(SourceError, match="permission is absent"):
        auth.token()
    assert seen["tokens"] == 1


def test_refresh_rotates_tokens_with_issued_client_and_preserves_host(tmp_path, signing_key):
    transport, seen = provider(signing_key)
    auth = ChatGPTAuth(tmp_path, transport=transport)
    login(auth, seen)
    old = auth.active()
    old["expires_at"] = 1
    private_json(auth._profile_path(old["client_id"]), old)
    token = auth.token()
    assert token == "synthetic-access-secret-2"
    assert auth.active()["refresh_token"] == "synthetic-refresh-secret-2"
    assert seen["last_form"]["grant_type"] == ["refresh_token"]
    assert seen["last_form"]["client_id"] == ["oaiapp_synthetic"]
    assert "scope" not in seen["last_form"]
    assert auth.active()["ext_agent_host_id"] == old["ext_agent_host_id"]


def test_account_catalog_and_no_unverified_model_substitution(tmp_path, signing_key, monkeypatch):
    transport, seen = provider(signing_key)
    auth = ChatGPTAuth(tmp_path, transport=transport)
    login(auth, seen)
    assert len(auth.models()) == 1
    with pytest.raises(SourceError, match="absent"):
        auth.choose_model("model-not-in-account")
    monkeypatch.setattr("builtins.input", lambda _: "1")
    assert auth.choose_model(interactive=True) == "account-model"
    assert auth.choose_model() == "account-model"
    message = auth.logout()
    assert not auth.status()["connected"] and "未确认" in message
    login(auth, seen)
    assert seen["authorization"]["client_id"] == ["oaiapp_synthetic"]


def test_terminal_refresh_error_clears_tokens_but_retains_registration(tmp_path, signing_key):
    transport, seen = provider(signing_key, refresh_failure=True)
    auth = ChatGPTAuth(tmp_path, transport=transport)
    login(auth, seen)
    profile = auth.active()
    profile["expires_at"] = 1
    private_json(auth._profile_path(profile["client_id"]), profile)
    with pytest.raises(SourceError, match="invalid_grant"):
        auth.token()
    assert not auth.status()["connected"]
    assert auth.active()["client_id"] == "oaiapp_synthetic"


def test_temporary_refresh_network_error_preserves_tokens(tmp_path, signing_key):
    transport, seen = provider(signing_key)
    auth = ChatGPTAuth(tmp_path, transport=transport)
    login(auth, seen)
    profile = auth.active()
    profile["expires_at"] = 1
    private_json(auth._profile_path(profile["client_id"]), profile)
    def offline(request):
        raise httpx.ReadTimeout("synthetic network outage", request=request)
    auth.transport = httpx.MockTransport(offline)
    with pytest.raises(SourceError, match="network failure"):
        auth.token()
    assert auth.active()["refresh_token"] == profile["refresh_token"]


@pytest.mark.parametrize("method,expected_calls", [("GET", 3), ("POST", 1)])
def test_auth_network_retry_is_bounded_and_never_repeats_token_posts(tmp_path, monkeypatch, method, expected_calls):
    calls = []
    monkeypatch.setattr("chatgpt_auth.time.sleep", lambda _: None)

    def offline(request):
        calls.append(request)
        raise httpx.ConnectTimeout("synthetic-sensitive-detail", request=request)

    auth = ChatGPTAuth(tmp_path, transport=httpx.MockTransport(offline))
    with pytest.raises(SourceError, match="network failure \\(ConnectTimeout\\)") as failure:
        auth._request_json(method, ISSUER + "/test")
    assert len(calls) == expected_calls
    assert "synthetic-sensitive-detail" not in str(failure.value)


def test_auth_discovery_recovers_from_transient_transport_failure(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("chatgpt_auth.time.sleep", lambda _: None)

    def recovered(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("synthetic transport failure", request=request)
        return httpx.Response(200, json={"issuer": ISSUER})

    auth = ChatGPTAuth(tmp_path, transport=httpx.MockTransport(recovered))
    assert auth._request_json("GET", ISSUER + "/test") == {"issuer": ISSUER}
    assert len(calls) == 3


def test_account_switch_does_not_silently_change_billing_account(tmp_path, signing_key):
    transport, seen = provider(signing_key)
    auth = ChatGPTAuth(tmp_path, transport=transport)
    login(auth, seen)
    auth.token()
    other = {**auth.active(), "client_id": "oaiapp_other", "subject": "other-user"}
    private_json(auth._profile_path(other["client_id"]), other)
    private_json(tmp_path / "active.json", {"client_id": other["client_id"]})
    with pytest.raises(SourceError, match="changed"):
        auth.token()


class StubPlan:
    def token(self):
        return "synthetic-plan-secret"


def sse(data):
    return ("event: " + data["type"] + "\ndata: " + json.dumps(data) + "\n\n").encode()


@pytest.mark.parametrize("media_type", [None, "text/event-stream", "text/event-stream; charset=utf-8"])
@pytest.mark.parametrize("sparse", [False, True])
def test_plan_pipeline_uses_oauth_sse_and_never_api_key(tmp_path, media_type, sparse):
    transport, calls = transports()
    original = transport["model"]
    headers = []
    def model(request):
        payload = json.loads(request.content)
        headers.append(request.headers["authorization"])
        assert payload["stream"] is True and payload["store"] is False
        assert "max_output_tokens" not in payload
        completed = original.handle_request(request).json()
        text = completed["output"][0]["content"][0]["text"]
        if sparse:
            completed["output"] = []
        wire = sse({"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                    "delta": text}) + sse({"type": "response.completed", "response": completed})
        response_headers = {} if media_type is None else {"content-type": media_type}
        return httpx.Response(200, content=wire, headers=response_headers)
    transport["model"] = httpx.MockTransport(model)
    config = {**CONFIG, "AETERNITAS_AUTH": "chatgpt"}
    run_id, report = run_research(tmp_path, "AAPL", "0000320193", date(2025, 10, 1), "Question", config,
                                   transports=transport, evidence_kind="controlled_test", plan_auth=StubPlan())
    assert report["billing_mode"] == "chatgpt_plan" and len(calls["model"]) == 4
    assert set(headers) == {"Bearer synthetic-plan-secret"}
    assert all(row["stream_header_missing"] == (media_type is None) for row in report["model_usage"])
    assert all(row["stream_output_reconstructed"] == sparse for row in report["model_usage"])
    saved_report(tmp_path, run_id)
    assert all("synthetic-plan-secret" not in p.read_text() for p in (tmp_path / "runs").rglob("*.json"))


@pytest.mark.parametrize("failure", ["interrupted", "failed", "incomplete", "quota", "oversized"])
@pytest.mark.parametrize("missing_header", [False, True])
def test_bad_plan_stream_never_finishes_analysis(tmp_path, failure, missing_header):
    calls = []
    def model(request):
        calls.append(request)
        if failure == "quota":
            return httpx.Response(429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded"}}, headers={"x-request-id": "req_synthetic"})
        if failure == "oversized":
            wire = sse({"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                        "delta": "x" * 24001})
        elif failure in {"failed", "incomplete"}:
            wire = sse({"type": "response." + failure, "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}})
        else:
            wire = sse({"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                        "delta": "unfinished"})
        response_headers = {} if missing_header else {"content-type": "text/event-stream"}
        return httpx.Response(200, content=wire, headers=response_headers)
    with ResponsesAnalyst(None, "account-model", tmp_path, Budget(), transport=httpx.MockTransport(model), plan_auth=StubPlan()) as analyst:
        with pytest.raises(SourceError):
            analyst.analyze("fundamentals", "AAPL", "2025-10-01", "Question", [], {})
    assert len(calls) == 1 and not (tmp_path / "fundamentals.json").exists()


def test_legacy_env_migrates_to_plan_without_asking_for_platform_key(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('SEC_CONTACT="test@example.invalid"\nFINNHUB_API_KEY="saved-news-key"\nOPENAI_API_KEY="unused-old-key"\nAETERNITAS_MODEL="gpt-4.1-mini"\n')
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Existing source values should be reused"))
    configure(path, provider="chatgpt")
    values = load_config(path)
    assert values["AETERNITAS_AUTH"] == "chatgpt" and values["AETERNITAS_MODEL"] == "auto"
    assert values["FINNHUB_API_KEY"] == "saved-news-key"


def test_batch_script_stays_on_plan_with_old_paid_mode_environment(tmp_path):
    import os
    import shutil
    import subprocess
    from pathlib import Path
    source = Path(__file__).parent
    (tmp_path / "scripts").mkdir()
    (tmp_path / ".venv/bin").mkdir(parents=True)
    shutil.copyfile(source / "scripts/complete_m1.sh", tmp_path / "scripts/complete_m1.sh")
    interpreter = tmp_path / ".venv/bin/python"
    interpreter.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                           'with open(os.environ["TEST_CALL_LOG"], "a") as f:\n'
                           '    f.write(json.dumps({"args":sys.argv[1:], "mode":os.environ.get("AETERNITAS_AUTH")})+"\\n")\n')
    interpreter.chmod(0o700)
    log = tmp_path / "calls.jsonl"
    env = {**os.environ, "AETERNITAS_AUTH": "api_key", "TEST_CALL_LOG": str(log)}
    subprocess.run(["bash", str(tmp_path / "scripts/complete_m1.sh")], env=env, check=True, timeout=15)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[-1]["args"] == ["aeternitas.py", "run-local", "AAPL", "--refresh"]
    assert all(row["mode"] == "chatgpt" for row in rows)
