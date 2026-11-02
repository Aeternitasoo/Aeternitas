"""Explicit, bounded live M5 crash/recovery acceptance using the existing plan login.

Run only after reviewing this harness. It deliberately consumes real model calls
and kills its own worker at four named boundaries. It never switches providers,
extends the original deadline, edits lease timestamps, or retries an exhausted
role. Interrupted provider billing is retained as unknown. Existing acceptance
records are never replaced. No production task except this run is claimed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))

from aeternitas import load_config
from evidence_repository import json_value
from local_api import create_app, local_token
from research_pipeline import saved_report
from research_runtime import utc_now
from research_service import ResearchService
import run_repository as tasks
from sources.sec import write_json


STAGES=(
    ('fundamentals-after-request','after_request','fundamentals',True),
    ('fundamentals-after-checkpoint','after_checkpoint','fundamentals',False),
    ('events-after-response','after_response','events',True),
    ('after-publication-files','after_publication_files',None,False),
)
CRASH_EXIT=97
QUESTION='请研究经营变化与主要风险，区分财务事实、事件、可能解释和仍缺少的证据。'


def arguments():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=PROJECT)
    parser.add_argument('--as-of',default='2026-10-23')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--child-stage',choices=[stage[0] for stage in STAGES]+['finish'],help=argparse.SUPPRESS)
    parser.add_argument('--run-id',help=argparse.SUPPRESS)
    parser.add_argument('--record',type=Path,help=argparse.SUPPRESS)
    return parser.parse_args()


def proxy_context():
    # This is deliberately booleans only: proxies may contain user information.
    proxy=os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy') or ''
    entries=[item for key in ('NO_PROXY','no_proxy') for item in os.environ.get(key,'').split(',') if item]
    entries=list(dict.fromkeys(['localhost','127.0.0.1','::1',*entries]))
    os.environ['NO_PROXY']=os.environ['no_proxy']=','.join(entries)
    return dict(inherited_proxy_configured=bool(proxy),effective_proxy_matches_previous_7897=proxy=='http://127.0.0.1:7897',
                localhost_bypass=True,loopback_HTTP_client_trust_env=False)


def configuration(project):
    config=load_config(project/'.env')
    if config.get('AETERNITAS_AUTH')!='chatgpt':
        raise ValueError('Live M5 acceptance requires the existing ChatGPT subscription configuration')
    if not config.get('DATABASE_URL'):
        raise ValueError('Existing local PostgreSQL configuration is required')
    return config


def clean(value,secrets):
    if isinstance(value,dict): return {key:clean(child,secrets) for key,child in value.items()}
    if isinstance(value,list): return [clean(child,secrets) for child in value]
    if isinstance(value,str):
        for secret in secrets:
            if secret: value=value.replace(secret,'[redacted]')
        return value
    return value


def persist(path,record,secrets=()):
    # The acceptance record contains only explicit public diagnostics. Exact
    # configured secret values are also removed as a final defensive boundary.
    serializable=json.loads(json.dumps(clean(record,secrets),ensure_ascii=False,default=json_value))
    write_json(path,serializable)


def snapshot(service,rid,secrets):
    with service.connection() as connection:
        row=tasks.get(connection,rid,root=service.root)
    if row is None: raise ValueError('Acceptance run disappeared')
    public={key:row.get(key) for key in ('run_id','ticker','as_of_date','model','billing_mode','status','step',
           'model_attempts','model_usage','dataset_id','error','created_at','started_at','finished_at',
           'deadline_at','lease_until','resume_count','cancel_requested','request_fingerprint','workflow_config_fingerprint')}
    public['steps']=[{key:step.get(key) for key in ('step','ordinal','status','model_attempts','error',
                     'artifact_sha256','workflow_fingerprint','verified_at','started_at','finished_at')}
                     for step in row['steps']]
    public['attempts']=[{key:attempt.get(key) for key in ('role','role_attempt','total_ordinal','status','usage',
                       'started_at','finished_at')} for attempt in row['attempts']]
    return clean(public,secrets)


def child(args):
    project=args.project.resolve();root=project/'data/live'
    if project!=PROJECT or not isinstance(args.run_id,str) or not re.fullmatch('[0-9a-f]{32}',args.run_id):
        raise ValueError('Worker child requires this project and its explicit acceptance run ID')
    proxy_context()
    config=configuration(project)
    record=args.record.resolve()
    if not record.is_relative_to((root/'m5').resolve()): raise ValueError('Worker record is outside acceptance storage')
    stage_dir=record.parent/'live-stages'/args.run_id/args.child_stage
    stage_dir.mkdir(parents=True,exist_ok=True)
    stage=next((item for item in STAGES if item[0]==args.child_stage),None)
    marker=stage_dir/'fault-fired.json'
    if marker.exists(): raise ValueError('This fault stage already fired; refusing to repeat it')
    def fault(event,**details):
        if stage is None or event!=stage[1] or (stage[2] is not None and details.get('role')!=stage[2]): return
        if details.get('run_id',args.run_id)!=args.run_id: return
        # os._exit terminates the real worker process and its DB sockets without
        # finally handlers. No synthetic Python exception substitutes for it.
        write_json(marker,dict(stage=stage[0],event=event,role=details.get('role'),
                               attempt=details.get('attempt'),run_id=args.run_id,at=utc_now(),exit_code=CRASH_EXIT))
        os._exit(CRASH_EXIT)
    service=ResearchService(root,config,schema='public',fault_hook=fault)
    with service.connection() as connection:
        task=tasks.claim(connection,root,'m5-live-'+uuid4().hex,lease_seconds=15,run_id=args.run_id)
        if connection.info.transaction_status.name!='IDLE': raise ValueError('Claim did not commit before work')
    if task is None: raise ValueError('Acceptance task unavailable or two active local workers already occupy capacity')
    if task['billing_mode']!='chatgpt_plan': raise ValueError('Acceptance task has an unexpected billing configuration')
    service.execute(task)
    with service.connection() as connection: result=tasks.get(connection,args.run_id,root=root)
    if stage is not None: raise ValueError('Expected crash boundary was not reached; saved failure must be inspected')
    if result['status'] not in {'completed','completed_with_gaps'}: raise ValueError('Final publication did not complete')
    return 0


@contextmanager
def local_http(service,token):
    import httpx
    import uvicorn
    listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    listener.bind(('127.0.0.1',0));listener.listen(128)
    port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(create_app(service,token=token),host='127.0.0.1',port=port,
                                        log_level='critical',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True,name='m5-live-local-http')
    thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and thread.is_alive() and time.monotonic()<deadline: time.sleep(.05)
        if not server.started: raise ValueError('Acceptance loopback HTTP server did not start')
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=20,
                          headers={'X-Aeternitas-Token':token}) as client:
            yield client
    finally:
        server.should_exit=True;thread.join(timeout=10);listener.close()


def invoke_child(args,rid,record_path,stage,secrets):
    directory=record_path.parent/'live-stages'/rid/stage
    directory.mkdir(parents=True,exist_ok=True)
    log=directory/'worker.log'
    command=[sys.executable,str(Path(__file__).resolve()),'--project',str(args.project.resolve()),
             '--as-of',args.as_of,'--child-stage',stage,'--run-id',rid,'--record',str(record_path)]
    started=time.monotonic()
    fd=os.open(log,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as output:
        process=subprocess.Popen(command,cwd=args.project,stdout=output,stderr=output,env=os.environ.copy())
        # A hard upper bound protects the harness too. The pipeline retains its
        # earlier absolute deadline and the lease still fences a killed child.
        try: code=process.wait(timeout=1850)
        except subprocess.TimeoutExpired:
            process.kill();process.wait();code=process.returncode
    body=log.read_text(encoding='utf-8',errors='replace')
    redacted=clean(body,secrets)
    if redacted!=body: log.write_text(redacted,encoding='utf-8')
    return dict(exit_code=code,elapsed_seconds=round(time.monotonic()-started,3),
                log=log.relative_to(args.project).as_posix(),log_sha256=hashlib.sha256(log.read_bytes()).hexdigest())


def naturally_expire(service,rid,secrets):
    started=time.monotonic();polls=[]
    while time.monotonic()-started<=20:
        with service.connection() as connection:
            changed=tasks.recover_expired(connection,service.root)
        status=snapshot(service,rid,secrets)
        polls.append(dict(at=utc_now(),status=status['status'],lease_until=status['lease_until'],recovered_count=changed))
        if status['status']=='interrupted':
            return dict(elapsed_seconds=round(time.monotonic()-started,3),polls=polls,state=status)
        if status['status']!='running': raise ValueError('Expected a crashed running worker before natural lease expiry')
        time.sleep(1)
    raise ValueError('Worker lease did not naturally expire within twenty seconds; no SQL time override was performed')


def source_audit(service,report):
    # saved_report has already compared every receipt, original source byte hash,
    # filing span and deterministic calculation. Independently cross-check the
    # selected SEC values against both raw JSON pointers and NUMERIC DB rows.
    source=report['financials']['source']
    raw=json.loads((service.root/source['snapshot_path']).read_text(),parse_float=Decimal)
    checked=[]
    with service.connection() as connection:
        import evidence_repository as domain
        stored={fact['fact_id']:fact for fact in domain.facts(connection,report['ticker'],report['dataset_id'])}
        for annual in report['financials']['annual_periods']:
            for metric,fact in annual['facts'].items():
                if fact is None: continue
                value=raw
                for part in fact['source_json_pointer'].split('/')[1:]: value=value[int(part)] if isinstance(value,list) else value[part]
                if Decimal(str(value['val']))!=Decimal(fact['value']) or stored[fact['fact_id']]['value']!=Decimal(fact['value']):
                    raise ValueError('Selected SEC value differs from raw source or PostgreSQL')
                checked.append(dict(metric=metric,period_end=fact['period_end'],fact_id=fact['fact_id'],
                                    source_json_pointer=fact['source_json_pointer'],value_usd=fact['value']))
    return dict(all_source_bytes_receipts_filing_spans_and_calculations='saved_report_passed',
                raw_JSON_and_PostgreSQL_facts_checked=checked,semantic_support='human_review_required')


def main(args):
    project=args.project.resolve();args.project=project
    if project!=PROJECT: raise ValueError('Project must be the checked-out repository containing this reviewed harness')
    config=configuration(project);proxy=proxy_context()
    root=project/'data/live';target=root/'m5';target.mkdir(parents=True,exist_ok=True)
    record_path=(args.output or target/'live-acceptance.json').resolve()
    if not record_path.is_relative_to(target.resolve()): raise ValueError('Acceptance output must stay inside data/live/m5')
    if record_path.exists(): raise ValueError('Acceptance record already exists; inspect it instead of replacing it')
    token=local_token()
    secrets=[config.get(key,'') for key in ('OPENAI_API_KEY','FINNHUB_API_KEY','SEC_CONTACT','DATABASE_URL')]+[token]
    service=ResearchService(root,config,schema='public')
    with service.connection() as connection:
        import evidence_repository as domain
        domain.migrate(connection)
    record=dict(schema_version='m5-live-acceptance-v1',status='in_progress',started_at=utc_now(),
                billing_mode='chatgpt_plan',proxy=proxy,stages=[],original_deadline_seconds=1800,
                expected_calls_without_repairs=6,max_calls=8,
                deliberate_faults='own subprocess os._exit; no SQL-forced lease expiry',
                unknown_call_acknowledgement='explicit per named experiment boundary; not global retry policy')
    persist(record_path,record,secrets)
    rid=None
    try:
        request=dict(ticker='AAPL',as_of_date=args.as_of,question=QUESTION,refresh=True,news_days=14,deadline=1800)
        key='m5-live-'+uuid4().hex
        created=service.create(idempotency_key=key,**request);rid=created['run_id']
        if created['billing_mode']!='chatgpt_plan': raise ValueError('Unexpected billing mode')
        again=service.create(idempotency_key=key,**request)
        if again['run_id']!=rid or not again['reused']: raise ValueError('Live idempotency replay did not reuse this task')
        record.update(run_id=rid,idempotency_key=key,idempotency_replay_same_id=True,
                      model=created['model'],request=request)
        persist(record_path,record,secrets)
        with local_http(service,token) as http:
            for stage,event,role,ack in STAGES:
                before=snapshot(service,rid,secrets)
                proc=invoke_child(args,rid,record_path,stage,secrets)
                marker=record_path.parent/'live-stages'/rid/stage/'fault-fired.json'
                item=dict(stage=stage,event=event,role=role,process=proc,before=before,
                          after_process=snapshot(service,rid,secrets))
                record['stages'].append(item);persist(record_path,record,secrets)
                if proc['exit_code']!=CRASH_EXIT or not marker.is_file():
                    raise ValueError('Worker failed before the expected reviewed crash boundary; no automatic recovery attempted')
                item['fault']=json.loads(marker.read_text())
                item['natural_expiry']=naturally_expire(service,rid,secrets)
                withheld=http.get(f'/research-runs/{rid}/report.html')
                item['HTTP_report_before_resume']=withheld.status_code
                if withheld.status_code!=409: raise ValueError('A crashed/uncommitted research report was exposed over HTTP')
                # These are the two explicitly authorized uncertain-call resumes:
                # one accepted request with unknown outcome, one completed reply
                # whose result had not reached a validated checkpoint.
                item['acknowledge_unknown']=ack
                resumed=service.resume(rid,acknowledge_unknown=ack)
                if resumed['model_attempts']!=item['after_process']['model_attempts']:
                    raise ValueError('Resume reset the cumulative model call budget')
                item['resumed']=snapshot(service,rid,secrets)
                persist(record_path,record,secrets)
            before_calls=snapshot(service,rid,secrets)['model_attempts']
            final_proc=invoke_child(args,rid,record_path,'finish',secrets)
            final_state=snapshot(service,rid,secrets)
            record.update(final_process=final_proc,final_state=final_state)
            persist(record_path,record,secrets)
            if final_proc['exit_code']!=0: raise ValueError('Final worker did not finish successfully')
            if final_state['model_attempts']!=before_calls: raise ValueError('Publication-only resume launched an unnecessary model call')
            folder,report=saved_report(root,rid)
            if not 6<=report['model_attempts']<=8: raise ValueError('Live model attempt count differs from the reviewed bounded experiment')
            response=http.get(f'/research-runs/{rid}/report.json')
            html=http.get(f'/research-runs/{rid}/report.html')
            if response.status_code!=200 or response.json()!=report or html.status_code!=200:
                raise ValueError('Final HTTP report round trip failed')
            evidence=http.get(f'/research-runs/{rid}/evidence')
            if evidence.status_code!=200 or len(evidence.json()['items'])!=len(report['evidence']):
                raise ValueError('HTTP evidence round trip failed')
            verification=subprocess.run([sys.executable,str(project/'aeternitas.py'),'--data-dir',str(root),'verify',rid],
                                         cwd=project,capture_output=True,text=True,timeout=60)
            if verification.returncode: raise ValueError('Offline verify CLI rejected final report')
            record['delivery']=dict(HTTP_report_JSON=200,HTTP_report_HTML=200,HTTP_evidence_count=len(report['evidence']),
                                    verify_CLI_exit=0,verify_CLI_summary=clean(verification.stdout.strip(),secrets),
                                    source_audit=source_audit(service,report),
                                    publication_manifest=json.loads((folder/'published.json').read_text()),
                                    publication_resume_no_new_calls=True,
                                    report_path=(folder/'report.html').relative_to(project).as_posix())
        known={name:sum(usage['usage'].get(name,0) for usage in report['model_usage'] if isinstance(usage.get('usage'),dict))
               for name in ('input_tokens','output_tokens','total_tokens')}
        record.update(status='passed',finished_at=utc_now(),model_attempts=report['model_attempts'],
                      known_usage=known,cost='unknown',unknown_usage_retained=True,
                      limitations=['Deliberate interruption cannot prove whether a remote accepted request was charged',
                                   'Original absolute deadline and cumulative call budgets were preserved',
                                   'Quote/source checks do not automatically certify semantic support'])
        persist(record_path,record,secrets)
        print(json.dumps(dict(run_id=rid,status='passed',model_attempts=report['model_attempts'],
                              record=record_path.relative_to(project).as_posix()),ensure_ascii=False))
        return 0
    except BaseException as exc:
        record.update(status='failed',finished_at=utc_now(),failure_type=type(exc).__name__,
                      failure=clean(str(exc),secrets)[:500])
        if rid is not None:
            try: record['failure_state']=snapshot(service,rid,secrets)
            except Exception as snapshot_error: record['snapshot_failure_type']=type(snapshot_error).__name__
        persist(record_path,record,secrets)
        print(json.dumps(dict(run_id=rid,status='failed',failure_type=type(exc).__name__,
                              record=record_path.relative_to(project).as_posix()),ensure_ascii=False))
        return 1


if __name__=='__main__':
    parsed=arguments()
    try:
        raise SystemExit(child(parsed) if parsed.child_stage else main(parsed))
    except (KeyboardInterrupt,SystemExit): raise
    except BaseException as error:
        # Early setup and child failures print types only, never configuration,
        # provider exception strings, headers, credential files or DSNs.
        print(json.dumps({'status':'failed','failure_type':type(error).__name__}))
        raise SystemExit(1)
