"""One provider: Finnhub company-news; summaries are never called full articles."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from research_runtime import public_url
from sources.sec import SourceError


PARSER_VERSION = "finnhub-summary-m2-v1"


def canonical_news_url(url):
    """Remove fragments/known tracking parameters, retaining article identity."""
    parts = urlsplit(public_url(url))
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port and not (parts.scheme == "https" and port == 443 or parts.scheme == "http" and port == 80):
        host += f":{port}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(sorted(query)), ""))


def normalize_news(rows, ticker, cik, cutoff, days):
    if not isinstance(rows, list):
        raise ValueError("News must be a list, not an API error object")
    start = cutoff - timedelta(days=days - 1)
    eligible = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or type(row.get("datetime")) is not int:
            raise ValueError("News needs a publication timestamp")
        stamp = datetime.fromtimestamp(row["datetime"], timezone.utc)
        if not start <= stamp.astimezone(ZoneInfo("Asia/Shanghai")).date() <= cutoff:
            continue
        related = row.get("related")
        if related and ticker not in {x.strip().upper() for x in str(related).split(",")}:
            continue
        headline, summary = row.get("headline"), row.get("summary", "")
        if not isinstance(headline, str) or not headline.strip() or not isinstance(summary, str):
            raise ValueError("News headline / summary invalid")
        url = public_url(row.get("url"))
        canonical = canonical_news_url(url)
        provider_id = row.get("id")
        if provider_id is not None and (type(provider_id) not in (str, int) or len(str(provider_id)) > 200):
            raise ValueError("Invalid news source ID")
        provider_id = str(provider_id) if provider_id not in (None, "", 0, "0") else ""
        eligible.append({"id": f"N-{hashlib.sha256(canonical.encode()).hexdigest()[:16]}",
                         "kind": "news", "company_cik": cik, "title": headline[:400],
                         "text": f"{headline[:400]}\n{summary[:1400]}", "text_level": "provider_summary",
                         "source_url": url, "published_at": stamp.isoformat(), "time_precision": "second",
                         "locator": {"response_index": index, "provider_id": provider_id,
                                     "canonical_url": canonical, "parser_version": PARSER_VERSION,
                                     "company_match": "related_field" if related else "provider_query_only"}})
    # Latest provider observation wins when duplicate IDs/URLs have revisions.
    eligible.sort(key=lambda r: (r["published_at"], r["locator"]["response_index"]), reverse=True)
    news, urls, ids = [], set(), set()
    for item in eligible:
        canonical, provider_id = item["locator"]["canonical_url"], item["locator"]["provider_id"]
        if canonical in urls or provider_id and provider_id in ids:
            continue
        urls.add(canonical)
        if provider_id:
            ids.add(provider_id)
        news.append(item)
    return news


def load_news(web, ticker, cik, cutoff, key, *, days=14, refresh=False):
    start = cutoff - timedelta(days=days - 1)
    raw, source = web.get(
        "https://finnhub.io/api/v1/company-news",
        params={"symbol": ticker, "from": (start - timedelta(days=1)).isoformat(), "to": cutoff.isoformat()},
        headers={"X-Finnhub-Token": key}, ttl=900, refresh=refresh, max_bytes=5 * 1024 * 1024,
    )
    try:
        rows = json.loads(raw)
        news = [{**item, "source": source} for item in normalize_news(rows, ticker, cik, cutoff, days)]
    except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
        raise SourceError("Invalid Finnhub company-news response") from exc
    return news[:10], source, {
        "requested_start": start.isoformat(), "requested_end": cutoff.isoformat(),
        "provider_rows": len(rows), "eligible_unique_rows": len(news), "selected_rows": min(10, len(news)),
        "state": "results" if news else "no_results_returned",
        "parser_version": PARSER_VERSION,
        "oldest_returned_eligible": min((n["published_at"] for n in news), default=None),
        "newest_returned_eligible": max((n["published_at"] for n in news), default=None),
        "limitation": "Provider results do not prove exhaustive coverage; summaries only, not full articles.",
    }
