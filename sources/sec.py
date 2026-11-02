"""Bounded SEC companyfacts client with immutable snapshots and visible caching."""

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx

from models import normalize_cik


class SourceError(RuntimeError):
    """An upstream response or saved source could not be trusted or obtained."""


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def decode_companyfacts(raw_bytes, expected_cik):
    try:
        raw = json.loads(
            raw_bytes, parse_float=Decimal, parse_constant=_reject_constant,
        )
        if not isinstance(raw, dict):
            raise ValueError("companyfacts must be an object")
        if normalize_cik(raw.get("cik")) != expected_cik:
            raise ValueError("companyfacts CIK does not match the request")
        if not isinstance(raw.get("entityName"), str) or not raw["entityName"].strip():
            raise ValueError("companyfacts entityName is missing")
        facts = raw.get("facts")
        if not isinstance(facts, dict) or not isinstance(facts.get("us-gaap"), dict):
            raise ValueError("companyfacts facts.us-gaap is missing")
        return raw
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SourceError(f"Invalid SEC companyfacts: {exc}") from exc


def write_atomic(path, content):
    """Publish a complete file; do not leave a partly written cache pointer."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


@dataclass(frozen=True)
class Snapshot:
    data: dict
    metadata: dict
    cache_hit: bool

    def reference(self):
        return {**self.metadata, "cache_hit": self.cache_hit}


class SecClient:
    """Only fetch the constructed HTTPS companyfacts endpoint, never arbitrary URLs.

    Timeouts bound individual I/O waits. An elapsed-time check bounds retries
    and rejects overlong downloads; this synchronous client does not promise
    hard cancellation of an already blocked socket. Worker deadlines come later.
    """

    def __init__(self, root, contact, *, transport=None, ttl_seconds=86400,
                 timeout_seconds=20, elapsed_budget_seconds=60,
                 max_bytes=25 * 1024 * 1024):
        if not isinstance(contact, str) or not contact.isascii() or not re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+", contact,
        ):
            raise ValueError("SEC_CONTACT must be your contact email")
        if ttl_seconds < 0 or min(timeout_seconds, elapsed_budget_seconds, max_bytes) <= 0:
            raise ValueError("Invalid source limits")
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self.elapsed_budget_seconds = elapsed_budget_seconds
        self.max_bytes = max_bytes
        self.http = httpx.Client(
            headers={
                "User-Agent": f"EvidenceResearchDesk/0.1 {contact}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    def _read_cache(self, cik, url):
        pointer = self.root / "cache" / f"companyfacts-{cik}.json"
        if not pointer.exists():
            return None
        try:
            saved = json.loads(pointer.read_text(encoding="utf-8"))
            retrieval_id = saved["retrieval_id"]
            if not re.fullmatch(r"[0-9a-f]{32}", retrieval_id):
                raise ValueError("Invalid retrieval ID")
            metadata = json.loads(
                (self.root / "retrievals" / f"{retrieval_id}.json").read_text(encoding="utf-8")
            )
            if metadata["cik"] != cik or metadata["source_url"] != url:
                raise ValueError("Cached source identity mismatch")
            fetched_at = datetime.fromisoformat(metadata["fetched_at"])
            if fetched_at.utcoffset() is None:
                raise ValueError("Cached timestamp must include timezone")
            age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
            if age < 0:
                raise ValueError("Cached timestamp is in the future")
            if age >= self.ttl_seconds:
                return None
            fingerprint = metadata["sha256"]
            if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                raise ValueError("Invalid cached fingerprint")
            relative = f"snapshots/companyfacts/{cik}/{fingerprint}.json"
            if metadata["snapshot_path"] != relative:
                raise ValueError("Invalid cached snapshot path")
            raw = (self.root / relative).read_bytes()
            if len(raw) > self.max_bytes or hashlib.sha256(raw).hexdigest() != fingerprint:
                raise ValueError("Cached snapshot hash / size mismatch")
            return Snapshot(decode_companyfacts(raw, cik), metadata, True)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise SourceError(f"Cache is invalid; use --refresh: {exc}") from exc

    def _download(self, url):
        started = time.monotonic()
        last_error = "unknown upstream error"
        for attempt in range(1, 4):
            remaining = self.elapsed_budget_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise SourceError("SEC download elapsed-time budget exceeded")
            retry_delay = min(0.5 * (2 ** (attempt - 1)), 5)
            try:
                with self.http.stream(
                    "GET", url, timeout=min(self.timeout_seconds, remaining),
                ) as response:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        last_error = f"SEC HTTP {response.status_code}"
                        hint = response.headers.get("Retry-After")
                        if hint is not None:
                            try:
                                requested_delay = float(hint)
                            except ValueError:
                                raise SourceError(
                                    "SEC requested a later retry; try again later"
                                ) from None
                            if not 0 <= requested_delay <= 5:
                                raise SourceError(
                                    "SEC Retry-After exceeds this request's wait budget"
                                )
                            retry_delay = max(retry_delay, requested_delay)
                    elif response.status_code != 200:
                        # Redirects are rejected rather than followed across hosts.
                        raise SourceError(f"SEC HTTP {response.status_code}; no stale fallback")
                    else:
                        media_type = response.headers.get("Content-Type", "").split(";")[0]
                        if media_type.lower().strip() != "application/json":
                            raise SourceError("SEC did not return application/json")
                        content = bytearray()
                        for chunk in response.iter_bytes():
                            if time.monotonic() - started >= self.elapsed_budget_seconds:
                                raise SourceError("SEC download elapsed-time budget exceeded")
                            if len(content) + len(chunk) > self.max_bytes:
                                raise SourceError("SEC response exceeds configured byte limit")
                            content.extend(chunk)
                        if time.monotonic() - started >= self.elapsed_budget_seconds:
                            raise SourceError("SEC download elapsed-time budget exceeded")
                        return bytes(content), attempt
            except httpx.TransportError:
                # Do not log request headers or proxy credentials from exception text.
                last_error = "SEC network / timeout error"
            if attempt < 3:
                remaining = self.elapsed_budget_seconds - (time.monotonic() - started)
                if retry_delay >= remaining:
                    break
                time.sleep(retry_delay)
        raise SourceError(f"{last_error}; bounded retry exhausted (max 3 attempts)")

    def companyfacts(self, cik, *, refresh=False):
        cik = normalize_cik(cik)
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        if not refresh:
            cached = self._read_cache(cik, url)
            if cached is not None:
                return cached
        raw_bytes, attempts = self._download(url)
        data = decode_companyfacts(raw_bytes, cik)
        fingerprint = hashlib.sha256(raw_bytes).hexdigest()
        relative = f"snapshots/companyfacts/{cik}/{fingerprint}.json"
        snapshot_path = self.root / relative
        if snapshot_path.exists():
            if snapshot_path.read_bytes() != raw_bytes:
                raise SourceError("Existing snapshot does not match its content hash")
        else:
            write_atomic(snapshot_path, raw_bytes)
        retrieval_id = uuid4().hex
        metadata = {
            "schema_version": "sec-companyfacts-source-v1",
            "retrieval_id": retrieval_id,
            "data_mode": "live",
            "cik": cik,
            "source_url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sha256": fingerprint,
            "size_bytes": len(raw_bytes),
            "snapshot_path": relative,
            "request_attempts": attempts,
            "forced_refresh": refresh,
        }
        write_json(self.root / "retrievals" / f"{retrieval_id}.json", metadata)
        write_json(self.root / "cache" / f"companyfacts-{cik}.json", {"retrieval_id": retrieval_id})
        return Snapshot(data, metadata, False)
