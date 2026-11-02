from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json,os,sys
from pathlib import Path
import pytest,httpx
from research_memory import MemoryAdapter,normalized_text,memory_utility,select_task_memories
from test_research_memory import item,search
from research_analysis import ResponsesAnalyst
from research_runtime import Budget

@pytest.fixture
def adapter(tmp_path):
 python=os.environ.get('AETERNITAS_MEMORY_PYTHON')
 if not python:pytest.skip('Optional locked Mem0 sidecar interpreter not configured')
 return MemoryAdapter(python,tmp_path/'mem')

def test_idempotent_same_observation_keeps_original_value(adapter,tmp_path):
 a=item(tmp_path);a['text']='  Evidence   authorization. '
 one=adapter.remember(a,trusted=True)
 b=deepcopy(a);b['text']='Evidence authorization.'
 two=adapter.remember(b,trusted=True)
 assert one['memory_id']==two['memory_id'] and two['dedup_status']=='reused_observation'
 assert two['text']==a['text']

def test_distinct_observation_time_preserved_and_related(adapter,tmp_path):
 a=item(tmp_path);one=adapter.remember(a,trusted=True)
 a['observed_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
 two=adapter.remember(a,trusted=True)
 assert one['memory_id']!=two['memory_id']
 assert two['related_observations'][0]['same_fact']
 assert two['version']==1

def test_other_source_is_independent_observation(adapter,tmp_path):
 a=item(tmp_path);one=adapter.remember(a,trusted=True)
 p=tmp_path/'other.txt';p.write_text('independent real source reference in synthetic test')
 import hashlib
 a['sources']=[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}]
 two=adapter.remember(a,trusted=True)
 assert one['memory_id']!=two['memory_id'] and two['related_observations']

def test_similar_numeric_conflict_is_preserved_not_merged(adapter,tmp_path):
 a=item(tmp_path,text='Revenue for this scope is USD 100 million.')
 one=adapter.remember(a,trusted=True)
 a['text']='Revenue for this scope is USD 200 million.'
 two=adapter.remember(a,trusted=True)
 assert one['memory_id']!=two['memory_id'] and one['memory_id'] in two['similar_memory_ids']
 assert search(adapter)['results']==[] and search(adapter)['conflicts']

def test_dynamic_scope_and_redundancy_drop_without_false_utility():
 class Fake:
  def search(self,*a,**k):raise AssertionError('unrelated scope must not retrieve')
 task={'cik':'Y','period':'2026','needs':['financial'],'question':'cash flow'}
 scope={'memory_type':'research','entity':'X','scope':'financial','period':'2025'}
 assert not select_task_memories(Fake(),task,[scope],as_of='2026-11-01T10:00:00+00:00')['context']
 x=memory_utility(retrieved=[1],injected=[1],extra_tokens=300)
 assert x['used'] is None and x['helpful'] is None

def test_transport_failure_remains_unknown_not_zero(tmp_path):
 def fail(req):raise httpx.ConnectTimeout('sensitive body '+'sk-'+'do-not-log-this-exception-message')
 c=ResponsesAnalyst('mock-not-real','gpt-6.1-sol',tmp_path,Budget(seconds=20),transport=httpx.MockTransport(fail))
 with pytest.raises(Exception):c.complete_json([{'role':'user','content':'test'}],{'type':'object'})
 d=c.diagnostics.state
 assert d['request_attempted'] and d['request_sent'] is None and d['http_status'] is None
 assert d['response_completed'] is None and d['token_usage'] is None and not d['commit_status']
 assert 'sensitive' not in ''.join(p.read_text() for p in c.diagnostics.path.glob('*.json'))

def test_http_rejection_is_known_but_cost_unknown(tmp_path):
 c=ResponsesAnalyst('mock-not-real','gpt-6.1-sol',tmp_path,Budget(seconds=20),
  transport=httpx.MockTransport(lambda req:httpx.Response(403,json={'error':'private'})))
 with pytest.raises(Exception):c.complete_json([],{'type':'object'})
 d=c.diagnostics.state
 assert d['http_status']==403 and d['request_sent'] is True and d['token_usage'] is None
 assert d['root_cause']=='http_status_rejected'

def test_completed_but_bad_structured_response_records_completion(tmp_path):
 c=ResponsesAnalyst('mock-not-real','gpt-6.1-sol',tmp_path,Budget(seconds=20),
  transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'status':'completed','usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5},'output':[{'type':'message','content':[{'type':'output_text','text':'invalid JSON'}]}]})))
 with pytest.raises(Exception):c.complete_json([],{'type':'object'})
 assert c.diagnostics.state['response_completed'] is True
 assert not c.diagnostics.state['response_persisted']
 assert c.diagnostics.state['token_usage']['total_tokens']==5

def test_missing_completed_sse_is_not_committed(tmp_path):
 class Auth:
  def token(self):return 'mock-not-real'
 c=ResponsesAnalyst(None,'gpt-6.1-sol',tmp_path,Budget(seconds=20),plan_auth=Auth(),
  transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},content=b'data: [DONE]\n\n')))
 with pytest.raises(Exception):c.complete_json([],{'type':'object'})
 assert c.diagnostics.state['http_status']==200 and c.diagnostics.state['response_completed'] is False

def test_workflow_validation_failure_keeps_model_success(tmp_path):
 from test_research_workflow import config,sources,factory,CONFIG
 from research_workflow import run_workflow
 def invalid(r):r['sections'][0]['claims'][0]['citations']=[]
 with pytest.raises(Exception):
  run_workflow(config(),tmp_path/'runs',provider_config=CONFIG,transports=sources(),client_factory=factory([],invalid))
 folder=next((tmp_path/'runs').iterdir())
 d=json.loads(sorted((folder/'diagnostics').glob('event-*.json'))[-1].read_text())
 assert d['response_completed'] and d['response_persisted'] and not d['commit_status']
 assert d['root_cause']=='output_or_quality_validation_rejected'

def test_local_commit_failure_is_not_model_failure(tmp_path,monkeypatch):
 import research_workflow as w
 from test_research_workflow import config,sources,factory,CONFIG
 save0=w.save
 def save(path,value):
  if path.name=='COMMITTED.json':raise OSError('local disk failure')
  return save0(path,value)
 monkeypatch.setattr(w,'save',save)
 with pytest.raises(Exception):
  w.run_workflow(config(),tmp_path/'runs',provider_config=CONFIG,transports=sources(),client_factory=factory([]))
 folder=next((tmp_path/'runs').iterdir())
 d=json.loads(sorted((folder/'diagnostics').glob('event-*.json'))[-1].read_text())
 assert d['response_completed'] and d['response_persisted'] and not d['commit_status']
 assert d['root_cause']=='local_commit_failed'

@pytest.mark.parametrize('response,expected',[
 ({'results':[],'backend':'mem0ai-2.2.1'},'retrieval_completed'),
 ({'results':[],'backend':'unavailable','status':'memory_unavailable'},'retrieval_unavailable')])
def test_memory_empty_and_failed_retrieval_are_distinct(response,expected):
 class Adapter:
  def search(self,*args,**kwargs):return response
 task={'cik':'X','period':'2025','needs':['citation'],'question':'evidence role'}
 scope={'memory_type':'procedural','entity':'research','scope':'runtime','period':'all','task_tag':'citation'}
 result=select_task_memories(Adapter(),task,[scope],as_of='2026-11-01T10:00:00Z')
 assert result['context']==[] and result['decisions'][0]['decision']==expected

def test_core_plan_and_capabilities_never_contact_services(capsys):
 from core_cli import main
 assert main(['capabilities'])==0
 assert 'requires credentials' in capsys.readouterr().out
 assert main(['plan','--config',str(Path(__file__).parent/'config/research_cloud_compute_power.json')])==0
 assert json.loads(capsys.readouterr().out)['real_requests']==0

def test_candidate_builder_rejects_unapproved_runtime_paths_before_io(tmp_path):
 from infrastructure_rsi import build_quality_candidates
 with pytest.raises(ValueError,match='scope'):
  build_quality_candidates(tmp_path,None,None,runtime_names=['../../untrusted.py'],
                           previous=tmp_path,profile_parent='unused')

def test_core_cli_reuses_selected_oauth_model_without_changing_provider(tmp_path,monkeypatch):
 import core_cli,chatgpt_auth,aeternitas
 policy=tmp_path/'quality.json';policy.write_text('{}')
 seen={}
 def run(config,output,**kwargs):
  seen.update(kwargs['provider_config']);return output/'record'
 monkeypatch.setattr(core_cli,'run_workflow',run)
 monkeypatch.setattr(aeternitas,'load_config',lambda _: {'AETERNITAS_AUTH':'chatgpt','AETERNITAS_MODEL':'auto'})
 monkeypatch.setattr(chatgpt_auth.ChatGPTAuth,'status',lambda _: {'model':'gpt-6.1-sol'})
 assert core_cli.main(['research','--config',str(Path(__file__).parent/'config/research_cloud_compute_power.json'),
  '--quality-policy',str(policy),'--output',str(tmp_path/'out'),'--execute'])==0
 assert seen=={'AETERNITAS_AUTH':'chatgpt','AETERNITAS_MODEL':'gpt-6.1-sol'}
