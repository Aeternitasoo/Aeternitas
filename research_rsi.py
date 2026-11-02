"""RSI over frozen real research artifacts and the existing Research Workflow.

No source acquisition, new model client, arbitrary code runner or production
promotion. Candidate choice uses development only; requests are durably bounded.
"""
from collections import Counter
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from candidate_registry import CandidateRegistry
from evidence_repository import digest
from lab_evaluation import freeze_dataset,load_split,read,pins,verify_pins
from research_contract_profile import LEGACY,MULTI,from_files,validate_multi,validate_output_profile
from research_runtime import utc_now
from research_workflow import (INSTRUCTION,FrozenInputs,run_workflow,verify_workflow,
                               save,sha,validate_output)

VERSION='research-rsi-v1.1'
POLICY={'scope':'research','allow_unknown_cost':False,'max_extra_tokens':2000,
        'max_latency_seconds':180,'minimum_fixed_cases':1,
        'minimum_independent_holdout':2,'requires_semantic_review':True}


def artifact(path):
    p=Path(path).resolve()
    if not p.is_file() or p.is_symlink():raise ValueError('Source artifact missing / symlink')
    return {'path':str(p),'sha256':sha(p)}


def material(case,name):
    item=case['artifacts'][name];p=Path(item['path'])
    if p.is_symlink() or sha(p)!=item['sha256']:raise ValueError('Frozen real artifact changed')
    return read(p)


def source_record(identity,source,source_kind,group,*,task=None,execution_provenance='unknown'):
    """Inventory explicit known runs, not discovery of every historical folder."""
    source=Path(source).resolve();files={}
    choices={'workflow':{'response':'raw-response.json','config':'config.json','evidence':'evidence.json',
                         'request':'request.json','input':'input.json','state':'state.json',
                         'metadata':'model-metadata.json','trace':'events.jsonl','manifest':'input-manifest.json'},
             'industry_case':{'response':'raw-structured-response.json','report':'report.final.json',
                              'metadata':'model-metadata.json','commit':'COMMITTED.json','input':'research-input.json'},
             'legacy_company':{'report':'report.json','bundle':'input_bundle.json','state':'state.json','commit':'published.json'},
             'harness':{'request':'request.json','state':'state.json','trace':'events.jsonl','commit':'COMMITTED.json'}}
    if source_kind not in choices:raise ValueError('Unsupported real record type')
    for name,relative in choices[source_kind].items():
        if (source/relative).exists():files[name]=artifact(source/relative)
    state=read(source/'state.json') if (source/'state.json').exists() else {}
    meta=read(source/'model-metadata.json') if (source/'model-metadata.json').exists() else {}
    report=read(source/'report.json') if (source/'report.json').exists() else {}
    mode=state.get('model_execution') or report.get('model_execution')
    real=(source_kind in {'workflow','industry_case'} and meta.get('http_status')==200)
    if source_kind=='legacy_company':
        real=report.get('billing_mode')=='chatgpt_plan' and bool(report.get('model_usage'))
    if source_kind=='harness':real=mode=='configured_client'
    if execution_provenance not in {'recorded_real','controlled','unknown'}:raise ValueError('Unknown execution provenance')
    suggested=real
    if execution_provenance!='recorded_real':real=False if execution_provenance=='controlled' else None
    if source_kind=='workflow' and 'config' in files and material({'artifacts':files},'config').get('evidence_kind')=='controlled_test':
        real=False
    known_failure=state.get('error_code') if state.get('status')=='failed' else None
    return {'id':identity,'kind':'saved_research','source_kind':source_kind,'source_run_id':state.get('run_id') or report.get('run_id') or source.name,
        'source_path':str(source),'independence_group':group,'artifacts':files,'material':'derived_public_snapshot',
        'real_model_execution':real,'real_execution_evidence':execution_provenance,
        'provider_metadata_suggests_real':suggested,'provenance_independently_server_authenticated':False,
        'harness_version':state.get('harness_version'),'skill_versions':state.get('loaded_skills') or None,
        'prompt_version':state.get('prompt_version') or report.get('prompt_version') or
            (read(source/'request.json').get('prompt',{}).get('version') if source_kind=='workflow' else None),
        'report_committed':(source/'COMMITTED.json').exists() if source_kind in {'workflow','industry_case','harness'} else (source/'published.json').exists(),
        'known_failure':known_failure,'task':task,
        'eligible_metrics':(['execution','tools','skills','versions'] if source_kind=='harness' else
                            ['output_contract','literal_citations','evidence_coverage','publication','usage_if_recorded','latency_if_recorded']),
        'semantic_quality':'requires_independent_human_review'}


def freeze_real_dataset(folder,splits,*,request_plan):
    groups={};artifacts={}
    for split,cases in splits.items():
        for case in cases:
            group=case['independence_group']
            if group in groups and groups[group]!=split:raise ValueError('Related real run crosses evaluation splits')
            groups[group]=split
            for name,item in case['artifacts'].items():
                if name in {'input','bundle','response','report'}:
                    pin=item['sha256']
                    if pin in artifacts and artifacts[pin]!=split:raise ValueError('Same real artifact crosses splits')
                    artifacts[pin]=split
    plan={'request_limit':4,'automatic_retries':0,'requests':request_plan,
          'model':'gpt-6.1-sol','auth':'chatgpt','data_acquisition':False,
          'selection':'existing_saved_task_families_before_candidate_evaluation'}
    policy={**POLICY,'request_plan':plan}
    return freeze_dataset(folder,splits,version=VERSION,policy=policy)


def citation_diagnostics(result,evidence):
    by_id={e['id']:e['text'] for e in evidence};counts=Counter();locations=[]
    for section in result.get('sections',[]):
        for index,claim in enumerate(section.get('claims',[])):
            seen={};pairs=set()
            for cite in claim.get('citations',[]):
                eid,quote=cite.get('evidence_id'),cite.get('quote')
                counts['citations']+=1
                if not isinstance(quote,str):counts['invalid_quote_type']+=1;continue
                if eid not in by_id:counts['unknown_evidence_ids']+=1
                elif quote in by_id[eid]:counts['literal_matches']+=1
                else:counts['nonliteral_quotes']+=1
                if not 12<=len(quote)<=300:counts['quote_length_violations']+=1
                pair=(eid,quote)
                if pair in pairs:counts['identical_duplicate_fragments']+=1
                elif eid in seen:
                    counts['same_source_distinct_fragments']+=1
                    locations.append({'topic':section.get('topic'),'claim_index':index,'evidence_id':eid,
                                      'quote_sha256':digest(quote),'quote_length':len(quote)})
                pairs.add(pair);seen[eid]=quote
    return {'counts':dict(counts),'same_source_locations':locations}


def analyze_research_traces(cases,dataset_pin):
    if not cases or any(c['split']!='development' for c in cases):raise ValueError('Research failure analysis is development-only')
    issues=[];run_summaries=[];counts=Counter()
    for case in cases:
        if case['source_kind']!='workflow':
            run_summaries.append({'case_id':case['id'],'real_model_execution':case['real_model_execution'],
                                  'analysis':'context_record_not_treated_as_workflow_failure'})
            continue
        result=material(case,'response');evidence=material(case,'evidence');state=material(case,'state')
        request=material(case,'request');meta=material(case,'metadata')
        diagnostic=citation_diagnostics(result,evidence)
        trace_path=case['artifacts'].get('trace');trace=[]
        if trace_path:
            if sha(trace_path['path'])!=trace_path['sha256']:raise ValueError('Trace changed')
            trace=[json.loads(line) for line in Path(trace_path['path']).read_text().splitlines()]
        completed_model=meta.get('http_status')==200 and meta.get('completion_status')=='completed'
        if diagnostic['counts'].get('same_source_distinct_fragments'):
            name='source_identity_is_not_excerpt_identity';counts[name]+=1
            issues.append({'category':name,'dimension':'evidence_contract','owner':'validator_contract_design',
                'case_id':case['id'],'source_run_id':case['source_run_id'],'occurrences_within_run':len(diagnostic['same_source_locations']),
                'independent_runs':1,'evidence':diagnostic['same_source_locations'],
                'code_locations':['analysis_contracts.py:validate_analysis','research_workflow.py:validate_output'],
                'impact':'Different literal fragments from one source cannot be represented under the unique-ID contract',
                'not_attributed_to_model_transport':completed_model})
        cite=request['schema']['properties']['sections']['items']['properties']['claims']['items']['properties']['citations']
        quote_schema=cite['items']['properties']['quote']
        if 'maxLength' not in quote_schema or 'maxItems' not in cite:
            name='schema_local_contract_gap';counts[name]+=1
            issues.append({'category':name,'dimension':'prompt_and_output_schema','owner':'workflow_contract',
                'case_id':case['id'],'code_locations':['research_workflow.py:schema_for'],
                'evidence':{'schema_sha256':digest(request['schema']),'quote_maxLength_present':'maxLength' in quote_schema,
                            'observed_overlong_quotes':diagnostic['counts'].get('quote_length_violations',0)},
                'impact':'Local limits are absent from the decoding schema; a completed response can be rejected afterwards'})
        run_summaries.append({'case_id':case['id'],'state':state.get('status'),'failure_stage':state.get('step'),
            'model_completed':completed_model,'usage':meta.get('usage'),'diagnostics':diagnostic,
            'trace_stages':[e['name'] for e in trace if e['phase']=='start']})
    proposal=None
    if counts.get('source_identity_is_not_excerpt_identity') or counts.get('schema_local_contract_gap'):
        proposal={'template':'bounded_multi_excerpt_contract-v1','candidate_kind':'research_contract',
            'reason':'Version excerpt identity separately from source identity and express existing local limits in the model schema',
            'expected_metrics':['publication_success','bounded_literal_citations','schema_local_parity'],
            'side_effects':['More excerpts can increase output tokens and latency','New version requires explicit selection; old acceptance remains unchanged'],
            'alternatives_not_selected':{'skill_only':'A Skill cannot override a validator contract',
                'prompt_only':'Wording alone cannot make same-source excerpts legal',
                'retrieval_rewrite':'No evidence that the rejected literal quotations were missing from input'}}
    return {'version':VERSION,'created_at':utc_now(),'split':'development','dataset_fingerprints':[dataset_pin],
        'evaluation_ids':[c['id'] for c in cases],'failure_counts':dict(counts),'issues':issues,'runs':run_summaries,
        'dimensions':{'harness':'no real Harness failure established','tool_calling':'not_used_by_real_synthesis_workflow',
            'skills':'not_loaded_by_real_synthesis_workflow','retrieval':'literal source membership assessed; retrieval relevance requires review',
            'token_efficiency':'actual usage retained; no semantic-efficiency judgement',
            'recovery':'no automatic retry; failure artifacts preserved'},'proposal':proposal}


def propose_candidate(registry,analysis,dataset_pin,*,parent=None):
    if analysis['split']!='development' or analysis['proposal'] is None:raise ValueError('No supported development proposal')
    baseline={'contracts/research.json':json.dumps(LEGACY,sort_keys=True,indent=2)+'\n','prompts/research.md':INSTRUCTION}
    if parent is None:
        parent=registry.create_version(baseline,kind='research_contract',reason='Frozen existing industry-research-v1 baseline')
        registry.initialize(parent)
    else:
        meta,folder=registry.version(parent)
        if meta['kind']!='research_contract' or any((folder/'files'/name).read_text()!=text for name,text in baseline.items()):
            raise ValueError('Existing baseline differs')
    old='每项论点同一ID只引用一次，摘录十二至三百字符。'
    new=('同一来源可引用不同的逐字片段，引用身份由证据ID和原文片段共同确定。每段仍为十二至三百字符；'
         '每项论点最多六段、最多三个不同来源，累计引文不超过一千二百字符，拒绝重复的相同片段。'
         '不得为满足唯一性而丢弃有效的同源证据。unknown无引文时正文不超过六百字符。')
    if INSTRUCTION.count(old)!=1:raise ValueError('Baseline Prompt changed; proposal template no longer applicable')
    changed={'contracts/research.json':json.dumps(MULTI,sort_keys=True,indent=2)+'\n',
             'prompts/research.md':INSTRUCTION.replace(old,new)}
    candidate=registry.create_version(changed,kind='research_contract',parent=parent,
        reason=analysis['proposal']['reason'],failure_analysis=analysis,dataset_fingerprint=dataset_pin,
        proposal_metadata=analysis['proposal'])
    return parent,candidate


class RequestLedger:
    """At most four HTTP sends; a crash or restart cannot reset the allowance."""
    def __init__(self,root,plan,limit=4):
        if limit!=4:raise ValueError('RSI v1.1 hard maximum is four requests')
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.plan=deepcopy(plan);self.limit=limit
        path=self.root/'plan.json'
        if path.exists():
            if read(path)!={'limit':limit,'plan':plan}:raise ValueError('Request plan cannot change')
        else:save(path,{'limit':limit,'plan':plan})

    def before_request(self,request,*,case_id,version,input_fingerprint):
        if request.method!='POST' or str(request.url)!='https://api.openai.com/v1/responses':raise ValueError('Unexpected model endpoint')
        body=json.loads(request.content)
        if body.get('model')!='gpt-6.1-sol' or body.get('store') is not False:raise ValueError('Model configuration changed')
        actual=digest(json.loads(body['input'][1]['content']))
        if actual!=input_fingerprint:raise ValueError('Frozen model input differs')
        with (self.root/'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            files=sorted(self.root.glob('request-*.json'))
            pair={'case_id':case_id,'version':version}
            if pair not in self.plan:raise ValueError('Request not in frozen plan')
            if any(all(read(p).get(k)==v for k,v in pair.items()) for p in files):raise ValueError('No automatic resend for this case/version')
            if len(files)>=self.limit:raise ValueError('Persistent model request budget exhausted')
            number=len(files)+1
            save(self.root/f'request-{number:04d}.json',{**pair,'number':number,'started_at':utc_now(),
                'endpoint':'https://api.openai.com/v1/responses','model':body['model'],'input_fingerprint':actual,
                'request_payload_fingerprint':digest(body),'transport_attempted':True,'automatic_retry':False})
            return number

    def after_response(self,number,response):
        save(self.root/f'http-{number:04d}.json',{'number':number,'http_status':response.status_code,'received_at':utc_now()})


def client_factory(ledger,case_id,version,input_fingerprint,provider_config):
    """Use stock OAuth/ResponsesAnalyst, with content-free HTTP accounting hooks."""
    from chatgpt_auth import ChatGPTAuth
    from research_analysis import ResponsesAnalyst
    if provider_config['AETERNITAS_AUTH']!='chatgpt':raise ValueError('Existing ChatGPT subscription required')
    def factory(folder,budget):
        client=ResponsesAnalyst(None,'gpt-6.1-sol',folder,budget,plan_auth=ChatGPTAuth())
        attempt=[]
        def before(request):
            attempt.append(ledger.before_request(request,case_id=case_id,version=version,input_fingerprint=input_fingerprint))
        def after(response):ledger.after_response(attempt[-1],response)
        client.http.event_hooks['request'].append(before);client.http.event_hooks['response'].append(after)
        return client
    return factory


def execute_frozen_case(case,profile_folder,output_root,ledger,version,provider_config,*,expected_input,sink=None):
    task=case['task'];config=task['config'];frozen=FrozenInputs(task['source_root'],task['bundles'])
    profile=from_files(profile_folder)
    before={p.name for p in Path(output_root).iterdir()} if Path(output_root).exists() else set()
    try:
        folder=run_workflow(config,output_root,provider_config=provider_config,collector=frozen,profile=profile,sink=sink,
            client_factory=client_factory(ledger,case['id'],version,expected_input,provider_config))
    except Exception:
        created=[p for p in Path(output_root).iterdir() if p.is_dir() and p.name not in before]
        if len(created)!=1:raise
        folder=created[0]
    return folder


def prepare_frozen_input(case,output_root,*,profile=None):
    """Reuse the Workflow up to its model boundary; no response or API request."""
    from research_analysis import ResponsesAnalyst
    from sources.sec import SourceError
    task=case['task'];captured={}
    class InputBoundaryReached(SourceError):pass
    class InputOnlyClient(ResponsesAnalyst):
        def complete_json(self,messages,schema,**kwargs):
            payload=json.loads(messages[1]['content'])
            captured.update(input_fingerprint=digest(payload),evidence_count=len(payload['evidence']),
                input_bytes=len(json.dumps(messages,ensure_ascii=False).encode()),model_requests=0,
                prompt_messages_fingerprint=digest(messages),schema_fingerprint=digest(schema))
            raise InputBoundaryReached('input_preparation_no_model_request')
    def factory(folder,budget):
        return InputOnlyClient(None,task['config']['model'],folder,budget,plan_auth=object())
    try:
        run_workflow(task['config'],output_root,collector=FrozenInputs(task['source_root'],task['bundles']),
                     client_factory=factory,profile=profile)
    except SourceError:
        if not captured:raise
    return captured


def evaluate_workflow(folder,criteria,output_root,*,profile,version,expected_input,real_request_evidence):
    """Independent fixed target checks apply equally to old and new responses."""
    folder=Path(folder);files=pins(folder);config=read(folder/'config.json');state=read(folder/'state.json')
    result=read(folder/'raw-response.json') if (folder/'raw-response.json').exists() else None
    evidence=read(folder/'evidence.json') if (folder/'evidence.json').exists() else []
    meta=read(folder/'model-metadata.json') if (folder/'model-metadata.json').exists() else {}
    metrics={};failures=[]
    def check(k,condition):
        metrics[k]=int(bool(condition))
        if not condition:failures.append(k)
    if (folder/'COMMITTED.json').exists():
        try:verify_workflow(folder);committed=True
        except Exception:committed=False
    else:committed=False
    check('completion',committed)
    actual_input=digest(read(folder/'input.json')) if (folder/'input.json').exists() else None
    check('frozen_input',actual_input==expected_input)
    try:validate_multi(result,config,evidence);valid=True
    except (ValueError,TypeError,KeyError):valid=False
    check('common_bounded_output',valid)
    try:validate_output_profile(result,config,evidence,profile);valid=True
    except (ValueError,TypeError,KeyError):valid=False
    check('version_contract',valid)
    try:validate_output(result,config,evidence);old_valid=True
    except (ValueError,TypeError,KeyError):old_valid=False
    diagnostic=citation_diagnostics(result or {},evidence);d=diagnostic['counts'];cites=d.get('citations',0)
    metrics['literal_citation_rate']=d.get('literal_matches',0)/cites if cites else None
    claims=[c for s in (result or {}).get('sections',[]) for c in s.get('claims',[])]
    required=[c for c in claims if c.get('classification') in {'fact','inference'}]
    metrics['evidence_coverage']=sum(bool(c.get('citations')) for c in required)/len(required) if required else None
    metrics['tool_call_correctness']=None;metrics['skill_selection_quality']=None
    metrics['schema_bounds_expressed']=int(profile['spec']==MULTI)
    quote_ids={c['evidence_id'] for claim in claims for c in claim.get('citations',[]) if isinstance(c.get('evidence_id'),str)}
    metrics['evidence_utilization']=len(quote_ids)/len(evidence) if evidence else None
    events=[json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
    latency=next((e['elapsed_seconds'] for e in reversed(events) if e['name']=='research' and e['phase']=='end'),None)
    tokens=(meta.get('usage') or {}).get('total_tokens');tokens=tokens if type(tokens) is int and tokens>=0 else None
    check('model_identity',meta.get('requested_model')==config['model']
          and (not real_request_evidence or config['model']=='gpt-6.1-sol'))
    value={'evaluation_id':uuid4().hex,'evaluator_version':VERSION,'evaluator_code_sha256':sha(__file__),
        'run_id':state['run_id'],'run_path':str(folder.resolve()),'run_files':files,'created_at':utc_now(),
        'criteria':criteria,'criteria_fingerprint':digest(criteria),'subject_version':version,
        'permission_budget_fingerprint':digest({'tools':[],'permissions':['frozen_evidence.read','model.single_request'],
            'model':config['model'],'seconds':config['seconds'],'max_model_calls':1}),
        'metrics':metrics,'costs':{'actual_total_tokens':tokens,'currency_cost':None,'latency_seconds':latency,
            'actual_input_tokens':(meta.get('usage') or {}).get('input_tokens'),
            'actual_output_tokens':(meta.get('usage') or {}).get('output_tokens'),'model_calls':state.get('model_attempts')},
        'hard_checks_passed':not failures,'failure_categories':failures,'semantic_review':'requires_independent_human_review',
        'overall_score':None,'legacy_contract_outcome':'accepted' if old_valid else 'rejected',
        'diagnostics':diagnostic,'requested_model':meta.get('requested_model'),'returned_model':meta.get('returned_model'),
        'response_id':meta.get('response_id'),'prompt':read(folder/'request.json')['prompt'] if (folder/'request.json').exists() else None,
        'real_model_execution':real_request_evidence,'output_and_evidence_are_not_semantic_certification':True}
    out=Path(output_root)/value['evaluation_id'];out.mkdir(parents=True,exist_ok=False);save(out/'evaluation.json',value)
    verify_pins(folder,files)
    return value


def evaluate_legacy_archive(case,criteria,output_root,*,profile,version):
    """Compatibility replay of all original claims, not a new model execution."""
    from analysis_contracts import validate_analysis
    started=time.monotonic();report=material(case,'report');evidence=report['evidence']
    original_ok=True;candidate_ok=True;diagnostics=[];claims=[]
    for role,analysis in report['analyses'].items():
        try:validate_analysis(analysis,evidence)
        except (ValueError,TypeError,KeyError):original_ok=False
        adapted=[{'classification':'inference','text':c['text'],'citations':c['citations']} for c in analysis['claims']]
        adapted.extend({'classification':'unknown','text':text,'citations':[]} for text in analysis['gaps'])
        # Revalidate every original claim separately; no truncation to Workflow's
        # different per-section bound and no rewriting historical labels.
        for claim in adapted:
            result={'sections':[{'topic':role,'claims':[claim]}]};config={'sections':[{'id':role}]}
            try:validate_output_profile(result,config,evidence,profile)
            except (ValueError,TypeError,KeyError):candidate_ok=False
            diagnostics.append(citation_diagnostics(result,evidence));claims.append(claim)
    out=Path(output_root)/uuid4().hex;out.mkdir(parents=True,exist_ok=False)
    save(out/'archive-check.json',{'source_case':case,'legacy_acceptance':original_ok,'candidate_acceptance':candidate_ok,
                                 'claims_checked':len(claims),'new_model_requests':0,'historical_labels_changed':False})
    files=pins(out)
    value={'evaluation_id':uuid4().hex,'evaluator_version':VERSION,'evaluator_code_sha256':sha(__file__),
        'run_id':out.name,'run_path':str(out),'run_files':files,'created_at':utc_now(),'criteria':criteria,
        'criteria_fingerprint':digest(criteria),'subject_version':version,
        'permission_budget_fingerprint':digest({'mode':'offline_archive_revalidation','tools':[],'model_calls':0}),
        'metrics':{'legacy_archive_preserved':int(original_ok),'completion':int(candidate_ok),'claims_preserved':1},
        'costs':{'actual_total_tokens':0,'currency_cost':None,'latency_seconds':time.monotonic()-started,
                 'model_calls':0,'scope':'current_offline_revalidation_only_source_run_usage_not_imputed'},
        'hard_checks_passed':original_ok and candidate_ok,'failure_categories':[] if original_ok and candidate_ok else ['archive_contract_regression'],
        'semantic_review':'requires_independent_human_review','overall_score':None,
        'real_source_model_execution':case['real_model_execution'],'new_real_model_execution':False,'source_run_usage':report.get('model_usage')}
    destination=Path(output_root)/value['evaluation_id'];destination.mkdir();save(destination/'evaluation.json',value)
    return value


def execute_trial(prepared_path,*,execute_real=False,sink=None):
    """Single frozen proposal; resume reuses journals and never resends a case."""
    from aeternitas import load_config
    from chatgpt_auth import ChatGPTAuth
    prepared=read(prepared_path);root=Path(prepared_path).resolve().parent
    dataset=Path(prepared['dataset']);pin=prepared['dataset_fingerprint']
    if sha(dataset/'manifest.json')!=pin:raise ValueError('Prepared dataset changed')
    registry=CandidateRegistry(prepared['registry'],authority_root=prepared['authority'])
    versions={'baseline':prepared['baseline'],'candidate':prepared['candidate']}
    profiles={name:from_files(registry.version(v)[1]/'files') for name,v in versions.items()}
    ledger=RequestLedger(root/'model-requests',prepared['request_plan'])
    journal=root/'execution-journal';journal.mkdir(exist_ok=True)
    provider=load_config(Path(__file__).with_name('.env'))
    auth=ChatGPTAuth().status()
    if provider.get('AETERNITAS_AUTH')!='chatgpt' or auth.get('model')!='gpt-6.1-sol':
        raise ValueError('Existing provider/model selection differs; no switch performed')
    provider['AETERNITAS_MODEL']='gpt-6.1-sol'  # Resolve the existing local selection; no file update.
    blocked=None
    if not execute_real:blocked='real_execution_not_requested'
    if not auth['connected'] or not auth['plan_permission']:blocked='existing_authorization_required'
    def criteria(case):
        return {'case_id':case['id'],'split':case['split'],'dataset_fingerprint':pin,
            'independence_group':case['independence_group'],'target_evidence_contract':'research-contract-v2',
            'historical_labels':'unchanged','semantic_quality':'requires_independent_human_review'}
    def invoke(case,variant):
        nonlocal blocked
        note=journal/(case['id']+'-'+variant+'.json')
        if note.exists():
            value=read(note);evaluation=read(value['evaluation_path'])
            if digest(evaluation)!=value['evaluation_fingerprint']:raise ValueError('Saved evaluation changed')
            verify_pins(evaluation['run_path'],evaluation['run_files']);return evaluation
        if blocked:
            folder=root/'not-executed'/(case['id']+'-'+variant);folder.mkdir(parents=True,exist_ok=False)
            save(folder/'blocked.json',{'reason':blocked,'model_request_sent':False,'case_id':case['id']})
            value={'evaluation_id':uuid4().hex,'evaluator_version':VERSION,'evaluator_code_sha256':sha(__file__),
                'run_id':folder.name,'run_path':str(folder),'run_files':pins(folder),'created_at':utc_now(),
                'criteria':criteria(case),'criteria_fingerprint':digest(criteria(case)),'subject_version':versions[variant],
                'permission_budget_fingerprint':digest({'tools':[],'permissions':['frozen_evidence.read','model.single_request'],
                    'model':'gpt-6.1-sol','seconds':case['task']['config']['seconds'],'max_model_calls':1}),
                'metrics':{'completion':0},'costs':{'actual_total_tokens':None,'currency_cost':None,'latency_seconds':None},
                'hard_checks_passed':False,'failure_categories':['not_executed'],'reason':blocked,
                'semantic_review':'requires_independent_human_review','overall_score':None,'real_model_execution':False}
            out=root/'evaluations'/value['evaluation_id'];out.mkdir(parents=True);save(out/'evaluation.json',value)
        else:
            print(json.dumps({'stage':'model_start','case_id':case['id'],'variant':variant,
                'requests_used':len(list(ledger.root.glob('request-*.json'))),'limit':4}),flush=True)
            folder=execute_frozen_case(case,registry.version(versions[variant])[1]/'files',root/'workflow-runs',
                ledger,variant,provider,expected_input=prepared['expected_inputs'][case['id']],sink=sink)
            attempts=[read(p) for p in ledger.root.glob('request-*.json')]
            matching=[r for r in attempts if r['case_id']==case['id'] and r['version']==variant]
            state=read(folder/'state.json')
            if matching:
                http=ledger.root/f"http-{matching[0]['number']:04d}.json"
                if http.exists() and read(http)['http_status'] in {400,401,403,404,422}:
                    blocked='nonretryable_model_http_'+str(read(http)['http_status'])
            elif state.get('step')=='model':
                blocked='model_pre_send_or_authorization_failure'
            value=evaluate_workflow(folder,criteria(case),root/'evaluations',profile=profiles[variant],
                version=versions[variant],expected_input=prepared['expected_inputs'][case['id']],
                real_request_evidence=bool(matching))
            print(json.dumps({'stage':'model_finished','case_id':case['id'],'variant':variant,
                'status':state['status'],'hard_checks_passed':value['hard_checks_passed'],
                'tokens':value['costs']['actual_total_tokens'],'requests_used':len(attempts)}),flush=True)
        evaluation_path=root/'evaluations'/value['evaluation_id']/'evaluation.json'
        save(note,{'evaluation_path':str(evaluation_path),'evaluation_fingerprint':digest(value)})
        if sink is not None:
            try:sink.evaluation(value)
            except Exception:pass
        return value
    development,_=load_split(dataset,'development',pin)
    main=next(c for c in development if c['source_kind']=='workflow')
    baseline_dev=evaluate_workflow(main['source_path'],criteria(main),root/'evaluations',profile=profiles['baseline'],
        version=versions['baseline'],expected_input=prepared['expected_inputs'][main['id']],real_request_evidence=main['real_model_execution'])
    candidate_dev=invoke(main,'candidate')
    if not (root/'development-comparison.json').exists():
        save(root/'development-comparison.json',{'baseline':baseline_dev,'candidate':candidate_dev,
             'usage_scope':'baseline_is_saved_historical_call_not_new_budget','promotion_evidence':False})
    baseline=[];changed=[]
    regression,_=load_split(dataset,'regression',pin)
    for case in regression:
        for variant,rows in (('baseline',baseline),('candidate',changed)):
            rows.append(evaluate_legacy_archive(case,criteria(case),root/'evaluations',profile=profiles[variant],version=versions[variant]))
    # Candidate files remain frozen; no holdout data goes to failure analysis.
    holdout,_=load_split(dataset,'holdout',pin)
    for case in holdout:
        baseline.append(invoke(case,'baseline'));changed.append(invoke(case,'candidate'))
    _,manifest=load_split(dataset,'holdout',pin)
    comparison,path=registry.record_comparison(versions['candidate'],baseline,changed,policy=manifest['policy'],dataset_root=dataset)
    actual=[read(p) for p in ledger.root.glob('request-*.json')]
    real_evals=[candidate_dev,*[e for e in baseline+changed if e['criteria']['split']=='holdout']]
    known=[e['costs']['actual_total_tokens'] for e in real_evals if e.get('real_model_execution') and e['costs']['actual_total_tokens'] is not None]
    tokens_unknown=max(0,len(actual)-len(known))
    summary={'version':VERSION,'created_at':utc_now(),'dataset_fingerprint':pin,'baseline':versions['baseline'],
        'candidate':versions['candidate'],'comparison_path':str(path),'promotion_recommendation':comparison['recommendation'],
        'eligible':comparison['eligible'],'holdout_groups':comparison['holdout_independent_groups'],
        'semantic_review_gate_passed':comparison['semantic_review_gate_passed'],'active_version':registry.active(),
        'model_request_attempts':len(actual),'request_limit':4,'automatic_retries':0,
        'known_new_model_tokens':sum(known),'requests_with_unknown_usage':tokens_unknown,'currency_cost':None,
        'new_model_total_tokens':sum(known) if tokens_unknown==0 else None,'stop_reason':blocked,
        'development_baseline_reused':True,'regression_type':'offline_real_archive_compatibility_not_new_model_research',
        'langfuse':'requires_credentials_local_recorder_retained','production_promotion_performed':False,
        'trial_is_preliminary':True,'true_semantic_improvement_established':False,
        'holdout_results':[{'variant':v,'metrics':e['metrics'],'costs':e['costs'],'failures':e['failure_categories'],
                           'run_path':e['run_path'],'real_model_execution':e.get('real_model_execution')}
                          for v,e in (('baseline',baseline[-1]),('candidate',changed[-1]))]}
    destination=root/('summary-'+uuid4().hex+'.json');save(destination,summary)
    return summary,destination
