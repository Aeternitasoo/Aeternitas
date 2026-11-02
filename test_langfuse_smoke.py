"""Offline readback-contract checks; no cloud service or real model."""
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import pytest

from scripts.check_agent_infrastructure import remote_smoke, verify_readback
from test_lab_evaluation import FakeSDK, export_config, events


class ReadbackSDK(FakeSDK):
    def __init__(self, path, *, corrupt=None, empty=False, fail_read=False):
        super().__init__(path)
        self.corrupt, self.empty, self.fail_read = corrupt, empty, fail_read
        self.requests = []
        self.api = SimpleNamespace(observations=SimpleNamespace(get_many=self.read))

    def read(self, **kwargs):
        self.requests.append(kwargs)
        if self.fail_read:
            raise TimeoutError('secret-provider-body-must-not-be-recorded')
        rows = {}
        for item in events(self.path):
            if item['kind'] == 'start':
                rows[item['id']] = SimpleNamespace(
                    id=item['id'], trace_id=item['trace_context']['trace_id'],
                    parent_observation_id=item['trace_context'].get('parent_span_id'),
                    name=item['name'], type=item['as_type'].upper(), model=item.get('model'),
                    version=item.get('version'), metadata=item['metadata'], level='DEFAULT',
                    usage_details={}, start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    end_time=datetime(2026, 1, 1, tzinfo=timezone.utc))
            elif item['kind'] == 'update':
                row = rows[item['id']]
                for key, value in item.items():
                    if key not in {'kind', 'id'}:
                        setattr(row, key, value)
        if self.corrupt:
            self.corrupt(rows)
        return SimpleNamespace(data=[] if self.empty else list(rows.values()))


def execute(tmp_path, **settings):
    sdk = ReadbackSDK(tmp_path/'remote.jsonl', **settings)
    result = remote_smoke(config=export_config(), identity='fixture-run',
        local_path=str(tmp_path/'local.jsonl'), sdk_factory=lambda c: sdk,
        read_attempts=2, pause=0)
    return sdk, result


def test_complete_readback_uses_mapper_and_proves_links_without_real_usage(tmp_path):
    sdk, result = execute(tmp_path)
    assert result['remote_delivery_verified'] and result['verification']['passed']
    assert result['actual_model_tokens'] is None and result['currency_cost'] is None
    assert result['usage_kind'] == 'synthetic_fixture_not_model_usage'
    assert len(events(tmp_path/'local.jsonl')) == 6
    assert sdk.requests[0]['request_options']['max_retries'] == 0
    assert sdk.requests[0]['fields'] == 'core,basic,metadata,model,usage'
    assert events(sdk.path)[-1]['kind'] == 'shutdown'


@pytest.mark.parametrize('field,value',[
    ('parent_observation_id', 'wrong'), ('trace_id', 'wrong'),
    ('usage_details', {'input': 700}), ('model', 'wrong'),
    ('version', 'wrong'), ('end_time', None), ('type', 'SPAN'),
    ('metadata', {'private_text': 'must-not-copy'})])
def test_names_and_flush_are_insufficient(tmp_path, field, value):
    def corrupt(rows):
        row = next(r for r in rows.values() if r.name == 'llm_synthesis')
        setattr(row, field, value)
    sdk, result = execute(tmp_path, corrupt=corrupt)
    assert not result['remote_delivery_verified'] and result['flush_completed']
    assert result['read_requests'] == 2
    assert 'private_text' not in json.dumps(result)
    assert events(sdk.path)[-1]['kind'] == 'shutdown'


def test_error_state_must_survive_export(tmp_path):
    def corrupt(rows):
        next(r for r in rows.values() if r.name == 'harness_tool').level = 'DEFAULT'
    _, result = execute(tmp_path, corrupt=corrupt)
    assert not result['remote_delivery_verified']


def test_ingestion_lag_is_not_a_success_and_polling_is_finite(tmp_path):
    sdk, result = execute(tmp_path, empty=True)
    assert not result['remote_delivery_verified']
    assert len(sdk.requests) == 2


def test_api_timeout_leaves_full_local_trace_and_shuts_down(tmp_path):
    sdk = ReadbackSDK(tmp_path/'remote.jsonl', fail_read=True)
    with pytest.raises(TimeoutError):
        remote_smoke(config=export_config(), identity='fixture-run',
            local_path=str(tmp_path/'local.jsonl'), sdk_factory=lambda c: sdk)
    assert len(events(tmp_path/'local.jsonl')) == 6
    assert 'secret-provider-body' not in (tmp_path/'local.jsonl').read_text()
    assert events(sdk.path)[-1]['kind'] == 'shutdown'


def test_installed_sdk_supports_required_readback_fields():
    import inspect
    from importlib.metadata import version
    from langfuse.api.observations.client import ObservationsClient
    assert version('langfuse') == '5.0.4'
    assert {'trace_id', 'fields', 'request_options'} <= set(inspect.signature(ObservationsClient.get_many).parameters)


def test_missing_credentials_does_not_construct_sdk(tmp_path, monkeypatch, capsys):
    import sys
    import scripts.check_agent_infrastructure as smoke
    monkeypatch.setattr(smoke, 'load_config', lambda p: {})
    for key in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL', 'LANGFUSE_HOST'):
        monkeypatch.delenv(key, raising=False)
    def forbidden(*args, **kw):
        pytest.fail('No export may run without credentials')
    monkeypatch.setattr(smoke, 'bounded_call', forbidden)
    monkeypatch.setattr(sys, 'argv', ['check', '--output-root', str(tmp_path)])
    smoke.main()
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'Requires Credentials'
    assert result['model_requests'] == 0 and not result['remote_delivery_verified']
    assert result['actual_model_tokens'] is None
