"""Opt-in, content-free Langfuse 5 export. Local Recorder stays authoritative.

An expendable process owns SDK threads and network I/O. The caller never waits
on export while recording; flush/shutdown have a hard wall-clock bound. Dropped
telemetry is diagnostic, never a reason to repeat research or a model request.
"""
from dataclasses import dataclass, field
import hashlib
import logging
import math
import multiprocessing as mp
import os
from queue import Full
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from research_observability import FIELDS

VERSION = 'langfuse-adapter-v1'
NAMES = frozenset({'harness', 'harness_model', 'harness_tool_admission',
    'harness_tool', 'harness_validation', 'research', 'source_collection',
    'evidence_organization', 'llm_synthesis', 'output_validation', 'report_save',
    'quality_source_financial','quality_counterevidence_claim_falsification',
    'infrastructure','memory_read','memory_write','rsi_analysis','candidate_record'})
NUMBERS = frozenset({'input_tokens','output_tokens','total_tokens','elapsed_seconds',
    'evidence_count','source_count','request_attempts','http_status','round_number','retry_count','memory_count'})
SECRET = re.compile(r'(?i)(bearer|authorization|password|secret|api[_-]?key|sk-|eyJ|access[_-]?token|refresh[_-]?token)')


def trace_id(run_id):
    return hashlib.sha256(str(run_id).encode()).hexdigest()[:32]


def mask_data(*,data,**_):
    # Langfuse applies mask() to metadata too. Preserve only our already scrubbed
    # flat telemetry fields; withholding *all* metadata would lose versions.
    keys=FIELDS | {'trace_id','run_id_hash','parent_run_id_hash','response_id_hash','call_id_hash',
                   'evaluation_id','evaluator_version'}
    if isinstance(data,dict) and set(data)<=keys:
        return data
    return None if data is None else '[content withheld]'


@dataclass(frozen=True)
class Config:
    public_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    base_url: str
    queue_size: int = 128
    timeout: float = 3.0

    def __post_init__(self):
        u = urlsplit(self.base_url)
        if (u.scheme not in {'http','https'} or not u.hostname or u.username or u.password
                or u.query or u.fragment or not self.public_key or not self.secret_key
                or not 1 <= self.queue_size <= 1024 or not 0.1 <= self.timeout <= 15):
            raise ValueError('Invalid Langfuse configuration (values withheld)')
        if u.scheme == 'http' and u.hostname not in {'localhost','127.0.0.1','::1'}:
            raise ValueError('Remote Langfuse requires HTTPS')

    @classmethod
    def from_env(cls):
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls,values):
        public = values.get('LANGFUSE_PUBLIC_KEY')
        secret = values.get('LANGFUSE_SECRET_KEY')
        host = values.get('LANGFUSE_BASE_URL') or values.get('LANGFUSE_HOST')
        return cls(public, secret, host) if public and secret and host else None


def sanitize(event):
    """Second allowlist at export boundary, even if a caller bypasses Recorder."""
    if event.get('name') not in NAMES or event.get('phase') not in {'start','end'}:
        raise ValueError('Unknown telemetry event')
    out = {'name':event['name'], 'phase':event['phase'],
           'trace_id':trace_id(event['trace_id']),
           'run_id_hash':hashlib.sha256(str(event['trace_id']).encode()).hexdigest()}
    for k in ('span_id','parent_span_id'):
        value=event.get(k)
        if value is not None and not re.fullmatch(r'[0-9a-f]{32}', str(value)):
            raise ValueError('Invalid local span ID')
        out[k]=value
    for k in FIELDS:
        value=event.get(k)
        if value is None:
            continue
        if k in NUMBERS:
            if type(value) in {int,float} and math.isfinite(value) and value >= 0:
                out[k]=value
        elif k == 'cached' and type(value) is bool:
            out[k]=value
        elif k in {'parent_run_id','response_id','call_id'}:
            out[k+'_hash']=hashlib.sha256(str(value).encode()).hexdigest()
        elif (isinstance(value,str) and len(value)<=120
              and re.fullmatch(r'[A-Za-z0-9_.:-]+',value) and not SECRET.search(value)):
            out[k]=value
    return out


class Mapper:
    """Official SDK calls, separately testable without any transport."""
    def __init__(self, sdk):
        self.sdk, self.active = sdk, {}

    def event(self, e):
        key=(e['trace_id'],e['span_id'])
        metadata={k:v for k,v in e.items() if k not in {'name','phase','span_id','parent_span_id'}}
        if e['phase']=='start':
            if len(self.active)>=1024 or key in self.active:
                raise ValueError('Telemetry span capacity / duplicate')
            parent=self.active.get((e['trace_id'],e['parent_span_id']))
            if e['parent_span_id'] is not None and parent is None:
                raise ValueError('Telemetry parent missing (possibly dropped)')
            context={'trace_id':e['trace_id']}
            if parent is not None:
                context['parent_span_id']=parent.id
            kind='generation' if e['name'] in {'harness_model','llm_synthesis'} else 'span'
            self.active[key]=self.sdk.start_observation(name=e['name'], as_type=kind,
                trace_context=context, metadata=metadata, model=e.get('model') if kind=='generation' else None,
                version=e.get('prompt_version') or e.get('harness_version'))
        else:
            obs=self.active.pop(key,None)
            if obs is None:
                raise ValueError('Telemetry start missing')
            update={'metadata':metadata, 'level':'ERROR' if e.get('status')=='failed' else 'DEFAULT'}
            if e['name'] in {'harness_model','llm_synthesis'}:
                usage={k[:-7] if k.endswith('_tokens') else k:e[k]
                       for k in ('input_tokens','output_tokens','total_tokens') if k in e}
                if usage: update['usage_details']=usage
            obs.update(**update)
            obs.end()

    def score(self, value):
        for name, score in value['metrics'].items():
            metadata={'evaluation_id':value['evaluation_id'],'evaluator_version':value['evaluator_version']}
            if value.get('candidate_version'):metadata['candidate_version']=value['candidate_version']
            self.sdk.create_score(name=name, value=score, trace_id=value['trace_id'],
                score_id=hashlib.sha256((value['evaluation_id']+name).encode()).hexdigest(),
                data_type='NUMERIC', metadata=metadata)


def _sdk(config):
    # Imports/client construction happen only in the explicitly enabled exporter.
    import httpx
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    os.environ['OTEL_BSP_MAX_QUEUE_SIZE']='128'
    os.environ['OTEL_BSP_MAX_EXPORT_BATCH_SIZE']='32'
    os.environ['OTEL_BSP_EXPORT_TIMEOUT']='2000'
    os.environ['OTEL_EXPORTER_OTLP_TIMEOUT']='2'
    return Langfuse(public_key=config.public_key,secret_key=config.secret_key,
        base_url=config.base_url, timeout=2, flush_at=32,flush_interval=1,
        debug=False,tracer_provider=TracerProvider(),
        httpx_client=httpx.Client(timeout=2,transport=httpx.HTTPTransport(retries=0)),
        mask=mask_data)


def _worker(config, events, ack, factory):
    logging.disable(logging.CRITICAL)  # Never log SDK exception payloads/headers.
    errors=0
    try:
        sdk=factory(config); mapper=Mapper(sdk)
        while True:
            command=events.get()
            try:
                if command['kind']=='event': mapper.event(command['value'])
                elif command['kind']=='score': mapper.score(command['value'])
                else:
                    sdk.flush()
                    if command['kind']=='shutdown': sdk.shutdown()
                    ack.send({'id':command['id'],'errors':errors})
                    if command['kind']=='shutdown': return
            except Exception:
                errors+=1  # No raw provider messages cross the boundary.
                if command['kind'] in {'flush','shutdown'}:
                    ack.send({'id':command['id'],'errors':errors})
                    if command['kind']=='shutdown': return
    except Exception:
        pass  # Parent detects process exit; research remains unaffected.
    finally:
        ack.close()


class LangfuseAdapter:
    def __init__(self,config=None,*,sdk_factory=_sdk):
        self.config=config
        self.stats={'enabled':config is not None,'accepted':0,'dropped':0,'errors':0,
                    'timeouts':0,'remote_delivery_verified':False}
        self.closed=False; self.process=None
        if config is not None:
            ctx=mp.get_context('spawn')
            self.queue=ctx.Queue(maxsize=config.queue_size)
            self.ack, child=ctx.Pipe(duplex=False)
            self.process=ctx.Process(target=_worker,args=(config,self.queue,child,sdk_factory),daemon=True)
            try:
                self.process.start(); child.close()
            except Exception:
                child.close();self.stats['errors']+=1;self.closed=True
                self.queue.cancel_join_thread();self.queue.close();self.ack.close()

    def _put(self, item):
        if not self.config or self.closed: return False
        if not self.process.is_alive(): self.stats['errors']+=1; return False
        try:
            self.queue.put_nowait(item);self.stats['accepted']+=1;return True
        except (Full,OSError,ValueError):
            self.stats['dropped']+=1;return False

    def __call__(self,event):
        if not self.config or self.closed: return
        try: self._put({'kind':'event','value':sanitize(event)})
        except Exception: self.stats['errors']+=1

    def evaluation(self,result):
        # Independent evaluator artifacts remain local; export numerical scores only.
        if (not re.fullmatch(r'[0-9a-f]{32}',result['evaluation_id'])
                or not re.fullmatch(r'[a-z0-9_-]{1,60}',result['evaluator_version'])):
            raise ValueError('Invalid evaluation telemetry identifiers')
        metrics={k:float(v) for k,v in result['metrics'].items()
                 if re.fullmatch(r'[a-z_]{1,60}',k) and type(v) in {int,float,bool}
                 and math.isfinite(v)}
        self._put({'kind':'score','value':{'trace_id':trace_id(result['run_id']),
            'evaluation_id':result['evaluation_id'],'evaluator_version':result['evaluator_version'],
            'candidate_version':result.get('subject_version') if re.fullmatch(r'[a-f0-9]{32,64}',str(result.get('subject_version',''))) else None,
            'metrics':metrics}})

    def _barrier(self,kind):
        if not self.config or self.closed: return False
        start=time.monotonic(); command=uuid4().hex
        sent=False
        while time.monotonic()-start < self.config.timeout:
            if not sent:
                try: self.queue.put_nowait({'kind':kind,'id':command});sent=True
                except Full: pass
            if self.ack.poll(0.01):
                try: answer=self.ack.recv()
                except EOFError: break
                if answer['id']==command:
                    self.stats['errors']+=answer['errors'];return True
            if not self.process.is_alive(): break
        self.stats['timeouts']+=1
        self._stop();return False

    def flush(self): return self._barrier('flush')

    def _stop(self):
        self.closed=True
        if self.process is not None and self.process.pid is not None:
            if self.process.is_alive(): self.process.terminate()
            self.process.join(timeout=0.2)
            if self.process.is_alive(): self.process.kill();self.process.join(timeout=0.2)
            self.queue.cancel_join_thread();self.queue.close();self.ack.close()

    def shutdown(self):
        ok=self._barrier('shutdown') if not self.closed else False
        self._stop();return ok

    def __enter__(self): return self
    def __exit__(self,*_): self.shutdown()
