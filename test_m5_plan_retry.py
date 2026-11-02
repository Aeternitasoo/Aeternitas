"""Permanent subscription exhaustion differs from transient HTTP429/5xx.

Real PostgreSQL, synthetic transport and fake authorization only. These cases
exercise the formal durable engine, including every reserved attempt.
"""
import json

import httpx
import pytest

from test_m5_recovery import recovery, claim, PAYLOAD
from research_service import ServiceError


class Plan:
    def choose_model(self, model): return model
    def token(self): return 'synthetic-plan-bearer-never-persisted'


@pytest.mark.parametrize('code', ['subscription_sharing_usage_limit_exceeded',
                                  'insufficient_quota','billing_hard_limit_reached'])
def test_plan_quota_is_permanent_and_consumes_one_durable_attempt(recovery, monkeypatch, code):
    service,_=recovery
    monkeypatch.setattr('research_service.ChatGPTAuth',Plan)
    service.config['AETERNITAS_AUTH']='chatgpt'
    requests=[]
    def quota(request):
        requests.append(request)
        return httpx.Response(429,json={'error':{'code':code}})
    service.transports['model']=httpx.MockTransport(quota)
    rid=service.create(**PAYLOAD)['run_id']
    service.execute(claim(service))
    row=service.get(rid)
    assert row['status']=='failed' and row['model_attempts']==len(requests)==1
    assert code in row['error'] and row['model_usage'][0]['status']=='plan_error'
    assert not (service.root/'runs'/rid/'published.json').exists()


@pytest.mark.parametrize('status',[429,503])
def test_plan_transient_error_uses_bounded_retry_and_keeps_both_attempts(recovery,monkeypatch,status):
    service,_=recovery
    monkeypatch.setattr('research_service.ChatGPTAuth',Plan)
    service.config['AETERNITAS_AUTH']='chatgpt'
    original=service.transports['model']
    requests=[]
    def transient(request):
        requests.append(request)
        if len(requests)==1:
            return httpx.Response(status,json={'error':{'code':'rate_limit_exceeded'}},headers={'Retry-After':'0'})
        data=original.handle_request(request).json()
        wire='data: '+json.dumps({'type':'response.completed','response':data})+'\n\n'
        return httpx.Response(200,text=wire,headers={'Content-Type':'text/event-stream'})
    service.transports['model']=httpx.MockTransport(transient)
    rid=service.create(**PAYLOAD)['run_id']
    service.execute(claim(service))
    row=service.get(rid)
    assert row['status']=='completed_with_gaps' and row['model_attempts']==len(requests)==5
    assert [a['total_ordinal'] for a in row['attempts']]==[1,2,3,4,5]
    assert row['model_usage'][0]['status']=='provider_error'
    assert service.report(rid)[1]['model_attempts']==5


def test_interrupted_plan_stream_requires_acknowledgement_before_another_call(recovery,monkeypatch):
    service,_=recovery
    monkeypatch.setattr('research_service.ChatGPTAuth',Plan)
    service.config['AETERNITAS_AUTH']='chatgpt'
    original=service.transports['model']
    requests=[]
    def interrupted(request):
        requests.append(request)
        if len(requests)==1:
            wire='data: '+json.dumps({'type':'response.output_text.delta','output_index':0,
                                     'content_index':0,'delta':'unfinished'})+'\n\n'
        else:
            data=original.handle_request(request).json()
            wire='data: '+json.dumps({'type':'response.completed','response':data})+'\n\n'
        return httpx.Response(200,text=wire,headers={'Content-Type':'text/event-stream'})
    service.transports['model']=httpx.MockTransport(interrupted)
    rid=service.create(**PAYLOAD)['run_id']
    service.execute(claim(service))
    row=service.get(rid)
    assert row['status']=='failed' and row['model_attempts']==len(requests)==1
    assert row['attempts'][0]['status']=='unknown'
    assert row['model_usage'][0]['status']=='unconfirmed_error'
    with pytest.raises(ServiceError,match='acknowledgement required'):
        service.resume(rid)
    assert len(requests)==1
    service.resume(rid,acknowledge_unknown=True)
    service.execute(claim(service))
    assert service.report(rid)[1]['model_attempts']==len(requests)==5
