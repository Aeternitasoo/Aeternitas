from pathlib import Path
import json
from research_observability import Recorder
from langfuse_adapter import sanitize,Mapper,Config,trace_id
from test_lab_evaluation import FakeSDK


def test_new_spans_versions_and_evaluation_links_are_exportable(tmp_path):
    sdk=FakeSDK(tmp_path/'remote.jsonl');mapper=Mapper(sdk)
    recorder=Recorder(tmp_path/'events.jsonl','run',lambda e:mapper.event(sanitize(e)),
        context={'candidate_version':'a'*32,'evaluation_id':'b'*32})
    with recorder.span('infrastructure'):
        with recorder.span('quality_source_financial',input_fingerprint='c'*64):pass
        with recorder.span('memory_read',memory_count=1,memory_ids_sha256='d'*64):pass
        with recorder.span('llm_synthesis',model='saved-real-model',input_tokens=10,output_tokens=2,total_tokens=12):pass
    rows=[json.loads(l) for l in sdk.path.read_text().splitlines()]
    starts=[r for r in rows if r['kind']=='start']
    assert starts[-1]['as_type']=='generation'
    assert all(r['metadata']['candidate_version']=='a'*32 for r in starts)
    assert all(r['trace_context'].get('parent_span_id')==starts[0]['id'] for r in starts[1:])


def test_local_recorder_redacts_secrets_and_retains_fallback(tmp_path):
    def broken(e):raise RuntimeError('telemetry unavailable')
    recorder=Recorder(tmp_path/'events.jsonl','safe',broken)
    with recorder.span('rsi_analysis',model='Bearer very-private',candidate_version='a'*32):pass
    assert 'very-private' not in recorder.path.read_text() and recorder.export_errors==2
    assert Config.from_mapping({}) is None
