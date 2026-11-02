"""Real pinned Mem0 SDK in its isolated interpreter; no model or network calls."""
from datetime import datetime,timezone,timedelta
import hashlib
import os
from pathlib import Path
import pytest
from research_memory import MemoryAdapter,HashEmbedding,memory_tool
from agent_harness import ToolRegistry


@pytest.fixture
def backend(tmp_path):
    python=os.environ.get('AETERNITAS_MEMORY_PYTHON')
    if not python:pytest.skip('Optional locked Mem0 sidecar interpreter not configured')
    return MemoryAdapter(python,tmp_path/'store')


def item(tmp_path,kind='procedural',**changes):
    p=tmp_path/'source.txt'
    if not p.exists():p.write_text('Fixed offline source evidence, not a real company fact.')
    result={'memory_type':kind,'entity':'X','scope':'test','period':'2025','assertion_key':'rule',
        'text':'Source authorization is independent from evidence role.', 'classification':'fact',
        'sources':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}],
        'observed_at':datetime.now(timezone.utc).isoformat(),'confidence':1,'origin':'verified_execution'}
    return {**result,**changes}


def search(backend,**changes):
    return backend.search('source authorization evidence role',**{'memory_type':'procedural','entity':'X','scope':'test','period':'2025',**changes})


@pytest.mark.parametrize('kind',['project','research','procedural','task'])
def test_real_sdk_persists_all_four_types_across_processes(backend,tmp_path,kind):
    rec=backend.remember(item(tmp_path,kind),trusted=True)
    rows=search(backend,memory_type=kind)['results']
    assert rows[0]['memory_id']==rec['memory_id'] and rows[0]['version']==1
    assert rows[0]['sources']==rec['sources'] and rows[0]['verified']
    assert search(backend,memory_type=kind,entity='OTHER')['results']==[]
    assert search(backend,memory_type=kind,period='2024')['results']==[]


def test_unverified_model_output_never_becomes_trusted_memory(backend,tmp_path):
    backend.remember(item(tmp_path,origin='model_output'),trusted=True)
    assert search(backend)['results']==[]


def test_conflict_is_visible_not_silently_resolved(backend,tmp_path):
    backend.remember(item(tmp_path),trusted=True)
    backend.remember(item(tmp_path,text='An incompatible assertion.'),trusted=True)
    result=search(backend)
    assert result['results']==[] and result['conflicts']


def test_supersession_expiry_and_no_resurrection(backend,tmp_path):
    old=backend.remember(item(tmp_path),trusted=True)
    new=backend.remember(item(tmp_path,text='Reviewed correction.',supersedes=old['memory_id']),trusted=True)
    assert search(backend)['results'][0]['version']==2
    assert search(backend)['results'][0]['memory_id']==new['memory_id']
    backend.remember(item(tmp_path,text='Expired correction.',supersedes=new['memory_id'],expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()),trusted=True)
    assert search(backend)['results']==[]


def test_source_change_and_time_boundary_exclude_memory(backend,tmp_path):
    record=backend.remember(item(tmp_path),trusted=True)
    assert search(backend,as_of='2020-01-01T00:00:00+00:00')['results']==[]
    assert search(backend,authoritative_keys=['rule'])['results']==[]
    (tmp_path/'source.txt').write_text('changed')
    assert search(backend)['results']==[]


def test_lower_authority_and_unverified_cannot_replace_source(backend,tmp_path):
    old=backend.remember(item(tmp_path,origin='official_disclosure'),trusted=True)
    with pytest.raises(RuntimeError):backend.remember(item(tmp_path,supersedes=old['memory_id'],origin='user_declared'),trusted=True)
    with pytest.raises(RuntimeError):backend.remember(item(tmp_path,supersedes=old['memory_id']),trusted=False)


def test_hashing_repeatable_and_harness_has_read_only_scope(tmp_path):
    assert HashEmbedding().embed('source role')==HashEmbedding().embed('source role')
    adapter=MemoryAdapter('/not-installed',tmp_path)
    tool=memory_tool(adapter,memory_type='procedural',entity='X',scope='test',period='all')
    registry=ToolRegistry().register(tool)
    assert registry.get('memory.search').permissions==frozenset({'memory.read'})
    other=memory_tool(adapter,memory_type='procedural',entity='OTHER',scope='test',period='all')
    assert tool.metadata()['binding_fingerprint']!=other.metadata()['binding_fingerprint']
    assert search(adapter)['status']=='memory_unavailable'
