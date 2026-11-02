"""Controlled offline RSI cycle over real Harness execution, not real AI research.

Only this synthetic model interprets the fixture marker. It neither claims that
an actual LLM improved nor changes the production Skills or permission policy.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from agent_harness import Limits,Tool,ToolRegistry,_object,run_harness
from agent_skills import SkillRegistry
from candidate_registry import CandidateRegistry
from evidence_repository import digest
from lab_evaluation import (analyze_failures,evaluate_run,freeze_dataset,load_split,read,pins,
                            evaluate_saved_research)
from langfuse_adapter import LangfuseAdapter
from research_runtime import utc_now
from research_workflow import save,sha

RESULT=_object({'verified':{'type':'boolean'},'answer':{'type':'integer','minimum':0,'maximum':20000}})
ARGUMENT=_object({'value':{'type':'integer','minimum':0,'maximum':100}})
BASE_SKILL='''---
name: fixture-research
description: "Synthetic verification method, not financial research"
---
Goal: return a checked answer for the supplied synthetic value.
Input: one integer; output: verified boolean and integer answer.
Use only registered tools. If verification fails, return a failure, never invent evidence.
'''
CHANGE='\nverify-before-final: required\nCall fixture.verify before final; use its answer without alteration.\n'
POLICY={'scope':'controlled_fixture','allow_unknown_cost':True,'max_extra_tokens':0,
        'max_latency_seconds':30,'minimum_fixed_cases':2}


def verification(value):
    return {'answer':value*value+1,'verified':True}


@dataclass
class FixtureModel:
    value:int
    name:str='controlled-rsi-fixture'
    execution_mode:str='controlled_test'

    def __call__(self,*,messages,schema,seconds):
        results=[json.loads(m['content'])['tool_result_untrusted'] for m in messages[2:] if m['role']=='user']
        if not results:
            action={'kind':'tool','call_id':'load','name':'skills.load','arguments':{'name':'fixture-research'}}
        elif len(results)==1 and ('verify-before-final: required' in results[0]['result']['content']
             or 'verify-before-final: required' in json.loads(messages[1]['content'])['task']):
            action={'kind':'tool','call_id':'verify','name':'fixture.verify','arguments':{'value':self.value}}
        else:
            final=results[-1]['result'] if results[-1]['name']=='fixture.verify' else {'verified':False,'answer':self.value}
            action={'kind':'final','result':final}
        return {'decision':{'decision':action},'metadata':{'requested_model':self.name,
            'completion_status':'completed','usage':None,'response_id':None}}


def fixture_splits():
    return {split:[{'id':split+'-'+str(n),'kind':'scripted_task','input':{'value':n},
                    'expected':{'verified':True,'answer':n*n+1},'material':'synthetic'} for n in values]
            for split,values in {'regression':[2,3],'development':[4,5],'holdout':[7,9]}.items()}


def execute_case(case,release,registry,output,dataset_pin,*,sink=None):
    meta,root=registry.version(release)
    if meta['kind'] not in {'skill','prompt'}: raise ValueError('No arbitrary candidate Python execution')
    task='Synthetic verification task; no finance judgement'
    if meta['kind']=='skill':
        skills=SkillRegistry(root/'files'/'skills')
    else:
        task+='\n'+(root/'files'/next(iter(meta['files']))).read_text()
        # Host-owned constant test Skill, not candidate executable code.
        fixed=output/'fixture_skills'/'fixture-research'/'SKILL.md'
        fixed.parent.mkdir(parents=True,exist_ok=True)
        if not fixed.exists():
            with fixed.open('x') as stream:stream.write(BASE_SKILL)
        elif fixed.read_text()!=BASE_SKILL:raise ValueError('Fixture Skill changed')
        skills=SkillRegistry(fixed.parent.parent)
    tools=ToolRegistry().register(Tool('fixture.verify','Check a synthetic integer',ARGUMENT,verification,
        frozenset({'fixture.read'}),repeat_safe=True,version='fixture-verify-v1'))
    folder=run_harness(task,FixtureModel(case['input']['value']),
        tools,skills,output/'runs',allowed_tools=frozenset({'skills.load','fixture.verify'}),
        permissions=frozenset({'skills.read','fixture.read'}),result_schema=RESULT,
        limits=Limits(max_rounds=3,max_tool_calls=2,total_seconds=30),sink=sink)
    criteria={'case_id':case['id'],'split':case['split'],'dataset_fingerprint':dataset_pin,
        'expected_status':'completed','required_tools':['fixture.verify'],'required_skills':['fixture-research'],
        'allowed_skills':['fixture-research'],'matches':{'/'+k:v for k,v in case['expected'].items()},
        'weights':{'completion':1,'required_tools':1,'structured_output':1,'expected_output':1}}
    result,path=evaluate_run(folder,criteria,output/'evaluations',expected_files=pins(folder),
                             subject_version=release,sink=sink)
    return result,path


def run_demo(output,*,sink=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    dataset=output/'dataset';pin=freeze_dataset(dataset,fixture_splits(),version='controlled-harness-v1',policy=POLICY)
    registry=CandidateRegistry(output/'registry',authority_root=output/'manual-authority',
                               scope='controlled_fixture',create=True)
    initial=registry.create_version({'skills/fixture-research/SKILL.md':BASE_SKILL},kind='skill',
                                     reason='Frozen synthetic baseline before development')
    registry.initialize(initial)
    baseline_pins=pins(registry.version(initial)[1])
    development,_=load_split(dataset,'development',pin)
    dev=[execute_case(c,initial,registry,output,pin,sink=sink)[0] for c in development]
    analysis=analyze_failures(dev);save(output/'failure-analysis.json',analysis)
    candidate=registry.create_version({'skills/fixture-research/SKILL.md':BASE_SKILL+CHANGE},kind='skill',
        parent=initial,reason=analysis['proposal']['reason'],failure_analysis=analysis,dataset_fingerprint=pin)
    # Only after candidate freeze: regression first, holdout last. Both versions
    # use identical cases and criteria; no holdout feedback changes the candidate.
    baseline=[];changed=[];evaluation_paths=[];order=[]
    for split in ('regression','holdout'):
        cases,_=load_split(dataset,split,pin);order.append(split)
        for case in cases:
            for release,rows in ((initial,baseline),(candidate,changed)):
                result,path=execute_case(case,release,registry,output,pin,sink=sink)
                rows.append(result);evaluation_paths.append(str(path))
    recommendation,comparison=registry.record_comparison(candidate,baseline,changed,policy=POLICY,dataset_root=dataset)
    save(output/'evaluation-order.json',{'candidate_created_before_holdout':True,'order':order,
                                       'evaluation_paths':evaluation_paths})
    if not recommendation['eligible']: raise ValueError('Controlled candidate failed independent gates')
    approval=registry.fixture_approval('promote',candidate,sha(comparison))
    promoted=registry.apply('promote',candidate,approval,comparison)
    selected=registry.active_files()
    rollback=registry.fixture_approval('rollback',initial,sha(comparison))
    rolled_back=registry.apply('rollback',initial,rollback,comparison)
    if pins(registry.version(initial)[1])!=baseline_pins: raise ValueError('Baseline changed')
    summary={'schema':'controlled-rsi-demo-v1','created_at':utc_now(),'dataset_path':str(dataset),
        'dataset_fingerprint':pin,'baseline_version':initial,'candidate_version':candidate,
        'development_failures':sum(not e['hard_checks_passed'] for e in dev),
        'baseline_passed':sum(e['hard_checks_passed'] for e in baseline),
        'candidate_passed':sum(e['hard_checks_passed'] for e in changed),'comparison_path':str(comparison),
        'recommendation':recommendation['recommendation'],'promoted_fixture_path':str(selected),
        'promotion_generation':promoted['generation'],'rollback_generation':rolled_back['generation'],
        'active_after_rollback':registry.active(),'evaluation_order':order,
        'human_approval':'simulated_fixture_only','production_files_modified':False,
        'real_model_requests':0,'langfuse_delivery':'not_validated_requires_credentials',
        'tokens':None,'research_quality_validated':False}
    save(output/'summary.json',summary)
    save(output/'COMMITTED.json',{'files':{str(p.relative_to(output)):sha(p) for p in output.rglob('*')
                                          if p.is_file() and p.name!='approval-key'}})
    return summary


def replay_real_failure(source,output):
    """Optional local-only regression over existing original failed GPT output."""
    source=Path(source);output=Path(output);output.mkdir(parents=True,exist_ok=False)
    names={'response':'raw-response.json','config':'config.json','evidence':'evidence.json'}
    case={'id':'saved-real-citation-failure','kind':'saved_research','expected_outcome':'rejected',
          'material':'derived_public_snapshot','artifacts':{k:{'path':str(source/n),'sha256':sha(source/n)}
                                                        for k,n in names.items()}}
    save(output/'case.json',case)
    result=evaluate_saved_research(case,dataset_fingerprint=digest(case))
    save(output/'result.json',result)
    return result
