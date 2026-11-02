"""Thin M5 API/CLI contracts with no database, credentials or provider traffic."""

from copy import deepcopy
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

import aeternitas
import api_client
from api_client import APIClient, request_key
from local_api import create_app
from research_service import ServiceError
from sources.sec import SourceError


TOKEN='synthetic-m5-token-0123456789abcdef'
RUN_ID='1'*32
PAYLOAD=dict(ticker='AAPL',as_of_date='2025-10-01',question='Controlled question',
             refresh=False,news_days=14,deadline=300)


def row(status='queued'):
    return {'run_id':RUN_ID,'status':status,'step':'created','model_attempts':0,
            'links':{'status':f'/research-runs/{RUN_ID}'}}


class FakeService:
    def __init__(self):
        self.keys={}
        self.calls=[]

    def create(self,*,idempotency_key=None,**request):
        self.calls.append(('create',idempotency_key,deepcopy(request)))
        if idempotency_key in self.keys:
            if self.keys[idempotency_key]!=request:
                raise ServiceError(409,'idempotency_conflict','Same key requires the same request')
            return {**row(),'reused':True}
        self.keys[idempotency_key]=deepcopy(request)
        return {**row(),'reused':False}

    def cancel(self,run_id):
        self.calls.append(('cancel',run_id))
        return row('cancelled')

    def resume(self,run_id,acknowledge_unknown=False):
        self.calls.append(('resume',run_id,acknowledge_unknown))
        if not acknowledge_unknown:
            raise ServiceError(409,'unknown_call_acknowledgement_required','Unconfirmed external call; acknowledgement required')
        return row()


@pytest.fixture
def thin_api():
    service=FakeService()
    with TestClient(create_app(service,token=TOKEN),base_url='http://127.0.0.1:8767',
                    client=('127.0.0.1',1234),headers={'X-Aeternitas-Token':TOKEN}) as client:
        yield service,client


def test_api_passes_stable_header_returns_receipt_and_preserves_conflict(thin_api):
    service,client=thin_api
    key='m5-request:stable-1'
    first=client.post('/research-runs',json=PAYLOAD,headers={'Idempotency-Key':key})
    second=client.post('/research-runs',json=PAYLOAD,headers={'Idempotency-Key':key})
    assert first.status_code==second.status_code==202
    assert first.headers['idempotency-key']==second.headers['idempotency-key']==key
    assert first.json()['idempotency_key']==second.json()['idempotency_key']==key
    assert first.json()['run_id']==second.json()['run_id']==RUN_ID
    assert first.json()['reused'] is False and second.json()['reused'] is True
    assert first.headers['location']==f'/research-runs/{RUN_ID}'
    changed=client.post('/research-runs',json={**PAYLOAD,'question':'Changed'},headers={'Idempotency-Key':key})
    assert changed.status_code==409 and changed.json()['error']['code']=='idempotency_conflict'
    assert len(service.keys)==1


def test_api_without_key_generates_a_nonsecret_reusable_receipt(thin_api):
    service,client=thin_api
    result=client.post('/research-runs',json=PAYLOAD)
    key=result.json()['idempotency_key']
    assert re.fullmatch(r'[0-9a-f]{32}',key)
    assert key==result.headers['idempotency-key']==service.calls[0][1]
    assert TOKEN not in result.text


@pytest.mark.parametrize('key',['','bad key','x'*129,'/path','中文','!invalid'])
def test_api_rejects_bad_header_without_forwarding_or_echoing_it(thin_api,key):
    service,client=thin_api
    # HTTP headers are latin1; Unicode is tested at the CLI helper boundary.
    if not key.isascii():
        with pytest.raises(ValueError): request_key(key)
        return
    result=client.post('/research-runs',json=PAYLOAD,headers={'Idempotency-Key':key})
    assert result.status_code==422 and service.calls==[]
    assert result.json()['error']['code']=='invalid_request'


def test_cancel_and_resume_are_typed_json_commands_without_budget_expansion(thin_api):
    service,client=thin_api
    cancelled=client.post(f'/research-runs/{RUN_ID}/cancel',json={})
    assert cancelled.status_code==200 and cancelled.json()['status']=='cancelled'
    assert service.calls[-1]==('cancel',RUN_ID)
    refused=client.post(f'/research-runs/{RUN_ID}/resume',json={})
    assert refused.status_code==409 and service.calls[-1]==('resume',RUN_ID,False)
    resumed=client.post(f'/research-runs/{RUN_ID}/resume',json={'acknowledge_unknown':True})
    assert resumed.status_code==202 and resumed.headers['location']==f'/research-runs/{RUN_ID}'
    assert service.calls[-1]==('resume',RUN_ID,True)
    forwarded=len(service.calls)
    for payload in [{'acknowledge_unknown':'true'},{'acknowledge_unknown':1},{'deadline':1800},
                    {'acknowledge_unknown':False,'OPENAI_API_KEY':'synthetic-secret'}]:
        rejected=client.post(f'/research-runs/{RUN_ID}/resume',json=payload)
        assert rejected.status_code==422 and 'synthetic-secret' not in rejected.text
    assert client.post(f'/research-runs/{RUN_ID}/cancel',json={'reason':'not supported'}).status_code==422
    assert len(service.calls)==forwarded


@pytest.mark.parametrize('action',['cancel','resume'])
def test_new_commands_keep_token_origin_host_and_media_type_protection(thin_api,action):
    service,client=thin_api
    path=f'/research-runs/{RUN_ID}/{action}'
    assert client.post(path,json={},headers={'X-Aeternitas-Token':''}).status_code==401
    assert client.post(path,json={},headers={'Origin':'https://attacker.invalid'}).status_code==403
    assert client.post(path,json={},headers={'Host':'attacker.invalid'}).status_code==403
    assert client.post(path,json={},headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert client.post(path,content='{}',headers={'Content-Type':'text/plain'}).status_code==415
    assert client.post(path,content='x'*8193,headers={'Content-Type':'application/json'}).status_code==413
    assert service.calls==[]


def test_client_post_sends_empty_json_and_wait_recognizes_cancelled():
    received=[]
    def handler(request):
        received.append(request)
        if request.method=='POST':
            assert request.headers['content-type']=='application/json' and json.loads(request.content)=={}
        return httpx.Response(200,json=row('cancelled'))
    with APIClient(token=TOKEN,transport=httpx.MockTransport(handler)) as client:
        assert client.request('POST',f'/research-runs/{RUN_ID}/cancel')['status']=='cancelled'
        assert client.wait(RUN_ID,seconds=1)['status']=='cancelled'
    assert len(received)==2


def test_client_uncertain_submit_makes_one_request_and_preserves_key():
    received=[]
    def handler(request):
        received.append(request)
        assert request.headers['Idempotency-Key']=='fixed-retry-key'
        assert TOKEN not in str(request.url)
        raise httpx.ReadTimeout('Controlled timeout',request=request)
    with APIClient(token=TOKEN,transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceError,match='reuse --idempotency-key fixed-retry-key'):
            client.create(PAYLOAD,idempotency_key='fixed-retry-key')
    assert len(received)==1


@pytest.fixture
def cli(monkeypatch):
    calls=[]
    class Client:
        url='http://127.0.0.1:8767'
        result=row()
        fail=False
        interrupt=False
        def __init__(self,*args,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def create(self,payload,*,idempotency_key=None):
            calls.append(('create',deepcopy(payload),idempotency_key))
            if self.fail: raise SourceError('Controlled submit timeout')
            return deepcopy(self.result)
        def request(self,method,path,**kwargs):
            calls.append((method,path,kwargs))
            return deepcopy(self.result)
        def wait(self,*args):
            calls.append(('wait',args))
            if self.interrupt: raise KeyboardInterrupt
            return deepcopy(self.result)
    monkeypatch.setattr(aeternitas,'load_config',lambda *args:{})
    monkeypatch.setattr(api_client,'APIClient',Client)
    return Client,calls


def test_cli_generates_key_before_uncertain_submission_and_accepts_explicit_reuse(cli,capsys):
    Client,calls=cli
    Client.fail=True
    assert aeternitas.main(['run','AAPL','--as-of','2025-10-01'])==1
    output=capsys.readouterr()
    key=calls[0][2]
    assert re.fullmatch(r'[0-9a-f]{32}',key) and key in output.out
    assert len(calls)==1 and 'Controlled submit timeout' in output.err
    Client.fail=False
    assert aeternitas.main(['run','AAPL','--as-of','2025-10-01','--idempotency-key',key])==0
    assert calls[0][1:]==calls[1][1:]


def test_cli_rejects_invalid_key_before_transport(cli,capsys):
    _,calls=cli
    assert aeternitas.main(['run','AAPL','--idempotency-key','unsafe key'])==1
    assert calls==[] and 'Idempotency key' in capsys.readouterr().err


@pytest.mark.parametrize('command',[['status',RUN_ID],['wait',RUN_ID],
    ['run','AAPL','--as-of','2025-10-01','--wait']])
def test_cli_cancelled_status_or_wait_is_failure_exit(cli,command):
    Client,_=cli
    Client.result=row('cancelled')
    assert aeternitas.main(command)==1


def test_cli_cancel_reports_accepted_command_and_resume_requires_explicit_ack_flag(cli,capsys):
    Client,calls=cli
    Client.result=row('cancelled')
    assert aeternitas.main(['cancel',RUN_ID])==0
    assert calls[-1]==('POST',f'/research-runs/{RUN_ID}/cancel',{})
    Client.result=row()
    assert aeternitas.main(['resume',RUN_ID])==0
    assert calls[-1]==('POST',f'/research-runs/{RUN_ID}/resume',{'json':{'acknowledge_unknown':False}})
    assert aeternitas.main(['resume',RUN_ID,'--acknowledge-unknown'])==0
    assert calls[-1]==('POST',f'/research-runs/{RUN_ID}/resume',{'json':{'acknowledge_unknown':True}})
    assert '可能再次消耗额度' in capsys.readouterr().out


def test_ctrl_c_stops_wait_without_sending_cancel(cli,capsys):
    Client,calls=cli
    Client.interrupt=True
    assert aeternitas.main(['wait',RUN_ID])==130
    assert calls==[('wait',(RUN_ID,360))]
    assert '后台任务没有因此取消' in capsys.readouterr().err
