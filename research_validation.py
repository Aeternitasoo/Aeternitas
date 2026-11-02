"""Independent paired rounds over immutable RSI v1.1 profile versions.

Reuses the original Workflow, HTTP accounting, evaluator and Registry. All
additional records live in a new round; consumed holdout feedback is explicitly
development material for *future* candidates, never a new blind test for them.
"""
from collections import Counter,defaultdict
from copy import deepcopy
import json
from pathlib import Path
import shutil
from uuid import uuid4

from candidate_registry import CandidateRegistry
from evidence_repository import digest
from harness_contracts import validate
from lab_evaluation import freeze_dataset,load_split,read,verify_pins
from research_contract_profile import from_files,schema
from research_rsi import (RequestLedger,execute_frozen_case,evaluate_workflow,
                          evaluate_legacy_archive,citation_diagnostics)
from research_runtime import utc_now
from research_workflow import save,sha

VERSION='research-validation-v1.2'
POLICY={'scope':'research','max_requests':6,'max_groups':3,'requests_per_group':2,
    'max_extra_tokens':2000,'max_token_ratio':1.25,'max_latency_seconds':180,
    'max_latency_ratio':1.50,'latency_noise_seconds':5,'minimum_independent_holdout':3,
    'required_candidate_submission_rate':1.0,'maximum_new_failures':0,
    'requires_semantic_review':True,'automatic_retries':0,
    'model':'gpt-6.1-sol','auth':'chatgpt','candidate_changes_during_round':False}


def verify_frozen(frozen):
    for name,pin in {**frozen['version_files'],**frozen['implementation']}.items():
        path=Path(name) if Path(name).is_absolute() else Path(__file__).parent/name
        if sha(path)!=pin:raise ValueError('Frozen v1.1 version / implementation changed')
    return True


def import_frozen_versions(root,frozen):
    verify_frozen(frozen)
    root=Path(root)
    registry=CandidateRegistry(root/'registry',authority_root=root/'manual-authority',create=True)
    for identity in (frozen['baseline_id'],frozen['candidate_id']):
        source=Path(frozen['prior_root'])/'registry/versions'/identity
        shutil.copytree(source,registry.root/'versions'/identity)
        registry.version(identity)
    registry.initialize(frozen['baseline_id'])
    save(root/'version-import.json',{'source':frozen['prior_root'],'baseline_id':frozen['baseline_id'],
        'candidate_id':frozen['candidate_id'],'byte_identical':True,'source_registry_modified':False})
    return registry


class PairedLedger(RequestLedger):
    """Reuse v1.1 send/resume protection with an explicit six-send round plan."""
    def __init__(self,root,plan):
        grouped=defaultdict(list)
        for item in plan:
            if set(item)!={'case_id','version'}:raise ValueError('Invalid paired request plan')
            grouped[item['case_id']].append(item['version'])
        if (not 1<=len(grouped)<=3 or len(plan)>6
                or any(sorted(v)!=['baseline','candidate'] for v in grouped.values())):
            raise ValueError('At most three groups, exactly one request per variant')
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.plan=deepcopy(plan);self.limit=6
        record={'protocol':VERSION,'limit':6,'plan':plan}
        path=self.root/'plan.json'
        if path.exists():
            if read(path)!=record:raise ValueError('Paired request plan cannot change')
        else:save(path,record)


def freeze_validation_dataset(folder,splits,frozen,*,policy=POLICY):
    verify_frozen(frozen)
    groups={};issuers=set();content={}
    for split,cases in splits.items():
        for case in cases:
            group=case['independence_group']
            if group in groups and groups[group]!=split:raise ValueError('Related task crosses splits')
            groups[group]=split
            if split=='holdout':
                if (case['symbol'] in frozen['excluded_from_new_holdout']
                        or case['cik'] in issuers or case.get('previously_used',True)):
                    raise ValueError('Consumed / duplicate issuer cannot be fresh holdout')
                issuers.add(case['cik'])
            for name,a in case.get('artifacts',{}).items():
                if name not in {'bundle','input','response','report'}:continue
                pin=a['sha256']
                if pin in content and content[pin]!=split:raise ValueError('Artifact shared across splits')
                content[pin]=split
    if not 1<=len(splits['holdout'])<=3:raise ValueError('One to three actual holdout tasks required')
    return freeze_dataset(folder,splits,version=VERSION,policy=deepcopy(policy))


def evaluate_pair_run(folder,case,variant,version,profile,output_root,dataset_pin,*,attempts):
    criteria={'case_id':case['id'],'split':'holdout','dataset_fingerprint':dataset_pin,
        'independence_group':case['independence_group'],'target_evidence_contract':'research-contract-v2',
        'semantic_quality':'requires_independent_human_review','historical_labels':'unchanged'}
    source=evaluate_workflow(folder,criteria,Path(output_root)/'component-evaluations',profile=profile,
        version=version,expected_input=case['expected_input_fingerprint'],real_request_evidence=bool(attempts))
    folder=Path(folder);result=read(folder/'raw-response.json') if (folder/'raw-response.json').exists() else None
    config=read(folder/'config.json');evidence=read(folder/'evidence.json') if (folder/'evidence.json').exists() else []
    try:validate(result,schema(config,profile));schema_ok=True
    except (ValueError,KeyError,TypeError):schema_ok=False
    d=citation_diagnostics(result or {},evidence)['counts'];count=d.get('citations',0)
    metrics={**source['metrics'],'schema_validity':int(schema_ok),
        'citation_length_validity':(count-d.get('quote_length_violations',0)-d.get('invalid_quote_type',0))/count if count else None,
        'meaningless_duplicate_fragments':d.get('identical_duplicate_fragments',0),'agent_tool_calls':0,
        'same_source_distinct_fragments':d.get('same_source_distinct_fragments',0),
        'tool_call_errors':None,'failure_rate':1-source['metrics']['completion']}
    flags=[]
    for section in (result or {}).get('sections',[]):
        for i,claim in enumerate(section.get('claims',[])):
            if claim.get('classification')=='fact' and any(word in claim.get('text','') for word in ['导致','证明','验证了','因此']):
                flags.append({'topic':section['topic'],'claim_index':i,'flag':'review_fact_causality_entailment',
                              'automatic_verdict':None})
    value={**source,'evaluation_id':uuid4().hex,'evaluator_version':VERSION,'evaluator_code_sha256':sha(__file__),
        'source_evaluation_fingerprint':digest(source),'variant':variant,'symbol':case['symbol'],
        'metrics':metrics,'actual_model_requests':len(attempts),'automatic_retries':0,
        'research_semantic_quality':None,'semantic_review':'not_reviewed_by_independent_human',
        'complete_response_available':result is not None,
        'preliminary_codex_flags':flags,'costs':{**source['costs'],'actual_model_requests':len(attempts),
            'agent_tool_calls':0,'automatic_retries':0,'currency_cost':None}}
    out=Path(output_root)/'evaluations'/value['evaluation_id'];out.mkdir(parents=True)
    save(out/'evaluation.json',value)
    return value,out/'evaluation.json'


def paired_comparison(evaluations,regression,policy):
    by_case=defaultdict(dict)
    for value in evaluations:
        key=value['criteria']['case_id'];variant=value['variant']
        if variant in by_case[key]:raise ValueError('Duplicate paired evaluation')
        by_case[key][variant]=value
    if any(set(v)!= {'baseline','candidate'} for v in by_case.values()):raise ValueError('Missing paired status record')
    rows=[];hard_reasons=[];evidence_reasons=[];confirmed_hard=False
    for key,pair in sorted(by_case.items()):
        b,c=pair['baseline'],pair['candidate']
        if (b['criteria_fingerprint']!=c['criteria_fingerprint']
                or b['permission_budget_fingerprint']!=c['permission_budget_fingerprint']
                or b['evaluator_code_sha256']!=c['evaluator_code_sha256']):raise ValueError('Pair has different input policy / evaluator')
        bt,ct=b['costs']['actual_total_tokens'],c['costs']['actual_total_tokens']
        bl,cl=b['costs']['latency_seconds'],c['costs']['latency_seconds']
        token_known=bt is not None and ct is not None;latency_known=bl is not None and cl is not None
        token_ok=token_known and ct<=bt+policy['max_extra_tokens'] and (bt==0 or ct<=bt*policy['max_token_ratio'])
        latency_ok=latency_known and cl<=policy['max_latency_seconds'] and cl<=bl*policy['max_latency_ratio']+policy['latency_noise_seconds']
        new_failures=sorted(set(c['failure_categories'])-set(b['failure_categories']))
        fixed=sorted(set(b['failure_categories'])-set(c['failure_categories']))
        if new_failures:
            hard_reasons.append(key+':new_failures')
            if 'evaluation_engine_error' in new_failures:evidence_reasons.append(key+':independent_evaluation_incomplete')
            else:confirmed_hard |= c.get('complete_response_available',False)
        if token_known and not token_ok:
            hard_reasons.append(key+':token_growth_over_limit');confirmed_hard=True
        if latency_known and not latency_ok:
            hard_reasons.append(key+':latency_growth_over_limit');confirmed_hard=True
        if not token_known or not latency_known:evidence_reasons.append(key+':usage_or_latency_unknown')
        rows.append({'case_id':key,'symbol':c.get('symbol'),'group':c['criteria']['independence_group'],
            'baseline':{k:b[k] for k in ('metrics','costs','failure_categories','run_path','evaluation_id')},
            'candidate':{k:c[k] for k in ('metrics','costs','failure_categories','run_path','evaluation_id')},
            'new_failures':new_failures,'fixed_failures':fixed,'token_delta':ct-bt if token_known else None,
            'input_token_delta':(c['costs']['actual_input_tokens']-b['costs']['actual_input_tokens'])
                if c['costs'].get('actual_input_tokens') is not None and b['costs'].get('actual_input_tokens') is not None else None,
            'output_token_delta':(c['costs']['actual_output_tokens']-b['costs']['actual_output_tokens'])
                if c['costs'].get('actual_output_tokens') is not None and b['costs'].get('actual_output_tokens') is not None else None,
            'token_ratio':ct/bt if token_known and bt else None,'latency_delta':cl-bl if latency_known else None,
            'token_gate_passed':bool(token_ok),'latency_gate_passed':bool(latency_ok)})
    totals={}
    for variant in ('baseline','candidate'):
        values=[p[variant] for p in by_case.values()];n=len(values)
        def weighted_rate(metric,count_key=None):
            usable=[e['metrics'][metric] for e in values if e['metrics'].get(metric) is not None]
            return sum(usable)/len(usable) if usable else None
        tokens=[e['costs']['actual_total_tokens'] for e in values]
        times=[e['costs']['latency_seconds'] for e in values]
        totals[variant]={'planned_tasks':n,'report_submissions':sum(e['metrics']['completion'] for e in values),
            'report_submission_rate':sum(e['metrics']['completion'] for e in values)/n,
            'failure_rate':sum(1-e['metrics']['completion'] for e in values)/n,
            'citation_matching_macro_mean':weighted_rate('literal_citation_rate'),
            'citation_metric_known_tasks':sum(e['metrics'].get('literal_citation_rate') is not None for e in values),
            'evidence_coverage_macro_mean':weighted_rate('evidence_coverage'),
            'schema_validity_rate':sum(e['metrics'].get('schema_validity',0) for e in values)/n,
            'tokens_total':sum(tokens) if all(v is not None for v in tokens) else None,
            'known_tokens':sum(v for v in tokens if v is not None),'unknown_usage_tasks':sum(v is None for v in tokens),
            'elapsed_total_seconds':sum(times) if all(v is not None for v in times) else None,
            'actual_model_requests':sum(e['actual_model_requests'] for e in values),'agent_tool_calls':0,
            'tool_call_errors':None,'automatic_retries':0,'currency_cost':None,'human_semantic_quality':None}
        for kind in ('actual_input_tokens','actual_output_tokens'):
            known=[e['costs'].get(kind) for e in values]
            totals[variant][kind]=sum(known) if all(x is not None for x in known) else None
    if not all(e['hard_checks_passed'] for e in regression):
        hard_reasons.append('regression_failed');confirmed_hard=True
    if totals['candidate']['report_submission_rate']<policy['required_candidate_submission_rate']:
        hard_reasons.append('candidate_submission_rate_below_frozen_threshold')
    groups={r['group'] for r in rows}
    if len(groups)<policy['minimum_independent_holdout']:evidence_reasons.append('independent_holdout_coverage_insufficient')
    if policy['requires_semantic_review']:evidence_reasons.append('independent_human_semantics_not_reviewed')
    # Missing transport responses are evidence gaps, not invented adverse quality.
    unobserved=any(not e.get('actual_model_requests') or e['costs']['actual_total_tokens'] is None
                   or 'evaluation_engine_error' in e['failure_categories'] for e in evaluations)
    decision='reject' if confirmed_hard or (hard_reasons and not unobserved) else 'hold_for_more_evidence' if evidence_reasons or unobserved else 'promote_recommended'
    return {'version':VERSION,'created_at':utc_now(),'policy':policy,'pairs':rows,'totals':totals,
        'new_failures':{r['case_id']:r['new_failures'] for r in rows if r['new_failures']},
        'fixed_failures':{r['case_id']:r['fixed_failures'] for r in rows if r['fixed_failures']},
        'regression_passed':all(e['hard_checks_passed'] for e in regression),
        'independent_groups':len(groups),'decision':decision,'hard_gate_reasons':hard_reasons,
        'missing_evidence_reasons':evidence_reasons,'human_review_complete':False,
        'statistical_significance_claimed':False,'production_promotion_performed':False}


def failure_analysis(comparison,evaluations):
    patterns=defaultdict(list);traces={e['evaluation_id']:{'path':e['run_path'],'version':e['subject_version'],
        'trace':str(Path(e['run_path'])/'events.jsonl'),'split':e['criteria']['split']} for e in evaluations}
    for evaluation in evaluations:
        folder=Path(evaluation['run_path']);trace=folder/'events.jsonl';state_path=folder/'state.json'
        binding=traces[evaluation['evaluation_id']]
        if trace.exists():
            events=[json.loads(line) for line in trace.read_text().splitlines()]
            state=read(state_path) if state_path.exists() else {}
            binding.update(trace_sha256=sha(trace),failure_stage=state.get('step'),error_type=state.get('error_type'),
                stage_results=[{k:event.get(k) for k in ('name','span_id','parent_span_id','status','elapsed_seconds','error_code')}
                               for event in events if event['phase']=='end'])
            if state.get('status')=='failed':
                step=state.get('step','')
                owner=('source_or_evidence_admission' if step.startswith(('collection','evidence')) else
                       'provider_or_transport_or_authorization' if step=='model' else
                       'report_publication' if step.startswith('publication') else 'output_contract')
                binding['stage_owner']=owner
            if (folder/'request.json').exists():binding['prompt']=read(folder/'request.json').get('prompt')
    for row in comparison['pairs']:
        b,c=row['baseline'],row['candidate']
        for variant,value in (('baseline',b),('candidate',c)):
            for code in value['failure_categories']:
                owner='evidence_contract' if code=='version_contract' else 'workflow_or_output_contract'
                if code=='evaluation_engine_error':owner='evaluator_implementation'
                patterns[(variant,code,owner)].append(row['case_id'])
        if row['token_delta'] is not None and row['token_delta']>0:
            patterns[('candidate','token_growth_observed','prompt_efficiency_hypothesis')].append(row['case_id'])
        if row.get('output_token_delta') is not None and row['output_token_delta']>0:
            patterns[('candidate','output_token_growth_observed','prompt_efficiency_hypothesis')].append(row['case_id'])
        if row['latency_delta'] is not None and row['latency_delta']>0:
            patterns[('candidate','latency_growth_observed','runtime_efficiency_hypothesis')].append(row['case_id'])
    records=[]
    for (variant,code,owner),ids in sorted(patterns.items()):
        distinct=sorted(set(ids));groups={r['group'] for r in comparison['pairs'] if r['case_id'] in distinct}
        records.append({'variant':variant,'pattern':code,'owner':owner,'case_ids':distinct,'independent_groups':len(groups),
            'recurrence':'cross_task_observation' if len(groups)>=2 else 'single_task_observation',
            'causal_attribution_proven':False})
    proposal=None
    cost=[r for r in records if r['variant']=='candidate' and r['pattern']=='output_token_growth_observed' and r['independent_groups']>=2]
    if cost:
        proposal={'template':'remove_redundant_prose_preserve_evidence-v1','kind':'research_contract',
            'reason':'Repeated candidate token-growth observation; test concision without changing evidence rules',
            'scope':['prompts/research.md'],'expected_metrics':['output_tokens','latency_at_equal_deterministic_quality'],
            'side_effects':['Concision can omit counterevidence or nuance; requires fresh semantic and holdout review'],
            'source_case_ids':cost[0]['case_ids'],'not_evaluated_in_this_round':True}
    return {'version':VERSION,'created_at':utc_now(),'split':'development','source_stage':'post_evaluation_feedback',
        'source_splits':['holdout'],'source_holdout_now_consumed_for_next_generation':True,
        'prohibited_as_next_generation_independent_holdout':True,
        'dataset_fingerprints':[evaluations[0]['criteria']['dataset_fingerprint']],
        'evaluation_ids':[e['evaluation_id'] for e in evaluations],
        'failure_counts':dict(Counter(r['pattern'] for r in records)),'patterns':records,'trace_bindings':traces,
        'dimensions':{'harness':'not_changed_no_failure_attributed_without_trace_evidence',
            'skills':'not_loaded_by_this_workflow','tools':'not_called_by_model_not_scored_as_perfect',
            'retrieval':'semantic_relevance_requires_independent_review'},'proposal':proposal}


def create_next_candidate(registry,current,analysis,dataset_pin):
    if analysis['proposal'] is None:return None
    meta,folder=registry.version(current)
    files={name:(folder/'files'/name).read_text() for name in meta['files']}
    files['prompts/research.md']+=('\n效率候选：避免跨章节近义复述和无信息的铺垫，先组织独立判断再选择支撑片段。'
        '保持事实、推断、假设和未知的区分，保留必要的同源多段证据、反面证据、风险和推翻条件；'
        '不得通过省略引用、隐瞒缺口或弱化校验来减少Token。只压缩冗余表达。')
    return registry.create_version(files,kind=meta['kind'],parent=current,reason=analysis['proposal']['reason'],
        failure_analysis=analysis,dataset_fingerprint=dataset_pin,proposal_metadata={**analysis['proposal'],
            'evaluation_status':'not_run','prompt_revision':'rsi-v1.2-next-generation',
            'same_evidence_contract':True,'requires_fresh_unconsumed_holdout':True})


def write_review_packet(output,cases,evaluations,seed):
    """Blinded material with unfilled human fields, never an automated pass."""
    output=Path(output);output.mkdir(parents=True,exist_ok=False);mapping={};reviews=[]
    for case in cases:
        values=[e for e in evaluations if e['criteria']['case_id']==case['id']]
        values=sorted(values,key=lambda e:digest([seed,case['id'],e['variant']]))
        for letter,e in zip(('A','B'),values):
            folder=Path(e['run_path']);response=read(folder/'raw-response.json') if (folder/'raw-response.json').exists() else None
            evidence=read(folder/'evidence.json') if (folder/'evidence.json').exists() else []
            item={'case_id':case['id'],'symbol':case['symbol'],'blind_label':letter,
                'question':case['task']['config']['question'],'response':response,'evidence':evidence,
                'human_review':{k:None for k in ['source_entailment','causal_reasoning','classification',
                    'financial_scope','counterevidence','unknowns','risks_and_falsifiers','overall_verdict']},
                'reviewer':None,'reviewed_at':None,'codex_preliminary_flags_not_human_judgement':e.get('preliminary_codex_flags',[])}
            path=output/(case['id']+'-'+letter+'.json');save(path,item);reviews.append(str(path))
            mapping[case['id']+'-'+letter]={'variant':e['variant'],'evaluation_id':e['evaluation_id']}
    save(output/'review-manifest.json',{'files':{Path(p).name:sha(p) for p in reviews},
        'human_review_complete':False,'blinding_seed_sha256':digest(seed)})
    return mapping


def append_validation(registry,frozen,dataset,pin,comparison,evaluations,regression):
    """Append a new validation round; do not rewrite v1.1's creation dataset."""
    verify_frozen(frozen)
    manifest=read(Path(dataset)/'manifest.json')
    if sha(Path(dataset)/'manifest.json')!=pin or manifest['policy']!=comparison['policy']:
        raise ValueError('Validation dataset / policy changed')
    cases,_=load_split(dataset,'holdout',pin)
    expected={(c['id'],v) for c in cases for v in ('baseline','candidate')}
    if {(e['criteria']['case_id'],e['variant']) for e in evaluations}!=expected:
        raise ValueError('Missing / extra frozen paired evaluations')
    if len(evaluations)!=len(expected):raise ValueError('Duplicate paired evaluations')
    for e in evaluations:
        if e['subject_version']!=frozen[e['variant']+'_id'] or e['criteria']['dataset_fingerprint']!=pin:
            raise ValueError('Validation version / dataset binding mismatch')
        verify_pins(e['run_path'],e['run_files'])
    meta,folder=registry.version(frozen['candidate_id'])
    value={**comparison,'round_type':'independent_validation_v1.2',
        'candidate_id':frozen['candidate_id'],'parent_version':frozen['baseline_id'],
        'candidate_manifest_sha256':sha(folder/'manifest.json'),
        'candidate_creation_dataset':meta['dataset_fingerprint'],'validation_dataset_fingerprint':pin,
        'validation_dataset_root':str(Path(dataset).resolve()),
        'evaluation_fingerprints':[digest(e) for e in evaluations],
        'regression_fingerprints':[digest(e) for e in regression],
        'eligible_for_automatic_apply':False,'human_approval_recorded':False}
    path=registry.root/'comparisons'/('validation-'+uuid4().hex+'.json');save(path,value)
    registry.event(frozen['candidate_id'],'evaluating',validation_round=VERSION,
                   recommendation=value['decision'],comparison_sha256=sha(path))
    registry.event(frozen['candidate_id'],value['decision'],validation_round=VERSION,
                   comparison_sha256=sha(path),no_production_promotion=True)
    return path


def execute_round(prepared_path,*,execute_real=False,sink=None):
    from aeternitas import load_config
    from chatgpt_auth import ChatGPTAuth
    prepared=read(prepared_path);root=Path(prepared_path).resolve().parent
    frozen=read(prepared['frozen_path']);verify_frozen(frozen)
    dataset=prepared['dataset'];pin=prepared['dataset_fingerprint']
    cases,manifest=load_split(dataset,'holdout',pin)
    registry=CandidateRegistry(root/'registry',authority_root=root/'manual-authority')
    versions={v:frozen[v+'_id'] for v in ('baseline','candidate')}
    profiles={v:from_files(registry.version(identity)[1]/'files') for v,identity in versions.items()}
    ledger=PairedLedger(root/'model-requests',prepared['request_plan'])
    provider=load_config(Path(__file__).with_name('.env'));auth=ChatGPTAuth().status()
    if provider['AETERNITAS_AUTH']!='chatgpt' or auth['model']!='gpt-6.1-sol':raise ValueError('Existing model/auth selection differs')
    provider['AETERNITAS_MODEL']='gpt-6.1-sol'
    blocked='not_authorized_to_execute_real' if not execute_real else None
    if not auth['connected'] or not auth['plan_permission']:blocked='authorization_unavailable'
    journal=root/'execution-journal';journal.mkdir(exist_ok=True)
    evaluations=[]
    def blocked_result(case,variant,reason,*,observed_folder=None,request_count=0):
        folder=Path(observed_folder) if observed_folder else root/'not-executed'/(case['id']+'-'+variant)
        if observed_folder is None:
            folder.mkdir(parents=True,exist_ok=False)
            save(folder/'blocked.json',{'reason':reason,'model_requests':0})
        from lab_evaluation import pins
        criteria={'case_id':case['id'],'split':'holdout','dataset_fingerprint':pin,'independence_group':case['independence_group']}
        usage={};elapsed=None
        if observed_folder is not None:
            try:
                usage=read(folder/'model-metadata.json').get('usage') or {}
                events=[json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
                elapsed=next((e['elapsed_seconds'] for e in reversed(events) if e['name']=='research' and e['phase']=='end'),None)
            except (ValueError,KeyError,OSError):pass
        return {'evaluation_id':uuid4().hex,'evaluator_version':VERSION,'evaluator_code_sha256':sha(__file__),
            'run_id':folder.name,'run_path':str(folder),'run_files':pins(folder),'criteria':criteria,
            'criteria_fingerprint':digest(criteria),'subject_version':versions[variant],'variant':variant,'symbol':case['symbol'],
            'permission_budget_fingerprint':case['permission_budget_fingerprint'],
            'metrics':{'completion':0,'schema_validity':0,'literal_citation_rate':None,'evidence_coverage':None},
            'costs':{'actual_total_tokens':usage.get('total_tokens'),'actual_input_tokens':usage.get('input_tokens'),
                     'actual_output_tokens':usage.get('output_tokens'),'latency_seconds':elapsed,'currency_cost':None},
            'actual_model_requests':request_count,'automatic_retries':0,'hard_checks_passed':False,'failure_categories':['not_executed'],
            'semantic_review':'not_reviewed_by_independent_human','complete_response_available':False,'reason':reason}
    for planned in prepared['request_plan']:
        case=next(c for c in cases if c['id']==planned['case_id']);variant=planned['version']
        entry=journal/(case['id']+'-'+variant+'.json')
        verify_frozen(frozen)
        if entry.exists():
            note=read(entry);value=read(note['evaluation_path'])
            if digest(value)!=note['fingerprint']:raise ValueError('Evaluation journal changed')
            verify_pins(value['run_path'],value['run_files']);evaluations.append(value);continue
        if blocked:
            value=blocked_result(case,variant,blocked)
            out=root/'evaluations'/value['evaluation_id'];out.mkdir(parents=True);path=out/'evaluation.json';save(path,value)
        else:
            print(json.dumps({'stage':'start','task':case['id'],'variant':variant,'requests_used':len(list(ledger.root.glob('request-*.json'))),'limit':6}),flush=True)
            folder=execute_frozen_case(case,registry.version(versions[variant])[1]/'files',root/'workflow-runs',ledger,
                variant,provider,expected_input=case['expected_input_fingerprint'],sink=sink)
            attempts=[read(p) for p in ledger.root.glob('request-*.json')]
            attempts=[a for a in attempts if a['case_id']==case['id'] and a['version']==variant]
            if attempts:
                http=ledger.root/f"http-{attempts[0]['number']:04d}.json"
                if http.exists() and read(http)['http_status'] in {400,401,403,404,422,429}:
                    blocked='nonretryable_or_rate_limited_http_'+str(read(http)['http_status'])
            elif read(folder/'state.json').get('step')=='model':blocked='pre_send_or_authorization_failure'
            try:
                value,path=evaluate_pair_run(folder,case,variant,versions[variant],profiles[variant],root,pin,attempts=attempts)
            except Exception as exc:
                # Keep paid responses and stop further sends if the evaluator
                # itself fails. Unknown quality must not become a passing grade.
                from lab_evaluation import pins
                blocked='evaluation_engine_error_requires_review'
                value=blocked_result(case,variant,blocked,observed_folder=folder,request_count=len(attempts))
                value.update(run_path=str(folder),run_files=pins(folder),actual_model_requests=len(attempts),
                    failure_categories=['evaluation_engine_error'],evaluation_error_type=type(exc).__name__)
                out=root/'evaluations'/value['evaluation_id'];out.mkdir(parents=True)
                path=out/'evaluation.json';save(path,value)
            print(json.dumps({'stage':'finished','task':case['id'],'variant':variant,
                'submitted':value['metrics']['completion'],'tokens':value['costs']['actual_total_tokens'],
                'latency':value['costs']['latency_seconds'],'failures':value['failure_categories']},ensure_ascii=False),flush=True)
        save(entry,{'evaluation_path':str(path),'fingerprint':digest(value)})
        if sink is not None:
            try:sink.evaluation(value)
            except Exception:pass
        evaluations.append(value)
    regressions=[]
    for case in load_split(dataset,'regression',pin)[0]:
        criteria={'case_id':case['id'],'split':'regression','dataset_fingerprint':pin,'independence_group':case['independence_group']}
        for variant in ('baseline','candidate'):
            regressions.append(evaluate_legacy_archive(case,criteria,root/'regression-evaluations',profile=profiles[variant],version=versions[variant]))
    comparison=paired_comparison(evaluations,regressions,manifest['policy'])
    comparison['dataset_fingerprint']=pin;comparison['candidate_id']=versions['candidate'];comparison['baseline_id']=versions['baseline']
    record=append_validation(registry,frozen,dataset,pin,comparison,evaluations,regressions)
    analysis=failure_analysis(comparison,evaluations);save(root/('failure-analysis-'+uuid4().hex+'.json'),analysis)
    next_candidate=create_next_candidate(registry,versions['candidate'],analysis,pin)
    review_dir=root/('human-review-'+uuid4().hex)
    review_key=write_review_packet(review_dir,cases,evaluations,prepared['review_blinding_seed'])
    save(root/('review-key-'+review_dir.name+'.json'),review_key)
    summary={**comparison,'comparison_record':str(record),'next_candidate_id':next_candidate,
        'next_candidate_evaluation_status':'not_run' if next_candidate else 'no_supported_proposal',
        'model_request_attempts':len(list(ledger.root.glob('request-*.json'))),'maximum_requests':6,
        'automatic_retries':0,'active_version':registry.active(),'human_review_packet':str(review_dir),
        'langfuse':'requires_credentials_local_traces_retained','stop_reason':blocked,
        'old_candidate_changed':False,'AAPL_previous_holdout_used_as_new_sample':False}
    verify_frozen(frozen)
    path=root/('summary-'+uuid4().hex+'.json');save(path,summary)
    return summary,path
