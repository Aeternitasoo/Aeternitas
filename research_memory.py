"""Mem0 sidecar with source-bound records, no implicit inference or paid embeddings.

Base Workflow does not import Mem0. The configured sidecar interpreter runs the
locked real SDK. Qdrant/history remain its storage, not a second memory database.
"""
from datetime import datetime,timezone
from functools import partial
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
import fcntl
from difflib import SequenceMatcher
from uuid import uuid4

TYPES={'project','research','procedural','task'}
AUTHORITY={'official_disclosure':3,'project_rule':3,'verified_execution':2,'task_state':2,'user_declared':1,'model_output':0}
SECRET=re.compile(r'(?i)(Bearer\s+\S+|sk-[a-z0-9_-]{20,}|eyJ[a-zA-Z0-9_-]{20,}\.)')


def stamp(value):
    d=datetime.fromisoformat(value)
    if d.utcoffset() is None:raise ValueError('Memory timestamp requires timezone')
    return d.astimezone(timezone.utc)


def source_check(ref):
    p=Path(ref['path'])
    if not p.is_absolute() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=ref['sha256']:
        raise ValueError('Memory source missing or changed')


class HashEmbedding:
    """Local lexical signed hashing, not a trained semantic embedding model."""
    def embed(self,text,memory_action=None):
        v=[0.0]*256
        tokens=re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]',text.lower())
        tokens+=[''.join(tokens[i:i+2]) for i in range(len(tokens)-1)]
        for token in tokens:
            h=hashlib.sha256(token.encode()).digest();v[int.from_bytes(h[:2],'big')%256]+=1 if h[2]%2 else -1
        norm=math.sqrt(sum(x*x for x in v)) or 1
        return [x/norm for x in v]


class NoInference:
    def generate_response(self,*a,**k):raise RuntimeError('Memory inference disabled')


def engine(root):
    os.environ['MEM0_TELEMETRY']='false';os.environ['MEM0_DIR']=str(root/'sdk-state')
    from importlib.metadata import version
    if version('mem0ai')!='2.2.1':raise ValueError('Locked Mem0 SDK required')
    from mem0 import Memory
    # Pinned SDK requires provider objects even for infer=False. These constructors
    # perform no requests. A noncredential marker and loopback dead port prevent
    # reading project keys or accidentally selecting a paid provider. Replace
    # components before any operation; integration tests block all sockets.
    marker='local-memory-no-external-inference'
    m=Memory.from_config({'llm':{'provider':'openai','config':{'model':'unused','api_key':marker,'openai_base_url':'http://127.0.0.1:1'}},
      'embedder':{'provider':'openai','config':{'model':'unused','api_key':marker,'openai_base_url':'http://127.0.0.1:1','embedding_dims':256}},
      'vector_store':{'provider':'qdrant','config':{'collection_name':'aeternitas','path':str(root/'vectors'),'embedding_model_dims':256}},
      'history_db_path':str(root/'history.sqlite')})
    m.embedding_model=HashEmbedding();m.llm=NoInference()
    return m


def records(m,namespace):
    rows=m.get_all(filters={'user_id':namespace},limit=1000)['results']
    if len(rows)>=1000:raise ValueError('Memory scope capacity reached; pagination required')
    return [(r,json.loads(r['metadata']['aeternitas_record'])) for r in rows]



def normalized_text(text):
    # Preserve case, punctuation, all numbers and the original text.
    return ' '.join(unicodedata.normalize('NFKC',text).split())

def identity_keys(item):
    scope=[item[k] for k in ('memory_type','entity','scope','period','assertion_key','classification')]
    fact=hashlib.sha256(json.dumps([scope,normalized_text(item['text'])],ensure_ascii=False).encode()).hexdigest()
    observation=[fact,sorted((s['path'],s['sha256']) for s in item['sources']),
        stamp(item['observed_at']).isoformat(),stamp(item['expires_at']).isoformat() if item.get('expires_at') else None,
        item['origin'],item['verified'],float(item['confidence']),item.get('supersedes')]
    return fact,hashlib.sha256(json.dumps(observation,ensure_ascii=False).encode()).hexdigest()

def memory_utility(*,retrieved,injected,actual_use=None,error_reduction=None,quality_delta=None,
                   extra_tokens=None,extra_seconds=None):
    return {'retrieved':len(retrieved),'injected':len(injected),
      'used':actual_use,'helpful':True if error_reduction is True and quality_delta is not None and quality_delta>0 else None,
      'error_reduction':error_reduction,'quality_delta':quality_delta,
      'extra_tokens':extra_tokens,'extra_seconds':extra_seconds,
      'not_equivalent':['retrieved','injected','used','helpful'],
      'evidence_requirement':'Observed use and matched independent outcome required; recall alone is not utility.'}

def select_task_memories(adapter,task,scopes,*,as_of,covered_rules=(),max_chars=3000):
    # Host supplies whitelisted scopes. The model cannot choose entities or paths.
    logs=[];selected=[];seen=set()
    for scope in scopes:
        applicable=(scope['memory_type']=='research' and scope['entity']==task['cik'] and scope['period']==task['period']
                    or scope['memory_type']=='procedural' and scope.get('task_tag') in task['needs'])
        if not applicable:logs.append({'scope':scope,'decision':'unrelated_scope'});continue
        found=adapter.search(task.get('retrieval_query',task['question']),**{k:scope[k] for k in ('memory_type','entity','scope','period')},as_of=as_of)
        logs.append({'scope':scope,'decision':'retrieval_unavailable' if found.get('status')=='memory_unavailable'
                     else 'retrieval_completed','result_count':len(found.get('results',[])),
                     'backend':found.get('backend'),'memory_ids':[m['memory_id'] for m in found.get('results',[])]})
        for m in found.get('results',[]):
            reason='eligible'
            if m['origin']=='model_output' or not m['verified']:reason='unverified'
            elif stamp(m['created_at'])>stamp(as_of) or stamp(m['observed_at'])>stamp(as_of):reason='future'
            elif m.get('expires_at') and stamp(m['expires_at'])<=stamp(as_of):reason='expired'
            elif m['assertion_key'] in covered_rules:reason='already_enforced_by_runtime'
            elif normalized_text(m['text']) in seen:reason='duplicate_context'
            elif sum(len(x['text']) for x in selected)+len(m['text'])>max_chars:reason='context_budget'
            if reason=='eligible':
                for ref in m['sources']:source_check(ref)
                selected.append(m);seen.add(normalized_text(m['text']))
            logs.append({'memory_id':m['memory_id'],'scope':scope,'decision':reason})
    return {'context':selected,'decisions':logs,'retrieval_method':'local_lexical_hash_not_semantic',
            'utility':memory_utility(retrieved=[x for x in logs if 'memory_id' in x],injected=selected)}

def operation(m,request):
    namespace=request['namespace'];op=request['operation'];all_rows=records(m,namespace)
    if op=='add':
        item=dict(request['record']);trusted=request.get('trusted',False)
        required={'memory_type','entity','scope','period','assertion_key','text','classification','sources','observed_at','confidence','origin'}
        if not required<=item.keys() or item['memory_type'] not in TYPES:raise ValueError('Incomplete typed memory')
        if item['origin'] not in AUTHORITY or not isinstance(item['text'],str) or not 1<=len(item['text'])<=8000:
            raise ValueError('Invalid memory origin or content')
        if item['classification'] not in {'fact','inference','unknown'} or not 0<=item['confidence']<=1:
            raise ValueError('Invalid memory assertion')
        if SECRET.search(json.dumps(item,ensure_ascii=False)):raise ValueError('Secret-like memory content refused')
        if not item['sources']:raise ValueError('Source reference required')
        for ref in item['sources']:source_check(ref)
        now=datetime.now(timezone.utc)
        if stamp(item['observed_at'])>now:raise ValueError('Future memory observation')
        item['verified']=bool(trusted and item['origin']!='model_output')
        if item.get('expires_at'):stamp(item['expires_at'])
        fact_key,dedup_key=identity_keys(item)
        peers=[]
        for raw,prior in all_rows:
            pf,pk=identity_keys(prior)
            if pk==dedup_key:
                return {**prior,'engine_id':raw['id'],'dedup_status':'reused_observation','dedup_key':pk}
            same_scope=all(prior[k]==item[k] for k in ('memory_type','entity','scope','period','assertion_key','classification'))
            if same_scope:
                similarity=SequenceMatcher(None,normalized_text(prior['text']),normalized_text(item['text'])).ratio()
                peers.append({'memory_id':prior['memory_id'],'same_fact':pf==fact_key,'similarity':similarity})
        item.update(fact_key=fact_key,dedup_key=dedup_key,related_observations=peers,
                    similar_memory_ids=[v['memory_id'] for v in peers if v['similarity']>=.85])
        parent=item.get('supersedes')
        if parent:
            prior=next((x for _,x in all_rows if x['memory_id']==parent),None)
            keys=['memory_type','entity','scope','period','assertion_key','classification']
            if prior is None or any(prior[k]!=item[k] for k in keys):raise ValueError('Invalid memory supersession scope')
            if prior['verified'] and not item['verified']:raise ValueError('Unverified memory cannot supersede verified source')
            if AUTHORITY[item['origin']]<AUTHORITY[prior['origin']]:raise ValueError('Lower authority cannot supersede source')
            if stamp(item['observed_at'])<stamp(prior['observed_at']):raise ValueError('Older observation cannot supersede newer memory')
            item['version']=prior['version']+1
        else:item['version']=1
        item.update(memory_id=uuid4().hex,created_at=now.isoformat(),parent=parent,
                    validity='active',retrieval_method='mem0_local_lexical_hash_v1')
        result=m.add(item['text'],user_id=namespace,metadata={'aeternitas_record':json.dumps(item,ensure_ascii=False),
            'memory_type':item['memory_type'],'entity':item['entity'],'scope':item['scope'],'period':item['period']},infer=False)
        item['engine_id']=result['results'][0]['id']
        return item
    if op!='search':raise ValueError('Unsupported memory operation')
    now=stamp(request['as_of']);types=request['memory_type'];entity=request['entity'];scope=request['scope'];period=request['period']
    if types not in TYPES:raise ValueError('Memory type required')
    eligible=[]
    for raw,item in all_rows:
        if (item['memory_type'],item['entity'],item['scope'],item['period'])!=(types,entity,scope,period):continue
        if not item['verified'] or stamp(item['created_at'])>now or stamp(item['observed_at'])>now:continue
        if item.get('expires_at') and stamp(item['expires_at'])<=now:continue
        if request.get('classification') and item['classification']!=request['classification']:continue
        if item['assertion_key'] in request.get('authoritative_keys',[]):continue
        try:
            for ref in item['sources']:source_check(ref)
        except (OSError,ValueError):continue
        eligible.append((raw,item))
    superseded={item['supersedes'] for _,item in all_rows if item.get('supersedes') and item['verified']
        and (item['memory_type'],item['entity'],item['scope'],item['period'])==(types,entity,scope,period)
        and stamp(item['created_at'])<=now and stamp(item['observed_at'])<=now}
    eligible=[(raw,item) for raw,item in eligible if item['memory_id'] not in superseded]
    conflicts={}
    for _,item in eligible:
        key=(item['assertion_key'],item['classification']);conflicts.setdefault(key,set()).add(normalized_text(item['text']))
    conflicts={k for k,values in conflicts.items() if len(values)>1}
    allowed={raw['id']:item for raw,item in eligible if (item['assertion_key'],item['classification']) not in conflicts}
    result=m.search(request['query'],filters={'user_id':namespace,'memory_type':types,'entity':entity,'scope':scope,'period':period},top_k=100,threshold=0,rerank=False)
    matches=[{**allowed[row['id']],'engine_id':row['id'],'score':row.get('score')} for row in result['results'] if row['id'] in allowed]
    return {'results':matches[:request.get('limit',5)],'conflicts':[list(k) for k in sorted(conflicts)],
            'backend':'mem0ai-2.2.1','retrieval_method':'local_lexical_hash_not_semantic','authoritative':False}


class MemoryAdapter:
    def __init__(self,python,root,namespace='aeternitas',timeout=20):
        self.python=str(python);self.root=str(Path(root).resolve());self.namespace=namespace;self.timeout=timeout

    @classmethod
    def from_mapping(cls,values):
        python=values.get('AETERNITAS_MEMORY_PYTHON');root=values.get('AETERNITAS_MEMORY_ROOT')
        return cls(python,root) if python and root else None

    def _call(self,**request):
        p=subprocess.run([self.python,'-B',str(Path(__file__).resolve()),'--worker',self.root],
            input=json.dumps({'namespace':self.namespace,**request}),text=True,capture_output=True,timeout=self.timeout,
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','MEM0_TELEMETRY':'false'})
        result=json.loads(p.stdout)
        if p.returncode or not result['ok']:raise RuntimeError('Memory operation failed: '+result.get('error_type','unknown'))
        return result['value']

    def remember(self,record,*,trusted=False):return self._call(operation='add',record=record,trusted=trusted)

    def search(self,query,*,memory_type,entity,scope,period,as_of=None,classification=None,authoritative_keys=()):
        try:return self._call(operation='search',query=query,memory_type=memory_type,entity=entity,scope=scope,period=period,
                as_of=as_of or datetime.now(timezone.utc).isoformat(),classification=classification,authoritative_keys=list(authoritative_keys))
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):
            return {'results':[],'conflicts':[],'backend':'unavailable','status':'memory_unavailable','authoritative':False}


def memory_tool(adapter,*,memory_type,entity,scope,period):
    from agent_harness import Tool
    return Tool('memory.search','Read source-bound scoped memory; never changes permissions',
        {'type':'object','properties':{'query':{'type':'string','minLength':1,'maxLength':500}},'required':['query'],'additionalProperties':False},
        partial(adapter.search,memory_type=memory_type,entity=entity,scope=scope,period=period),
        permissions=frozenset({'memory.read'}),timeout_seconds=30,output_bytes=262144,version='mem0-adapter-v1',
        binding_fingerprint=hashlib.sha256(json.dumps([adapter.namespace,adapter.root,memory_type,entity,scope,period]).encode()).hexdigest())


if __name__=='__main__':
    import contextlib,io,logging
    logging.disable(logging.CRITICAL)
    try:
        root=Path(sys.argv[2]);root.mkdir(parents=True,exist_ok=True)
        with contextlib.redirect_stdout(io.StringIO()):
            with (root/'adapter.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                m=engine(root);value=operation(m,json.load(sys.stdin))
        print(json.dumps({'ok':True,'value':value},ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok':False,'error_type':type(exc).__name__}));sys.exit(1)
