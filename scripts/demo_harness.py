"""Offline Harness demonstration over a host-pinned existing company bundle.

Model decisions are deterministic fixtures, never a real research judgement.
No data or model network requests. Run from WSL Python 3.13.
"""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent_harness import Limits,run_harness,verify_run,_object
from agent_skills import SkillRegistry
from agent_tools import research_tools


@dataclass
class OfflineDemo:
    dataset: str
    name: str='offline-harness-demo'
    execution_mode: str='controlled_test'

    def __call__(self, *, messages,schema,seconds):
        history=[]
        for message in messages[2:]:
            if message['role']=='user':history.append(json.loads(message['content'])['tool_result_untrusted'])
        step=len(history)
        def tool(name,arguments):
            return {'kind':'tool','call_id':'call_'+str(step),'name':name,'arguments':arguments}
        if step in (0,1,4):
            choice={0:'industry-research',1:'financial-analysis',4:'evidence-verification'}[step]
            action=tool('skills.load',{'name':choice})
        elif step==2:
            action=tool('research.financial_summary',{'dataset':self.dataset})
        elif step==3:
            summary=next(h['result']['financials'] for h in history if h['name']=='research.financial_summary')
            fact=summary['annual_periods'][0]['facts']['revenue']
            action=tool('research.evidence',{'dataset':self.dataset,'evidence_ids':['V-'+fact['fact_id'][:16]]})
        elif step==5:
            evidence=next(h['result']['evidence'][0] for h in history if h['name']=='research.evidence')
            action=tool('research.verify_citations',{'dataset':self.dataset,'citations':[
                {'evidence_id':evidence['id'],'quote':evidence['text'][:100]}]})
        else:
            verification=history[-1]['result']
            action={'kind':'final','result':{'summary':'离线执行演示：技能按需加载，冻结财务与逐字引用工具已执行；不构成新的AI研究结论。',
                'verified_evidence_ids':verification['verified_evidence_ids']}}
        return {'decision':{'decision':action},'metadata':{'requested_model':self.name,
                'completion_status':'completed','usage':None,'response_id':None}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dataset',default='company')
    args=parser.parse_args()
    tools=research_tools({args.dataset:{'bundle_path':str(args.bundle),'source_root':str(args.source_root),'sha256':args.sha256}})
    result_schema=_object({'summary':{'type':'string','maxLength':1000},
        'verified_evidence_ids':{'type':'array','items':{'type':'string','maxLength':200},'maxItems':3}})
    folder=run_harness('演示冻结数据工具链及按需技能加载',OfflineDemo(args.dataset),tools,
        SkillRegistry(ROOT/'agent_skills'),args.output,
        allowed_tools=frozenset({'skills.load',*(t['name'] for t in tools.metadata())}),
        permissions=frozenset({'skills.read','research.read','evidence.verify'}),
        result_schema=result_schema,limits=Limits(max_rounds=8,tool_seconds=20))
    state=json.loads((folder/'state.json').read_text())
    print(json.dumps({'folder':str(folder),'status':state['status'],'loaded_skills':state['loaded_skills'],
                      'tool_executions':state['tool_executions'],'real_model_requests':0},ensure_ascii=False))
    if state['status']=='completed':verify_run(folder);return 0
    return 1


if __name__=='__main__':
    raise SystemExit(main())
