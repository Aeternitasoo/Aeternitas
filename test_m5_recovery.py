"""Real PostgreSQL recovery contracts; every external source/model is synthetic."""

from collections import Counter
from uuid import uuid4
from datetime import datetime,timezone
from pathlib import Path
import hashlib
import json
import os
import threading

import httpx
import psycopg
from psycopg import sql
import pytest
from fastapi.testclient import TestClient

import evidence_repository as domain
import run_repository as tasks
from analysis_contracts import ROLE_TASKS
from local_api import create_app
from research_service import ResearchService, ServiceError
from research_test_support import CONFIG, transports


DSN=os.environ.get('AETERNITAS_TEST_DATABASE')
TOKEN='synthetic-m5-recovery-token-0123456789abcdef'
PAYLOAD=dict(ticker='AAPL',as_of_date='2025-10-01',question='Controlled recovery question',
             refresh=True,news_days=14,deadline=300)
SUCCESS={'completed','completed_with_gaps'}
RECORDED_CRASHES=json.loads((Path(__file__).parent/'tests/fixtures/m5_recorded_crash_shapes_v1.json').read_text())


@pytest.fixture
def recovery(tmp_path):
    if not DSN:
        pytest.skip('Requires explicit real test PostgreSQL DSN')
    schema='m5_recovery_'+uuid4().hex
    with psycopg.connect(DSN,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with domain.connect(DSN,schema=schema) as connection:
            domain.migrate(connection)
        controlled,calls=transports()
        service=ResearchService(tmp_path,{**CONFIG,'DATABASE_URL':DSN},schema=schema,transports=controlled)
        assert service.runner is None
        yield service,calls
    finally:
        with psycopg.connect(DSN,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def claim(service):
    with service.connection() as connection:
        task=tasks.claim(connection,service.root,'controlled-recovery-worker')
        assert connection.info.transaction_status.name=='IDLE'
    assert task is not None
    return task


def execute(service):
    task=claim(service)
    service.execute(task)
    return task


def internal(service,run_id):
    with service.connection() as connection:
        return tasks.get(connection,run_id)


def assert_public_has_no_owner_paths(service,run_id,*owner_tokens):
    public=service.get(run_id)
    encoded=json.dumps(public,default=str)
    assert 'owner_token' not in encoded and 'artifact_path' not in encoded
    assert all(token not in encoded for token in owner_tokens)
    assert all(isinstance(step['checkpoint_saved'],bool) for step in public['steps'])
    return public


def once(event_name,role=None):
    triggered=[]
    def crash(event,**details):
        if not triggered and event==event_name and (role is None or details.get('role')==role):
            triggered.append(details)
            raise SystemExit('Controlled worker interruption')
    return crash,triggered


def test_real_db_idempotency_reuses_identical_request_and_rejects_changes(recovery):
    service,calls=recovery
    first=service.create(idempotency_key='same-request',**PAYLOAD)
    repeated=service.create(idempotency_key='same-request',**PAYLOAD)
    assert first['run_id']==repeated['run_id']
    assert first['reused'] is False and repeated['reused'] is True
    assert first['idempotency_key']==repeated['idempotency_key']=='same-request'
    with pytest.raises(ServiceError) as rejected:
        service.create(idempotency_key='same-request',**{**PAYLOAD,'question':'Changed request'})
    assert rejected.value.status==409 and rejected.value.code=='idempotency_conflict'
    assert len(service.listing())==1 and calls['model']==[]


def test_checkpoint_crash_resumes_without_repeating_successful_first_role(recovery):
    service,calls=recovery
    rid=service.create(idempotency_key='checkpoint-crash',**PAYLOAD)['run_id']
    service.fault_hook,triggered=once('after_checkpoint','fundamentals')
    task=claim(service)
    with pytest.raises(SystemExit): service.execute(task)
    interrupted=service.get(rid)
    assert triggered and interrupted['status']=='interrupted'
    completed=next(step for step in internal(service,rid)['steps'] if step['step']=='fundamentals')
    assert completed['status']=='completed' and completed['artifact_sha256']
    assert interrupted['model_attempts']==len(calls['model'])==1
    original_deadline=interrupted['deadline_at']
    assert service.resume(rid)['status']=='queued'
    resumed=execute(service)
    result=assert_public_has_no_owner_paths(service,rid,task['owner_token'],resumed['owner_token'])
    assert resumed['owner_token']!=task['owner_token']
    assert resumed['deadline_at']==result['deadline_at']==original_deadline
    assert result['status'] in SUCCESS and result['resume_count']==1
    assert result['model_attempts']==len(calls['model'])==4
    assert Counter(role for role,_ in calls['model'])==Counter({'fundamentals':1,'events':1,'risk':1,'synthesis':1})
    reused=next(step for step in internal(service,rid)['steps'] if step['step']=='fundamentals')
    assert reused['artifact_path']==completed['artifact_path']
    assert reused['artifact_sha256']==completed['artifact_sha256']
    assert service.report(rid)[1]['model_attempts']==4


@pytest.mark.parametrize('recorded',RECORDED_CRASHES['cases'],ids=lambda case:case['source_stage'])
def test_uncommitted_call_requires_acknowledgement_and_preserves_consumed_budget(recovery,recorded):
    service,calls=recovery
    event,role=recorded['event'],recorded['role']
    rid=service.create(idempotency_key='uncertain-'+event,**PAYLOAD)['run_id']
    observations=[]
    def crash(event_name,**details):
        if not observations and event_name==event and details.get('role')==role:
            observations.append(service.get(rid))
            raise SystemExit('Synthetic replay of recorded process-exit shape')
    def shape(state):
        ledger=next(attempt for attempt in state['attempts']
                    if attempt['role']==role and attempt['role_attempt']==recorded['role_attempt'])
        checkpoint=next(step for step in state['steps'] if step['step']==role)
        return {'status':state['status'],'ledger_status':ledger['status'],
                'usage_status':(ledger.get('usage') or {}).get('status'),
                'role_checkpoint_saved':checkpoint['checkpoint_saved']}
    service.fault_hook=crash
    with pytest.raises(SystemExit): execute(service)
    interrupted=service.get(rid)
    assert len(observations)==1 and shape(observations[0])==recorded['after_process']
    assert shape(interrupted)==recorded['after_natural_expiry']
    consumed_before_resume=list(ROLE_TASKS).index(role)+1
    assert interrupted['model_attempts']==len(calls['model'])==consumed_before_resume
    assert recorded['requires_acknowledgement'] is True
    with pytest.raises(ServiceError) as rejected:
        service.resume(rid)
    assert rejected.value.status==409 and 'acknowledgement required' in rejected.value.message
    assert service.get(rid)['status']=='interrupted' and len(calls['model'])==consumed_before_resume
    with pytest.raises(ServiceError) as unavailable: service.report(rid)
    assert unavailable.value.status==recorded['report_http_before_resume']
    assert service.resume(rid,acknowledge_unknown=recorded['acknowledge_unknown_used'])['status']=='queued'
    execute(service)
    result=service.get(rid)
    expected_calls=recorded['expected_totalcalls_after_onefault_resume']
    assert result['status'] in SUCCESS and result['model_attempts']==len(calls['model'])==expected_calls
    expected_roles=Counter({name:1 for name in ROLE_TASKS})
    expected_roles[role]+=1
    assert Counter(name for name,_ in calls['model'])==expected_roles
    assert [attempt['total_ordinal'] for attempt in result['attempts']]==[1,2,3,4,5]
    assert [attempt['role_attempt'] for attempt in result['attempts'] if attempt['role']==role]==[1,2]
    _,report=service.report(rid)
    assert report['model_attempts']==expected_calls and len(report['model_usage'])==expected_calls
    if recorded['after_natural_expiry']['usage_status']=='network_unknown':
        unknown=next(usage for usage in report['model_usage'] if usage['role']==role and usage['attempt']==recorded['role_attempt'])
        assert unknown['status']=='network_unknown' and unknown['usage'] is None


@pytest.mark.parametrize('stop',['cancel','deadline'])
def test_stop_during_blocked_model_fences_late_response_and_following_roles(recovery,stop):
    service,calls=recovery
    original=service.transports['model']
    entered=threading.Event()
    release=threading.Event()
    errors=[]
    def blocked(request):
        result=original.handle_request(request)
        entered.set()
        if not release.wait(10): raise AssertionError('Test failed to release controlled model response')
        return result
    service.transports['model']=httpx.MockTransport(blocked)
    rid=service.create(idempotency_key='stop-in-flight',**{**PAYLOAD,'deadline':2 if stop=='deadline' else 300})['run_id']
    task=claim(service)
    def run():
        try: service.execute(task)
        except BaseException as exc: errors.append(exc)
    worker=threading.Thread(target=run,name='controlled-cancel-worker')
    worker.start()
    try:
        assert entered.wait(10),'The synthetic model request never started'
        assert len(calls['model'])==1
        if stop=='cancel':
            stopped=service.cancel(rid)
            assert stopped['status']=='cancelled' and stopped['cancel_requested'] is True
        else:
            remaining=(task['deadline_at']-datetime.now(timezone.utc)).total_seconds()
            assert remaining<3
            threading.Event().wait(max(0,remaining)+.03)
            with service.connection() as connection:
                assert tasks.recover_expired(connection,service.root)==1
            stopped=service.get(rid)
            assert stopped['status']=='interrupted' and stopped['deadline_at']==task['deadline_at']
        assert stopped['attempts'][0]['status']=='unknown'
        release.set()
        worker.join(10)
        assert not worker.is_alive() and errors==[]
    finally:
        release.set()
        worker.join(10)
    result=service.get(rid)
    assert result['status']==('cancelled' if stop=='cancel' else 'interrupted')
    assert result['model_attempts']==len(calls['model'])==1
    assert [role for role,_ in calls['model']]==['fundamentals']
    assert not (service.root/'runs'/rid/'published.json').exists()
    assert not (service.root/'runs'/rid/'report.html').exists()
    with TestClient(create_app(service,token=TOKEN),base_url='http://127.0.0.1:8767',
                    client=('127.0.0.1',2)) as client:
        assert client.get(f'/research-runs/{rid}/report.json').status_code==409
    with pytest.raises(ServiceError) as rejected: service.resume(rid,acknowledge_unknown=True)
    assert rejected.value.status==409


def test_files_written_before_db_commit_are_hidden_and_resume_uses_all_role_checkpoints(recovery):
    service,calls=recovery
    rid=service.create(idempotency_key='publication-crash',**PAYLOAD)['run_id']
    service.fault_hook,triggered=once('after_publication_files')
    with pytest.raises(SystemExit): execute(service)
    interrupted=service.get(rid)
    assert triggered and interrupted['status']=='interrupted'
    assert interrupted['model_attempts']==len(calls['model'])==4
    assert (service.root/'runs'/rid/'published.json').exists()
    with TestClient(create_app(service,token=TOKEN),base_url='http://127.0.0.1:8767',
                    client=('127.0.0.1',2)) as client:
        assert client.get(f'/research-runs/{rid}/report.html').status_code==409
        assert client.get(f'/research-runs/{rid}/report.json').status_code==409
    checkpoint_hashes={step['step']:step['artifact_sha256'] for step in interrupted['steps']
                       if step['step'] in {'fundamentals','events','risk','synthesis'}}
    assert len(checkpoint_hashes)==4 and all(checkpoint_hashes.values())
    assert service.resume(rid)['status']=='queued'
    execute(service)
    result=service.get(rid)
    assert result['status'] in SUCCESS and result['model_attempts']==len(calls['model'])==4
    assert checkpoint_hashes=={step['step']:step['artifact_sha256'] for step in result['steps']
                              if step['step'] in checkpoint_hashes}
    assert service.report(rid)[1]['model_attempts']==4


def test_resume_never_extends_original_deadline_or_starts_calls_after_expiry(recovery):
    service,calls=recovery
    rid=service.create(idempotency_key='original-deadline',**{**PAYLOAD,'deadline':2})['run_id']
    service.fault_hook,triggered=once('after_checkpoint','data_import')
    with pytest.raises(SystemExit): execute(service)
    interrupted=service.get(rid)
    assert triggered and interrupted['status']=='interrupted' and calls['model']==[]
    deadline=interrupted['deadline_at']
    remaining=(deadline-datetime.now(timezone.utc)).total_seconds()
    assert remaining<3
    threading.Event().wait(max(0,remaining)+.03)
    with pytest.raises(ServiceError) as rejected:
        service.resume(rid,acknowledge_unknown=True)
    assert rejected.value.status==409 and 'deadline' in rejected.value.message.lower()
    result=service.get(rid)
    assert result['deadline_at']==deadline and result['resume_count']==0
    assert result['status']=='interrupted' and result['model_attempts']==0 and calls['model']==[]
    with service.connection() as connection:
        assert tasks.claim(connection,service.root,'should-not-run') is None


def test_expired_owner_cannot_overwrite_or_remove_new_owner_publication(recovery):
    service,calls=recovery
    entered=threading.Event()
    release=threading.Event()
    blocked=[]
    errors=[]
    def hold_old_owner(event,**details):
        if event=='before_publication' and not blocked:
            blocked.append(details)
            entered.set()
            if not release.wait(10): raise AssertionError('Old owner publication was not released')
    service.fault_hook=hold_old_owner
    rid=service.create(idempotency_key='stale-publication-owner',**PAYLOAD)['run_id']
    old=claim(service)
    def run_old():
        try: service.execute(old)
        except BaseException as exc: errors.append(exc)
    worker=threading.Thread(target=run_old,name='controlled-stale-owner')
    worker.start()
    try:
        assert entered.wait(10)
        assert len(calls['model'])==4
        with service.connection() as connection:
            connection.execute("UPDATE research_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",(rid,))
            assert tasks.recover_expired(connection,service.root)==1
        assert service.get(rid)['status']=='interrupted'
        assert service.resume(rid)['status']=='queued'
        new=execute(service)
        assert new['owner_token']!=old['owner_token']
        assert assert_public_has_no_owner_paths(service,rid,old['owner_token'],new['owner_token'])['status'] in SUCCESS
        assert len(calls['model'])==4
        canonical=service.root/'runs'/rid
        names=('published.json','report.json','report.html')
        before={name:hashlib.sha256((canonical/name).read_bytes()).hexdigest() for name in names}
        release.set()
        worker.join(10)
        assert not worker.is_alive() and errors==[]
        after={name:hashlib.sha256((canonical/name).read_bytes()).hexdigest() for name in names}
        assert after==before
        assert service.get(rid)['status'] in SUCCESS and len(calls['model'])==4
        assert service.report(rid)[1]['model_attempts']==4
    finally:
        release.set()
        worker.join(10)


@pytest.mark.parametrize('step',['data_import','fundamentals'])
def test_resumed_execution_rejects_tampered_checkpoint_before_new_calls(recovery,step):
    service,calls=recovery
    rid=service.create(idempotency_key='tampered-checkpoint',**PAYLOAD)['run_id']
    service.fault_hook,_=once('after_checkpoint',step)
    with pytest.raises(SystemExit): execute(service)
    interrupted=service.get(rid)
    count=len(calls['model'])
    checkpoint=next(item for item in internal(service,rid)['steps'] if item['step']==step)
    path=service.root/checkpoint['artifact_path']
    path.write_bytes(path.read_bytes()+b'\n')
    assert service.resume(rid)['status']=='queued'
    execute(service)
    result=service.get(rid)
    assert result['status']=='failed' and 'Checkpoint fingerprint differs' in result['error']
    assert result['model_attempts']==count==len(calls['model'])
    assert not (service.root/'runs'/rid/'published.json').exists()
    with pytest.raises(ServiceError) as rejected: service.report(rid)
    assert rejected.value.status==409


def test_changed_workflow_fingerprint_conflicts_with_key_and_refuses_resume(recovery,monkeypatch):
    import recoverable_pipeline
    service,calls=recovery
    rid=service.create(idempotency_key='version-bound-request',**PAYLOAD)['run_id']
    service.fault_hook,_=once('after_checkpoint','fundamentals')
    with pytest.raises(SystemExit): execute(service)
    assert len(calls['model'])==1
    current=recoverable_pipeline.configuration_fingerprint()
    changed='0'*64 if current!='0'*64 else '1'*64
    monkeypatch.setattr(recoverable_pipeline,'configuration_fingerprint',lambda:changed)
    with pytest.raises(ServiceError) as conflict:
        service.create(idempotency_key='version-bound-request',**PAYLOAD)
    assert conflict.value.status==409 and conflict.value.code=='idempotency_conflict'
    with pytest.raises(ServiceError) as rejected: service.resume(rid)
    assert rejected.value.status==409 and 'version changed' in rejected.value.message
    assert len(service.listing())==1 and len(calls['model'])==1


def test_durable_429_retry_uses_two_attempts_then_completes_within_cumulative_budget(recovery):
    service,calls=recovery
    original=service.transports['model']
    received=[]
    def rate_limited_once(request):
        payload=json.loads(request.content)
        role=next(role for role,task in ROLE_TASKS.items() if task in payload['input'][0]['content'])
        received.append(role)
        if len(received)==1:
            calls['model'].append((role,payload))
            return httpx.Response(429,headers={'Retry-After':'0.01'},json={'error':{'message':'Controlled temporary quota limit'}})
        return original.handle_request(request)
    service.transports['model']=httpx.MockTransport(rate_limited_once)
    rid=service.create(idempotency_key='bounded-rate-limit',**PAYLOAD)['run_id']
    execute(service)
    result=service.get(rid)
    assert result['status'] in SUCCESS and result['model_attempts']==len(received)==len(calls['model'])==5
    assert received==['fundamentals','fundamentals','events','risk','synthesis']
    assert result['attempts'][0]['status']=='failed'
    assert result['attempts'][0]['usage']['status']=='provider_error'
    assert result['attempts'][1]['status']=='completed'
    assert [attempt['total_ordinal'] for attempt in result['attempts']]==[1,2,3,4,5]
    assert service.report(rid)[1]['model_attempts']==5


def test_durable_permanent_auth_error_does_not_retry_or_publish(recovery):
    service,_=recovery
    service.transports,calls=transports(model_failure='auth')
    rid=service.create(idempotency_key='permanent-auth-error',**PAYLOAD)['run_id']
    execute(service)
    result=service.get(rid)
    assert result['status']=='failed' and result['model_attempts']==len(calls['model'])==1
    assert len(result['attempts'])==1 and result['attempts'][0]['status']=='failed'
    assert 'HTTP 401' in result['error'] and CONFIG['OPENAI_API_KEY'] not in json.dumps(result,default=str)
    assert not (service.root/'runs'/rid/'published.json').exists()


def test_first_source_checkpoint_freezes_audited_inputs_without_refresh_on_resume(recovery):
    service,calls=recovery
    rid=service.create(idempotency_key='first-source-checkpoint',**PAYLOAD)['run_id']
    service.fault_hook,triggered=once('after_checkpoint','financials')
    with pytest.raises(SystemExit): execute(service)
    interrupted=service.get(rid)
    assert triggered and interrupted['status']=='interrupted' and calls['model']==[]
    frozen={step['step'] for step in interrupted['steps'] if step.get('artifact_sha256')}
    assert frozen=={'financials'}
    source_counts={name:len(calls[name]) for name in ('sec','web')}
    assert all(source_counts.values())
    assert service.resume(rid)['status']=='queued'
    execute(service)
    result=service.get(rid)
    assert result['status'] in SUCCESS and result['model_attempts']==len(calls['model'])==4
    assert source_counts=={name:len(calls[name]) for name in source_counts}
    completed={step['step'] for step in result['steps']
               if step['status']=='completed' and step.get('artifact_sha256')}
    assert {'financials','filing_text','company_news','data_import','fundamentals','events','risk','synthesis'}<=completed
    assert service.report(rid)[1]['model_attempts']==4


def test_full_eight_call_budget_can_resume_publication_with_all_four_role_checkpoints(recovery):
    service,calls=recovery
    original=service.transports['model']
    seen=Counter()
    def malformed_first_attempt_per_role(request):
        payload=json.loads(request.content)
        role=next(role for role,task in ROLE_TASKS.items() if task in payload['input'][0]['content'])
        seen[role]+=1
        data=original.handle_request(request).json()
        if seen[role]==1:
            result=json.loads(data['output'][0]['content'][0]['text'])
            result['claims'][0]['citations'][0]['evidence_id']='not-in-this-role-evidence'
            data['output'][0]['content'][0]['text']=json.dumps(result)
        return httpx.Response(200,json=data)
    service.transports['model']=httpx.MockTransport(malformed_first_attempt_per_role)
    service.fault_hook,triggered=once('after_publication_files')
    rid=service.create(idempotency_key='eight-calls-publication-only',**PAYLOAD)['run_id']
    with pytest.raises(SystemExit): execute(service)
    interrupted=service.get(rid)
    assert triggered and interrupted['status']=='interrupted'
    assert interrupted['model_attempts']==len(calls['model'])==8
    assert seen==Counter({role:2 for role in ROLE_TASKS})
    checkpoints={step['step']:step['artifact_sha256'] for step in interrupted['steps']
                 if step['step'] in ROLE_TASKS and step['status']=='completed'}
    assert len(checkpoints)==4 and all(checkpoints.values())
    source_counts={name:len(calls[name]) for name in ('sec','web')}
    assert service.resume(rid)['status']=='queued'
    execute(service)
    result=service.get(rid)
    assert result['status'] in SUCCESS and result['resume_count']==1
    assert result['model_attempts']==len(calls['model'])==8
    assert seen==Counter({role:2 for role in ROLE_TASKS})
    assert source_counts=={name:len(calls[name]) for name in source_counts}
    assert checkpoints=={step['step']:step['artifact_sha256'] for step in result['steps'] if step['step'] in checkpoints}
    _,report=service.report(rid)
    assert report['model_attempts']==8 and len(report['model_usage'])==8
