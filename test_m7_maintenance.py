"""Real isolated PostgreSQL / pg_dump / pg_restore; all providers synthetic."""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
import shutil

from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

import evidence_repository as domain
import run_repository as tasks
from local_api import create_app
from m7_maintenance import (backup,restore,verify_backup,maintenance_scan,repair_completed,safe_path)
from research_service import ResearchService,ServiceError
from research_test_support import CONFIG,transports
from sources.sec import SourceError
from sources.sec import write_json,write_atomic
from analysis_workflow import publication_files
from research_pipeline import saved_report
from research_report import render_report

DSN=os.environ.get('AETERNITAS_TEST_DATABASE')
RESTORE_DSN=os.environ.get('AETERNITAS_TEST_RESTORE_DATABASE')
PAYLOAD=dict(ticker='AAPL',as_of_date='2025-10-01',question='Synthetic disaster recovery',refresh=True,news_days=14,deadline=300)
TOKEN='synthetic-m7-maintenance-token-0123456789abcdef'


@pytest.fixture
def installation(tmp_path):
    if not DSN: pytest.skip('Requires explicit isolated test PostgreSQL DSN')
    schema='m7_recovery_'+uuid4().hex
    with psycopg.connect(DSN,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with domain.connect(DSN,schema=schema) as connection: domain.migrate(connection)
        controlled,calls=transports()
        service=ResearchService(tmp_path/'original',{**CONFIG,'DATABASE_URL':DSN},schema=schema,transports=controlled)
        service.root.mkdir()
        rid=service.create(idempotency_key='maintenance-synthetic',**PAYLOAD)['run_id']
        with service.connection() as connection: task=tasks.claim(connection,service.root,'synthetic-m7-worker')
        service.execute(task)
        assert service.get(rid)['status'] in {'completed','completed_with_gaps'}
        yield service,rid,calls,tmp_path
    finally:
        with psycopg.connect(DSN,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


def _api_report(service,rid):
    with TestClient(create_app(service,token=TOKEN),base_url='http://127.0.0.1:8767',client=('127.0.0.1',43210)) as client:
        assert client.get(f'/research-runs/{rid}/report.html').status_code==200
        assert client.get(f'/research-runs/{rid}/report.json').status_code==200


def test_deleted_completed_report_repaired_from_successful_owner_without_new_calls(installation):
    service,rid,calls,tmp=installation
    original=dict(service.get(rid))
    html=service.root/'runs'/rid/'report.html'
    old=html.read_bytes(); html.unlink()
    assert maintenance_scan(service)['reports']==[{'run_id':rid,'status':'integrity_failed'}]
    with pytest.raises(ServiceError): service.report(rid)
    counters={k:len(v) for k,v in calls.items()}
    result=repair_completed(service,rid,quiesced_ack=True)
    assert result['source']=='verified_successful_owner'
    assert result['new_model_requests']==result['new_source_requests']==0
    assert html.read_bytes()==old and service.get(rid)==original
    assert counters=={k:len(v) for k,v in calls.items()}
    assert maintenance_scan(service)['reports'][0]['status']=='verified'
    _api_report(service,rid)


def test_backup_then_missing_raw_and_corrupt_canonical_repair_retains_corrupt_bytes(installation):
    service,rid,calls,tmp=installation
    secret=b'never include this credential sentinel'
    (service.root/'.env').write_bytes(secret)
    (service.root/'local-api-token').write_bytes(secret)
    folder=tmp/'backup'
    summary=backup(service,folder,quiesced_ack=True)
    manifest=verify_backup(folder)
    assert summary['files']>20
    assert '.env' not in manifest['files'] and 'local-api-token' not in manifest['files']
    assert not any(secret in p.read_bytes() for p in folder.rglob('*') if p.is_file())
    _,report=service.report(rid)
    raw=service.root/report['sources'][0]['snapshot_path']
    old_raw=raw.read_bytes(); raw.unlink()
    canonical=service.root/'runs'/rid/'report.html'
    canonical.write_bytes(b'corrupt canonical evidence must be retained')
    counters={k:len(v) for k,v in calls.items()}
    with pytest.raises(SourceError): repair_completed(service,rid,quiesced_ack=True)
    result=repair_completed(service,rid,quiesced_ack=True,backup_folder=folder)
    assert raw.read_bytes()==old_raw
    previous=service.root/result['operation_directory']/'previous/report.html'
    assert previous.read_bytes()==b'corrupt canonical evidence must be retained'
    assert counters=={k:len(v) for k,v in calls.items()}
    assert result['history_changed'] is False
    _api_report(service,rid)


def test_real_pg_dump_restore_to_empty_independent_database_and_relocated_root(installation):
    if not RESTORE_DSN: pytest.skip('Requires a second explicit empty test DB')
    service,rid,calls,tmp=installation
    # This test owns only its freshly chosen schema in the independent target DB.
    with domain.connect(RESTORE_DSN) as connection:
        if connection.execute("SELECT 1 FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') LIMIT 1").fetchone():
            pytest.skip('Second test database must be empty')
    folder=tmp/'backup'; target=tmp/'relocated'
    counters={k:len(v) for k,v in calls.items()}
    original=service.get(rid)
    # A verified file publication that predates research_runs remains a file
    # history, with source references, and must survive the same DB restore.
    legacy_id=uuid4().hex
    legacy=service.root/'runs'/legacy_id
    legacy.mkdir()
    source=service.root/'runs'/rid
    for name in publication_files(source)+['state.json','published.json']:
        target_file=legacy/name;target_file.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source/name,target_file)
    report=json.loads((legacy/'report.json').read_text()); report['run_id']=legacy_id
    state=json.loads((legacy/'state.json').read_text());state['run_id']=legacy_id
    write_json(legacy/'report.json',report);write_json(legacy/'state.json',state)
    write_atomic(legacy/'report.html',render_report(report).encode())
    write_json(legacy/'published.json',{'files':{name:hashlib.sha256((legacy/name).read_bytes()).hexdigest() for name in publication_files(legacy)}})
    saved_report(service.root,legacy_id)
    backup(service,folder,quiesced_ack=True)
    assert verify_backup(folder)['legacy_reports']==[{'run_id':legacy_id,'status':report['status'],'ticker':'AAPL'}]
    try:
        result=restore(folder,target,RESTORE_DSN,trusted_local_backup=True)
        restored=ResearchService(target,{**CONFIG,'DATABASE_URL':RESTORE_DSN},schema=service.schema)
        assert result['status']=='verified' and result['completed_reports_verified']==[rid]
        assert result['legacy_reports_verified']==[legacy_id]
        assert saved_report(target,legacy_id)[1]==report
        with pytest.raises(ServiceError): restored.get(legacy_id)
        assert restored.get(rid)==original
        assert restored.report(rid)[1]==service.report(rid)[1]
        assert restored.evidence(rid)==service.evidence(rid)
        assert counters=={k:len(v) for k,v in calls.items()}
        _api_report(restored,rid)
        with pytest.raises(SourceError): restore(folder,tmp/'second',RESTORE_DSN,trusted_local_backup=True)
    finally:
        with psycopg.connect(RESTORE_DSN,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(service.schema)))


def test_backup_requires_offline_ack_and_rejects_active_queue(installation):
    service,rid,calls,tmp=installation
    with pytest.raises(SourceError): backup(service,tmp/'not-acknowledged')
    queued=service.create(idempotency_key='still-queued',**PAYLOAD)
    with pytest.raises(SourceError): backup(service,tmp/'queue-remains',quiesced_ack=True)
    service.cancel(queued['run_id'])
    assert (tmp/'queue-remains'/'incomplete.json').exists()


def test_backup_restore_rows_are_invariant_under_pg_session_timezone(installation):
    """A +08 source and UTC target represent the exact same persisted instants."""
    if not RESTORE_DSN: pytest.skip('Requires a second explicit empty test DB')
    service,rid,calls,tmp=installation
    with domain.connect(RESTORE_DSN) as connection:
        if connection.execute("SELECT 1 FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') LIMIT 1").fetchone():
            pytest.skip('Second test database must be empty')
    service.config['DATABASE_URL']=make_conninfo(DSN,options='-c timezone=Asia/Shanghai')
    target_dsn=make_conninfo(RESTORE_DSN,options='-c timezone=UTC')
    with service.connection() as connection:
        assert connection.execute('SHOW timezone').fetchone()['TimeZone']=='Asia/Shanghai'
    with domain.connect(target_dsn) as connection:
        assert connection.execute('SHOW timezone').fetchone()['TimeZone']=='UTC'
    before={k:len(v) for k,v in calls.items()}
    folder=tmp/'timezone-backup'
    backup(service,folder,quiesced_ack=True)
    try:
        result=restore(folder,tmp/'timezone-restored',target_dsn,trusted_local_backup=True)
        assert result['status']=='verified'
        restored=ResearchService(tmp/'timezone-restored',{'DATABASE_URL':target_dsn},schema=service.schema)
        assert restored.report(rid)[1]==service.report(rid)[1]
        assert restored.get(rid)['created_at']==service.get(rid)['created_at']
        assert restored.get(rid)['as_of_date']==service.get(rid)['as_of_date']
        assert {k:len(v) for k,v in calls.items()}==before
    finally:
        with psycopg.connect(RESTORE_DSN,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(service.schema)))


@pytest.mark.parametrize('relative',['../secret','runs/../../.env','/etc/passwd','runs\\escape','runs/x:ads'])
def test_rejects_path_escape(tmp_path,relative):
    with pytest.raises(SourceError): safe_path(tmp_path,relative)


def test_rejects_internal_symlink_and_tampered_backup(installation):
    service,rid,calls,tmp=installation
    folder=tmp/'backup'; backup(service,folder,quiesced_ack=True)
    manifest=verify_backup(folder)
    name=next(iter(manifest['files']))
    path=folder/'files'/name
    real=tmp/'moved-original';path.rename(real);path.symlink_to(real)
    with pytest.raises(SourceError): verify_backup(folder)
    path.unlink();path.write_bytes(real.read_bytes()+b'corrupt')
    with pytest.raises(SourceError): verify_backup(folder)


def test_orphan_generation_scan_is_dry_run_and_never_deletes_history(installation):
    service,rid,calls,tmp=installation
    owners=service.root/'runs'/rid/'owners'
    empty=owners/str(uuid4());empty.mkdir()
    nonempty=owners/str(uuid4());nonempty.mkdir();(nonempty/'unknown-file').write_text('historical evidence')
    scan=maintenance_scan(service)
    rows={Path(g['generation']).name:g for g in scan['generations']}
    assert rows[empty.name]['safe_cleanup_candidate'] is True
    assert rows[nonempty.name]['safe_cleanup_candidate'] is False
    assert empty.is_dir() and (nonempty/'unknown-file').read_text()=='historical evidence'
