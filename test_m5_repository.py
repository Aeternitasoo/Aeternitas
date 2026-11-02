"""Real PostgreSQL contracts, isolated schemas, no source/model/API requests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from threading import Barrier, Event
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

import evidence_repository as domain
import run_repository as tasks

DSN=os.environ.get('AETERNITAS_TEST_DATABASE')
pytestmark=pytest.mark.skipif(not DSN,reason='Requires explicit AETERNITAS_TEST_DATABASE')
REQUEST=dict(ticker='AAPL',cik='0000320193',as_of_date='2025-10-01',question='Controlled repository contract',
             refresh=True,news_days=14,deadline=300)
WF='f'*64

@pytest.fixture
def database():
    schema='m5_repository_'+uuid4().hex
    with psycopg.connect(DSN,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with domain.connect(DSN,schema=schema) as connection:
            domain.migrate(connection)
            yield connection,schema
    finally:
        with psycopg.connect(DSN,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

def create(connection,root='/controlled/m5',**kwargs):
    return tasks.create(connection,uuid4().hex,kwargs.pop('request',REQUEST),root,'api_key','controlled-model',
                        workflow_config_fingerprint=WF,**kwargs)

def claimed(connection,root='/controlled/m5'):
    row=create(connection,root)
    task=tasks.claim(connection,root,'controlled-worker')
    assert row['run_id']==task['run_id']
    return task

def state(step,**kwargs): return dict(step=step,status='running',model_attempts=0,**kwargs)

def outcome(role,attempt,status='completed'):
    return dict(role=role,attempt=attempt,status=status,usage={'input_tokens':1,'output_tokens':1})

def cp(connection,task,step):
    return tasks.checkpoint(connection,task['run_id'],task['owner_token'],step,
                            f"work/{task['owner_token']}/{step}.json",'a'*64,WF)

def test_same_idempotency_key_is_atomic_across_two_connections_and_conflicts(database):
    connection,schema=database
    barrier=Barrier(2)
    def insert(_):
        with domain.connect(DSN,schema=schema) as own:
            barrier.wait(timeout=5)
            return create(own,idempotency_key='same-key')['run_id']
    with ThreadPoolExecutor(max_workers=2) as pool: ids=list(pool.map(insert,range(2)))
    assert ids[0]==ids[1]
    assert len(tasks.listing(connection,root='/controlled/m5'))==1
    with pytest.raises(tasks.RepoConflict):
        create(connection,idempotency_key='same-key',request={**REQUEST,'question':'Changed input'})
    assert create(connection,root='/controlled/other',idempotency_key='same-key')['run_id']!=ids[0]
    assert connection.info.transaction_status.name=='IDLE'

def test_claim_once_is_committed_before_work_and_root_scoped(database):
    connection,schema=database
    row=create(connection)
    create(connection,root='/controlled/other')
    barrier=Barrier(2)
    def claim(_):
        with domain.connect(DSN,schema=schema) as own:
            barrier.wait(timeout=5)
            result=tasks.claim(own,'/controlled/m5',uuid4().hex)
            assert own.info.transaction_status.name=='IDLE'
            return result
    with ThreadPoolExecutor(max_workers=2) as pool: claimed_rows=list(pool.map(claim,range(2)))
    winners=[item for item in claimed_rows if item]
    assert len(winners)==1 and winners[0]['run_id']==row['run_id']
    assert winners[0]['owner_token'] and winners[0]['deadline_at']>winners[0]['started_at']
    assert tasks.get(connection,row['run_id'],root='/controlled/other') is None

def test_observations_do_not_blindly_complete_predecessors_or_lower_budget(database):
    connection,_=database;task=claimed(connection);rid=task['run_id'];token=task['owner_token']
    tasks.observe(connection,rid,state('financials'),owner_token=token)
    tasks.observe(connection,rid,state('filing_text'),owner_token=token)
    assert [s['status'] for s in tasks.get(connection,rid)['steps']]==['running','running']
    cp(connection,task,'financials')
    tasks.reserve_attempt(connection,rid,token,'fundamentals')
    tasks.observe(connection,rid,state('model:fundamentals'),owner_token=token)
    row=tasks.get(connection,rid)
    assert row['steps'][0]['status']=='completed' and row['steps'][1]['status']=='running'
    assert row['model_attempts']==1
    assert row['steps'][0]['artifact_sha256']=='a'*64 and row['steps'][0]['verified_at']

def test_global_and_role_budgets_are_committed_before_calls_and_cannot_expand(database):
    connection,_=database;task=claimed(connection);rid=task['run_id'];token=task['owner_token']
    for role in tasks.ROLES:
        for attempt in (1,2):
            entry=tasks.reserve_attempt(connection,rid,token,role)
            assert entry['role_attempt']==attempt
            assert connection.info.transaction_status.name=='IDLE'
            tasks.record_attempt(connection,rid,token,role,attempt,outcome(role,attempt,'invalid_output'))
        with pytest.raises(tasks.AttemptBudgetError): tasks.reserve_attempt(connection,rid,token,role)
    row=tasks.get(connection,rid)
    assert row['model_attempts']==8 and [a['total_ordinal'] for a in row['attempts']]==list(range(1,9))
    tasks.finish(connection,rid,'failed',owner_token=token)
    with pytest.raises(tasks.AttemptBudgetError): tasks.resume(connection,rid,workflow_config_fingerprint=WF)

def test_resume_reuses_valid_checkpoints_and_rejects_stale_owner_and_unknown_billing(database):
    connection,_=database;task=claimed(connection);rid=task['run_id'];token=task['owner_token']
    cp(connection,task,'inputs')
    tasks.reserve_attempt(connection,rid,token,'fundamentals')
    connection.execute("UPDATE research_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",(rid,))
    assert tasks.recover_expired(connection,'/controlled/m5')==1
    assert tasks.get(connection,rid)['attempts'][0]['status']=='unknown'
    with pytest.raises(tasks.RepoConflict,match='acknowledgement'):
        tasks.resume(connection,rid,workflow_config_fingerprint=WF)
    original_deadline=task['deadline_at']
    resumed=tasks.resume(connection,rid,workflow_config_fingerprint=WF,acknowledge_unknown=True)
    assert resumed['model_attempts']==1 and resumed['resume_count']==1
    new=tasks.claim(connection,'/controlled/m5','new-worker')
    assert new['owner_token']!=token and new['deadline_at']==original_deadline
    for action in (
        lambda:tasks.observe(connection,rid,state('risk'),owner_token=token),
        lambda:tasks.finish(connection,rid,'failed',owner_token=token),
        lambda:tasks.reserve_attempt(connection,rid,token,'events'),
        lambda:tasks.renew(connection,rid,token),
        lambda:cp(connection,task,'risk'),
    ):
        with pytest.raises(tasks.OwnershipError): action()
    second=tasks.reserve_attempt(connection,rid,new['owner_token'],'fundamentals')
    assert second['role_attempt']==2 and second['total_ordinal']==2
    assert tasks.get(connection,rid)['steps'][0]['status']=='completed'
    tasks.record_attempt(connection,rid,new['owner_token'],'fundamentals',2,outcome('fundamentals',2))
    cp(connection,new,'fundamentals')
    tasks.finish(connection,rid,'interrupted',owner_token=new['owner_token'])
    # The acknowledged first unknown is retained as an accounting fact, but it
    # must not demand a second acknowledgement after the role is checkpointed.
    assert tasks.resume(connection,rid,workflow_config_fingerprint=WF)['status']=='queued'
    third=tasks.claim(connection,'/controlled/m5','third-worker')
    tasks.reserve_attempt(connection,rid,third['owner_token'],'events')
    tasks.record_attempt(connection,rid,third['owner_token'],'events',1,outcome('events',1))
    tasks.finish(connection,rid,'interrupted',owner_token=third['owner_token'])
    # This completed response began after the prior acknowledgement and has no
    # checkpoint, so its potential repeat cost still requires a new decision.
    with pytest.raises(tasks.RepoConflict,match='acknowledgement'):
        tasks.resume(connection,rid,workflow_config_fingerprint=WF)

@pytest.mark.parametrize('boundary',['lease','deadline','cancel'])
def test_expired_lease_deadline_or_cancellation_fence_every_owner_mutation(database,boundary):
    connection,_=database;task=claimed(connection);rid=task['run_id'];token=task['owner_token']
    tasks.reserve_attempt(connection,rid,token,'fundamentals')
    if boundary=='cancel':
        result=tasks.request_cancel(connection,rid)
        assert result['status']=='cancelled' and result['attempts'][0]['status']=='unknown'
    else:
        column='lease_until' if boundary=='lease' else 'deadline_at'
        connection.execute(f"UPDATE research_runs SET {column}=clock_timestamp()-interval '1 second' WHERE run_id=%s",(rid,))
    for action in (
        lambda:tasks.observe(connection,rid,state('risk'),owner_token=token),
        lambda:tasks.finish(connection,rid,'failed',owner_token=token),
        lambda:tasks.reserve_attempt(connection,rid,token,'events'),
        lambda:tasks.record_attempt(connection,rid,token,'fundamentals',1,outcome('fundamentals',1)),
        lambda:tasks.renew(connection,rid,token),lambda:cp(connection,task,'inputs'),
    ):
        with pytest.raises(tasks.OwnershipError): action()
    with pytest.raises(tasks.OwnershipError):
        with tasks.publication_guard(connection,rid,token): pytest.fail('Expired owner obtained publication guard')

def test_queued_cancel_is_immediate_and_unclaimable(database):
    connection,_=database;row=create(connection)
    assert tasks.request_cancel(connection,row['run_id'])['status']=='cancelled'
    assert tasks.claim(connection,'/controlled/m5','worker') is None
    assert tasks.get(connection,row['run_id'])['model_attempts']==0

def test_completed_but_uncheckpointed_response_requires_acknowledgement(database):
    connection,_=database;task=claimed(connection);rid=task['run_id'];token=task['owner_token']
    tasks.reserve_attempt(connection,rid,token,'fundamentals')
    tasks.record_attempt(connection,rid,token,'fundamentals',1,outcome('fundamentals',1))
    tasks.finish(connection,rid,'failed',owner_token=token,error='Stopped before validated checkpoint')
    with pytest.raises(tasks.RepoConflict,match='acknowledgement'):
        tasks.resume(connection,rid,workflow_config_fingerprint=WF)
    assert tasks.resume(connection,rid,workflow_config_fingerprint=WF,acknowledge_unknown=True)['status']=='queued'

def test_eight_calls_with_all_checkpoints_can_resume_for_publication_only(database):
    connection,_=database;task=claimed(connection);rid=task['run_id'];token=task['owner_token']
    for role in tasks.ROLES:
        for attempt in (1,2):
            tasks.reserve_attempt(connection,rid,token,role)
            tasks.record_attempt(connection,rid,token,role,attempt,outcome(role,attempt))
        cp(connection,task,role)
    tasks.finish(connection,rid,'failed',owner_token=token,error='Stopped before publication')
    assert tasks.resume(connection,rid,workflow_config_fingerprint=WF)['model_attempts']==8
    new=tasks.claim(connection,'/controlled/m5','publisher')
    with pytest.raises(tasks.RepoConflict,match='checkpoint'):
        tasks.reserve_attempt(connection,rid,new['owner_token'],'fundamentals')

def test_changed_workflow_and_expired_original_deadline_cannot_resume(database):
    connection,_=database;task=claimed(connection);rid=task['run_id'];token=task['owner_token']
    tasks.finish(connection,rid,'failed',owner_token=token)
    with pytest.raises(tasks.RepoConflict,match='version changed'):
        tasks.resume(connection,rid,workflow_config_fingerprint='b'*64)
    connection.execute("UPDATE research_runs SET deadline_at=clock_timestamp()-interval '1 second' WHERE run_id=%s",(rid,))
    with pytest.raises(tasks.RunDeadline): tasks.resume(connection,rid,workflow_config_fingerprint=WF)

def test_bad_checkpoint_scope_role_outcome_and_transaction_rollback_are_reusable(database):
    connection,_=database;task=claimed(connection);rid=task['run_id'];token=task['owner_token']
    for path in ('/absolute/file.json','../../other.json','work/different-owner/file.json'):
        with pytest.raises(ValueError): tasks.checkpoint(connection,rid,token,'inputs',path,'a'*64,WF)
    with pytest.raises(tasks.RepoConflict): cp(connection,task,'fundamentals')
    with pytest.raises(RuntimeError):
        with connection.transaction():
            tasks.reserve_attempt(connection,rid,token,'fundamentals')
            raise RuntimeError('Controlled rollback before external call')
    assert tasks.get(connection,rid)['model_attempts']==0
    assert tasks.get(connection,rid)['attempts']==[]
    cp(connection,task,'inputs')
    with pytest.raises(tasks.RepoConflict):
        tasks.checkpoint(connection,rid,token,'inputs',f'work/{token}/changed.json','b'*64,WF)
    assert connection.info.transaction_status.name=='IDLE'

def test_listing_filters_remain_text_and_scoped_and_migrations_repeat(database):
    connection,_=database
    create(connection,request={**REQUEST,'question':"'; DROP TABLE research_runs; --"})
    create(connection,root='/controlled/other')
    assert len(tasks.listing(connection,root='/controlled/m5',ticker='AAPL',status='queued'))==1
    assert tasks.listing(connection,root='/controlled/m5',ticker="'; DROP TABLE research_runs; --")==[]
    domain.migrate(connection)
    assert connection.execute('SELECT count(*) AS n FROM research_runs').fetchone()['n']==2

def test_waiting_for_row_lock_cannot_renew_a_lease_that_expired_while_waiting(database):
    connection,schema=database;task=claimed(connection);rid=task['run_id'];token=task['owner_token']
    started=Event()
    def waiter():
        with domain.connect(DSN,schema=schema) as own:
            started.set()
            with pytest.raises(tasks.OwnershipError,match='lease expired'):
                tasks.renew(own,rid,token)
            assert own.info.transaction_status.name=='IDLE'
    with ThreadPoolExecutor(max_workers=1) as pool:
        with connection.transaction():
            connection.execute('SELECT run_id FROM research_runs WHERE run_id=%s FOR UPDATE',(rid,))
            connection.execute("UPDATE research_runs SET lease_until=clock_timestamp()+interval '150 milliseconds' WHERE run_id=%s",(rid,))
            pending=pool.submit(waiter)
            assert started.wait(5)
            connection.execute('SELECT pg_sleep(0.25)')
        pending.result(timeout=5)

def test_two_active_owner_limit_is_atomic_and_releases_after_finish(database):
    connection,_=database
    submitted=[create(connection) for _ in range(3)]
    first=tasks.claim(connection,'/controlled/m5','worker-one',run_id=submitted[-1]['run_id'])
    assert first['run_id']==submitted[-1]['run_id']
    second=tasks.claim(connection,'/controlled/m5','worker-two')
    assert first and second and first['run_id']!=second['run_id']
    assert tasks.claim(connection,'/controlled/m5','worker-three') is None
    tasks.finish(connection,first['run_id'],'failed',owner_token=first['owner_token'])
    third=tasks.claim(connection,'/controlled/m5','worker-three')
    assert third and third['run_id'] not in {first['run_id'],second['run_id']}
