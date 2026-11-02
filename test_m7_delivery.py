"""M7 install-mode boundaries and genuine real-PG, multi-process smoke."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from scripts.container_app import configuration,seed_auth


def test_controlled_configuration_never_reads_a_credential_file(tmp_path,monkeypatch):
    monkeypatch.setenv('AETERNITAS_CONFIG_FILE',str(tmp_path/'must-not-be-read'))
    monkeypatch.setenv('AETERNITAS_DATABASE_URL','dbname=synthetic')
    monkeypatch.delenv('AETERNITAS_DB_PASSWORD_FILE',raising=False)
    config=configuration(controlled=True)
    assert config['AETERNITAS_MODEL']=='synthetic-m7-no-external-model'
    assert config['OPENAI_API_KEY']=='synthetic-not-a-credential'


def test_unconfigured_deployment_has_no_selected_model_and_rejects_billing_switch(tmp_path,monkeypatch):
    monkeypatch.delenv('AETERNITAS_CONFIG_FILE',raising=False)
    monkeypatch.setenv('AETERNITAS_DATABASE_URL','dbname=synthetic')
    monkeypatch.delenv('AETERNITAS_DB_PASSWORD_FILE',raising=False)
    config=configuration()
    assert config['AETERNITAS_AUTH']=='chatgpt' and config['AETERNITAS_MODEL']=='auto'
    assert 'OPENAI_API_KEY' not in config
    bad=tmp_path/'config';bad.write_text('AETERNITAS_AUTH=api_key\n')
    monkeypatch.setenv('AETERNITAS_CONFIG_FILE',str(bad))
    with pytest.raises(ValueError,match='no API-key switch'):configuration()


def test_explicit_auth_copy_is_private_once_and_excludes_other_local_data(tmp_path):
    source=tmp_path/'source';(source/'profiles').mkdir(parents=True)
    (source/'active.json').write_text('{"client_id":"synthetic"}')
    (source/'profiles/example.json').write_text('{"access_token":"synthetic-only"}')
    (source/'local-api-token').write_text('synthetic-do-not-copy')
    destination=tmp_path/'destination'
    seed_auth(source,destination)
    assert not (destination/'local-api-token').exists()
    assert (destination/'active.json').stat().st_mode & 0o077==0
    (destination/'profiles/example.json').write_text('{"access_token":"synthetic-rotated"}')
    seed_auth(source,destination)
    assert json.loads((destination/'profiles/example.json').read_text())['access_token']=='synthetic-rotated'
    assert json.loads((source/'profiles/example.json').read_text())['access_token']=='synthetic-only'


def test_offline_verification_blocks_external_socket_before_a_request(tmp_path):
    root=Path(__file__).resolve().parent
    env={**os.environ,'PYTHONPATH':str(root/'scripts/offline_guard')}
    program="""import socket
with socket.socket() as connection:
    connection.settimeout(.1)
    try: connection.connect(('203.0.113.1',443))
    except RuntimeError as error:
        assert 'Offline verification blocked' in str(error)
    else: raise AssertionError('External connection was not blocked')
"""
    result=subprocess.run([sys.executable,'-c',program],env=env,cwd=tmp_path,capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr


@pytest.mark.skipif(not os.environ.get('AETERNITAS_TEST_DATABASE'),reason='Requires explicit real isolated PostgreSQL')
def test_clean_real_process_install_preserves_history_without_model_credentials(tmp_path,monkeypatch):
    from scripts.smoke_m7 import run
    dsn=os.environ['AETERNITAS_TEST_DATABASE'];schema='m7_delivery_'+uuid4().hex
    monkeypatch.setenv('AETERNITAS_DATABASE_URL',dsn)
    monkeypatch.delenv('AETERNITAS_DB_PASSWORD_FILE',raising=False)
    with socket.socket() as bound:
        bound.bind(('127.0.0.1',0));port=bound.getsockname()[1]
    try:
        result=run(tmp_path/'fresh',schema,port)
        assert result['status']=='passed' and result['synthetic_model_attempts']==4
        assert result['external_model_requests']==result['external_source_requests']==0
        assert result['new_model_work_rejected_without_credentials']
        assert result['saved_report_read_without_model_credentials']
    finally:
        with psycopg.connect(dsn,autocommit=True) as connection:
            connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
