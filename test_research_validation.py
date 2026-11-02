"""Offline independent-round controls; all models/HTTP here are synthetic."""
from copy import deepcopy
from pathlib import Path

import pytest

from candidate_registry import CandidateRegistry
from evidence_repository import digest
from lab_evaluation import read
from research_contract_profile import MULTI
from research_rsi import evaluate_legacy_archive
from research_validation import (POLICY,PairedLedger,paired_comparison,failure_analysis,
    create_next_candidate,freeze_validation_dataset,write_review_packet,evaluate_pair_run)
from research_workflow import save,run_workflow
from test_research_rsi import request,profile,multiple_quotes
from test_research_workflow import config,sources,factory


def plan(n=3):return [{'case_id':str(i),'version':v} for i in range(n) for v in ('baseline','candidate')]


def sample(case,variant,passed=True,tokens=100,latency=10,unknown=False):
    criteria={'case_id':case,'split':'holdout','dataset_fingerprint':'f'*64,'independence_group':'issuer-'+case}
    return {'evaluation_id':case+'-'+variant,'evaluator_code_sha256':'e'*64,'variant':variant,'symbol':case,
        'subject_version':variant,'run_path':'/synthetic/'+case+'/'+variant,'criteria':criteria,'criteria_fingerprint':digest(criteria),
        'permission_budget_fingerprint':'same','metrics':{'completion':int(passed),'schema_validity':1,
          'literal_citation_rate':1.0,'evidence_coverage':1.0,'version_contract':int(passed)},
        'costs':{'actual_total_tokens':None if unknown else tokens,'latency_seconds':None if unknown else latency,
          'actual_input_tokens':None if unknown else 50,'actual_output_tokens':None if unknown else tokens-50},
        'failure_categories':[] if passed else ['version_contract'],'actual_model_requests':1,
        'complete_response_available':not unknown,'hard_checks_passed':passed}


def samples():return [sample(str(i),v,v=='candidate') for i in range(3) for v in ('baseline','candidate')]
REG=[{'hard_checks_passed':True}]


def test_six_request_budget_persists_and_no_variant_resend(tmp_path):
    ledger=PairedLedger(tmp_path,plan())
    for p in plan():ledger.before_request(request(),**{'case_id':p['case_id'],'version':p['version'],'input_fingerprint':digest({'value':1})})
    assert len(list(tmp_path.glob('request-*.json')))==6
    again=PairedLedger(tmp_path,plan())
    with pytest.raises(ValueError,match='resend'):again.before_request(request(),case_id='0',version='baseline',input_fingerprint=digest({'value':1}))
    with pytest.raises(ValueError,match='cannot change'):PairedLedger(tmp_path,plan(2))


@pytest.mark.parametrize('bad',[plan(4),[{'case_id':'a','version':'baseline'}],
    [{'case_id':'a','version':'candidate'}]*2])
def test_invalid_pair_plan_rejected(tmp_path,bad):
    with pytest.raises(ValueError):PairedLedger(tmp_path,bad)


def test_all_pairs_visible_and_manual_review_blocks_promotion():
    result=paired_comparison(samples(),REG,POLICY)
    assert result['decision']=='hold_for_more_evidence' and result['independent_groups']==3
    assert result['totals']['baseline']['report_submission_rate']==0
    assert result['totals']['candidate']['report_submission_rate']==1
    assert result['totals']['candidate']['tokens_total']==300
    assert result['totals']['candidate']['tool_call_errors'] is None
    assert not result['human_review_complete'] and not result['statistical_significance_claimed']


def test_single_task_regression_not_hidden_by_average():
    values=samples();values[1]=sample('0','candidate',False)
    result=paired_comparison(values,REG,POLICY)
    assert result['decision']=='reject' and result['totals']['candidate']['report_submission_rate']==pytest.approx(2/3)
    assert not result['pairs'][0]['candidate']['metrics']['completion']


def test_unknown_usage_remains_unknown_and_not_a_free_run():
    values=samples();values[1]=sample('0','candidate',False,unknown=True)
    result=paired_comparison(values,REG,POLICY)
    assert result['decision']=='hold_for_more_evidence'
    assert result['totals']['candidate']['tokens_total'] is None
    assert result['totals']['candidate']['unknown_usage_tasks']==1
    assert result['pairs'][0]['token_delta'] is None


def test_known_severe_regression_not_cancelled_by_another_missing_task():
    values=samples();values[1]=sample('0','candidate',False)
    values[1]['failure_categories']=['nonliteral_quote']
    values[3]=sample('1','candidate',False,unknown=True)
    result=paired_comparison(values,REG,POLICY)
    assert result['decision']=='reject' and result['new_failures']['0']==['nonliteral_quote']


@pytest.mark.parametrize('change',['tokens','latency','policy'])
def test_efficiency_and_policy_gates(change):
    values=samples()
    if change=='tokens':values[1]['costs']['actual_total_tokens']=130
    elif change=='latency':values[1]['costs']['latency_seconds']=25
    else:values[1]['permission_budget_fingerprint']='changed'
    if change=='policy':
        with pytest.raises(ValueError,match='different'):paired_comparison(values,REG,POLICY)
    else:assert paired_comparison(values,REG,POLICY)['decision']=='reject'


def test_duplicate_paired_record_rejected():
    values=samples();values.append(values[0])
    with pytest.raises(ValueError,match='Duplicate'):paired_comparison(values,REG,POLICY)


def test_consumed_and_duplicate_issuers_not_new_holdout(tmp_path):
    frozen={'version_files':{},'implementation':{},'excluded_from_new_holdout':['AAPL']}
    def row(identity,symbol,cik):return {'id':identity,'kind':'saved_research','independence_group':identity,
        'symbol':symbol,'cik':cik,'previously_used':False,'artifacts':{}}
    splits={'development':[row('d','OLD','old')],'regression':[row('r','OTHER','other')],
            'holdout':[row('h','AAPL','a')]}
    with pytest.raises(ValueError,match='Consumed'):freeze_validation_dataset(tmp_path/'used',splits,frozen)
    splits['holdout']=[row('h1','NEW','same'),row('h2','NEW-B','same')]
    with pytest.raises(ValueError,match='duplicate'):freeze_validation_dataset(tmp_path/'alias',splits,frozen)


def test_cross_task_feedback_marked_consumed_and_next_candidate_not_run(tmp_path):
    values=samples()
    for e in values:
        if e['variant']=='candidate':e['costs']['actual_total_tokens']=110;e['costs']['actual_output_tokens']=60
    result=paired_comparison(values,REG,POLICY);analysis=failure_analysis(result,values)
    assert analysis['source_holdout_now_consumed_for_next_generation']
    assert analysis['prohibited_as_next_generation_independent_holdout'] and analysis['proposal']
    registry=CandidateRegistry(tmp_path/'registry',authority_root=tmp_path/'authority',create=True)
    current=registry.create_version({'contracts/research.json':__import__('json').dumps(MULTI),
        'prompts/research.md':'Frozen original candidate instructions.'},kind='research_contract',reason='fixture')
    registry.initialize(current);before=registry.version(current)[0]['content_fingerprint']
    candidate=create_next_candidate(registry,current,analysis,'f'*64)
    meta,_=registry.version(candidate)
    assert meta['parent_version']==current and meta['proposal_metadata']['evaluation_status']=='not_run'
    assert registry.active()==current and registry.version(current)[0]['content_fingerprint']==before


def test_only_input_token_overhead_does_not_invent_output_redundancy():
    values=samples()
    for e in values:
        if e['variant']=='candidate':e['costs']['actual_total_tokens']=110;e['costs']['actual_input_tokens']=60
    result=paired_comparison(values,REG,POLICY)
    assert failure_analysis(result,values)['proposal'] is None


def test_real_component_evaluator_reused_and_human_packet_unscored(tmp_path):
    from research_test_support import CONFIG
    from lab_evaluation import pins
    folder=run_workflow(config(),tmp_path/'runs',provider_config=CONFIG,transports=sources(),
        client_factory=factory([],mutate=multiple_quotes),profile=profile())
    case={'id':'fixture','symbol':'SYNTHETIC','independence_group':'fixture',
        'expected_input_fingerprint':digest(read(folder/'input.json')),'task':{'config':config()}}
    e,path=evaluate_pair_run(folder,case,'candidate','candidate',profile(),tmp_path,'f'*64,attempts=[])
    assert e['metrics']['citation_length_validity']==1 and e['metrics']['meaningless_duplicate_fragments']==0
    assert e['research_semantic_quality'] is None and e['actual_model_requests']==0
    before=pins(folder);b=deepcopy(e);b['variant']='baseline';b['evaluation_id']='other'
    mapping=write_review_packet(tmp_path/'review',[case],[e,b],'fixed-seed')
    records=[read(p) for p in (tmp_path/'review').glob('fixture-*.json')]
    assert len(mapping)==2 and all(r['reviewer'] is None for r in records)
    assert all(all(v is None for v in r['human_review'].values()) for r in records)
    assert all('variant' not in r for r in records) and pins(folder)==before
