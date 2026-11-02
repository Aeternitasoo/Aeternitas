"""Connect existing real run evaluations, scoped Mem0 and the Candidate Registry.

Known failure templates only; no autonomous code writing or production apply.
"""
import json
from pathlib import Path
from uuid import uuid4

from evidence_repository import digest
from lab_evaluation import analyze_failures,verify_pins
from research_workflow import save,sha
from research_observability import Recorder


def review(evaluations,memory,registry,candidate_id,output,*,sink=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    recorder=Recorder(output/'events.jsonl',uuid4().hex,sink,context={'candidate_version':candidate_id})
    with recorder.span('rsi_analysis'):
        derived=[];sources=[]
        for path in evaluations:
            e=json.loads(Path(path).read_text());verify_pins(e['path'],e['run_file_pins'])
            trace=Path(e['path'])/'events.jsonl'
            events=[json.loads(line) for line in trace.read_text().splitlines()]
            sources.append({'evaluation':str(path),'evaluation_sha256':sha(path),'trace':str(trace),
                            'trace_sha256':sha(trace),'failed_spans':[v['name'] for v in events if v['phase']=='end' and v['status']=='failed']})
            category=('evidence_role_authorization' if e.get('gate_diagnostic')=='Counterevidence not in audited retrieval'
                      else 'research_contract' if e.get('failure_stage')=='validation' else None)
            derived.append({'evaluation_id':digest([str(path),sha(path)])[:32],
                'criteria':{'split':'development','dataset_fingerprint':digest(e['run_file_pins'])},
                'original_split':'previously_consumed_holdout','failure_categories':[category] if category else [],
                'source_evaluation_sha256':sha(path)})
        analysis=analyze_failures(derived)
        with recorder.span('memory_read') as span:
            found=memory.search('evidence authorization counterevidence duplicate role',memory_type='procedural',
                entity='research_quality.challenge',scope='agent_runtime',period='all',classification='fact')
            memories=found['results'];span.update(memory_count=len(memories),memory_ids_sha256=digest([m['memory_id'] for m in memories]))
        meta,folder=registry.version(candidate_id);runtime=meta['proposal_metadata']
        if sha(runtime['runtime_path'])!=runtime['runtime_sha256']:raise ValueError('Candidate runtime changed')
        if sha(runtime['runtime_diff_path'])!=runtime['runtime_diff_sha256']:raise ValueError('Candidate diff changed')
        qualified=bool(memories and analysis['failure_counts'].get('evidence_role_authorization'))
        analysis.update(source_records=sources,historical_memory_ids=[m['memory_id'] for m in memories],
            memory_backend=found['backend'],candidate_id=candidate_id,
            proposal={'owner':'research_contract','action':'review_existing_tested_runtime_fix',
                'candidate_id':candidate_id,'source_diff_sha256':runtime['runtime_diff_sha256'],
                'expected_improvement':'remove false authorization refusals with unchanged semantic-review ceiling',
                'side_effects':['Cross-role reuse still needs semantic review; same-role duplicate checks remain required']} if qualified else None,
            independent_quality_improvement_verified=False,production_apply_permitted=False)
        save(output/'failure-analysis.json',analysis)
        with recorder.span('candidate_record'):
            result={'candidate_id':candidate_id,'analysis_sha256':sha(output/'failure-analysis.json'),
                'memory_used':qualified,'recommendation':'hold_for_more_evidence',
                'reason':'offline_replay_passed_but_independent_research_semantics_and_human_approval_pending',
                'human_approval_recorded':False,'production_applied':False,'token_usage':None,'new_model_requests':0}
            save(output/'recommendation.json',result)
            save(registry.root/'comparisons'/('infrastructure-'+uuid4().hex+'.json'),result)
            registry.event(candidate_id,'hold_for_more_evidence',analysis_sha256=sha(output/'failure-analysis.json'),no_apply=True)
    return result


CATEGORIES=('Source Retrieval','Evidence Selection','Financial Analysis','Reasoning','Counterevidence',
            'Prompt / Skill','Tool Execution','Harness','Memory Retrieval','Memory Management','Evaluation')

def build_quality_candidates(root,memory,registry,*,runtime_names,previous,profile_parent):
    """Known-pattern, evidence-bound builder. No unconstrained code generation."""
    root=Path(root)
    previous=Path(previous)
    allowed={'research_memory.py','research_analysis.py','research_workflow.py','infrastructure_rsi.py'}
    if not runtime_names or not set(runtime_names)<=allowed:raise ValueError('Unapproved runtime candidate scope')
    registry.version(profile_parent)
    review=json.loads((root/'historical-semantic-review.json').read_text())
    legacy=json.loads((previous/'rsi-ablation-feedback.json').read_text())
    traces=[]
    for row in json.loads((previous/'evaluation-summary.json').read_text())['rows']:
        folder=Path(row['run_path']);trace=folder/'events.jsonl'
        events=[json.loads(x) for x in trace.read_text().splitlines()]
        traces.append({'path':str(trace),'sha256':sha(trace),'run_id':folder.name,
            'failed_spans':[e['name'] for e in events if e['phase']=='end' and e['status']=='failed'],
            'quality_input':str(folder/'quality-input.json'),'quality_input_sha256':sha(folder/'quality-input.json')})
    with_memory=memory.search('source authorization evidence role',memory_type='procedural',
        entity='research_quality.challenge',scope='agent_runtime',period='all')
    flags=[{'run':x['run'],'topic':c['topic'],'claim_index':c['claim_index'],'flag':f,
            'raw_sha256':x['response_sha256']} for x in review['reports'] for c in x['claims'] for f in c['semantic_flags']]
    dataset=digest({'review':sha(root/'historical-semantic-review.json'),'traces':traces,
                    'memory_records':[m['memory_id'] for m in with_memory['results']]})
    analysis={'split':'development','dataset_fingerprints':[dataset],'version':'quality-failure-analyzer-v1.4',
        'category_coverage':{k:'no_established_failure' for k in CATEGORIES},'trace_sources':traces,
        'memory_readback':with_memory,'historical_registry_parent':profile_parent,
        'issues':flags,'review_kind':'AI-assisted, not independent human',
        'occurrences':len(flags),'independent_semantic_failure_runs':len({x['run'] for x in flags}),
        'known_failure_templates_only':True,'unbounded_self_modification':False}
    for category in ['Source Retrieval','Financial Analysis','Memory Retrieval','Memory Management','Evaluation']:
        analysis['category_coverage'][category]='evidence_or_coverage_limitation_observed'
    if flags:analysis['category_coverage']['Counterevidence']='AI_review_flags_not_ground_truth'
    analysis['proposal']={'reason':'Observed memory retry/diagnostic gaps and repeated counterargument-target flags',
         'runtime_scope':runtime_names,'prompt_scope':['prompts/research.md'],
         'expected_metrics':['idempotent_writes','diagnostic_state_completeness','counterargument_relevance'],
         'side_effects':['Over-normalization can erase valid provenance; retain observed time/source differences',
                         'Prompt changes can change cost and tone; independent holdout/human review required']}
    save(root/'failure-analysis.json',analysis)
    basefiles={'harness/'+n:(root/'baseline'/n).read_text() for n in runtime_names}
    changed={'harness/'+n:(root/'candidate'/n).read_text() for n in runtime_names}
    if basefiles==changed:raise ValueError('No actual runtime change')
    runtime_parent=registry.create_version(basefiles,kind='harness',reason='Frozen current runtime archive, no application')
    runtime_candidate=registry.create_version(changed,kind='harness',parent=runtime_parent,
       reason=analysis['proposal']['reason'],failure_analysis=analysis,dataset_fingerprint=dataset,
       proposal_metadata={'archive_only':True,'applied_to_production':False})
    parent=profile_parent
    meta,folder=registry.version(parent);files={n:(folder/'files'/n).read_text() for n in meta['files']}
    if flags:
        files['prompts/research.md']+=('反证必须针对本项论点的命题和方向。加强风险的材料不是风险论点的反证；竞争风险不能直接推翻产品具备某项用途的事实。'
          '没有相关反证时保留incomplete，不能为凑引用而附无关材料。推翻条件区分否定当前命题、削弱前提和仍无法检验；'
          '需要未来核验的机制不得写成已证实因果。避免跨章节重复同一事实，保留重要支持及反对证据。')
    else:raise ValueError('No supported Prompt change')
    candidate=registry.create_version(files,kind='research_contract',parent=parent,reason='Target-specific counterevidence checklist',
       failure_analysis=analysis,dataset_fingerprint=dataset,proposal_metadata={'runtime_candidate':runtime_candidate,
          'prompt_revision':'quality-v1.4-counterevidence-v1','human_review_required':True,'no_promotion':True})
    record={'runtime_parent':runtime_parent,'runtime_candidate':runtime_candidate,'profile_parent':parent,
        'profile_candidate':candidate,'analysis_sha256':sha(root/'failure-analysis.json'),
        'development_dataset_fingerprint':dataset,'production_applied':False}
    save(root/'candidate-creation.json',record)
    return record
