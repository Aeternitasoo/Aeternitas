"""Official Sign in with ChatGPT: local PKCE client and protected rotating tokens.

No ChatGPT cookies, Codex credentials, backend-api endpoints, or billing fallback.
"""

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx

from sources.sec import SourceError

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DIRECT = "chatgpt.tokens.use.direct"
MODEL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}")


class OAuthFailure(SourceError):
    def __init__(self, status, code="unknown"):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,100}", code) else "unknown"
        super().__init__(f"ChatGPT authorization HTTP {status}; code={self.code}; retry login / check permission; no API-key fallback")


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".saving-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    if path.stat().st_size > 256000:
        raise SourceError("Local ChatGPT credential record is too large")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise SourceError("Local ChatGPT record is invalid; run auth-status / login") from None


def checked_callback(target, expected_state, issued_client=None):
    parsed = urlsplit(target)
    if parsed.path != "/auth/callback" or len(target) > 16384:
        raise SourceError("Unexpected callback path")
    values = parse_qs(parsed.query, keep_blank_values=True)
    if any(len(v) != 1 for v in values.values()):
        raise SourceError("Duplicate OAuth callback parameter")
    state = values.get("state", [""])[0]
    if not hmac.compare_digest(state, expected_state):
        raise SourceError("OAuth state mismatch; callback rejected")
    if "error" in values:
        raise SourceError("ChatGPT authorization declined or failed; no token exchanged")
    client = values.get("client_id", [issued_client])[0]
    if not isinstance(client, str) or not re.fullmatch(r"oaiapp_[A-Za-z0-9_-]{1,200}", client):
        raise SourceError("Registration did not return an issued client ID")
    if issued_client and client != issued_client:
        raise SourceError("OAuth callback changed the selected registration")
    code = values.get("code", [""])[0]
    if not code or len(code) > 4096:
        raise SourceError("Missing / oversized OAuth code")
    return code, client


class ChatGPTAuth:
    def __init__(self, root=None, *, transport=None):
        self.root = Path(root) if root else Path.home() / ".config/aeternitas"
        self.transport = transport
        self.selected_client = None

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        fd = os.open(self.root / "session.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(fd, "a") as stream:
                until = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= until:
                            raise SourceError("Another Aeternitas login / refresh is running; retry after it finishes")
                        time.sleep(0.1)
                yield
        finally:
            # Closing the descriptor releases the lock, including on interruption.
            pass

    def _profile_path(self, client):
        return self.root / "profiles" / (hashlib.sha256(client.encode()).hexdigest() + ".json")

    def active(self):
        selection = read_json(self.root / "active.json", {})
        client = selection.get("client_id")
        if not client:
            return None
        return read_json(self._profile_path(client))

    def status(self):
        profile = self.active()
        return {"connected": bool(profile and profile.get("access_token")),
                "plan_permission": bool(profile and DIRECT in profile.get("scopes", [])),
                "expired": bool(profile and profile.get("expires_at", 0) <= time.time()),
                "model": profile.get("model") if profile else None,
                "saved_accounts": len(list((self.root / "profiles").glob("*.json")))}

    def _request_json(self, method, url, **kwargs):
        # Discovery/catalog GETs are safe to retry. A rotating-token POST is
        # deliberately attempted only once: a lost reply may already rotate it.
        attempts = 3 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            try:
                return self._request_json_once(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt + 1 == attempts:
                    raise SourceError(f"ChatGPT authorization network failure ({type(exc).__name__}); "
                                      "credentials were not printed or replaced") from None
                time.sleep(0.5 * (attempt + 1))

    def _request_json_once(self, method, url, **kwargs):
        try:
            with httpx.Client(transport=self.transport, follow_redirects=False, timeout=25) as client:
                with client.stream(method, url, **kwargs) as response:
                    if response.status_code != 200:
                        # Do not log provider bodies: they can contain sensitive tokens.
                        raw = bytearray()
                        for chunk in response.iter_bytes():
                            if len(raw) + len(chunk) > 65536:
                                break
                            raw.extend(chunk)
                        try:
                            details = json.loads(raw).get("error", {})
                            code = details.get("code", "unknown") if isinstance(details, dict) else details
                        except (ValueError, AttributeError):
                            code = "unknown"
                        raise OAuthFailure(response.status_code, code)
                    if response.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                        raise SourceError("ChatGPT authorization returned non-JSON data")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if len(raw) + len(chunk) > 1024 * 1024:
                            raise SourceError("ChatGPT authorization response too large")
                        raw.extend(chunk)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise SourceError("Invalid ChatGPT authorization response") from None

    def validate_identity(self, token, client, *, nonce=None, subject=None):
        import jwt
        try:
            metadata = self._request_json("GET", ISSUER + "/.well-known/openid-configuration")
            if metadata.get("issuer") != ISSUER:
                raise SourceError("Unexpected OpenAI identity issuer")
            # Discovery cannot redirect the client to an arbitrary credential endpoint.
            jwks_uri = metadata.get("jwks_uri", "")
            parsed = urlsplit(jwks_uri)
            if parsed.scheme != "https" or parsed.netloc != "auth.openai.com" or parsed.query or parsed.fragment:
                raise SourceError("Unexpected OpenAI signing-key URL")
            keys = self._request_json("GET", jwks_uri).get("keys", [])
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in {"RS256", "ES256"}:
                raise SourceError("Unsupported ID-token signature algorithm")
            candidates = [k for k in keys if k.get("kid") == header.get("kid") and k.get("use", "sig") == "sig"]
            if len(candidates) != 1:
                raise SourceError("ID-token signing key not found / ambiguous")
            key = jwt.PyJWK.from_dict(candidates[0])
            claims = jwt.decode(token, key.key, algorithms=[header["alg"]], issuer=ISSUER,
                                audience=client, leeway=5, options={"require": ["sub", "exp", "iat", "iss", "aud"]})
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise SourceError("ID token is missing account identity")
            if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
                raise SourceError("ID-token nonce mismatch")
            audience = claims["aud"]
            if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != client:
                raise SourceError("ID-token authorized-party mismatch")
            if subject is not None and claims["sub"] != subject:
                raise SourceError("Selected ChatGPT account changed; previous credentials retained")
            return claims
        except (jwt.PyJWTError, ValueError, KeyError, TypeError, AttributeError):
            raise SourceError("ID-token signature / issuer / audience / expiry validation failed") from None

    def _save_tokens(self, response, client, host, *, old=None, nonce=None):
        old = old or {}
        identity = self.validate_identity(response["id_token"], client, nonce=nonce, subject=old.get("subject")) if response.get("id_token") else None
        if identity is None and not old.get("subject"):
            raise SourceError("Initial login did not include a verified ID token")
        scope = response.get("scope")
        scopes = scope.split() if isinstance(scope, str) else old.get("scopes", [])
        expires = response.get("expires_in")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not 0 < expires <= 86400:
            raise SourceError("Missing / invalid OAuth token expiry")
        access, refresh = response.get("access_token"), response.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise SourceError("OAuth response lacks access / rotating refresh token")
        if any(len(token) > 32768 or not token.isascii() or any(c.isspace() for c in token) for token in (access, refresh)):
            raise SourceError("OAuth credential format is invalid")
        if str(response.get("token_type", "")).lower() != "bearer":
            raise SourceError("Unsupported OAuth token type")
        profile = {**old, "client_id": client, "ext_agent_host_id": host,
                   "subject": identity["sub"] if identity else old["subject"],
                   "id_token": response.get("id_token", old.get("id_token")),
                   "access_token": access, "refresh_token": refresh,
                   "scopes": scopes, "expires_at": time.time() + expires,
                   "saved_at": time.time()}
        private_json(self._profile_path(client), profile)
        private_json(self.root / "active.json", {"client_id": client})
        return profile

    def login(self, *, new_account=False, timeout=180, opener=None, announce=print):
        with self.locked():
            old = None if new_account else self.active()
            pending = {} if new_account else read_json(self.root / "registration.json", {})
            client_id = old["client_id"] if old else pending.get("client_id")
            host_record = read_json(self.root / "host.json")
            if not host_record:
                host_record = {"ext_agent_host_id": "urn:uuid:" + str(uuid4())}
                private_json(self.root / "host.json", host_record)
            host = host_record["ext_agent_host_id"]
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            received = {}
            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass  # Authorization codes must never enter access logs.
                def do_GET(self):
                    if urlsplit(self.path).path != "/auth/callback":
                        self.send_error(404)
                        return
                    try:
                        code, issued = checked_callback(self.path, state, client_id)
                    except SourceError as exc:
                        if "state mismatch" not in str(exc):
                            received["error"] = exc
                        self.send_response(400)
                        text = b"Authorization not accepted. Return to the terminal."
                    else:
                        received.update(code=code, client_id=issued)
                        self.send_response(200)
                        text = b"Authorization received. Return to Aeternitas in your terminal."
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    self.wfile.write(text)
            with HTTPServer(("127.0.0.1", 0), Callback) as server:
                server.timeout = 0.5
                redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
                params = {"client_id": client_id or "dynamic_agent_client", "ext_agent_host_id": host,
                          "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
                          "resource": RESOURCE, "state": state, "nonce": nonce,
                          "code_challenge_method": "S256", "code_challenge": challenge}
                if not client_id:
                    params["agent_name_hint"] = "Aeternitas"
                elif old and DIRECT not in old.get("scopes", []):
                    # Only the user's explicit retry after declined plan permission asks again.
                    params["prompt"] = "consent"
                url = AUTHORIZE + "?" + urlencode(params)
                announce("Continue with ChatGPT：在浏览器选择账号，并授权 Aeternitas 使用 ChatGPT 计划。")
                announce("浏览器若未自动打开，可在你本机浏览器打开这个登录地址（不用发给我）：\n" + url)
                (opener or open_browser)(url)
                until = time.monotonic() + timeout
                while not received:
                    if time.monotonic() >= until:
                        raise SourceError("ChatGPT login timed out; run login again; keep this WSL terminal open during sign-in")
                    server.handle_request()
            if "error" in received:
                raise received["error"]
            issued = received["client_id"]
            # Preserve the issued ID even if the one-time code later expires.
            private_json(self.root / "registration.json", {"client_id": issued})
            tokens = self._request_json("POST", TOKEN, data={"grant_type": "authorization_code", "client_id": issued,
                                        "code": received["code"], "code_verifier": verifier,
                                        "redirect_uri": redirect, "resource": RESOURCE})
            profile = self._save_tokens(tokens, issued, host, old=old, nonce=nonce)
            if DIRECT not in profile["scopes"]:
                raise SourceError("ChatGPT identity saved, but plan usage was not authorized; no inference or paid fallback")
            return self.status()

    def token(self):
        with self.locked():
            profile = self.active()
            if not profile or not profile.get("access_token"):
                raise SourceError("先运行 python aeternitas.py login 登录 ChatGPT")
            if DIRECT not in profile.get("scopes", []):
                raise SourceError("ChatGPT plan usage permission is absent; log in and authorize it explicitly")
            if self.selected_client is not None and profile["client_id"] != self.selected_client:
                raise SourceError("Active ChatGPT account changed during this operation; start a new run explicitly")
            self.selected_client = profile["client_id"]
            if profile["expires_at"] <= time.time() + 30:
                try:
                    response = self._request_json("POST", TOKEN, data={"grant_type": "refresh_token", "client_id": profile["client_id"],
                                                "refresh_token": profile["refresh_token"], "resource": RESOURCE})
                except OAuthFailure as exc:
                    if exc.code in {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                                    "refresh_token_invalidated", "refresh_token_reused"}:
                        safe = {k: v for k, v in profile.items() if k not in {"access_token", "refresh_token", "id_token"}}
                        private_json(self._profile_path(profile["client_id"]), safe)
                    raise
                profile = self._save_tokens(response, profile["client_id"], profile["ext_agent_host_id"], old=profile)
                if DIRECT not in profile.get("scopes", []):
                    raise SourceError("ChatGPT plan usage permission was removed; inference stopped")
            return profile["access_token"]

    def models(self):
        data = self._request_json("GET", RESOURCE + "/models", headers={"Authorization": "Bearer " + self.token()})
        rows = data.get("models")
        if not isinstance(rows, list):
            raise SourceError("Unexpected ChatGPT account model catalog")
        models = []
        for row in rows:
            if isinstance(row, dict) and row.get("visibility") == "list":
                slug = row.get("slug")
                if isinstance(slug, str) and MODEL_ID.fullmatch(slug):
                    models.append({"slug": slug, "display_name": str(row.get("display_name", slug))[:120]})
        if not models:
            raise SourceError("No models returned for this ChatGPT account; no paid fallback")
        return models

    def choose_model(self, configured=None, *, interactive=False):
        models = self.models()
        choices = {row["slug"] for row in models}
        profile = self.active()
        if profile["client_id"] != self.selected_client:
            raise SourceError("Active account changed while listing models; retry login / model choice")
        preferred = configured if configured and configured != "auto" else profile.get("model")
        if preferred in choices:
            return preferred
        if configured and configured != "auto":
            raise SourceError("Configured model is absent from this ChatGPT account; run models / login to choose")
        if not interactive:
            raise SourceError("Choose a model once with python aeternitas.py login; no automatic model substitution")
        for i, row in enumerate(models, 1):
            print(f"{i}. {row['display_name']} ({row['slug']})")
        selected = input("选择本次项目使用的模型编号：").strip()
        if not selected.isdigit() or not 1 <= int(selected) <= len(models):
            raise SourceError("Invalid model selection; run login again")
        model = models[int(selected) - 1]["slug"]
        with self.locked():
            profile = self.active()
            if profile["client_id"] != self.selected_client:
                raise SourceError("Active account changed before saving the model")
            profile["model"] = model
            private_json(self._profile_path(profile["client_id"]), profile)
        return model

    def logout(self):
        with self.locked():
            profile = self.active()
            if profile:
                safe = {k: v for k, v in profile.items() if k not in {"access_token", "refresh_token", "id_token"}}
                private_json(self._profile_path(profile["client_id"]), safe)
            # Keep registration / host identity; next login can reuse it.
        return "已清除本机活动账号令牌；未确认远端撤销，可在 ChatGPT 设置中断开 Aeternitas。"


def open_browser(url):
    if webbrowser.open(url):
        return
    if "microsoft" in os.uname().release.lower():
        # WSL bridge. Argument-list execution avoids cmd.exe shell interpolation.
        import shutil
        import subprocess
        binary = shutil.which("powershell.exe")
        if binary:
            escaped = url.replace("'", "''")
            subprocess.Popen([binary, "-NoProfile", "-NonInteractive", "-Command", "Start-Process '" + escaped + "'"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
