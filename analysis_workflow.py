"""Deterministic M4 evidence plans and check traces; no model, network or SQL calls."""

import hashlib
import json


WORKFLOW_VERSION='research-four-roles-m6-v1'
REPORT_SCHEMA_VERSION='aeternitas-report-m4-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def build_analysis_plan(bundle,model,billing,*,prompt_version=None):
    from analysis_contracts import (ROLES, PROMPT_VERSION, RESULT_SCHEMA_VERSION, prompt_instruction,
                                    role_prompt_versions)
    from evidence_selection import select_evidence, workflow_fingerprint
    chosen,manifests={},{}
    for role in ROLES:
        chosen[role],manifests[role]=select_evidence(role,bundle['ticker'],bundle['cik'],bundle['as_of_date'],
                                                   bundle['question'],bundle['evidence'])
    version=prompt_version or PROMPT_VERSION
    versions=role_prompt_versions(version)
    workflow=WORKFLOW_VERSION if version==PROMPT_VERSION else 'research-four-roles-m4-v1'
    instruction=lambda role: prompt_instruction(role) if version==PROMPT_VERSION else prompt_instruction(role,version)
    prompts={role:{'version':versions[role],
                   'instruction_sha256':hashlib.sha256(instruction(role).encode()).hexdigest()}
             for role in ROLES}
    plan={'workflow_version':workflow,'result_schema_version':RESULT_SCHEMA_VERSION,
          'prompt_version':version,'role_prompts':prompts,'role_manifests':manifests,
          'capabilities':{'model_tools':[],'tool_dispatcher':False,'arbitrary_URL_fetch':False,
                          'shell_tool':False,'file_read_tool':False,'trade_tool':False}}
    plan['workflow_fingerprint']=workflow_fingerprint(ticker=bundle['ticker'],cik=bundle['cik'],cutoff=bundle['as_of_date'],
       question=bundle['question'],model=model,billing=billing,
       prompt_versions={r:p['version']+':'+p['instruction_sha256'] for r,p in prompts.items()},
       result_schema_version=RESULT_SCHEMA_VERSION,role_manifests=manifests,workflow_version=workflow)
    return chosen,plan


def analysis_checks(analyses,chosen):
    from analysis_contracts import ROLES, validate_analysis
    result={'version':'analysis-checks-m4-v1',
            'hard_checks':{'structure':'passed','numeric_prose':'passed','evidence_membership':'passed',
                           'exact_quotation':'passed','company_date_source_scope':'passed'},
            'semantic_support':'human_review_required','research_quality':'not_automatically_certified','roles':{}}
    for role in ROLES:
        analysis=validate_analysis(analyses[role],chosen[role])
        by_id={e['id']:e for e in chosen[role]}
        traces=[]
        for index,claim in enumerate(analysis['claims']):
            fact_ids,calculation_ids=set(),set()
            for cite in claim['citations']:
                e=by_id[cite['evidence_id']]
                if e['kind']=='financial_fact': fact_ids.add(e['locator']['fact_id'])
                if e['kind']=='calculation':
                    calculation_ids.add(e['id'])
                    fact_ids.update(e['locator']['calculation']['input_fact_ids'])
            traces.append({'claim_index':index,'content_kind':'model_judgment','stance':claim['stance'],
              'evidence_ids':[c['evidence_id'] for c in claim['citations']],
              'quote_sha256':[hashlib.sha256(c['quote'].encode()).hexdigest() for c in claim['citations']],
              'fact_ids':sorted(fact_ids),'calculation_ids':sorted(calculation_ids),'semantic_support':'human_review_required'})
        result['roles'][role]={'claim_count':len(analysis['claims']),'gap_count':len(analysis['gaps']),
                              'claim_traces':traces,'result_sha256':digest(analysis)}
    return result


def publication_files(run_dir):
    """Only finite, known role / attempt filenames can enter a publication manifest."""
    from analysis_contracts import ROLES
    names=['report.html','report.json','input_bundle.json','analysis_plan.json']
    for role in ROLES:
        names.append(f'steps/{role}.json')
        for attempt in (1,2):
            for kind in ('input','output','error'):
                name=f'steps/{role}-attempt-{attempt}-{kind}.json'
                if (run_dir/name).is_file(): names.append(name)
    return names


def verify_analysis_artifacts(folder,report):
    from analysis_contracts import ROLES, RESULT_SCHEMA_VERSION, role_prompt_versions
    from research_analysis import prompt_for
    version=report.get('prompt_version')
    versions=role_prompt_versions(version)
    chosen,plan=build_analysis_plan(report,report['model'],report['billing_mode'],prompt_version=version)
    if report.get('analysis_provenance')!=plan or json.loads((folder/'analysis_plan.json').read_text())!=plan:
        raise ValueError('Analysis selection / prompt / workflow fingerprint differs')
    if report.get('analysis_checks')!=analysis_checks(report['analyses'],chosen):
        raise ValueError('Analysis check trace differs from actual role results')
    previous={}
    total=0
    for role in ROLES:
        step=json.loads((folder/'steps'/f'{role}.json').read_text())
        if (step.get('role')!=role or step.get('model')!=report['model'] or
            step.get('role_prompt_version')!=versions[role] or
            step.get('result_schema_version')!=RESULT_SCHEMA_VERSION or
            step.get('evidence_ids')!=[e['id'] for e in chosen[role]] or step.get('result')!=report['analyses'][role]):
            raise ValueError('Role checkpoint identity / evidence / result differs')
        prior=previous if role in {'risk','synthesis'} else {}
        attempts=[]
        for attempt in (1,2):
            path=folder/'steps'/f'{role}-attempt-{attempt}-input.json'
            if not path.exists():
                if attempt==1: raise ValueError('Missing actual role input')
                break
            total+=1
            attempts.append(attempt)
            data=json.loads(path.read_text())
            messages=data['input']
            if (data.get('model')!=report['model'] or data.get('role_prompt_version')!=versions[role]
                or data.get('prompt_version')!=report['prompt_version']
                or data.get('result_schema_version')!=RESULT_SCHEMA_VERSION or data.get('input_sha256')!=digest(messages)):
                raise ValueError('Actual role request version / hash differs')
            if (not isinstance(messages,list) or len(messages)!=2 or
                any(not isinstance(m,dict) or set(m)!={'role','content'} or not isinstance(m['content'],str) for m in messages)
                or [m['role'] for m in messages]!=['developer','user']):
                raise ValueError('Actual role request has unexpected instruction messages')
            payload=json.loads(messages[1]['content'])
            # Repair-only fields may differ, but evidence and prior validated results must not.
            expected=json.loads(prompt_for(role,report['ticker'],report['as_of_date'],report['question'],chosen[role],prior,
                                            prompt_version=version)[1]['content'])
            repair_keys={'previous_attempt_validation_error','previous_attempt_output_untrusted',
                         'previous_attempt_output_omitted'}
            if ({k:v for k,v in payload.items() if k not in repair_keys}!=expected or
                messages[0]!=prompt_for(role,report['ticker'],report['as_of_date'],report['question'],chosen[role],prior,
                                        prompt_version=version)[0]):
                raise ValueError('Actual role input is outside recorded evidence / prior results')
            if attempt==1 and repair_keys & set(payload):
                raise ValueError('First attempt cannot contain repair instructions')
            if attempt==2:
                error=json.loads((folder/'steps'/f'{role}-attempt-1-error.json').read_text())
                feedback=payload.get('previous_attempt_validation_error','')
                if feedback not in {error.get('error'),f"Model output validation failed: {error.get('error')}"}:
                    raise ValueError('Repair input is not bound to preceding failure')
                prior_output=folder/'steps'/f'{role}-attempt-1-output.json'
                draft=None
                if prior_output.exists():
                    bad=json.loads(prior_output.read_text())
                    prefix=bad['output_text'].encode()[:12000].decode('utf-8',errors='ignore')
                    draft={'text':prefix,'truncated':bad['output_truncated'] or len(prefix)<len(bad['output_text'])}
                expected_retry=prompt_for(role,report['ticker'],report['as_of_date'],report['question'],
                                         chosen[role],prior,feedback,draft,prompt_version=version)
                if messages!=expected_retry:
                    raise ValueError('Repair draft differs from preceding actual output')
        if step.get('input_sha256')!=data['input_sha256']:
            raise ValueError('Validated role result is not bound to its actual final input')
        output=json.loads((folder/'steps'/f'{role}-attempt-{attempts[-1]}-output.json').read_text())
        if (output.get('model')!=report['model'] or output.get('prompt_version')!=report['prompt_version']
            or output.get('output_truncated') is not False or json.loads(output['output_text'])!=step['result']):
            raise ValueError('Validated result differs from actual final model output')
        usage=[u for u in report['model_usage'] if u.get('role')==role and u.get('attempt')==attempts[-1]]
        if (len(usage)!=1 or usage[0].get('status')!='completed'
            or usage[0].get('response_id')!=output.get('response_id')):
            raise ValueError('Final output is not bound to a completed model response')
        previous[role]=report['analyses'][role]
    if total!=report['model_attempts']:
        raise ValueError('Model attempt count differs from persisted role inputs')
    return chosen,plan
