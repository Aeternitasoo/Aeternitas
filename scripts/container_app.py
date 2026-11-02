#!/usr/bin/env python3
"""Explicit container/local clean-install entry points; never auto-load repo .env."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evidence_repository as domain
from aeternitas import load_config
from research_service import ResearchService


def configuration(*, controlled=False):
    # A credential file is opt-in. An unset configuration can serve history but
    # cannot submit new model work, because no account model is configured.
    if controlled:
        from scripts.container_fixture import CONFIG
        config = dict(CONFIG)
    else:
        path = os.environ.get('AETERNITAS_CONFIG_FILE')
        config = load_config(Path(path)) if path else {'AETERNITAS_AUTH':'chatgpt','AETERNITAS_MODEL':'auto'}
    dsn = os.environ.get('AETERNITAS_DATABASE_URL')
    password_file = os.environ.get('AETERNITAS_DB_PASSWORD_FILE')
    if password_file:
        from psycopg.conninfo import make_conninfo
        password = Path(password_file).read_text().strip()
        if not password: raise ValueError('Database password file is empty')
        dsn = make_conninfo(host='127.0.0.1',port=os.environ.get('AETERNITAS_DB_PORT','55432'),
                           dbname='aeternitas',user='aeternitas',password=password)
    if not dsn: raise ValueError('Explicit AETERNITAS_DATABASE_URL or database password file required')
    config['DATABASE_URL'] = dsn
    if config['AETERNITAS_AUTH'] != 'chatgpt' and not controlled:
        raise ValueError('This deployment only supports existing ChatGPT subscription authorization; no API-key switch')
    return config


def service(*, controlled=False):
    root = Path(os.environ.get('AETERNITAS_DATA_DIR','/data'))
    schema = os.environ.get('AETERNITAS_SCHEMA','public')
    transport = {}
    if controlled:
        from scripts.container_fixture import transports
        transport = transports()
    return ResearchService(root,configuration(controlled=controlled),schema=schema,transports=transport)


def seed_auth(source, destination):
    """Only the explicit live worker seeds a private rotating-token volume once."""
    import shutil
    source, destination = Path(source), Path(destination)
    marker = destination/'.seeded-from-readonly'
    if marker.exists(): return
    if not (source/'active.json').is_file() or not (source/'profiles').is_dir():
        raise ValueError('Existing ChatGPT authorization is required for the live profile')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    destination.chmod(0o700)
    # Copy exactly auth selection/profile files, not arbitrary local files,
    # logs or local API access tokens from the source directory.
    for item in [source/'active.json', *sorted((source/'profiles').glob('*.json'))]:
        if item.is_symlink() or not item.is_file(): raise ValueError('Authorization source must contain regular files')
        relative = item.relative_to(source)
        output = destination/relative
        output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        shutil.copyfile(item,output)
        output.chmod(0o600)
    marker.write_text('Private persistent OAuth copy initialized; original mount remains read-only.\n')
    marker.chmod(0o600)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['migrate','api','worker','health'])
    parser.add_argument('--controlled',action='store_true',help='Explicit synthetic providers; never real model/source requests')
    parser.add_argument('--enable-execution',action='store_true',help='Permit queued model work; default worker only waits')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--port',type=int,default=int(os.environ.get('AETERNITAS_API_PORT','8767')))
    args=parser.parse_args(argv)
    instance=service(controlled=args.controlled)
    if args.command=='migrate':
        with instance.connection() as connection: domain.migrate(connection)
        print('Checked migrations applied; no source or model requests')
    elif args.command=='health':
        import httpx
        result=httpx.get(f'http://127.0.0.1:{args.port}/health',trust_env=False,timeout=3)
        result.raise_for_status()
    elif args.command=='api':
        import uvicorn
        from local_api import create_app
        uvicorn.run(create_app(instance),host='127.0.0.1',port=args.port,access_log=False,proxy_headers=False)
    elif args.enable_execution:
        if not args.controlled:
            seed_auth('/run/chatgpt-source',Path.home()/'.config/aeternitas')
        from research_worker import work
        work(instance,once=args.once)
    else:
        # A restored queue may contain unfinished live tasks. The default
        # history-only deployment must not consume or fail them accidentally.
        print('worker ready; execution disabled; saved reports available; explicit live authorization required',flush=True)
        stop=[]
        signal.signal(signal.SIGTERM,lambda *_:stop.append(True))
        signal.signal(signal.SIGINT,lambda *_:stop.append(True))
        Path('/tmp/aeternitas-worker-ready').write_text(str(os.getpid()))
        while not stop: time.sleep(1)
    return 0


if __name__=='__main__':
    try: raise SystemExit(main())
    except (ValueError,OSError,domain.SourceError) as exc:
        print(f'Container operation failed ({type(exc).__name__}); inspect configuration and database readiness; no billing fallback',file=sys.stderr)
        raise SystemExit(1)
