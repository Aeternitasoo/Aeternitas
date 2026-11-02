"""Deterministic issuer discovery from SEC's public ticker directory.

No model guesses a CIK. The existing two-company configuration remains an
explicit local override; it is no longer a whitelist. Resolution is a bounded
preflight, separate from a queued research task's execution deadline.
"""
import json
import re

from research_runtime import Budget, WebSources
from sources.sec import SourceError

DIRECTORY_URL = 'https://www.sec.gov/files/company_tickers.json'


class CompanyError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def normalize_ticker(value):
    if not isinstance(value, str):
        raise CompanyError('invalid_ticker', 'Enter a stock ticker, for example PLTR or NVDA')
    ticker = value.strip().upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*', ticker) or len(ticker) > 15:
        raise CompanyError('invalid_ticker', 'Enter a stock ticker of at most 15 characters; no paths or company names')
    # SEC uses BRK-B, while many terminals accept BRK.B. Exchange suffixes
    # are not stripped: 0700.HK must never turn into a different US company.
    if re.fullmatch(r'[A-Z]+\.[AB]', ticker):
        ticker = ticker.replace('.', '-')
    return ticker


def directory_entries(raw):
    try:
        table = json.loads(raw)
        if not isinstance(table, dict) or not table:
            raise ValueError()
        entries = {}
        for entry in table.values():
            ticker, cik, name = entry['ticker'], entry['cik_str'], entry['title']
            if type(cik) is not int or not 0 < cik < 10**10 or not isinstance(name, str) or not name.strip():
                raise ValueError()
            if not isinstance(ticker,str):
                raise ValueError()
            try:
                canonical = normalize_ticker(ticker)
            except CompanyError:
                # The real SEC directory includes a NONE. placeholder. An
                # unsupported symbol must not invalidate unrelated valid rows.
                continue
            row = dict(ticker=canonical, cik=f'{cik:010d}', name=name.strip())
            if canonical in entries and entries[canonical] != row:
                raise ValueError()
            entries[canonical] = row
        if not entries:
            raise ValueError()
        return entries
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise CompanyError('invalid_company_directory', 'SEC company directory is invalid or ambiguous; no company was selected') from None


def resolve_company(ticker, root, contact, *, overrides=None, transport=None, refresh=False):
    ticker = normalize_ticker(ticker)
    if ticker in (overrides or {}):
        row = overrides[ticker]
        cik = row.get('cik')
        if not isinstance(cik, str) or not re.fullmatch('[0-9]{10}', cik):
            raise CompanyError('invalid_company_override', 'Configured company CIK must contain ten digits')
        return dict(ticker=ticker, cik=cik, name=row.get('name'), source='local_override', directory=None)
    if '.' in ticker:
        raise CompanyError('unsupported_market', 'This SEC workflow supports US filing issuers; exchange-suffixed foreign markets need another audited data adapter')
    if not isinstance(contact, str) or not contact.isascii() or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', contact):
        raise CompanyError('sec_contact_missing', 'Configure SEC_CONTACT locally before automatic company discovery')
    try:
        with WebSources(root, Budget(12), transport=transport) as source:
            raw, receipt = source.get(DIRECTORY_URL, ttl=86400, refresh=refresh,
                                      headers={'User-Agent': f'Aeternitas/1.0 {contact}', 'Accept': 'application/json'})
        entries = directory_entries(raw)
    except SourceError:
        raise CompanyError('company_lookup_unavailable', 'SEC company lookup failed; no model call was made. Retry later or use resolve --refresh to repair the cache') from None
    if ticker not in entries:
        raise CompanyError('unknown_company', 'Unknown company ticker in the current SEC directory; check the code or market. No model call was made')
    return {**entries[ticker], 'source': 'sec_directory', 'directory': receipt}
