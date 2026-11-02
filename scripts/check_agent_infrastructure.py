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
from langfuse_adapter import Config,_sdk
from research_observability import Recorder
from research_workflow import save


def remote_smoke(*,config,identity):
    logging.disable(logging.CRITICAL)
    sdk=_sdk(config)
    root=sdk.start_observation(name='infrastructure_smoke',as_type='span',trace_context={'trace_id':identity})
    gen=sdk.start_observation(name='synthetic_generation',as_type='generation',model='synthetic-no-model-call',
        trace_context={'trace_id':identity,'parent_span_id':root.id})
    gen.end();root.end();sdk.flush()
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        result=sdk.api.observations.get_many(trace_id=identity,from_start_time=datetime.now(timezone.utc)-timedelta(minutes=2))
        rows=result.data
        if {'infrastructure_smoke','synthetic_generation'}<={r.name for r in rows}:
            sdk.shutdown()
            return {'status':'server_observations_received','remote_delivery_verified':True,'trace_id':identity,'observations':len(rows)}
        time.sleep(.5)
    sdk.shutdown()
    return {'status':'readback_not_visible_within_deadline','remote_delivery_verified':False,'trace_id':identity}


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
                answer=bounded_call(remote_smoke,{'config':config,'identity':uuid4().hex},12,32768)
                result=answer['value'] if answer['ok'] else {'status':'export_or_readback_failed','remote_delivery_verified':False}
            except Exception:result={'status':'export_or_readback_timeout_or_failure','remote_delivery_verified':False}
    result.update(local_trace=str(recorder.path),model_requests=0,synthetic_only=True)
    save(root/'langfuse-status.json',result);print(json.dumps(result))


if __name__=='__main__':main()
