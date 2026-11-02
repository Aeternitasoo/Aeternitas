"""Check existing telemetry configuration and perform a bounded synthetic export.

No model inference. A successful flush is never treated as server readback.
"""
import argparse
from datetime import datetime,timezone,timedelta
import json
import logging
import os
from pathlib import Path
import sys
import time
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aeternitas import load_config
from agent_harness import bounded_call
from langfuse_adapter import Config,_sdk,Mapper,sanitize,trace_id,VERSION
from research_observability import Recorder
from research_workflow import save


SMOKE_VERSION = 'langfuse-smoke-v2'
MODEL = 'synthetic-no-model-call'


def verify_readback(rows, expected):
    """Compare server fields, not mere presence. Never save remote IO/metadata."""
    by_id = {r.id: r for r in rows}
    checks = {}
    receipts = []
    for name, want in expected.items():
        row = by_id.get(want['id'])
        if row is None:
            checks[name] = False
            continue
        metadata = row.metadata if isinstance(row.metadata, dict) else {}
        valid = (row.name == name and row.trace_id == want['trace_id']
                 and row.parent_observation_id == want['parent']
                 and row.type.upper() == want['type']
                 and row.level == want['level']
                 and row.start_time is not None and row.end_time is not None
                 and row.end_time >= row.start_time
                 and all(metadata.get(k) == v for k, v in want['metadata'].items()))
        if name == 'llm_synthesis':
            valid = (valid and row.model == MODEL and row.version == SMOKE_VERSION
                     and all((row.usage_details or {}).get(k) == v
                             for k, v in {'input': 7, 'output': 3, 'total': 10}.items()))
        checks[name] = bool(valid)
        # Do not persist server-provided arbitrary text, model names or metadata.
        receipts.append({'expected_name': name, 'observation_id': want['id'],
                         'fields_verified': bool(valid)})
    return {'passed': len(checks) == 3 and all(checks.values()),
            'checks': checks, 'observations': receipts}


def remote_smoke(*,config,identity,local_path,sdk_factory=_sdk,
                 read_attempts=10,pause=2):
    """One synthetic trace via production Mapper; bounded v2 API readback."""
    logging.disable(logging.CRITICAL)
    sdk=sdk_factory(config)
    mapper=Mapper(sdk)
    expected={}
    context={'harness_version': VERSION, 'prompt_version': SMOKE_VERSION,
             'candidate_version': 'a'*64, 'evaluation_id': 'b'*32,
             'input_fingerprint': 'c'*64, 'output_fingerprint': 'd'*64}
    def sink(event):
        clean=sanitize(event)
        mapper.event(clean)
        if clean['phase']=='start':
            obs=mapper.active[(clean['trace_id'],clean['span_id'])]
            parent=mapper.active.get((clean['trace_id'],clean['parent_span_id']))
            expected[clean['name']]={'id':obs.id,'trace_id':clean['trace_id'],
                'parent':parent.id if parent else None,
                'type':'GENERATION' if clean['name']=='llm_synthesis' else 'SPAN',
                'level':'ERROR' if clean['name']=='harness_tool' else 'DEFAULT',
                'metadata':{k:clean[k] for k in context}}
        else:
            expected[clean['name']]['metadata'].update({
                k:clean[k] for k in ('tool_name','status','error_code','elapsed_seconds')
                if k in clean})
    recorder=Recorder(Path(local_path),identity,sink,context=context)
    result={'status':'readback_not_verified','remote_delivery_verified':False,
            'trace_id':trace_id(identity),'read_requests':0,
            'usage_kind':'synthetic_fixture_not_model_usage',
            'actual_model_tokens':None,'currency_cost':None}
    try:
        with recorder.span('infrastructure'):
            with recorder.span('llm_synthesis',model=MODEL) as generation:
                # Fixed test counters validate transport only, never actual usage.
                generation.update(input_tokens=7,output_tokens=3,total_tokens=10)
            try:
                with recorder.span('harness_tool',tool_name='synthetic.failure'):
                    raise RuntimeError('synthetic_failure')
            except RuntimeError:
                pass
        if recorder.export_errors:
            result['status']='mapping_failed'
            return result
        sdk.flush()
        result['flush_completed']=True  # NOT a remote-delivery assertion.
        for attempt in range(read_attempts):
            result['read_requests']+=1
            with Path(local_path).with_name('readback-attempts.jsonl').open('a') as stream:
                stream.write(json.dumps({'attempt':attempt+1,'at':datetime.now(timezone.utc).isoformat(),
                                         'kind':'observations_v2_read'})+'\n')
            response=sdk.api.observations.get_many(
                trace_id=result['trace_id'],limit=100,
                fields='core,basic,metadata,model,usage',
                from_start_time=datetime.now(timezone.utc)-timedelta(minutes=5),
                request_options={'timeout_in_seconds':2,'max_retries':0})
            verified=verify_readback(response.data,expected)
            result['verification']=verified
            if verified['passed']:
                result.update(status='server_readback_verified',remote_delivery_verified=True)
                return result
            if attempt+1<read_attempts:
                time.sleep(pause)
        return result
    finally:
        sdk.shutdown()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-root',required=True,type=Path)
    p.add_argument('--env',type=Path,default=Path(__file__).resolve().parents[1]/'.env');a=p.parse_args()
    root=a.output_root/uuid4().hex;root.mkdir(parents=True,exist_ok=False)
    values={**load_config(a.env),**os.environ};config=Config.from_mapping(values)
    recorder=Recorder(root/'events.jsonl','check-'+root.name)
    with recorder.span('infrastructure'):
        if config is None:result={'status':'Requires Credentials','remote_delivery_verified':False}
        else:
            try:
                answer=bounded_call(remote_smoke,{'config':config,'identity':uuid4().hex,
                    'local_path':str(root/'synthetic-events.jsonl')},45,32768)
                result=answer['value'] if answer['ok'] else {'status':'export_or_readback_failed','remote_delivery_verified':False}
            except Exception:result={'status':'export_or_readback_timeout_or_failure','remote_delivery_verified':False}
    result.update(local_trace=str(recorder.path),model_requests=0,synthetic_only=True,
                  smoke_version=SMOKE_VERSION,actual_model_tokens=None,currency_cost=None)
    save(root/'langfuse-status.json',result);print(json.dumps(result))


if __name__=='__main__':main()
