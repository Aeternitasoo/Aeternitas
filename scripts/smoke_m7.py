#!/usr/bin/env python3
"""Clean directory + real PostgreSQL/API/worker smoke, fixed synthetic sources/model only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.offline_guard import sitecustomize as offline_guard  # Install before any connections.
import httpx
from psycopg import sql
import evidence_repository as domain
from api_client import APIClient
from research_inputs import collect_inputs
from research_runtime import Budget
from scripts.container_app import configuration
from scripts.container_fixture import CONFIG,FIXTURE,transports


def need(condition,message):
    if not condition: raise RuntimeError(message)


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(5)


def start_api(env,port,log,*,controlled):
    args=[sys.executable,'-u',str(ROOT/'scripts/container_app.py'),'api','--port',str(port)]
    if controlled:args.append('--controlled')
    process=subprocess.Popen(args,env=env,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
    try:
        until=time.monotonic()+25
        while time.monotonic()<until:
            need(process.poll() is None,'API exited before readiness; inspect api.log')
            try:
                if httpx.get(f'http://127.0.0.1:{port}/health',trust_env=False,timeout=1).status_code==200:
                    return process
            except httpx.HTTPError:pass
            time.sleep(.1)
        raise RuntimeError('API did not become ready')
    except BaseException:
        stop(process);raise


def run(data_dir,schema,port):
    from datetime import date,datetime,timezone
    root=Path(data_dir).resolve()
    need(not root.exists() or not any(root.iterdir()),'Clean smoke refuses a non-empty data directory')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    config=configuration(controlled=True)
    # The caller provides a disposable database/schema. No live config file,
    # home authorization or existing data directory is read.
    with domain.connect(config['DATABASE_URL']) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    with domain.connect(config['DATABASE_URL'],schema=schema) as connection:
        domain.migrate(connection)
        first=connection.execute('SELECT name,sha256 FROM schema_migrations ORDER BY name').fetchall()
        domain.migrate(connection)
        second=connection.execute('SELECT name,sha256 FROM schema_migrations ORDER BY name').fetchall()
    need(first==second and len(first)>0,'Migration replay changed the recorded migrations')
    question='合成材料安装验收；禁止作为真实研究或投资意见。'
    bundle=collect_inputs(root,'AAPL','0000320193',date(2025,10,1),question,CONFIG,
        refresh=False,news_days=14,budget=Budget(),transports=transports(),evidence_kind='controlled_test')
    with domain.connect(config['DATABASE_URL'],schema=schema) as connection:
        imported=domain.import_bundle(connection,root,bundle)
    (root/'fixed-input-bundle.json').write_text(json.dumps(bundle,ensure_ascii=False,indent=2)+'\n')
    home=root/'private-runtime';home.mkdir(mode=0o700)
    env={key:value for key,value in os.environ.items() if key not in
         {'OPENAI_API_KEY','FINNHUB_API_KEY','SEC_CONTACT','AETERNITAS_CONFIG_FILE','DATABASE_URL','AETERNITAS_MODEL','AETERNITAS_AUTH'}}
    env.update(HOME=str(home),AETERNITAS_DATA_DIR=str(root),AETERNITAS_SCHEMA=schema,
        PYTHONPATH=os.pathsep.join([str(ROOT/'scripts/offline_guard'),str(ROOT)]),
        NO_PROXY='localhost,127.0.0.1,::1',no_proxy='localhost,127.0.0.1,::1')
    process=None
    with (root/'api.log').open('ab') as log:
        try:
            process=start_api(env,port,log,controlled=True)
            token=(home/'.config/aeternitas/local-api-token').read_text().strip()
            payload={'ticker':'AAPL','as_of_date':'2025-10-01','question':question,'refresh':False,'news_days':14,'deadline':300}
            with APIClient(f'http://127.0.0.1:{port}',token=token) as client:
                queued=client.create(payload,idempotency_key='m7-clean-synthetic')
                rid=queued['run_id']
                need(queued['status']=='queued' and queued['model_attempts']==0,'Submission unexpectedly executed model work')
            with (root/'worker.log').open('ab') as worker_log:
                worker=subprocess.run([sys.executable,'-u',str(ROOT/'scripts/container_app.py'),'worker',
                    '--controlled','--enable-execution','--once'],env=env,cwd=ROOT,
                    stdin=subprocess.DEVNULL,stdout=worker_log,stderr=worker_log,timeout=120)
            need(worker.returncode==0,'Formal worker exited unsuccessfully; inspect worker.log')
            with APIClient(f'http://127.0.0.1:{port}',token=token) as client:
                row=client.request('GET',f'/research-runs/{rid}')
                report=client.request('GET',f'/research-runs/{rid}/report.json')
                need(row['status'] in {'completed','completed_with_gaps'} and row['model_attempts']==4,'Four-role formal run did not complete in four synthetic calls')
                need(report['evidence_kind']=='controlled_test' and len(report['analyses'])==4,'Synthetic evidence label or four analyses missing')
                html=httpx.get(f'http://127.0.0.1:{port}/research-runs/{rid}/report.html',trust_env=False)
                need(html.status_code==200 and '受控' in html.text,'HTML report missing controlled-material label')
            stop(process);process=None
            # A fresh API with no source/model credentials can read saved
            # history/report, but it must reject attempts to start new research.
            process=start_api(env,port,log,controlled=False)
            with APIClient(f'http://127.0.0.1:{port}',token=token) as client:
                need(client.request('GET',f'/research-runs/{rid}')['status']==row['status'],'Restart lost persisted status')
                need(client.request('GET','/research-runs')['items'][0]['run_id']==rid,'Restart lost task history')
                need(client.request('GET',f'/research-runs/{rid}/report.json')==report,'Restart changed verified report')
                response=client.client.post('/research-runs',json=payload)
                need(response.status_code==503 and response.json()['error']['code']=='model_not_configured',
                     'Credential-free history API did not reject new model research clearly')
            from research_pipeline import saved_report
            folder,verified=saved_report(root,rid)
            need(verified==report,'Offline report verification differs from HTTP report')
        finally:
            if process is not None:stop(process)
    result={'version':'m7-clean-install-smoke-v1','status':'passed','finished_at':datetime.now(timezone.utc).isoformat(),
        'evidence_kind':'controlled_test','external_source_requests':0,'external_model_requests':0,
        'synthetic_model_attempts':4,'real_postgresql':True,'real_api_processes':2,'real_worker_process':True,
        'new_data_directory':str(root),'schema':schema,'run_id':rid,'dataset_id':report['dataset_id'],
        'initial_fixed_bundle_dataset_id':imported,'fixture_sha256':hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        'migration_replay_unchanged':True,'migration_count':len(first),
        'persisted_history_after_api_restart':True,'saved_report_read_without_model_credentials':True,
        'new_model_work_rejected_without_credentials':True,
        'report_html_sha256':hashlib.sha256((folder/'report.html').read_bytes()).hexdigest(),
        'report_json_sha256':hashlib.sha256((folder/'report.json').read_bytes()).hexdigest(),
        'limitations':['Synthetic providers verify delivery and contracts; they do not demonstrate current live source/model availability.']}
    (root/'clean-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--schema',required=True)
    parser.add_argument('--port',type=int,default=8787)
    args=parser.parse_args()
    need(args.schema.startswith('m7_') and len(args.schema)<=63 and args.schema.replace('_','').isalnum(),
         'Use an explicit new m7_ schema in a disposable database')
    result=run(args.data_dir,args.schema,args.port)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
