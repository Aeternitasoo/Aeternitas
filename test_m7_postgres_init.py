"""Real temporary PG18 cluster: bootstrap demotion regression and private app init."""
import os
from pathlib import Path
import shutil
import socket
import subprocess

import psycopg
from psycopg.rows import dict_row
import pytest

import evidence_repository as domain

BIN=Path('/usr/lib/postgresql/18/bin')
SCRIPT=Path(__file__).resolve().parent/'config/postgres-init.sh'


@pytest.fixture
def private_cluster(tmp_path):
    if not (BIN/'initdb').exists():pytest.skip('Requires installed PostgreSQL 18 server/client tools')
    if os.getuid()==0:pytest.skip('initdb requires a non-root test process')
    cluster=tmp_path/'pgdata';sockets=tmp_path/'socket';sockets.mkdir()
    log=tmp_path/'server.log'
    with socket.socket() as bound:
        bound.bind(('127.0.0.1',0));port=bound.getsockname()[1]
    env={**os.environ,'PATH':str(BIN)+os.pathsep+os.environ.get('PATH',''),
         'PGDATA':str(cluster),'PGHOST':str(sockets),'PGPORT':str(port),
         'POSTGRES_DB':'aeternitas','POSTGRES_USER':'postgres',
         'POSTGRES_PASSWORD':"synthetic-only-init-'quoted'-password-934761"}
    for key in ('PGPASSWORD','PGSERVICE','PGPASSFILE','PGDATABASE','PGUSER','PGOPTIONS'):
        env.pop(key,None)
    initialized=subprocess.run([str(BIN/'initdb'),'-D',str(cluster),'--username=postgres',
        '--auth-local=trust','--auth-host=scram-sha-256','--no-locale','-E','UTF8'],
        env=env,capture_output=True,text=True,timeout=30)
    assert initialized.returncode==0,initialized.stderr
    started=subprocess.run([str(BIN/'pg_ctl'),'-D',str(cluster),'-l',str(log),'-o',
        f'-c listen_addresses=127.0.0.1 -c unix_socket_directories={sockets} -p {port}',
        '-w','start'],env=env,capture_output=True,text=True,timeout=30)
    assert started.returncode==0,started.stderr
    try:
        with psycopg.connect(host=str(sockets),port=port,user='postgres',dbname='postgres',autocommit=True) as admin:
            admin.execute('CREATE DATABASE aeternitas')
        yield env,cluster,log,port
    finally:
        stopped=subprocess.run([str(BIN/'pg_ctl'),'-D',str(cluster),'-m','immediate','-w','stop'],
            env=env,capture_output=True,text=True,timeout=30)
        assert stopped.returncode==0,stopped.stderr


def test_real_bootstrap_failure_private_app_login_owner_migration_and_sanitized_failure(private_cluster):
    env,cluster,log,port=private_cluster
    with psycopg.connect(host=env['PGHOST'],port=port,user='postgres',dbname='aeternitas',autocommit=True) as admin:
        # Recorded real Docker failure: initdb's bootstrap superuser cannot be
        # demoted. Do not reproduce this against any existing/live cluster.
        with pytest.raises(psycopg.Error) as rejected:
            admin.execute('ALTER ROLE postgres NOSUPERUSER NOCREATEDB NOCREATEROLE')
        assert 'bootstrap' in str(rejected.value).lower()
    first=subprocess.run(['bash',str(SCRIPT)],env=env,capture_output=True,text=True,timeout=15)
    assert first.returncode==0,first.stderr
    assert not (cluster/'.aeternitas-init-password').exists()
    secret=env['POSTGRES_PASSWORD']
    assert secret not in first.stdout+first.stderr+log.read_text()
    with psycopg.connect(host='127.0.0.1',port=port,user='aeternitas',password=secret,
                         dbname='aeternitas',autocommit=True,row_factory=dict_row) as app:
        privileges=app.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolcanlogin FROM pg_roles WHERE rolname=current_user").fetchone()
        assert privileges=={'rolsuper':False,'rolcreatedb':False,'rolcreaterole':False,'rolreplication':False,'rolcanlogin':True}
        assert app.execute("SELECT pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname=current_database()").fetchone()['owner']=='aeternitas'
        domain.migrate(app)
        assert app.execute('SELECT count(*) AS n FROM schema_migrations').fetchone()['n']>0
    with psycopg.connect(host=env['PGHOST'],port=port,user='postgres',dbname='aeternitas',autocommit=True) as admin:
        assert admin.execute("SELECT rolpassword IS NULL FROM pg_authid WHERE rolname='postgres'").fetchone()[0] is True
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(host='127.0.0.1',port=port,user='postgres',password=secret,dbname='aeternitas',connect_timeout=3)
    # Repeating CREATE ROLE fails, exercising the inner dynamic-SQL exception.
    # The server error context must not disclose the supplied quoted password.
    second=subprocess.run(['bash',str(SCRIPT)],env=env,capture_output=True,text=True,timeout=15)
    assert second.returncode!=0 and 'private diagnostics withheld' in second.stderr
    assert secret not in second.stdout+second.stderr+log.read_text()
    assert not (cluster/'.aeternitas-init-password').exists()
