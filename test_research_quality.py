"""Offline barrier integration; source and model transports are synthetic."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from test_research_workflow import config,sources,factory,CONFIG
from research_workflow import run_workflow,verify_workflow
from research_quality import QualityBarrier,challenge,model_schema
from test_research_intelligence import metric
from research_intelligence import comparable_growth


def policy():
    return {'topics':{'AAPL':[{'id':'cash','terms':['operating'],'purpose':'support'},
        {'id':'risk','terms':['risk'],'purpose':'counterevidence'}]},'metrics':[],'calculations':[],'max_hits':1}


def enhanced(result):
    for section in result['sections']:
        for c in section['claims']:
            c.update(reasoning='证据支持的范围需复核',counterevidence=[],counterevidence_status='incomplete',metric_ids=[],
              falsification={'observation':'后续事实与方向相反','logical_bridge':'需核实边际贡献及替代解释',
                            'effect':'unresolved','alternative_explanations':['其他需求因素']})


def run(tmp_path, mutate=enhanced,p=None):
    calls=[]
    folder=run_workflow(config(),tmp_path,provider_config=CONFIG,transports=sources(),
        client_factory=factory(calls,mutate),quality_policy=p or policy())
    return folder,calls


def test_barriers_really_run_and_only_incomplete_publication(tmp_path):
    folder,calls=run(tmp_path)
    report=json.loads((folder/'report.json').read_text())
    assert len(calls)==1 and report['status']=='incomplete_research'
    assert report['financials']['annual_periods']==[]
    assert report['financials']['revenue_yoy']['value'] is None
    assert report['eligible_for_promotion'] is False
    assert (folder/'quality-input.json').exists() and (folder/'quality-evaluation.json').exists()
    assert verify_workflow(folder)
    assert 'quality_context' in calls[0]


@pytest.mark.parametrize('mode',['omit_dossier','unreviewed_numeric','counterfeit_metric','invent_counterevidence','empty_falsifier'])
def test_model_cannot_bypass_barrier_by_format(tmp_path,mode):
    def mutate(result):
        enhanced(result);c=result['sections'][0]['claims'][0]
        if mode=='omit_dossier':c.pop('reasoning')
        elif mode=='unreviewed_numeric':c['text']='AI收入增长20%'
        elif mode=='counterfeit_metric':c['metric_ids']=['ai-revenue-not-approved']
        elif mode=='invent_counterevidence':c['counterevidence_status']='found'
        else:c['falsification']['logical_bridge']=''
    with pytest.raises(Exception):run(tmp_path,mutate)
    folder=next(tmp_path.iterdir());assert not (folder/'COMMITTED.json').exists()
    assert (folder/'failure.json').exists()


@pytest.mark.parametrize('key,value',[('definition_id','customer-contract'),('entity','subsidiary'),
    ('definition_id','non-ai-revenue'),('basis','non-GAAP'),('unit','million USD'),('period_start','2025-02-01')])
def test_financial_scope_mismatch_refused(key,value):
    current=metric();prior=metric('23568000000',2024);current[key]=value
    with pytest.raises(ValueError):comparable_growth(current,prior)


def test_unaudited_financial_record_blocks_before_model(tmp_path):
    p=policy();p['metrics']=[{'metric_id':'bad','source_path':'missing','source_sha256':'0'*64,'metric':metric()}]
    with pytest.raises(Exception):run(tmp_path,p=p)
    folder=next(tmp_path.iterdir());assert not (folder/'model-attempt.json').exists()


def test_checker_timeout_fails_closed(tmp_path):
    p=policy();p['checker_seconds']=0.000001
    with pytest.raises(Exception):run(tmp_path,p=p)
    folder=next(tmp_path.iterdir());assert not (folder/'COMMITTED.json').exists()


def test_literal_quote_never_grants_semantic_approval(tmp_path):
    folder,_=run(tmp_path)
    e=json.loads((folder/'quality-evaluation.json').read_text())
    assert e['semantic_support']=='pending_independent_review'
    assert e['falsification_validity']=='pending_independent_review'
    assert e['formal_release_allowed'] is False


def test_preflight_scope_equal_in_comparison_arm(tmp_path):
    a,_=run(tmp_path/'candidate')
    calls=[]
    b=run_workflow(config(),tmp_path/'baseline',provider_config=CONFIG,transports=sources(),
        client_factory=factory(calls),comparison_context=policy())
    assert json.loads((a/'input.json').read_text())==json.loads((b/'input.json').read_text())
    assert verify_workflow(b)


def test_full_source_read_failure_blocks_before_model(tmp_path,monkeypatch):
    original=QualityBarrier.call
    def corrupt(self,name,**kwargs):
        if name=='prepare':
            bundle=next(iter(kwargs['bundles'].values()))
            filing=next(e for e in bundle['evidence'] if e['kind']=='filing')
            (Path(kwargs['root'])/filing['locator']['normalized_path']).write_text('corrupted synthetic fixture')
        return original(self,name,**kwargs)
    monkeypatch.setattr(QualityBarrier,'call',corrupt)
    with pytest.raises(Exception):run(tmp_path)
    folder=next(tmp_path.iterdir())
    assert not (folder/'model-attempt.json').exists() and not (folder/'COMMITTED.json').exists()
    assert (folder/'quality-failure.json').exists()


def test_invalid_falsifier_is_unverified_not_a_semantic_pass(tmp_path):
    def mutate(result):
        enhanced(result)
        result['sections'][0]['claims'][0]['falsification']={
            'observation':'非AI占比超过一半','logical_bridge':'所以AI没有任何正向贡献',
            'effect':'refutes','alternative_explanations':[]}
    folder,_=run(tmp_path,mutate)
    evaluation=json.loads((folder/'quality-evaluation.json').read_text())
    assert evaluation['formal_release_allowed'] is False
    assert evaluation['falsification_validity']=='pending_independent_review'


def test_counterevidence_omission_and_truncation_are_visible(tmp_path):
    folder,_=run(tmp_path)
    package=json.loads((folder/'quality-input.json').read_text())
    evaluation=json.loads((folder/'quality-evaluation.json').read_text())
    assert package['context']['full_source_completeness_certified'] is False
    assert package['context']['pagination_and_document_coverage'].endswith('not_certified_complete')
    assert all(c['counterevidence_search']['status']=='incomplete' for c in evaluation['claims'])
    assert evaluation['status']=='incomplete_research'


def test_permitted_existing_source_can_supply_counterevidence():
    """Requirement regression discovered in live ORCL; frozen candidate still fails.

    A retrieval-purpose label is not source authorization or semantic approval.
    Do not xfail or weaken this test to make the frozen candidate look ready.
    """
    from test_research_workflow import output
    from research_workflow import schema_for
    evidence=[{'id':'existing-risk','text':'The company faces material competition and pricing pressure.'}]
    result=output({'evidence':evidence,'sections':config()['sections']});enhanced(result)
    c=result['sections'][0]['claims'][0]
    c['counterevidence']=[{'evidence_id':'existing-risk','quote':evidence[0]['text']}]
    c['counterevidence_status']='found'
    package={'context':{'audited_metrics':{}},'source_pins':{'synthetic':'f'*64}}
    checked=challenge(result=result,evidence=evidence,package=package,schema=model_schema(schema_for(config())))
    assert checked['formal_release_allowed'] is False
