"""Fixed synthetic source fixture and deterministic four-role provider for M7 only."""
import json
from pathlib import Path

import httpx

from analysis_contracts import ROLE_TASKS

CONFIG={'SEC_CONTACT':'synthetic@example.invalid','FINNHUB_API_KEY':'synthetic-not-a-credential',
        'AETERNITAS_AUTH':'api_key','OPENAI_API_KEY':'synthetic-not-a-credential',
        'AETERNITAS_MODEL':'synthetic-m7-no-external-model'}
FIXTURE=Path(__file__).resolve().parents[1]/'tests/fixtures/m7_sources_v1.json'


def transports():
    fixture=json.loads(FIXTURE.read_text())
    def source(request):
        if request.url.host=='data.sec.gov':
            value=fixture['financials'] if 'companyfacts' in request.url.path else fixture['submissions']
            return httpx.Response(200,json=value)
        if request.url.host=='www.sec.gov':
            form='10-Q' if 'q.htm' in request.url.path else '10-K'
            return httpx.Response(200,content=fixture['filing_html'][form].encode(),headers={'content-type':'text/html'})
        if request.url.host=='finnhub.io': return httpx.Response(200,json=fixture['news'])
        raise AssertionError('Synthetic source fixture received unexpected route')
    def model(request):
        payload=json.loads(request.content)
        role=next(role for role,task in ROLE_TASKS.items() if task in payload['input'][0]['content'])
        evidence=json.loads(payload['input'][1]['content'])['evidence']
        result={'claims':[],'gaps':['合成材料只验证安装、数据库、四角色与引用流程，不能作为真实研究。']}
        if evidence:
            result['claims']=[{'text':f'合成 {role} 流程验证，不代表公司事实或投资结论。','stance':'neutral',
                'citations':[{'evidence_id':evidence[0]['id'],'quote':evidence[0]['text'][:80]}]}]
        return httpx.Response(200,json={'id':'synthetic-m7-response','status':'completed',
            'usage':{'input_tokens':100,'output_tokens':100},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(result,ensure_ascii=False)}]}]})
    return {'sec':httpx.MockTransport(source),'web':httpx.MockTransport(source),'model':httpx.MockTransport(model)}
