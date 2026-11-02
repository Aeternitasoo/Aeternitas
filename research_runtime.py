"""Small synchronous M1 runtime; durable worker recovery belongs to M3/M5."""

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from sources.sec import SourceError, write_atomic, write_json


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Budget:
    def __init__(self, seconds=300, max_calls=8, on_call=None):
        if not 1 <= seconds <= 1800 or not 1 <= max_calls <= 8:
            raise ValueError("Invalid deadline or model call limit")
        self.deadline = time.monotonic() + seconds
        self.max_calls = max_calls
        self.calls = 0
        self.on_call = on_call

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise SourceError("Research deadline exceeded; no further steps will start")
        return left

    def model_attempt(self, role):
        self.remaining()
        if self.calls >= self.max_calls:
            raise SourceError("Research model call budget exhausted")
        self.calls += 1
        if self.on_call:
            self.on_call(role, self.calls)

    def pause(self, seconds):
        if seconds >= self.remaining():
            raise SourceError("Retry wait exceeds remaining research deadline")
        time.sleep(seconds)


def public_url(value):
    from urllib.parse import urlsplit
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid source URL")
    p = urlsplit(value)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Source URL must be a public HTTP(S) link without credentials")
    return value


class WebSources:
    """GET only fixed SEC/Finnhub routes; never fetch a news article's URL.

    Cache keys include public query parameters; secrets travel in headers and
    are never persisted. Hash-verified receipts distinguish cache from refresh.
    """
    def __init__(self, root, budget, *, transport=None):
        self.root = Path(root)
        self.budget = budget
        self.http = httpx.Client(transport=transport, follow_redirects=False)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    @staticmethod
    def allowed(url):
        return bool(re.fullmatch(
            r"https://www\.sec\.gov/files/company_tickers\.json"
            r"|https://data\.sec\.gov/submissions/CIK[0-9]{10}(?:-submissions-[0-9]{3})?\.json"
            r"|https://www\.sec\.gov/Archives/edgar/data/[0-9]+/[0-9]{18}/[A-Za-z0-9_.-]+\.html?"
            r"|https://finnhub\.io/api/v1/(?:company-news|stock/candle)", url))

    def get(self, url, *, params=None, headers=None, ttl=3600, refresh=False,
            max_bytes=15 * 1024 * 1024, content_types=("application/json",), validate_response=None):
        if not self.allowed(url):
            raise SourceError("Source URL is outside the configured allowlist")
        query = dict(params or {})
        if any(re.search(r"token|key|secret", key, re.I) for key in query):
            raise ValueError("Credentials must not appear in source query parameters")
        key = hashlib.sha256(json.dumps([url, query], sort_keys=True).encode()).hexdigest()
        pointer = self.root / "cache" / f"web-{key}.json"
        self.budget.remaining()
        if not refresh and pointer.exists():
            try:
                meta = json.loads(pointer.read_text(encoding="utf-8"))
                if meta["source_url"] != url or meta["query"] != query:
                    raise ValueError("Source identity changed")
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(meta["fetched_at"])).total_seconds()
                if age < 0:
                    raise ValueError("Future cache time")
                if age < ttl:
                    sha = meta["sha256"]
                    if not re.fullmatch(r"[0-9a-f]{64}", sha):
                        raise ValueError("Bad fingerprint")
                    path = f"snapshots/web/{sha}.bin"
                    if meta["snapshot_path"] != path:
                        raise ValueError("Bad cache path")
                    raw = (self.root / path).read_bytes()
                    if len(raw) > max_bytes or hashlib.sha256(raw).hexdigest() != sha:
                        raise ValueError("Cache hash / size mismatch")
                    return raw, {**meta, "cache_hit": True}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise SourceError("Invalid source cache; rerun with --refresh") from exc
        raw = None
        for attempt in range(1, 4):
            delay = min(0.5 * 2 ** (attempt - 1), 5)
            if url == "https://finnhub.io/api/v1/stock/candle":
                self.budget.pause(1)  # Conservative per-client candle pacing, including retries.
            if ".sec.gov/" in url:
                self.budget.pause(0.15)  # One process stays below SEC's published request ceiling.
            try:
                with self.http.stream("GET", url, params=query, headers=headers,
                                      timeout=min(20, self.budget.remaining())) as response:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        hint = response.headers.get("Retry-After")
                        if hint:
                            try:
                                wait = float(hint)
                            except ValueError:
                                raise SourceError("Source asks for a later retry") from None
                            if not 0 <= wait <= 5:
                                raise SourceError("Source Retry-After exceeds wait limit")
                            delay = max(delay, wait)
                    elif response.status_code != 200:
                        raise SourceError(f"Source HTTP {response.status_code}; no stale fallback")
                    else:
                        media = response.headers.get("content-type", "").split(";")[0].strip().lower()
                        if media not in content_types:
                            raise SourceError("Unexpected source content type")
                        content = bytearray()
                        for chunk in response.iter_bytes():
                            self.budget.remaining()
                            if len(content) + len(chunk) > max_bytes:
                                raise SourceError("Source response exceeds size limit")
                            content.extend(chunk)
                        self.budget.remaining()
                        raw = bytes(content)
                        break
            except httpx.TransportError:
                pass  # Provider exception text may contain credentials; never persist it.
            if attempt < 3:
                self.budget.pause(delay)
        if raw is None:
            raise SourceError("Source network / retry limit reached")
        if validate_response is not None:
            validate_response(raw)  # Reject invalid/credential-echoing candle bodies before persistence.
        sha = hashlib.sha256(raw).hexdigest()
        relative = f"snapshots/web/{sha}.bin"
        path = self.root / relative
        if path.exists() and path.read_bytes() != raw:
            raise SourceError("Saved source bytes do not match fingerprint")
        if not path.exists():
            write_atomic(path, raw)
        meta = {"retrieval_id": uuid4().hex, "source_url": url, "query": query,
                "fetched_at": utc_now(), "sha256": sha, "snapshot_path": relative,
                "size_bytes": len(raw), "data_mode": "live", "request_attempts": attempt,
                "forced_refresh": refresh, "cache_hit": False}
        write_json(self.root / "retrievals" / f"{meta['retrieval_id']}.json", meta)
        write_json(pointer, meta)
        return raw, meta
