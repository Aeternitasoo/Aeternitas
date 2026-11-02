"""Role scope is explicit input, while immutable older instructions remain verifiable."""
import json

import pytest

from analysis_contracts import (HISTORICAL_PROMPT_VERSION, PROMPT_VERSION,
                                prompt_instruction, role_prompt_versions)
from research_analysis import prompt_for


EVIDENCE=[{'id':'N-controlled','kind':'news','title':'Controlled news',
           'published_at':'2025-09-30T00:00:00+00:00','text':'A controlled news excerpt about a proposed product.'}]
PREVIOUS={'events':{'claims':[], 'gaps':['本角色仅收到新闻摘要，缺少财务材料']}}


def test_current_role_scope_cannot_be_confused_with_global_material_coverage():
    messages=prompt_for('risk','AAPL','2025-10-01','Controlled question',EVIDENCE,PREVIOUS)
    payload=json.loads(messages[1]['content'])
    assert payload['prior_analyses_untrusted']==PREVIOUS
    assert payload['prior_analysis_scopes']['events']=={
        'allowed_evidence_kinds':['news'], 'coverage':'this_role_only_not_global_company_coverage'}
    assert payload['input_evidence_scope']['provided_kinds']==['news']
    assert '不是缺陷' in messages[0]['content'] and '不把' in messages[0]['content']


def test_historical_profile_preserves_old_message_shape_and_instruction_version():
    old=prompt_for('risk','AAPL','2025-10-01','Controlled question',EVIDENCE,PREVIOUS,
                   prompt_version=HISTORICAL_PROMPT_VERSION)
    payload=json.loads(old[1]['content'])
    assert set(payload)=={'company','as_of_date','question','evidence','prior_analyses_untrusted'}
    assert 'input_evidence_scope' not in payload and 'prior_analysis_scopes' not in payload
    assert HISTORICAL_PROMPT_VERSION in old[0]['content']
    assert PROMPT_VERSION not in old[0]['content']
    assert role_prompt_versions(HISTORICAL_PROMPT_VERSION)['risk']=='risk-m4-v1'
    assert role_prompt_versions()['risk']=='risk-m6-v1'


def test_front_roles_never_receive_other_role_results_or_scope_hints():
    payload=json.loads(prompt_for('events','AAPL','2025-10-01','Question',EVIDENCE,PREVIOUS)[1]['content'])
    assert payload['prior_analyses_untrusted']==payload['prior_analysis_scopes']=={}
    assert payload['input_evidence_scope']['role']=='events'


def test_unknown_prompt_version_is_refused_without_loading_an_arbitrary_path():
    with pytest.raises(ValueError,match='Unknown analysis prompt version'):
        prompt_instruction('risk','../../m4')
