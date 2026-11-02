"""Fixed offline checks; no real model/API calls and no promotion approval."""
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from candidate_registry import CandidateRegistry,checked_files
from evidence_repository import digest
from lab_evaluation import load_split,read,compare_versions
from research_contract_profile import LEGACY,MULTI,checked,schema,validate_multi
from research_rsi import (RequestLedger,analyze_research_traces,freeze_real_dataset,propose_candidate,
                          source_record,evaluate_workflow)
from research_workflow import INSTRUCTION,validate_output,run_workflow,verify_workflow
from test_research_workflow import config,sources,factory


def profile(spec=MULTI):
    return {'spec':deepcopy(spec),'instruction':INSTRUCTION,'prompt_version':
            'industry-research-v1' if spec==LEGACY else 'industry-research-multi-excerpt-v2'}


def claim(citations):
    return {'sections':[{'topic':'business','claims':[{'classification':'fact',
        'text':'本项仅核验真实摘录的契约身份','citations':citations}]}]}


def test_same_source_distinct_excerpts_and_legacy_unchanged():
    evidence=[{'id':'source','text':'First original sentence for testing. Second original sentence for testing.'}]
    result=claim([{'evidence_id':'source','quote':'First original sentence for testing.'},
                  {'evidence_id':'source','quote':'Second original sentence for testing.'}])
    frozen=deepcopy((result,evidence));c={'sections':[{'id':'business'}]}
    with pytest.raises(ValueError,match='Duplicate'):validate_output(result,c,evidence)
    assert validate_multi(result,c,evidence)==result and (result,evidence)==frozen


@pytest.mark.parametrize('problem',['same_pair','long','short','unknown','nonliteral','numbers'])
def test_v2_never_relaxes_source_exactness_or_quote_limits(problem):
    text='A valid bounded original quotation for this test. '+'x'*400
    citation={'evidence_id':'e','quote':text[:35]};result=claim([citation]);evidence=[{'id':'e','text':text}]
    if problem=='same_pair':result['sections'][0]['claims'][0]['citations'].append(deepcopy(citation))
    if problem=='long':citation['quote']=text[:301]
    if problem=='short':citation['quote']='A'
    if problem=='unknown':citation['evidence_id']='missing'
    if problem=='nonliteral':citation['quote']='This quotation is not present in the source.'
    if problem=='numbers':result['sections'][0]['claims'][0]['text']='利润增长10%'
    with pytest.raises(ValueError):validate_multi(result,{'sections':[{'id':'business'}]},evidence)


def test_v2_source_and_total_excerpt_budgets():
    evidence=[{'id':str(n),'text':f'Original source {n} includes this sufficiently long quotation. '+'x'*250} for n in range(5)]
    refs=[{'evidence_id':e['id'],'quote':e['text'][:40]} for e in evidence[:4]]
    with pytest.raises(ValueError,match='source count'):validate_multi(claim(refs),{'sections':[{'id':'business'}]},evidence)
    evidence=[{'id':'e','text':''.join(chr(65+n)*250 for n in range(5))}]
    refs=[{'evidence_id':'e','quote':chr(65+n)*250} for n in range(5)]
    with pytest.raises(ValueError,match='budget'):validate_multi(claim(refs),{'sections':[{'id':'business'}]},evidence)


def test_profile_data_cannot_add_permissions_or_change_bounds():
    bad=profile();bad['permissions']=['exec']
    with pytest.raises(ValueError):checked(bad)
    bad=profile();bad['spec']['max_quote_chars']=301
    with pytest.raises(ValueError):checked(bad)
    with pytest.raises(ValueError):checked_files({'contracts/permissions.json':'{}'},'research_contract')
    c=config();actual=schema(c,profile())
    cite=actual['properties']['sections']['items']['properties']['claims']['items']['properties']['citations']
    assert cite['maxItems']==6 and cite['items']['properties']['quote']['maxLength']==300


def multiple_quotes(result):
    for section in result['sections']:
        first=section['claims'][0]['citations'][0]
        section['claims'][0]['citations']=[{**first,'quote':first['quote'][:30]},{**first,'quote':first['quote'][30:60]}]


def test_workflow_profile_roundtrip_and_binding(tmp_path):
    from research_test_support import CONFIG
    folder=run_workflow(config(),tmp_path,provider_config=CONFIG,transports=sources(),
        client_factory=factory([],mutate=multiple_quotes),profile=profile())
    assert verify_workflow(folder)
    assert read(folder/'report.json')['research_contract_version']=='research-contract-v2'
    assert 'research_contract_profile.py' in read(folder/'code-version.json')
    request=read(folder/'request.json')
    assert request['schema']==schema(config(),profile())
    assert read(folder/'execution-profile.json')==profile()
    record=source_record('synthetic',folder,'workflow','synthetic',execution_provenance='recorded_real')
    assert record['real_model_execution'] is False
    criteria={'case_id':'synthetic','split':'holdout','dataset_fingerprint':'f'*64,'independence_group':'synthetic'}
    e=evaluate_workflow(folder,criteria,tmp_path/'evaluations',profile=profile(),version='synthetic',
        expected_input=digest(read(folder/'input.json')),real_request_evidence=False)
    assert e['hard_checks_passed'] and e['metrics']['literal_citation_rate']==1
    assert e['metrics']['tool_call_correctness'] is None and e['legacy_contract_outcome']=='rejected'


def failed_workflow(tmp_path):
    from research_test_support import CONFIG
    from sources.sec import SourceError
    with pytest.raises(SourceError):
        run_workflow(config(),tmp_path,provider_config=CONFIG,transports=sources(),
                     client_factory=factory([],mutate=multiple_quotes))
    return next(p for p in tmp_path.iterdir() if p.is_dir())


def test_failure_analyzer_design_owner_and_generated_candidate(tmp_path):
    folder=failed_workflow(tmp_path/'runs')
    case=source_record('dev',folder,'workflow','group',execution_provenance='controlled');case['split']='development'
    analysis=analyze_research_traces([case],'f'*64)
    owners={i['owner'] for i in analysis['issues']}
    assert 'validator_contract_design' in owners and 'workflow_contract' in owners
    assert analysis['failure_counts']['source_identity_is_not_excerpt_identity']==1
    assert analysis['runs'][0]['diagnostics']['counts']['same_source_distinct_fragments']==2
    registry=CandidateRegistry(tmp_path/'registry',authority_root=tmp_path/'authority',create=True)
    original,candidate=propose_candidate(registry,analysis,'f'*64)
    m,path=registry.version(candidate)
    assert registry.active()==original and m['parent_version']==original
    assert read(path/'files/contracts/research.json')==MULTI
    assert m['proposal_metadata']['side_effects']
    assert '"max_quote_chars": 300' in read(path/'diff.json')['contracts/research.json']
    case['split']='holdout'
    with pytest.raises(ValueError,match='development'):analyze_research_traces([case],'f'*64)


def test_related_real_tasks_cannot_cross_splits(tmp_path):
    def row(name,group):return {'id':name,'kind':'saved_research','independence_group':group,'artifacts':{}}
    splits={'development':[row('d','same')],'regression':[row('r','other')],'holdout':[row('h','same')]}
    with pytest.raises(ValueError,match='crosses'):freeze_real_dataset(tmp_path/'dataset',splits,request_plan=[])


def request(value=1,model='gpt-6.1-sol'):
    body={'model':model,'store':False,'input':[{'role':'developer','content':'Synthetic'},
          {'role':'user','content':json.dumps({'value':value})}]}
    return httpx.Request('POST','https://api.openai.com/v1/responses',json=body)


def test_model_ledger_hard_limit_restart_and_no_resend(tmp_path):
    plan=[{'case_id':str(n),'version':'candidate'} for n in range(5)]
    ledger=RequestLedger(tmp_path/'ledger',plan)
    for n in range(4):
        count=ledger.before_request(request(),case_id=str(n),version='candidate',input_fingerprint=digest({'value':1}))
        ledger.after_response(count,httpx.Response(200))
    restarted=RequestLedger(tmp_path/'ledger',plan)
    with pytest.raises(ValueError,match='budget'):restarted.before_request(request(),case_id='4',version='candidate',input_fingerprint=digest({'value':1}))
    with pytest.raises(ValueError,match='resend'):restarted.before_request(request(),case_id='0',version='candidate',input_fingerprint=digest({'value':1}))
    assert len(list((tmp_path/'ledger').glob('request-*.json')))==4


def test_model_ledger_rejects_input_or_model_change_before_reservation(tmp_path):
    ledger=RequestLedger(tmp_path,[{'case_id':'one','version':'v1'}])
    with pytest.raises(ValueError,match='input'):ledger.before_request(request(2),case_id='one',version='v1',input_fingerprint=digest({'value':1}))
    with pytest.raises(ValueError,match='configuration'):ledger.before_request(request(model='different'),case_id='one',version='v1',input_fingerprint=digest({'value':1}))
    assert not list(tmp_path.glob('request-*.json'))


def test_small_holdout_and_unreviewed_semantics_block_promotion():
    from test_lab_evaluation import fabricated_rows,POLICY
    old=fabricated_rows('old',False);new=fabricated_rows('new',True)
    policy={**POLICY,'minimum_independent_holdout':2,'requires_semantic_review':True}
    result=compare_versions(old,new,policy=policy)
    assert not result['eligible'] and not result['holdout_coverage_gate_passed'] and not result['semantic_review_gate_passed']


def test_input_preparation_reuses_workflow_without_a_model_attempt(tmp_path):
    from research_test_support import CONFIG
    from research_rsi import prepare_frozen_input
    from research_workflow import sha
    original=run_workflow(config(),tmp_path/'original',provider_config=CONFIG,transports=sources(),client_factory=factory([]))
    c=config();c['evidence_kind']='frozen_live_evidence'
    case={'task':{'config':c,'source_root':str(original/'data'),'bundles':{'AAPL':{
        'path':str(original/'AAPL-bundle.json'),'sha256':sha(original/'AAPL-bundle.json')}}}}
    captured=prepare_frozen_input(case,tmp_path/'preflight',profile=profile())
    assert captured['model_requests']==0
    folder=next((tmp_path/'preflight').iterdir())
    assert read(folder/'state.json')['model_attempts']==0
    assert not (folder/'raw-response.json').exists() and not (folder/'model-attempt.json').exists()
    assert captured['input_fingerprint']==digest(read(original/'input.json'))


def test_independent_metrics_preserve_failure_despite_exact_quotes(tmp_path):
    folder=failed_workflow(tmp_path/'failed')
    criteria={'case_id':'original-failure','split':'development','dataset_fingerprint':'f'*64}
    e=evaluate_workflow(folder,criteria,tmp_path/'evaluations',profile=profile(LEGACY),version='old',
        expected_input=digest(read(folder/'input.json')),real_request_evidence=False)
    assert not e['hard_checks_passed'] and e['metrics']['completion']==0
    assert e['metrics']['literal_citation_rate']==1 and e['legacy_contract_outcome']=='rejected'
    assert e['semantic_review']=='requires_independent_human_review'
