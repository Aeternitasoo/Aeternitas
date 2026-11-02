"""SEC submissions directory -> selected filings -> bounded section evidence."""

import hashlib
import json
import re
from datetime import date
from html.parser import HTMLParser

from models import normalize_cik
from sources.sec import SourceError, write_atomic


PARSER_VERSION = "filing-sections-m1-v1"


class FilingText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "ix:hidden"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "tr", "br", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "ix:hidden"} and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_sections(raw, form):
    try:
        html = raw.decode("utf-8")
    except UnicodeError as exc:
        raise SourceError("Filing is not valid UTF-8 HTML") from exc
    if "Request Rate Threshold Exceeded" in html or "Your Request Originates from an Undeclared" in html:
        raise SourceError("SEC returned an access / rate-limit page")
    parser = FilingText()
    parser.feed(html)
    text = "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())
    if len(text) < 200:
        raise SourceError("Filing has insufficient parsed text")
    headings = list(re.finditer(r"(?im)^\s*item\s+([0-9]{1,2}[a-c]?)\s*[.\-:–—]?\s*([^\n]{0,180})", text))
    wanted = {"1a": "risk_factors", "7" if form.startswith("10-K") else "2": "management_discussion"}
    sections = []
    for item, label in wanted.items():
        candidates = []
        for i, match in enumerate(headings):
            if match.group(1).lower() != item:
                continue
            title = match.group(2).lower()
            if label == "risk_factors" and "risk" not in title:
                continue
            if label == "management_discussion" and "management" not in title:
                continue
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            if end - match.start() >= 200:
                candidates.append((match.start(), end))
        if candidates:
            # Prefer the body over duplicate table-of-contents headers.
            start, section_end = max(candidates, key=lambda pair: pair[1] - pair[0])
            end = min(section_end, start + 2000)
            sections.append({"section": label, "item": item.upper(), "text": text[start:end],
                             "normalized_start": start, "normalized_end": end,
                             "is_excerpt": end < section_end})
    if not sections:
        raise SourceError("No supported filing sections found; parser review required")
    return text, sections


def directory_rows(data, cutoff):
    fields = ("accessionNumber", "form", "filingDate", "primaryDocument")
    if not isinstance(data, dict) or any(not isinstance(data.get(f), list) for f in fields):
        raise SourceError("Invalid SEC filing directory columns")
    if len({len(data[f]) for f in fields}) != 1:
        raise SourceError("SEC filing directory columns have different lengths")
    if "reportDate" in data and (not isinstance(data["reportDate"], list) or len(data["reportDate"]) != len(data["form"])):
        raise SourceError("SEC report period column has a different length")
    rows = []
    for index, (accession, form, filed, document) in enumerate(zip(*(data[f] for f in fields))):
        if form not in {"10-K", "10-Q", "10-K/A", "10-Q/A"}:
            continue
        if not isinstance(filed, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",filed):
            raise SourceError("Filing date missing")
        try:
            filing_date = date.fromisoformat(filed)
        except ValueError as exc:
            raise SourceError("Invalid filing date") from exc
        if filing_date > cutoff:
            continue
        if not isinstance(accession, str) or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
            raise SourceError("Invalid filing accession")
        if not isinstance(document, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.html?", document):
            raise SourceError("Invalid SEC primary document name")
        period = data.get("reportDate")
        period_end = period[index] if isinstance(period, list) and len(period) == len(data["form"]) else None
        if period_end:
            if not isinstance(period_end,str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",period_end):
                raise SourceError("Invalid filing report period")
            if date.fromisoformat(period_end) > filing_date:
                raise SourceError("Filing report period is after its filing date")
        rows.append({"accession": accession, "form": form, "filed_at": filed, "period_end": period_end or None,
                     "document": document, "source_index": index})
    return rows


def load_filing_evidence(web, cik, cutoff, contact, preferred_accession=None, refresh=False):
    cik = normalize_cik(cik)
    headers = {"User-Agent": f"Aeternitas/1.0 {contact}", "Accept-Encoding": "identity"}
    raw, directory_source = web.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                                    headers=headers, refresh=refresh)
    try:
        data = json.loads(raw)
        if normalize_cik(data["cik"]) != cik:
            raise ValueError("Company identity mismatch")
        rows = directory_rows(data["filings"]["recent"], cutoff)
        pages = data["filings"].get("files", [])
        if not isinstance(pages, list):
            raise ValueError("Invalid historical index")
        sources = [directory_source]
        # Historical pages may be listed oldest first. Skip wholly future pages
        # and spend the bounded request budget on the nearest eligible history.
        eligible_pages = []
        for page in pages:
            if not isinstance(page, dict):
                raise ValueError("Invalid historical page")
            first, last = page.get("filingFrom"), page.get("filingTo")
            if first and date.fromisoformat(first) > cutoff:
                continue
            if last:
                date.fromisoformat(last)
            eligible_pages.append(page)
        eligible_pages.sort(key=lambda p: p.get("filingTo", ""), reverse=True)
        for page in eligible_pages[:4]:
            found_preferred = not preferred_accession or any(r["accession"] == preferred_accession for r in rows)
            if len(rows) >= 2 and found_preferred:
                break
            name = page["name"]
            if not re.fullmatch(rf"CIK{cik}-submissions-[0-9]{{3}}\.json", name):
                raise ValueError("Invalid historical SEC file name")
            body, source = web.get(f"https://data.sec.gov/submissions/{name}", headers=headers, refresh=refresh)
            rows.extend(directory_rows(json.loads(body), cutoff))
            sources.append(source)
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError("Invalid SEC submissions response") from exc
    unique = {r["accession"]: r for r in rows}
    ordered = sorted(unique.values(), key=lambda r: (r["filed_at"], r["accession"]), reverse=True)
    chosen = []
    if preferred_accession in unique:
        chosen.append(unique[preferred_accession])
    for row in ordered:
        if row not in chosen:
            chosen.append(row)
        if len(chosen) >= 2:
            break
    if not chosen:
        raise SourceError("No covered financial filings before the research cutoff")
    gaps = []
    if preferred_accession and preferred_accession not in unique:
        gaps.append("Annual fact accession not found within bounded directory coverage")
    if len(chosen) < 2:
        gaps.append("Only one filing covered before cutoff")
    evidence = []
    for filing in chosen:
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
               f"{filing['accession'].replace('-', '')}/{filing['document']}")
        body, source = web.get(url, headers=headers, refresh=refresh, ttl=86400,
                               content_types=("text/html", "application/xhtml+xml"))
        sources.append(source)
        text, sections = extract_sections(body, filing["form"])
        text_sha = hashlib.sha256(text.encode()).hexdigest()
        relative = f"extracted/filings/{source['sha256']}-{PARSER_VERSION}.txt"
        write_atomic(web.root / relative, text.encode())
        if len(sections) < 2:
            gaps.append(f"One selected section missing in {filing['accession']}")
        for section in sections:
            evidence.append({"id": f"F-{source['sha256'][:12]}-{section['item']}",
                             "kind": "filing", "company_cik": cik, "source_url": url,
                             "published_at": filing["filed_at"], "time_precision": "date",
                             "title": f"{filing['form']} {section['section']}",
                             "text": section["text"], "source": source,
                             "locator": {**filing, **{k: v for k, v in section.items() if k != "text"},
                                         "parser_version": PARSER_VERSION,
                                         "normalized_path": relative, "normalized_sha256": text_sha}})
    return evidence, sources, gaps
