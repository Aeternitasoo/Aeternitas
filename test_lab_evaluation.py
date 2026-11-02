"""Offline SDK/export, independent scoring and gated candidate regressions."""
from copy import deepcopy
from functools import partial
import json
from pathlib import Path
import time
from uuid import uuid4

import pytest

from candidate_registry import CandidateRegistry,checked_files
from evidence_repository import digest
from lab_evaluation import (analyze_failures,compare_versions,evaluate_run,freeze_dataset,
                            load_split,pins,read,evaluate_saved_research)
from lab_evaluation_demo import (BASE_SKILL,CHANGE,POLICY,execute_case,fixture_splits,run_demo)
from langfuse_adapter import Config,LangfuseAdapter,Mapper,sanitize,trace_id,mask_data
from research_observability import Recorder
from research_workflow import save,sha


class FakeObservation:
    def __init__(self,sdk,kw):
        self.sdk=sdk;self.id=uuid4().hex[:16]
        sdk.write({'kind':'start','id':self.id,**kw})
    def update(self,**kw):self.sdk.write({'kind':'update','id':self.id,**kw})
    def end(self):self.sdk.write({'kind':'end','id':self.id})


class FakeSDK:
    def __init__(self,path,fail=False,delay=0):self.path=Path(path);self.fail=fail;self.delay=delay
    def write(self,value):
        if self.fail:raise RuntimeError('SECRET_DO_NOT_EXPORT')
        with self.path.open('a') as stream:stream.write(json.dumps(value)+'\n')
    def start_observation(self,**kw):return FakeObservation(self,kw)
    def create_score(self,**kw):self.write({'kind':'score',**kw})
    def flush(self):
        if self.delay:time.sleep(self.delay)
        self.write({'kind':'flush'})
    def shutdown(self):self.write({'kind':'shutdown'})


def fake_factory(config,*,path,fail=False,delay=0):return FakeSDK(path,fail,delay)
def dead_factory(config):raise RuntimeError('API_KEY_DO_NOT_LOG')


def export_config(timeout=2,queue=128):return Config('pk-test','not-a-real-credential','http://localhost',queue,timeout)
def events(path):return [json.loads(line) for line in Path(path).read_text().splitlines()]
def event(phase='start',parent=None,name='harness',sid='1'*32):
    return {'trace_id':'local-run','span_id':sid,'parent_span_id':parent,'name':name,'phase':phase}


def test_mapper_trace_parent_generation_tokens_and_evaluation(tmp_path):
    sdk=FakeSDK(tmp_path/'sdk.jsonl');mapper=Mapper(sdk)
    for e in [event(),event(name='harness_model',sid='2'*32,parent='1'*32),
              {**event('end',name='harness_model',sid='2'*32,parent='1'*32),'model':'gpt-fixture',
               'prompt_version':'fixture-v1','input_tokens':7,'output_tokens':3,'total_tokens':10,'status':'completed'},
              event('end')]:mapper.event(sanitize(e))
    rows=events(sdk.path);starts=[r for r in rows if r['kind']=='start']
    assert starts[1]['trace_context']['parent_span_id']==starts[0]['id']
    assert starts[0]['trace_context']['trace_id']==trace_id('local-run')
    assert starts[1]['as_type']=='generation'
    assert rows[2]['usage_details']=={'input':7,'output':3,'total':10}
    mapper.score({'trace_id':trace_id('local-run'),'evaluation_id':'e1','evaluator_version':'v1','metrics':{'completion':1}})
    assert events(sdk.path)[-1]['kind']=='score'


def test_mapper_unknown_tokens_and_dropped_parent(tmp_path):
    sdk=FakeSDK(tmp_path/'sdk.jsonl');mapper=Mapper(sdk)
    with pytest.raises(ValueError,match='parent'):mapper.event(sanitize(event(parent='9'*32)))
    mapper.event(sanitize(event(name='harness_model')))
    mapper.event(sanitize(event('end',name='harness_model')))
    assert 'usage_details' not in events(sdk.path)[1]


def test_real_sdk_in_memory_no_network():
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter=InMemorySpanExporter()
    sdk=Langfuse(public_key='pk-in-memory-only',secret_key='not-live',base_url='http://127.0.0.1:1',
                 tracer_provider=TracerProvider(),span_exporter=exporter,flush_interval=100,mask=mask_data)
    mapper=Mapper(sdk)
    mapper.event(sanitize(event()))
    mapper.event(sanitize(event(name='harness_model',parent='1'*32,sid='2'*32)))
    mapper.event(sanitize({**event('end',name='harness_model',parent='1'*32,sid='2'*32),'total_tokens':10}))
    mapper.event(sanitize(event('end')))
    sdk.flush()
    spans=exporter.get_finished_spans()
    root=next(s for s in spans if s.name=='harness');model=next(s for s in spans if s.name=='harness_model')
    assert model.parent.span_id==root.context.span_id
    assert root.context.trace_id==int(trace_id('local-run'),16)
    assert any('local-run' not in str(s.attributes) and 'run_id_hash' in str(s.attributes) for s in spans)
    sdk.shutdown()


def test_export_content_allowlist_and_secrets():
    value=sanitize({**event(),'input':'private financial report','api_key':'secret',
        'model':'Bearer_credential','error_code':'sk-private','tool_name':'fixture.verify',
        'input_tokens':None,'output_tokens':False,'total_tokens':float('nan'),
        'response_id':'sensitive-response-id','parent_run_id':'parent','prompt_sha256':'a'*64})
    text=json.dumps(value)
    assert all(s not in text for s in ['private financial','api_key','Bearer','sk-private','sensitive-response-id'])
    assert value['tool_name']=='fixture.verify' and 'input_tokens' not in value
    assert 'response_id_hash' in value and 'parent_run_id_hash' in value
    assert 'total_tokens' not in value and 'output_tokens' not in value


def test_disabled_no_credentials_no_process(monkeypatch):
    for key in ('LANGFUSE_PUBLIC_KEY','LANGFUSE_SECRET_KEY','LANGFUSE_BASE_URL','LANGFUSE_HOST'):monkeypatch.delenv(key,raising=False)
    assert Config.from_env() is None
    adapter=LangfuseAdapter();adapter(event());assert not adapter.flush();adapter.shutdown()
    assert adapter.process is None and adapter.stats['accepted']==0


def test_async_bounded_queue_timeout_and_fail_open(tmp_path):
    adapter=LangfuseAdapter(export_config(timeout=0.2,queue=1),sdk_factory=partial(fake_factory,path=str(tmp_path/'sdk.jsonl'),delay=20))
    started=time.monotonic()
    for _ in range(2000):adapter(event())
    assert time.monotonic()-started<1
    assert adapter.stats['dropped']>0
    assert not adapter.flush()
    assert adapter.stats['timeouts']==1 and not adapter.process.is_alive()
    adapter.shutdown()


@pytest.mark.parametrize('factory',[dead_factory,None])
def test_local_recorder_survives_export_failure(tmp_path,factory):
    factory=factory or partial(fake_factory,path=str(tmp_path/'sdk.jsonl'),fail=True)
    adapter=LangfuseAdapter(export_config(),sdk_factory=factory)
    recorder=Recorder(tmp_path/'events.jsonl','local',adapter)
    with recorder.span('harness'):
        with recorder.span('harness_tool',tool_name='fixture.verify'):pass
    adapter.flush();adapter.shutdown()
    assert len(events(recorder.path))==4
    assert 'SECRET' not in recorder.path.read_text()


def test_adapter_flush_shutdown_and_score(tmp_path):
    path=tmp_path/'sdk.jsonl'
    adapter=LangfuseAdapter(export_config(),sdk_factory=partial(fake_factory,path=str(path)))
    adapter(event());adapter(event('end'))
    adapter.evaluation({'run_id':'local-run','evaluation_id':'a'*32,'evaluator_version':'v1',
                        'metrics':{'completion':True,'unknown':None}})
    assert adapter.flush() and adapter.shutdown()
    assert [r['kind'] for r in events(path)]==['start','update','end','score','flush','flush','shutdown']


@pytest.fixture
def fixture_release(tmp_path):
    registry=CandidateRegistry(tmp_path/'registry',authority_root=tmp_path/'authority',scope='controlled_fixture',create=True)
    baseline=registry.create_version({'skills/fixture-research/SKILL.md':BASE_SKILL},kind='skill',reason='baseline')
    registry.initialize(baseline)
    pin=freeze_dataset(tmp_path/'dataset',fixture_splits(),version='fixture-v1',policy=POLICY)
    return registry,baseline,pin


def test_independent_metrics_and_explicit_weights(tmp_path,fixture_release):
    registry,baseline,pin=fixture_release
    cases,_=load_split(tmp_path/'dataset','development',pin)
    e,path=execute_case(cases[0],baseline,registry,tmp_path,pin)
    assert not e['hard_checks_passed'] and e['metrics']['completion']==1
    assert e['failure_categories']==['expected_output','required_tools']
    assert e['overall_score']==0.5 and e['costs']['actual_total_tokens'] is None
    assert e['costs']['currency_cost'] is None and e['semantic_review']=='requires_human_review'
    assert read(path)==e
    c=deepcopy(e['criteria']);del c['weights']
    again,_=evaluate_run(e['run_path'],c,tmp_path/'other',expected_files=e['run_files'])
    assert again['overall_score'] is None and again['metrics']==e['metrics']
    c['weights']={'unknown':1}
    with pytest.raises(ValueError,match='weights'):evaluate_run(e['run_path'],c,tmp_path/'bad')


def test_dataset_split_isolation_hash_and_content_duplicates(tmp_path):
    sets=fixture_splits();sets['holdout'][0]={**sets['development'][0],'id':'different-id'}
    with pytest.raises(ValueError,match='content'):freeze_dataset(tmp_path/'duplicate',sets,version='v1',policy=POLICY)
    pin=freeze_dataset(tmp_path/'dataset',fixture_splits(),version='v1',policy=POLICY)
    cases,_=load_split(tmp_path/'dataset','development',pin)
    assert all(c['split']=='development' for c in cases)
    target=tmp_path/'dataset/holdout/holdout-7.json';target.write_text('{}')
    assert load_split(tmp_path/'dataset','development',pin)[0]==cases
    with pytest.raises(ValueError,match='changed'):load_split(tmp_path/'dataset','holdout',pin)
    with pytest.raises(ValueError,match='manifest'):load_split(tmp_path/'dataset','holdout','a'*64)


def test_failure_analysis_never_consumes_holdout():
    with pytest.raises(ValueError,match='development'):analyze_failures([{'criteria':{'split':'holdout'}}])


@pytest.mark.parametrize('path,kind',[
    ('../../agent_harness.py','harness'),('skills/x/config.json','skill'),
    ('permissions.json','prompt'),('harness/limits.json','harness')])
def test_candidate_scope_restrictions(path,kind):
    with pytest.raises(ValueError,match='scope'):checked_files({path:'data'},kind)


def test_candidate_isolation_diff_and_tampering(tmp_path,fixture_release):
    registry,baseline,pin=fixture_release
    dev,_=load_split(tmp_path/'dataset','development',pin)
    e,_=execute_case(dev[0],baseline,registry,tmp_path,pin)
    analysis=analyze_failures([e])
    new=registry.create_version({'skills/fixture-research/SKILL.md':BASE_SKILL+CHANGE},kind='skill',parent=baseline,
        reason='controlled improvement',failure_analysis=analysis,dataset_fingerprint=pin)
    m,path=registry.version(new);base=registry.version(baseline)[1]/'files/skills/fixture-research/SKILL.md'
    assert base.read_text()==BASE_SKILL and registry.active()==baseline
    assert '+verify-before-final' in read(path/'diff.json')['skills/fixture-research/SKILL.md']
    assert m['failure_analysis']['evaluation_ids']==[e['evaluation_id']]
    (path/'files/skills/fixture-research/SKILL.md').write_text('tamper')
    with pytest.raises(ValueError,match='changed'):registry.version(new)


def fabricated_rows(version,passed=True):
    return [{'evaluation_id':uuid4().hex,'created_at':'2026-10-30T00:00:00+00:00','subject_version':version,
        'criteria':{'case_id':s,'split':s,'dataset_fingerprint':'f'*64},'criteria_fingerprint':digest(s),
        'hard_checks_passed':passed,'failure_categories':[] if passed else ['required_tools'],
        'metrics':{'completion':1,'required_tools':int(passed)},
        'costs':{'actual_total_tokens':None,'currency_cost':None,'latency_seconds':1}}
        for s in ('regression','holdout')]


def test_version_comparison_unknown_cost_and_new_failure():
    old=fabricated_rows('old',False);new=fabricated_rows('new',True)
    result=compare_versions(old,new,policy=POLICY)
    assert result['eligible'] and result['improved_cases']==2
    assert result['cases'][0]['token_delta'] is None
    policy={**POLICY,'scope':'research','allow_unknown_cost':False}
    assert not compare_versions(old,new,policy=policy)['eligible']
    new[0]['failure_categories']=['unsafe'];new[0]['hard_checks_passed']=False
    assert not compare_versions(old,new,policy=POLICY)['eligible']
    new[0]['criteria_fingerprint']='changed'
    with pytest.raises(ValueError,match='criteria'):compare_versions(old,new,policy=POLICY)


def test_non_fixture_cannot_simulate_approval(tmp_path):
    registry=CandidateRegistry(tmp_path/'production',authority_root=tmp_path/'private',create=True)
    with pytest.raises(ValueError,match='Simulation'):registry.fixture_approval('promote','a'*32,'f'*64)


def test_manual_approval_required_noninteractive(tmp_path,fixture_release,monkeypatch):
    registry,baseline,pin=fixture_release
    monkeypatch.setattr('sys.stdin.isatty',lambda:False)
    with pytest.raises(ValueError,match='interactive'):registry.approve_interactively('promote',baseline,pin)


def test_complete_offline_cycle_and_manual_gate(tmp_path):
    trace_log=tmp_path/'mock-langfuse.jsonl'
    with LangfuseAdapter(export_config(queue=256),sdk_factory=partial(fake_factory,path=str(trace_log))) as adapter:
        result=run_demo(tmp_path/'demo',sink=adapter)
        assert adapter.flush()
    rows=events(trace_log)
    assert len([r for r in rows if r['kind']=='start' and r['name']=='harness'])==10
    assert len([r for r in rows if r['kind']=='start' and r['as_type']=='generation'])==24
    assert any(r['kind']=='score' for r in rows)
    assert adapter.stats['dropped']==0
    assert result['development_failures']==2 and result['baseline_passed']==0 and result['candidate_passed']==4
    assert result['evaluation_order']==['regression','holdout']
    assert result['active_after_rollback']==result['baseline_version']
    assert result['human_approval']=='simulated_fixture_only' and result['real_model_requests']==0
    assert result['promotion_generation']==1 and result['rollback_generation']==2
    root=tmp_path/'demo';registry=CandidateRegistry(root/'registry',authority_root=root/'manual-authority',scope='controlled_fixture')
    report=Path(result['comparison_path']);pin=sha(report);candidate=result['candidate_version']
    frozen_report=read(report)
    with pytest.raises(ValueError,match='policy changed'):
        registry.record_comparison(candidate,frozen_report['baseline_reports'],frozen_report['candidate_reports'],
            policy={**POLICY,'max_latency_seconds':9999},dataset_root=root/'dataset')
    bad=root/'forged.json';save(bad,{'approved':True})
    with pytest.raises((ValueError,KeyError)):registry.apply('promote',candidate,bad,report)
    changed=read(report);changed['eligible']=False
    failed=root/'registry/comparisons/failed.json';save(failed,changed)
    receipt=registry.fixture_approval('promote',candidate,sha(failed))
    with pytest.raises(ValueError,match='Failed'):registry.apply('promote',candidate,receipt,failed)
    approvals=list((root/'registry/approvals').glob('*.json'))
    original=next(p for p in approvals if read(p)['action']=='promote' and read(p)['comparison_sha256']==pin)
    with pytest.raises(ValueError,match='already used'):registry.apply('promote',candidate,original,report)


def test_pinned_run_tampering_rejected(tmp_path,fixture_release):
    registry,baseline,pin=fixture_release
    case=load_split(tmp_path/'dataset','development',pin)[0][0]
    e,_=execute_case(case,baseline,registry,tmp_path,pin)
    (Path(e['run_path'])/'final.json').write_text('{}')
    with pytest.raises(ValueError,match='changed'):evaluate_run(e['run_path'],e['criteria'],tmp_path/'eval2',expected_files=e['run_files'])


def test_saved_response_rejection_and_hash(tmp_path):
    values={'response':{'sections':[]},'config':{'sections':[{'id':'required'}]},'evidence':[]}
    artifacts={}
    for name,value in values.items():
        path=tmp_path/(name+'.json');save(path,value);artifacts[name]={'path':str(path),'sha256':sha(path)}
    case={'id':'saved-failure','expected_outcome':'rejected','artifacts':artifacts}
    result=evaluate_saved_research(case,dataset_fingerprint='a'*64)
    assert result['actual_outcome']=='rejected' and result['hard_checks_passed']
    (tmp_path/'response.json').write_text('{}')
    with pytest.raises(ValueError,match='changed'):evaluate_saved_research(case,dataset_fingerprint='a'*64)


def test_failed_run_is_evaluated_and_preserved(tmp_path,fixture_release):
    from agent_harness import run_harness,ToolRegistry
    from agent_skills import SkillRegistry
    from lab_evaluation_demo import RESULT
    from test_agent_harness import Script,action
    registry,baseline,pin=fixture_release
    skills=SkillRegistry(registry.version(baseline)[1]/'files/skills')
    run=run_harness('Synthetic unauthorized call',Script([action(name='os.system')]),ToolRegistry(),skills,
        tmp_path/'failed-runs',allowed_tools=frozenset({'skills.load'}),permissions=frozenset({'skills.read'}),
        result_schema=RESULT)
    original=pins(run)
    e,_=evaluate_run(run,{'case_id':'refusal','split':'regression','dataset_fingerprint':pin,
        'expected_status':'failed','expected_error':'tool_not_registered'},tmp_path/'evaluation',expected_files=original)
    assert e['hard_checks_passed'] and e['metrics']['completion']==0
    assert e['metrics']['tool_request_validity']==0 and e['metrics']['execution_policy']==1
    assert pins(run)==original and 'tool_not_registered' in e['failure_categories']


def test_saved_citation_membership_and_quote_validation(tmp_path):
    # Reuse production financial/citation contracts; no semantic score invented.
    quote='Synthetic evidence supports a bounded execution check.'
    evidence=[{'id':'fixture-1','text':quote}]
    response={'sections':[{'topic':'business','claims':[{'classification':'fact',
        'text':'仅核验合成资料的逐字引用','citations':[{'evidence_id':'fixture-1','quote':quote}]}]}]}
    artifacts={}
    for name,value in {'response':response,'config':{'sections':[{'id':'business'}]},'evidence':evidence}.items():
        p=tmp_path/(name+'.json');save(p,value);artifacts[name]={'path':str(p),'sha256':sha(p)}
    case={'id':'quote','artifacts':artifacts,'expected_outcome':'accepted'}
    result=evaluate_saved_research(case,dataset_fingerprint='a'*64)
    assert result['hard_checks_passed'] and result['semantic_review']=='requires_human_review'
    response['sections'][0]['claims'][0]['citations'][0]['evidence_id']='missing'
    p=tmp_path/'bad.json';save(p,response);case['artifacts']['response']={'path':str(p),'sha256':sha(p)}
    case['expected_outcome']='rejected'
    result=evaluate_saved_research(case,dataset_fingerprint='a'*64)
    assert result['hard_checks_passed'] and result['actual_outcome']=='rejected'


def test_prompt_candidate_runs_as_data_not_code(tmp_path):
    registry=CandidateRegistry(tmp_path/'registry',authority_root=tmp_path/'private',scope='controlled_fixture',create=True)
    base=registry.create_version({'prompts/fixture.md':'Return the synthetic answer.'},kind='prompt',reason='baseline')
    registry.initialize(base)
    pin=freeze_dataset(tmp_path/'dataset',fixture_splits(),version='v1',policy=POLICY)
    c=load_split(tmp_path/'dataset','development',pin)[0][0]
    e,_=execute_case(c,base,registry,tmp_path,pin)
    candidate=registry.create_version({'prompts/fixture.md':'Return the synthetic answer.'+CHANGE},kind='prompt',
        parent=base,reason='Add registered verification instruction',failure_analysis=analyze_failures([e]),dataset_fingerprint=pin)
    n,_=execute_case(c,candidate,registry,tmp_path,pin)
    assert not e['hard_checks_passed'] and n['hard_checks_passed']
    assert e['permission_budget_fingerprint']==n['permission_budget_fingerprint']


def test_harness_candidates_archived_without_execution(tmp_path):
    registry=CandidateRegistry(tmp_path/'registry',authority_root=tmp_path/'private',create=True)
    identity=registry.create_version({'harness/agent_harness.py':'# frozen patch for manual review\n'},
        kind='harness',reason='Code candidate archive only')
    meta,_=registry.version(identity)
    assert meta['execution_policy']=='archive_only_requires_separate_trusted_runner'
    with pytest.raises(ValueError,match='trusted isolation runner'):
        registry.record_comparison(identity,[],[],policy={**POLICY,'scope':'research'})


def test_known_token_latency_and_permission_regression_gates():
    baseline=fabricated_rows('base',False);candidate=fabricated_rows('candidate',True)
    for e in baseline:e['costs']['actual_total_tokens']=100;e['permission_budget_fingerprint']='same'
    for e in candidate:e['costs']['actual_total_tokens']=101;e['permission_budget_fingerprint']='same'
    assert not compare_versions(baseline,candidate,policy=POLICY)['eligible']
    candidate[0]['costs']['actual_total_tokens']=100;candidate[1]['costs']['actual_total_tokens']=100
    assert compare_versions(baseline,candidate,policy=POLICY)['eligible']
    candidate[0]['costs']['latency_seconds']=1000
    assert not compare_versions(baseline,candidate,policy=POLICY)['eligible']
    candidate[0]['permission_budget_fingerprint']='changed'
    with pytest.raises(ValueError,match='Permission'):compare_versions(baseline,candidate,policy=POLICY)


def test_explicit_remote_host_and_safe_config_repr():
    config=Config('PRIVATE_PUBLIC_KEY','PRIVATE_SECRET_KEY','https://example.invalid')
    assert 'PRIVATE' not in repr(config)
    for host in ('https://user:password@example.invalid','http://example.invalid','file:///tmp/log','https://example.invalid/?key=value'):
        with pytest.raises(ValueError):Config('test','test',host)


def test_approval_interactive_host_boundary(tmp_path,fixture_release,monkeypatch):
    registry,baseline,pin=fixture_release
    monkeypatch.setattr('sys.stdin.isatty',lambda:True)
    monkeypatch.setattr('builtins.input',lambda _:f'promote {baseline} {pin}')
    receipt=read(registry.approve_interactively('promote',baseline,pin))
    assert receipt['simulated'] is False and receipt['signature']
    monkeypatch.setattr('builtins.input',lambda _:'approved')
    with pytest.raises(ValueError,match='declined'):registry.approve_interactively('promote',baseline,pin)
