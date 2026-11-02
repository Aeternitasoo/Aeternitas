"""Start/stop this project's local API and worker without requiring extra terminals.

No credentials in command arguments, PID metadata or logs. Linux/WSL only.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx


ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/'data/live/m3/runtime'


def active(name):
    path=RUNTIME/f'{name}.pid'
    try:
        pid=int(path.read_text())
        args=(Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
        return pid if str(ROOT/'aeternitas.py').encode() in args and name.encode() in args else None
    except (OSError,ValueError): return None


def start(name):
    if active(name): return
    with (RUNTIME/f'{name}.log').open('ab') as log:
        env=os.environ.copy()
        env['NO_PROXY']='localhost,127.0.0.1,::1,'+env.get('NO_PROXY',env.get('no_proxy',''))
        env['no_proxy']=env['NO_PROXY']
        proc=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-u',str(ROOT/'aeternitas.py'),name],
                              cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                              start_new_session=True,env=env)
    (RUNTIME/f'{name}.pid').write_text(str(proc.pid))


def stop(name):
    pid=active(name)
    if pid:
        os.kill(pid,signal.SIGTERM)
        for _ in range(100):
            if not active(name): break
            time.sleep(.1)
        else: raise RuntimeError(f'{name} did not stop; inspect its local log')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop','restart','status'])
    args=parser.parse_args()
    RUNTIME.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.action in {'stop','restart'}:
        for name in ('worker','serve'): stop(name)
    if args.action in {'start','restart'}:
        start('serve')
        for _ in range(80):
            try:
                result=httpx.get('http://127.0.0.1:8767/health',trust_env=False,timeout=1)
                if result.status_code==200: break
            except httpx.HTTPError: pass
            if not active('serve'): raise RuntimeError('API exited; inspect data/live/m3/runtime/serve.log')
            time.sleep(.1)
        else: raise RuntimeError('API not ready; check PostgreSQL and db-init')
        time.sleep(.3)
        if not active('serve'): raise RuntimeError('This project API exited; port 8767 may be occupied')
        start('worker')
        time.sleep(.3)
        if not active('worker'): raise RuntimeError('Worker exited; inspect data/live/m3/runtime/worker.log')
    print(json.dumps({name:('running' if active(name) else 'stopped') for name in ('serve','worker')}))


if __name__=='__main__':
    try: main()
    except (RuntimeError,OSError) as exc:
        print(f'Error: {exc}',file=sys.stderr)
        raise SystemExit(1)
