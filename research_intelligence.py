"""Opt-in RSI v1.3 development preflight; no network or production mutation.

Lexical coverage and citation presence are diagnostics, never entailment scores.
Reuse source audits before searching saved, normalized SEC full text.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
import re

from evidence_repository import digest
from evidence_selection import MAX_SELECTED_TEXT_BYTES
from research_pipeline import audit_inputs
from research_workflow import sha

VERSION = 'research-intelligence-v1.3'


def completeness(root, bundle, supplied, response, topics, *, max_hits=6):
    """Each topic is {id, terms, purpose}; purpose may be counterevidence.

    Search all occurrences, with deterministic capped evidence candidates. A
    cap is explicit: neither absence nor semantic completeness is inferred.
    """
    if not 1 <= max_hits <= 50:
        raise ValueError('Invalid retrieval budget')
    audit_inputs(root, bundle)
    root = Path(root).resolve()
    documents = {}
    for item in bundle['evidence']:
        if item['kind'] != 'filing':
            continue
        loc = item['locator']; path = (root / loc['normalized_path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Source path outside frozen source root')
        if sha(path) != loc['normalized_sha256']:
            raise ValueError('Normalized source hash mismatch')
        documents[str(path)] = (path.read_text(), item)
    cited = {c['evidence_id'] for s in response.get('sections', [])
             for claim in s.get('claims', []) for c in claim.get('citations', [])}
    rows = []; extras = []; bytes_used = 0
    # Evaluate each requested concept independently: finding cash flow must not
    # hide a missing capex concept in the same research question.
    if any(not t['terms'] or any(not isinstance(term,str) or not term.strip() for term in t['terms']) for t in topics):
        raise ValueError('Explicit nonempty topic terms required')
    atomic = [{**t, 'id':t['id']+':'+term, 'terms':[term]} for t in topics for term in t['terms']]
    for topic in atomic:
        terms = topic['terms']
        if not terms or any(not isinstance(t, str) or not t.strip() for t in terms):
            raise ValueError('Explicit nonempty topic terms required')
        pattern = re.compile('|'.join(re.escape(t) for t in terms), re.I)
        hits = []
        for path, (text, original) in sorted(documents.items()):
            for match in pattern.finditer(text):
                start, end = max(0, match.start()-240), min(len(text), match.end()+1000)
                hits.append((path, start, end, original, text[start:end]))
        extracted = [e['id'] for e in bundle['evidence'] if pattern.search(e['text'])]
        delivered = [e['id'] for e in supplied if pattern.search(e['text'])]
        used = sorted(set(delivered) & cited)
        state = ('cited_semantics_unreviewed' if used else 'supplied_not_cited' if delivered
                 else 'extracted_not_supplied' if extracted else 'source_present_not_extracted'
                 if hits else 'not_found_in_scanned_sources')
        emitted = 0
        for path, start, end, original, text in hits[:max_hits]:
            size = len(text.encode())
            if bytes_used+size > MAX_SELECTED_TEXT_BYTES:
                continue
            bytes_used += size; emitted += 1
            e = deepcopy(original)
            e.update(id=bundle['ticker']+':I-'+digest([original['source']['sha256'], start, end])[:24], text=text,
                     title=topic['id'], intelligence_purpose=topic['purpose'])
            e['locator'].update(normalized_start=start, normalized_end=end,
                                selection_version=VERSION)
            extras.append(e)
        rows.append({'topic':topic['id'], 'purpose':topic['purpose'], 'state':state,
                     'matches':len(hits), 'returned':emitted,
                     'truncated':len(hits)>emitted, 'extracted_ids':extracted,
                     'supplied_ids':delivered, 'cited_ids':used,
                     'source_absence_proven':False, 'semantic_review':'pending'})
    unique = {e['id']:e for e in extras}
    return {'version':VERSION, 'symbol':bundle['ticker'], 'topics':rows,
            'source_pins':{p:sha(p) for p in documents},
            'input_fingerprint':digest(supplied), 'retrieved':list(unique.values()),
            'retrieved_fingerprint':digest(list(unique.values())),
            'complete':False, 'retrieval_byte_limit':MAX_SELECTED_TEXT_BYTES,
            'scope':'bounded_lexical_search_of_frozen_filings_only'}


METRIC_FIELDS = {'metric_name','definition_id','accounting_definition','entity','segment',
                'period_start','period_end','currency','unit','basis','scope','source_reference',
                'value','definition_review'}


def validate_metric(metric, source):
    """Require an explicit definition; verify exact value and period at JSON pointer.

    A source tag is not evidence of consolidated scope. Definition review is
    separate and must remain pending until an accountable review supplies it.
    """
    from datetime import date
    if not METRIC_FIELDS <= metric.keys():
        raise ValueError('Incomplete financial metric contract')
    for key in METRIC_FIELDS - {'segment','source_reference'}:
        if not isinstance(metric[key], str) or not metric[key].strip():
            raise ValueError('Missing metric field: '+key)
    if metric['scope'] not in {'consolidated','segment'} or metric['basis'] not in {'GAAP','non-GAAP'}:
        raise ValueError('Explicit scope / accounting basis required')
    if (metric['scope']=='segment') != bool(metric['segment']):
        raise ValueError('Segment identity mismatch')
    if date.fromisoformat(metric['period_start']) >= date.fromisoformat(metric['period_end']):
        raise ValueError('Invalid metric period')
    value = Decimal(metric['value'])
    if not value.is_finite():
        raise ValueError('Nonfinite metric')
    ref = metric['source_reference']
    if digest(source) != ref['content_fingerprint']:
        raise ValueError('Source content changed')
    node = source
    for part in ref['json_pointer'].split('/')[1:]:
        part = part.replace('~1','/').replace('~0','~')
        node = node[int(part)] if isinstance(node,list) else node[part]
    if (Decimal(str(node['val'])) != value or node['start'] != metric['period_start']
            or node['end'] != metric['period_end'] or node['accn'] != ref['accession']):
        raise ValueError('Metric does not match source observation')
    # USD value cannot be silently rescaled to millions; converters need a new contract.
    if metric['unit'] != 'USD' or metric['currency'] != 'USD' or '/units/USD/' not in ref['json_pointer']:
        raise ValueError('Unsupported unit / currency conversion')
    return deepcopy(metric)


def comparable_growth(current, prior):
    from datetime import date
    keys = ['metric_name','definition_id','accounting_definition','entity','segment','currency','unit','basis','scope']
    if any(current[k] != prior[k] for k in keys):
        raise ValueError('Incomparable metric definitions / scope')
    if any(m['definition_review'] != 'approved' for m in (current, prior)):
        raise ValueError('Metric definition requires independent review')
    if (date.fromisoformat(current['period_start'])-date.fromisoformat(prior['period_end'])).days != 1:
        raise ValueError('Nonconsecutive periods')
    lengths=[(date.fromisoformat(m['period_end'])-date.fromisoformat(m['period_start'])).days for m in (current,prior)]
    if abs(lengths[0]-lengths[1])>7:
        raise ValueError('Period duration mismatch')
    a,b = Decimal(current['value']), Decimal(prior['value'])
    if not a.is_finite() or not b.is_finite() or b<=0:
        raise ValueError('Invalid growth denominator')
    with localcontext() as context:
        context.prec=50
        return str((a/b-1)*100)


def financial_growth(current, prior, current_source, prior_source):
    return comparable_growth(validate_metric(current,current_source),
                             validate_metric(prior,prior_source))


def claim_review(claim, evidence):
    """Validate an explicit reasoning dossier; human entailment never auto-passes."""
    required = {'claim_id','text','classification','support','counterevidence',
                'counterevidence_search','falsifier','unknowns'}
    if not required <= claim.keys():
        raise ValueError('Incomplete claim dossier')
    if claim['classification'] not in {'fact','inference','assumption','unknown'}:
        raise ValueError('Invalid claim classification')
    index = {e['id']:e for e in evidence}
    for cite in claim['support']+claim['counterevidence']:
        if not 12<=len(cite['quote'])<=300 or cite['quote'] not in index[cite['evidence_id']]['text']:
            raise ValueError('Invalid source quote')
    if claim['classification'] in {'fact','inference'} and not claim['support']:
        raise ValueError('Support required')
    f = claim['falsifier']
    if not {'observation','logical_bridge','effect','alternative_explanations'} <= f.keys():
        raise ValueError('Explicit falsification logic required')
    if f['effect'] not in {'weakens','refutes','unresolved'}:
        raise ValueError('Invalid falsification effect')
    if any(not isinstance(f[k],str) or not f[k].strip() for k in ['observation','logical_bridge']):
        raise ValueError('Empty falsification logic')
    search = claim['counterevidence_search']
    if (not isinstance(search,dict) or search.get('status') not in {'found','searched_not_found','incomplete'}
            or not isinstance(search.get('source_pins'),list) or not search['source_pins']):
        raise ValueError('Counterevidence search provenance required')
    if search['status']=='found' and not claim['counterevidence']:
        raise ValueError('Claimed counterevidence not attached')
    return {'claim_id':claim['claim_id'], 'literal_match':True,
            'counterevidence_found':bool(claim['counterevidence']),
            'counterevidence_search':claim['counterevidence_search'],
            'entailment':'requires_independent_review','causality':'requires_independent_review',
            'falsification_logic':'requires_independent_review','eligible_for_promotion':False}
