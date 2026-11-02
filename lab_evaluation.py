"""Independent, deterministic evaluation over frozen local execution artifacts.

Hard contract checks are not semantic investment accuracy. Criteria and dataset
hashes are supplied by the trusted host, never by a model or a loaded Skill.
"""
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from agent_harness import verify_run
from evidence_repository import digest
from evaluation_cases import _pointer, evaluate_case
from harness_contracts import validate
from research_runtime import utc_now
from research_workflow import save, sha, validate_output

VERSION='lab-evaluator-v1'
SPLITS=frozenset({'regression','development','holdout'})


def read(path):
    return json.loads(Path(path).read_text(),parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def pins(folder):
    folder=Path(folder)
    if folder.is_symlink(): raise ValueError('Artifact symlink')
    files={}
    for p in sorted(folder.iterdir()):
        if p.is_symlink(): raise ValueError('Artifact symlink')
        if p.is_file(): files[p.name]=sha(p)
    if not files: raise ValueError('No artifacts')
    return files


def verify_pins(folder,files):
    if pins(folder)!=files: raise ValueError('Frozen artifacts changed')


def freeze_dataset(folder, splits, *, version, policy):
    """Freeze all splits before any candidate selection; no executable fixtures."""
    if set(splits)!=SPLITS or not all(splits.values()): raise ValueError('Three nonempty splits required')
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    identities=set();contents=set();files={}
    for split in sorted(SPLITS):
        (folder/split).mkdir()
        for case in splits[split]:
            identity=case.get('id')
            if not isinstance(identity,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',identity) or identity in identities:
                raise ValueError('Duplicate / invalid case ID')
            content=digest({k:v for k,v in case.items() if k not in {'id','description','split'}})
            if content in contents: raise ValueError('Same case content across splits / IDs')
            if case.get('kind') not in {'scripted_task','m6_case','saved_research'}:
                raise ValueError('Unknown case kind')
            identities.add(identity);contents.add(content)
            path=folder/split/(identity+'.json');save(path,{**case,'split':split})
            files[str(path.relative_to(folder))]=sha(path)
    manifest={'schema':'lab-evaluation-dataset-v1','version':version,'created_at':utc_now(),
              'policy':policy,'files':files,'case_count':len(identities)}
    save(folder/'manifest.json',manifest)
    return sha(folder/'manifest.json')


def load_split(folder,split,expected_sha):
    folder=Path(folder)
    if split not in SPLITS or sha(folder/'manifest.json')!=expected_sha:
        raise ValueError('Dataset split / frozen manifest differs')
    m=read(folder/'manifest.json');cases=[]
    for name,pin in sorted(m['files'].items()):
        part=Path(name)
        if len(part.parts)!=2 or part.parts[0] not in SPLITS or part.suffix!='.json':
            raise ValueError('Invalid dataset path')
        if part.parts[0]!=split: continue  # Holdout payload not read for development.
        path=folder/part
        if path.is_symlink() or sha(path)!=pin: raise ValueError('Dataset case changed')
        case=read(path)
        if case['split']!=split: raise ValueError('Split mismatch')
        cases.append(case)
    if not cases: raise ValueError('Empty split')
    return cases,m


def _events(folder):
    return [json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]


def evaluate_run(folder,criteria,output_root,*,expected_files=None,subject_version=None,sink=None):
    """Read completed or failed Harness runs; never change source run files."""
    folder=Path(folder); frozen=expected_files or pins(folder)
    verify_pins(folder,frozen)
    state=read(folder/'state.json');request=read(folder/'request.json');events=_events(folder)
    metrics={};failures=[];reasons=[]
    def check(name,condition,reason):
        metrics[name]=int(bool(condition))
        if not condition: failures.append(name);reasons.append(reason)
    completed=state['status']=='completed';metrics['completion']=int(completed)
    expected_status=criteria.get('expected_status','completed')
    check('expected_status',state['status']==expected_status,'Unexpected final run status')
    if completed:
        try: verify_run(folder);intact=True
        except Exception: intact=False
    else: intact=(folder/'failure.json').is_file() and not (folder/'COMMITTED.json').exists()
    check('artifact_integrity',intact,'Run commitment / failure record invalid')
    if criteria.get('expected_error'):
        check('expected_error',state.get('error_code')==criteria['expected_error'],'Expected failure code absent')
    roots=[e for e in events if e['phase']=='start' and e['parent_span_id'] is None]
    active={};trace_ok=len(roots)==1
    for e in events:
        sid=e['span_id']
        if e['trace_id']!=state['run_id']: trace_ok=False
        if e['phase']=='start':
            if sid in active or (e['parent_span_id'] is not None and e['parent_span_id'] not in active): trace_ok=False
            active[sid]=e
        elif e['phase']=='end':
            start=active.pop(sid,None)
            if start is None or start['parent_span_id']!=e['parent_span_id'] or start['name']!=e['name']: trace_ok=False
        else: trace_ok=False
    check('trace_integrity',trace_ok and not active,'Trace parent / event pairing invalid')
    policies={t['name']:t for t in request['tools']};tool_names=[];execution_ok=True;admitted_ok=True
    for p in sorted(folder.glob('tool-*-request.json')):
        call=read(p);tool_names.append(call['name']);tool=policies.get(call['name'])
        try:
            if tool is None or call['name'] not in request['allowed_tools'] or not set(tool['permissions'])<=set(request['permissions']):
                raise ValueError('Policy')
            validate(call['arguments'],tool['parameters'])
            if digest(call['arguments'])!=call['arguments_sha256']: raise ValueError('Argument hash')
        except Exception: admitted_ok=False
    for p in folder.glob('tool-*-attempt.json'):
        if read(p).get('ok') is not True: execution_ok=False
    check('execution_policy',admitted_ok,'Executed tool not permitted or invalid arguments')
    metrics['tool_execution_success']=int(execution_ok)
    refused=[e for e in events if e['name']=='harness_tool_admission' and e['phase']=='end' and e.get('status')=='failed']
    metrics['tool_request_validity']=int(not refused)
    required_tools=set(criteria.get('required_tools',[]));required_skills=set(criteria.get('required_skills',[]))
    if required_tools:
        check('required_tools',required_tools<=set(tool_names),'Required trusted tool not called')
    if required_skills:
        check('required_skills',required_skills<=set(state['loaded_skills']),'Required Skill not loaded')
    if 'allowed_skills' in criteria:
        check('skill_selection',set(state['loaded_skills'])<=set(criteria['allowed_skills']),'Unexpected Skill selected')
    skill_ok=True
    tool_values=[read(p) for p in folder.glob('tool-*-result.json')]
    for binding in state['loaded_skills'].values():
        found=[v for v in tool_values if isinstance(v,dict) and v.get('name')==binding['name']]
        skill_ok &= bool(found) and all(v['sha256']==binding['sha256']==digest_text(v['content']) for v in found)
    check('skill_binding',skill_ok,'Loaded Skill content hash differs')
    if completed:
        final=read(folder/'final.json')
        try: validate(final,request['result_schema']);valid=True
        except Exception: valid=False
        check('structured_output',valid,'Final output schema invalid')
        projection_ok=True
        for pointer,value in criteria.get('matches',{}).items():
            try: actual=_pointer(final,pointer);valid=type(actual) is type(value) and actual==value
            except Exception: valid=False
            projection_ok &= valid
        if criteria.get('matches'):
            check('expected_output',projection_ok,'Expected output projection differs')
        domain=criteria.get('research_contract')
        if domain:
            try: validate_output(final,domain['config'],domain['evidence']);valid=True
            except (ValueError,KeyError,TypeError): valid=False
            check('citation_contract',valid,'Research citation / classification contract rejected')
    else:
        metrics['structured_output']=None
    model_responses=[read(p) for p in folder.glob('model-*-response.json')]
    tokens=[];model_ok=True
    for response in model_responses:
        meta=response.get('metadata',{});usage=meta.get('usage') or {}
        token=usage.get('total_tokens')
        tokens.append(token if type(token) is int and token>=0 else None)
        if meta.get('requested_model')!=state['model'] or meta.get('completion_status')!='completed': model_ok=False
    actual_tokens=sum(tokens) if (len(tokens)==state['rounds'] and all(t is not None for t in tokens)) else None
    check('usage_integrity',actual_tokens==state['actual_total_tokens'],'Stored usage differs from provider metadata')
    metrics['model_protocol']=int(model_ok)
    latency=next((e.get('elapsed_seconds') for e in reversed(events) if e['name']=='harness' and e['phase']=='end'),None)
    def usage_sum(key):
        values=[(r.get('metadata',{}).get('usage') or {}).get(key) for r in model_responses]
        return sum(values) if len(values)==state['rounds'] and all(type(v) is int and v>=0 for v in values) else None
    costs={'actual_total_tokens':actual_tokens,'actual_input_tokens':usage_sum('input_tokens'),
           'actual_output_tokens':usage_sum('output_tokens'),'currency_cost':None,'cost_status':'unknown',
           'latency_seconds':latency,'model_calls':state['rounds'],'tool_executions':state['tool_executions']}
    if 'max_latency_seconds' in criteria:
        check('latency_budget',latency is not None and latency<=criteria['max_latency_seconds'],'Latency threshold exceeded')
    if 'max_tokens' in criteria:
        check('token_budget',actual_tokens is not None and actual_tokens<=criteria['max_tokens'],'Token evidence unknown / over budget')
    weights=criteria.get('weights');overall=None
    if weights is not None:
        if not weights or any(k not in metrics or metrics[k] is None or type(w) not in {int,float}
                              or not math.isfinite(w) or w<=0 for k,w in weights.items()):
            raise ValueError('Explicit positive weights require known metrics')
        overall=sum(metrics[k]*w for k,w in weights.items())/sum(weights.values())
    result={'evaluation_id':uuid4().hex,'evaluator_version':VERSION,'evaluator_code_sha256':sha(__file__),
        'run_id':state['run_id'],'run_path':str(folder.resolve()),'run_files':frozen,'run_fingerprint':digest(frozen),
        'created_at':utc_now(),'criteria':deepcopy(criteria),'criteria_fingerprint':digest(criteria),
        'subject_version':subject_version or digest({k:state.get(k) for k in request if k.endswith('version')}),
        'execution_versions':{k:state.get(k) for k in request if k.endswith('version') or k.endswith('versions') or k.endswith('sha256')},
        'loaded_skill_versions':deepcopy(state['loaded_skills']),
        'permission_budget_fingerprint':digest({k:request[k] for k in ('allowed_tools','permissions','limits')}),
        'model_identity':{'requested_model':state['model'],
            'returned_models':sorted({r['metadata']['returned_model'] for r in model_responses
                                      if r.get('metadata',{}).get('returned_model')}),
            'execution_mode':state['model_execution']},
        'metrics':metrics,'costs':costs,'overall_score':overall,'hard_checks_passed':not failures,
        'failure_categories':sorted(set(failures+([state['error_code']] if state.get('error_code') else []))),
        'failure_reasons':reasons,'semantic_review':'requires_human_review',
        'suggestions':sorted({'Inspect '+name+' against frozen criteria' for name in failures})}
    destination=Path(output_root)/result['evaluation_id'];destination.mkdir(parents=True,exist_ok=False)
    save(destination/'evaluation.json',result)
    verify_pins(folder,frozen)
    if sink is not None:
        try: sink.evaluation(result)
        except Exception: pass
    return result,destination/'evaluation.json'


def digest_text(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def evaluate_saved_research(case,*,dataset_fingerprint):
    """Replay a pinned saved response through the existing research validator.

    A known invalid real response stays a rejected outcome, not a golden answer.
    """
    material={}
    for name,item in case['artifacts'].items():
        path=Path(item['path'])
        if path.is_symlink() or sha(path)!=item['sha256']: raise ValueError('Saved research artifact changed')
        material[name]=read(path)
    rejection=None
    try:
        validate_output(material['response'],material['config'],material['evidence'])
        outcome='accepted'
    except (ValueError,KeyError,TypeError) as exc:
        outcome='rejected';rejection={'category':'research_contract_rejected','error_type':type(exc).__name__}
    return {'case_id':case['id'],'dataset_fingerprint':dataset_fingerprint,'actual_outcome':outcome,
            'expected_outcome':case['expected_outcome'],'hard_checks_passed':outcome==case['expected_outcome'],
            'semantic_review':'requires_human_review','evaluator_version':VERSION,
            'artifact_fingerprint':digest(case['artifacts']),'rejection':rejection}


def analyze_failures(evaluations):
    if not evaluations or any(e['criteria']['split']!='development' for e in evaluations):
        raise ValueError('Failure analysis accepts development only; holdout is not tuning material')
    counts=Counter(f for e in evaluations for f in e['failure_categories'])
    return {'version':'failure-analysis-v1','created_at':utc_now(),'split':'development',
            'evaluation_ids':[e['evaluation_id'] for e in evaluations],
            'evaluation_fingerprints':[digest(e) for e in evaluations],
            'dataset_fingerprints':sorted({e['criteria']['dataset_fingerprint'] for e in evaluations}),
            'failure_counts':dict(sorted(counts.items())),'automatic_semantic_judgement':False,
            'proposal':{'reason':'Require explicit verification before producing final output',
                        'template':'verification-step-v1'} if counts.get('required_tools') else None}


def compare_versions(baseline,candidate,*,policy):
    """Same frozen cases/criteria only; unknown costs stay unknown, not zero."""
    def indexed(rows):
        values={}
        for e in rows:
            c=e['criteria'];key=(c['split'],c['case_id'])
            if key in values: raise ValueError('Duplicate comparison case')
            values[key]=e
        return values
    old,new=indexed(baseline),indexed(candidate)
    if not old or set(old)!=set(new): raise ValueError('Comparison sample sets differ')
    if set(k[0] for k in old)!={'regression','holdout'}: raise ValueError('Regression and holdout required')
    if len({e['subject_version'] for e in baseline})!=1 or len({e['subject_version'] for e in candidate})!=1:
        raise ValueError('Mixed subject versions')
    if len({e['criteria']['dataset_fingerprint'] for e in baseline+candidate})!=1:
        raise ValueError('Mixed dataset versions')
    changes=[];eligible=True
    if policy.get('scope')!='controlled_fixture' and policy.get('allow_unknown_cost'):
        raise ValueError('Unknown cost exception only allowed in controlled fixtures')
    for key,b in old.items():
        n=new[key]
        if b['criteria_fingerprint']!=n['criteria_fingerprint']: raise ValueError('Evaluation criteria differ')
        if (b.get('evaluator_version')!=n.get('evaluator_version')
                or b.get('evaluator_code_sha256')!=n.get('evaluator_code_sha256')):
            raise ValueError('Independent evaluator changed between versions')
        if b.get('permission_budget_fingerprint')!=n.get('permission_budget_fingerprint'):
            raise ValueError('Permission / budget policy changed')
        bf,nf=set(b['failure_categories']),set(n['failure_categories'])
        deltas={k:(n['metrics'][k]-b['metrics'][k]) for k in b['metrics'].keys() & n['metrics'].keys()
                if type(b['metrics'][k]) in {int,float} and type(n['metrics'][k]) in {int,float}}
        bc,nc=b['costs'],n['costs'];known=bc['actual_total_tokens'] is not None and nc['actual_total_tokens'] is not None
        cost_ok=(known and nc['actual_total_tokens']<=bc['actual_total_tokens']+policy['max_extra_tokens']) or (not known and policy.get('allow_unknown_cost') is True)
        latency_ok=(nc['latency_seconds'] is not None and nc['latency_seconds']<=policy['max_latency_seconds'])
        eligible &= n['hard_checks_passed'] and not (nf-bf) and cost_ok and latency_ok
        changes.append({'split':key[0],'case_id':key[1],'new_failures':sorted(nf-bf),'fixed_failures':sorted(bf-nf),
            'metric_deltas':deltas,'baseline_passed':b['hard_checks_passed'],'candidate_passed':n['hard_checks_passed'],
            'token_delta':nc['actual_total_tokens']-bc['actual_total_tokens'] if known else None,
            'latency_delta':nc['latency_seconds']-bc['latency_seconds'] if bc['latency_seconds'] is not None and nc['latency_seconds'] is not None else None,
            'cost_gate_passed':cost_ok,'latency_gate_passed':latency_ok})
    improved=sum(not r['baseline_passed'] and r['candidate_passed'] for r in changes)
    eligible &= improved>=policy['minimum_fixed_cases']
    holdout_groups={e['criteria'].get('independence_group',e['criteria']['case_id'])
                    for e in candidate if e['criteria']['split']=='holdout'}
    coverage_ok=len(holdout_groups)>=policy.get('minimum_independent_holdout',1)
    semantic_ok=not policy.get('requires_semantic_review',False) or all(
        e.get('semantic_review')=='independent_human_passed' for e in candidate)
    eligible &= coverage_ok and semantic_ok
    return {'version':'comparison-v1','created_at':utc_now(),'policy':policy,
            'baseline_version':baseline[0]['subject_version'],'candidate_version':candidate[0]['subject_version'],
            'dataset_fingerprint':baseline[0]['criteria']['dataset_fingerprint'],
            'baseline_evaluation_fingerprints':[digest(e) for e in baseline],
            'candidate_evaluation_fingerprints':[digest(e) for e in candidate],
            'cases':changes,'improved_cases':improved,'eligible':bool(eligible),
            'holdout_independent_groups':len(holdout_groups),'holdout_coverage_gate_passed':coverage_ok,
            'semantic_review_gate_passed':semantic_ok,
            'recommendation':'manual_review_for_promotion' if eligible else 'reject',
            'semantic_quality_validated':False}
