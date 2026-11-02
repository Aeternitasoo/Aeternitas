"""Real PostgreSQL + local ASGI contracts, synthetic providers; never external model calls."""

import json
import os
from pathlib import Path
from contextlib import contextmanager
from datetime import date
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

import evidence_repository as domain
import run_repository as tasks
from api_client import APIClient, local_url
from local_api import create_app, local_token
from research_pipeline import run_research
from research_service import ResearchService
from research_test_support import CONFIG, transports
from sources.sec import SourceError


DSN=os.environ.get('AETERNITAS_TEST_DATABASE')
TOKEN='synthetic-local-token-0123456789abcdef'
PAYLOAD=dict(ticker='AAPL',as_of_date='2025-10-01',question='Synthetic test',refresh=True,news_days=14,deadline=300)


@pytest.fixture
def app_state(tmp_path,monkeypatch):
    if not DSN: pytest.skip('Requires explicit real test PostgreSQL DSN')
    schema='m3_test_'+uuid4().hex
    original=domain.connect
    with psycopg.connect(DSN,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    monkeypatch.setattr(domain,'connect',lambda dsn=DSN,schema=schema: original(dsn,schema=schema))
    try:
        with original(DSN,schema=schema) as connection: domain.migrate(connection)
        transport,calls=transports()
        def controlled(*args,**kwargs):
            return run_research(*args,**kwargs,transports=transport,evidence_kind='controlled_test')
        service=ResearchService(tmp_path,{**CONFIG,'DATABASE_URL':DSN},schema=schema,runner=controlled)
        with TestClient(create_app(service,token=TOKEN),base_url='http://127.0.0.1:8767',
                        client=('127.0.0.1',1234),headers={'X-Aeternitas-Token':TOKEN}) as client:
            yield service,client,calls
    finally:
        with psycopg.connect(DSN,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def submit(client,**options):
    result=client.post('/research-runs',json={**PAYLOAD,**options})
    assert result.status_code==202,result.text
    assert result.headers['location']==f"/research-runs/{result.json()['run_id']}"
    return result.json()['run_id']


def execute(service):
    with service.connection() as connection:
        task=tasks.claim(connection,service.root,'controlled-worker')
        assert connection.info.transaction_status.name=='IDLE'
    service.execute(task)
    return task['run_id']


def test_real_db_queue_four_roles_publication_evidence_and_recreated_api(app_state):
    service,client,calls=app_state
    rid=submit(client)
    assert not calls['model'] and client.get(f'/research-runs/{rid}').json()['status']=='queued'
    assert client.get(f'/research-runs/{rid}/report.html').status_code==409
    assert client.get(f'/research-runs/{rid}/evidence').status_code==409
    assert execute(service)==rid
    state=client.get(f'/research-runs/{rid}').json()
    assert state['status'] in {'completed','completed_with_gaps'} and state['model_attempts']==len(calls['model'])==4
    steps=state['steps']
    assert [s['step'] for s in steps]==['financials','filing_text','company_news','data_import',
                                     'fundamentals','events','risk','synthesis','validation']
    assert all(s['status']=='completed' for s in steps)
    html=client.get(f'/research-runs/{rid}/report.html',headers={'X-Aeternitas-Token':''})
    assert html.status_code==200 and 'frame-ancestors' in html.headers['content-security-policy']
    report=client.get(f'/research-runs/{rid}/report.json').json()
    evidence=client.get(f'/research-runs/{rid}/evidence').json()['items']
    assert len(evidence)==len(report['evidence'])
    eid=evidence[-1]['id']
    assert client.get(f'/research-runs/{rid}/evidence/{eid}').json()==evidence[-1]
    assert client.get(f'/research-runs/{rid}/evidence/not-present').status_code==404
    with TestClient(create_app(ResearchService(service.root,service.config,schema=service.schema),token=TOKEN),
                    base_url='http://127.0.0.1:8767',client=('127.0.0.1',2),headers={'X-Aeternitas-Token':TOKEN}) as restarted:
        assert restarted.get(f'/research-runs/{rid}').json()['status']==state['status']
    # Queries reflect PostgreSQL, not an in-memory task dictionary.
    with service.connection() as connection:
        connection.execute('UPDATE research_runs SET question=%s WHERE run_id=%s',('Database changed field',rid))
    assert client.get(f'/research-runs/{rid}').json()['question']=='Database changed field'
    (service.root/'runs'/rid/'report.html').write_text('tampered')
    assert client.get(f'/research-runs/{rid}/report.html').status_code==409


def test_write_boundary_bad_requests_empty_and_unknown_are_distinct(app_state):
    service,client,calls=app_state
    assert client.get('/research-runs').json()['items']==[]
    assert client.get('/research-runs?ticker=MSFT').status_code==200
    assert client.get('/research-runs?ticker=INVALID').json()['items']==[]
    assert client.get('/research-runs/'+'0'*32).status_code==404
    assert client.get('/research-runs/not-a-run').status_code==400
    assert client.post('/research-runs',json=PAYLOAD,headers={'X-Aeternitas-Token':''}).status_code==401
    assert client.post('/research-runs',json=PAYLOAD,headers={'Origin':'https://attacker.invalid'}).status_code==403
    assert client.post('/research-runs',json=PAYLOAD,headers={'Host':'attacker.invalid'}).status_code==403
    assert client.post('/research-runs',json=PAYLOAD,headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert client.post('/research-runs',content='{}',headers={'Content-Type':'text/plain'}).status_code==415
    assert client.post('/research-runs',content='x'*9000,headers={'Content-Type':'application/json'}).status_code==413
    for changes in [dict(deadline=0),dict(news_days=31),dict(refresh='true'),dict(as_of_date='2999-01-01'),
                    dict(question=' '),dict(OPENAI_API_KEY='synthetic-key-not-echoed')]:
        result=client.post('/research-runs',json={**PAYLOAD,**changes})
        assert result.status_code==422,result.text
        assert 'synthetic-key-not-echoed' not in result.text
    assert not calls['model']


def test_sql_injection_is_only_text_and_evidence_is_scoped(app_state):
    service,client,calls=app_state
    injection="'; DROP TABLE research_runs; --"
    rid=submit(client,question=injection)
    assert client.get(f'/research-runs/{rid}').json()['question']==injection
    assert client.get('/research-runs',params={'ticker':injection}).status_code==400
    assert execute(service)==rid
    assert client.get(f'/research-runs/{rid}/evidence/{injection}').status_code==404
    assert len(client.get('/research-runs').json()['items'])==1
    assert 'synthetic-model-secret' not in client.get(f'/research-runs/{rid}').text


def test_worker_claim_once_and_interruption_does_not_requeue(app_state):
    service,client,calls=app_state
    rid=submit(client)
    with service.connection() as first,service.connection() as second:
        task=tasks.claim(first,service.root,'worker-a')
        assert task['run_id']==rid and tasks.claim(second,service.root,'worker-b') is None
        assert tasks.interrupt_orphans(first,service.root)==1
        assert tasks.claim(second,service.root,'worker-b') is None
    assert client.get(f'/research-runs/{rid}').json()['status']=='interrupted'
    assert not calls['model']


def test_failed_source_has_run_step_error_without_false_report(app_state):
    service,client,calls=app_state
    rid=submit(client)
    def broken(*args,**kwargs):
        kwargs['state_observer'](dict(step='company_news',status='failed',model_attempts=0))
        raise SourceError('Synthetic source failure synthetic-news-secret')
    service.runner=broken
    execute(service)
    state=client.get(f'/research-runs/{rid}').json()
    assert state['status']=='failed' and state['step']=='company_news'
    assert state['steps'][-1]['status']=='failed'
    assert '[redacted]' in state['error'] and 'synthetic-news-secret' not in json.dumps(state)
    assert client.get(f'/research-runs/{rid}/report.json').status_code==409


def test_database_unavailable_is_503_and_never_echoes_connection_details():
    class Down:
        @contextmanager
        def connection(self):
            raise SourceError('secret-password db.internal')
            yield
    with TestClient(create_app(Down(),token=TOKEN),base_url='http://localhost:8767',client=('127.0.0.1',2)) as client:
        result=client.get('/health')
        assert result.status_code==503 and 'secret' not in result.text and 'db.internal' not in result.text


def test_cli_client_bypasses_proxy_and_preserves_http_errors(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY','http://127.0.0.1:1')
    def handler(request):
        assert request.headers['X-Aeternitas-Token']==TOKEN
        assert TOKEN not in str(request.url)
        return httpx.Response(404,json={'error':{'code':'run_not_found','message':'No run'}})
    with APIClient(transport=httpx.MockTransport(handler),token=TOKEN) as client:
        with pytest.raises(SourceError,match='404.*run_not_found'): client.request('GET','/research-runs/'+'0'*32)
    for url in ['http://attacker.invalid:8767','http://127.0.0.1.attacker.invalid:8767',
                'http://user:secret@localhost:8767','http://localhost:8767/?token=secret','https://localhost:8767']:
        with pytest.raises(ValueError): local_url(url)


def test_local_token_created_private_without_printing(tmp_path):
    path=tmp_path/'private/token'
    token=local_token(path)
    assert len(token)>=32 and path.stat().st_mode&0o777==0o600
    assert local_token(path)==token
    path.chmod(0o644)
    with pytest.raises(ValueError,match='mode 600'): local_token(path)


def test_a_shared_validation_change_reaches_cli_and_api(app_state,monkeypatch,capsys):
    import research_service
    from aeternitas import main
    service,client,calls=app_state
    original=research_service.validate_request
    def changed_rule(*args,**kwargs):
        row=original(*args,**kwargs)
        if row['news_days']>7:
            raise research_service.ServiceError(422,'test_shared_rule','Synthetic shared rule: at most seven days')
        return row
    monkeypatch.setattr(research_service,'validate_request',changed_rule)
    assert main(['run','AAPL','--as-of','2025-10-01','--news-days','14'])==1
    assert 'Synthetic shared rule' in capsys.readouterr().err
    result=client.post('/research-runs',json=PAYLOAD)
    assert result.status_code==422 and result.json()['error']['code']=='test_shared_rule'
    assert client.get('/research-runs').json()['items']==[] and not calls['model']


def test_model_attempt_is_committed_before_provider_call_and_not_in_a_transaction(app_state):
    service,client,calls=app_state
    transport,calls=transports()
    handler=transport['model']
    rid=submit(client)
    observations=[]
    def checked(request):
        with service.connection() as connection:
            row=tasks.get(connection,rid)
            active=connection.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() AND state='idle in transaction'").fetchone()['n']
            observations.append(row['model_attempts'])
            assert row['status']=='running' and active==0
        return handler.handle_request(request)
    transport['model']=httpx.MockTransport(checked)
    service.runner=lambda *args,**kwargs: run_research(*args,**kwargs,transports=transport,evidence_kind='controlled_test')
    execute(service)
    assert observations==[1,2,3,4]
    assert client.get(f'/research-runs/{rid}').json()['status'] in {'completed','completed_with_gaps'}


def test_sanitized_real_no_annual_coverage_failure_before_model(app_state):
    service,client,calls=app_state
    fixture=json.loads((Path(__file__).parent/'tests/fixtures/m3_no_annual_coverage_v1.json').read_text())
    expected=fixture['expected']
    rid=submit(client,**fixture['request'])
    execute(service)
    row=client.get(f'/research-runs/{rid}').json()
    for key in ('status','step','model_attempts','model_usage','dataset_id'):
        assert row[key]==expected[key]
    assert row['error'].endswith(expected['error_suffix'])
    assert row['steps'][0]['status']=='failed' and row['steps'][0]['error']==expected['error_suffix']
    assert not calls['model'] and client.get(f'/research-runs/{rid}/report.html').status_code==expected['report_http_status']
    assert not (service.root/'runs'/rid/'published.json').exists()


def test_task_dataset_company_and_terminal_state_constraints(app_state):
    service,client,calls=app_state
    first=submit(client)
    execute(service)
    dataset=client.get(f'/research-runs/{first}').json()['dataset_id']
    second=submit(client,ticker='MSFT')
    with service.connection() as connection:
        task=tasks.claim(connection,service.root,'controlled-worker')
        assert task['run_id']==second
        with pytest.raises(psycopg.errors.ForeignKeyViolation),connection.transaction():
            connection.execute('UPDATE research_runs SET dataset_id=%s WHERE run_id=%s',(dataset,second))
        with pytest.raises(psycopg.errors.CheckViolation):
            tasks.finish(connection,second,'completed')
        assert connection.info.transaction_status.name=='IDLE'
        tasks.finish(connection,second,'failed',error='Controlled constraint check; no provider call')
    assert client.get(f'/research-runs/{second}').json()['status']=='failed'


def test_report_symlink_escape_is_rejected_before_reading(app_state,tmp_path,monkeypatch):
    import research_service
    service,client,calls=app_state
    rid=submit(client)
    execute(service)
    folder=service.root/'runs'/rid
    outside=service.root/'outside-run-storage'
    folder.rename(outside)
    folder.symlink_to(outside,target_is_directory=True)
    def must_not_read(*args): raise AssertionError('Escaped report must be rejected before file reads')
    monkeypatch.setattr(research_service,'saved_report',must_not_read)
    assert client.get(f'/research-runs/{rid}/report.html').status_code==409
