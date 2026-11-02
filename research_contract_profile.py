"""Opt-in, versioned same-source multi-excerpt contract for frozen RSI trials.

The old research validator remains unchanged. Profiles contain data, never
executable paths, permissions, budgets or evaluator code supplied by a model.
"""
from copy import deepcopy

from evidence_repository import digest

LEGACY={'version':'research-contract-v1','citation_identity':'evidence_id',
        'max_sources':3,'max_fragments':3,'min_quote_chars':12,'max_quote_chars':300}
MULTI={'version':'research-contract-v2','citation_identity':'evidence_id_and_quote',
       'max_sources':3,'max_fragments':6,'min_quote_chars':12,'max_quote_chars':300,
       'max_claim_quote_chars':1200}


def checked(profile):
    if (not isinstance(profile,dict) or set(profile)!={'spec','instruction','prompt_version'}
            or profile['spec'] not in (LEGACY,MULTI)
            or not isinstance(profile['instruction'],str) or not 1<=len(profile['instruction'])<=6000
            or profile['prompt_version'] not in {'industry-research-v1','industry-research-multi-excerpt-v2'}):
        raise ValueError('Unapproved research execution profile')
    expected='industry-research-v1' if profile['spec']==LEGACY else 'industry-research-multi-excerpt-v2'
    if profile['prompt_version']!=expected:raise ValueError('Contract / Prompt version mismatch')
    return deepcopy(profile)


def schema(config,profile):
    from research_workflow import schema_for
    p=checked(profile);value=schema_for(config)
    if p['spec']==LEGACY:return value
    sections=value['properties']['sections'];sections.update(minItems=len(config['sections']),maxItems=len(config['sections']))
    claims=sections['items']['properties']['claims'];claims.update(minItems=1,maxItems=4)
    text=claims['items']['properties']['text'];text.update(minLength=1,maxLength=1000)
    citations=claims['items']['properties']['citations'];citations.update(minItems=0,maxItems=6)
    citations['items']['properties']['evidence_id'].update(minLength=1,maxLength=200)
    citations['items']['properties']['quote'].update(minLength=12,maxLength=300)
    return value


def validate_multi(result,config,evidence):
    """Same source can support several distinct bounded literal excerpts.

    Shape, classification, quantities, source membership, exact text, per-quote
    limits and unsupported facts keep the original production checks. Only
    citation identity / fragment cardinality is explicitly versioned.
    """
    from research_workflow import validate_output
    from research_analysis import validate_result
    from harness_contracts import validate
    profile={'spec':MULTI,'instruction':'validation only','prompt_version':'industry-research-multi-excerpt-v2'}
    validate(result,schema(config,profile))
    first=deepcopy(result)
    for section in first['sections']:
        for claim in section['claims']:claim['citations']=claim['citations'][:1]
    validate_output(first,config,evidence)
    for section in result['sections']:
        for claim in section['claims']:
            pairs=set();sources=set();length=0
            for citation in claim['citations']:
                identity=(citation['evidence_id'],citation['quote'])
                if identity in pairs:raise ValueError('Duplicate identical source / quote fragment')
                pairs.add(identity);sources.add(identity[0]);length+=len(identity[1])
                validate_result({'claims':[{'text':claim['text'],'stance':'neutral','citations':[citation]}],
                                 'gaps':[]},evidence)
            if len(sources)>MULTI['max_sources']:raise ValueError('Distinct source count exceeds contract')
            if length>MULTI['max_claim_quote_chars']:raise ValueError('Claim excerpt budget exceeded')
    return result


def validate_output_profile(result,config,evidence,profile):
    from research_workflow import validate_output
    p=checked(profile)
    return validate_output(result,config,evidence) if p['spec']==LEGACY else validate_multi(result,config,evidence)


def from_files(folder):
    import json
    from pathlib import Path
    folder=Path(folder)
    spec=json.loads((folder/'contracts/research.json').read_text())
    return checked({'spec':spec,'instruction':(folder/'prompts/research.md').read_text(),
                    'prompt_version':'industry-research-v1' if spec==LEGACY else 'industry-research-multi-excerpt-v2'})
