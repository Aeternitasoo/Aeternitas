"""M5 execution: durable attempts, immutable checkpoints and fenced publication.

Provider calls never run in a database transaction. A lost owner can write only
its private generation. Only a live owner holding the final row lock may promote
the already verified, finite publication files.
"""
import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import evidence_repository as domain
import run_repository as tasks
from analysis_contracts import ROLES, PROMPT_VERSION
from analysis_workflow import (REPORT_SCHEMA_VERSION, analysis_checks, build_analysis_plan,
                               digest, publication_files)
from research_analysis import ResponsesAnalyst, prompt_for, validate_result
from research_inputs import collect_inputs
from research_pipeline import audit_inputs, validate_report_directory
from research_report import render_report
from research_runtime import utc_now
from sources.sec import SourceError, write_atomic, write_json

EXECUTION_VERSION = 'durable-research-m5-v1'
PROJECT = Path(__file__).resolve().parent


def configuration_fingerprint():
    files = ['analysis_contracts.py', 'analysis_workflow.py', 'evidence_selection.py',
             'financial_facts.py', 'filing_source.py', 'news_source.py', 'research_inputs.py',
             'research_analysis.py', 'chatgpt_stream.py', 'recoverable_pipeline.py']
    files += [p.relative_to(PROJECT).as_posix() for p in sorted((PROJECT/'prompts').rglob('*.md'))]
    return digest({'execution': EXECUTION_VERSION,
                   'files': {name: hashlib.sha256((PROJECT/name).read_bytes()).hexdigest() for name in files}})


def _read(path, max_bytes=2_000_000):
    raw = path.read_bytes()
    if len(raw)>max_bytes:
        raise SourceError('Checkpoint exceeds its byte budget')
    return json.loads(raw)


class Execution:
    allow_uncertain_retry = False

    def __init__(self, service, task):
        self.service, self.task = service, task
        self.rid, self.token = task['run_id'], task['owner_token']
        self.root = service.root
        self.folder = self.root/'runs'/self.rid/'owners'/self.token
        self.folder.mkdir(parents=True, exist_ok=False)
        self.calls = task['model_attempts']
        self.deadline_at = task['deadline_at']
        self._last_check = 0.0
        self._stopped = None
        self._closed = threading.Event()
        self._heartbeat = threading.Thread(target=self._renew, name='research-lease', daemon=True)
        self.state = dict(run_id=self.rid, ticker=task['ticker'], as_of_date=task['as_of_date'].isoformat(),
                          model=task['model'], billing_mode=task['billing_mode'], prompt_version=PROMPT_VERSION,
                          status='running', step='starting', model_attempts=self.calls, started_at=utc_now(),
                          execution_version=EXECUTION_VERSION)

    def __enter__(self):
        self.check(force=True)
        self._heartbeat.start()
        return self

    def __exit__(self, *_):
        self._closed.set()
        self._heartbeat.join(timeout=6)

    def _renew(self):
        while not self._closed.wait(5):
            try:
                with self.service.connection() as connection:
                    tasks.renew(connection, self.rid, self.token,
                                lease_seconds=self.task.get('lease_seconds',30))
            except Exception as exc:
                self._stopped = exc
                return

    def check(self, *, force=False):
        if self._stopped is not None:
            raise SourceError('Research stopped: ownership, cancellation, deadline or database unavailable')
        if force or time.monotonic()-self._last_check>=.2:
            try:
                with self.service.connection() as connection:
                    tasks.assert_owner(connection, self.rid, self.token)
            except Exception as exc:
                self._stopped = exc
                raise SourceError('Research stopped: ownership, cancellation, deadline or database unavailable') from None
            self._last_check = time.monotonic()
        left = (self.deadline_at-datetime.now(timezone.utc)).total_seconds()
        if left<=0:
            raise SourceError('Original research deadline expired; no further calls or publication')
        return left

    def remaining(self):
        return self.check()

    def pause(self, seconds):
        if seconds>=self.remaining():
            raise SourceError('Retry wait exceeds remaining research deadline')
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            self.check()
            self._closed.wait(min(.1, max(0, until-time.monotonic())))

    def model_attempt(self, role):
        self.check(force=True)
        with self.service.connection() as connection:
            attempt=tasks.reserve_attempt(connection, self.rid, self.token, role)
        self.calls=attempt['total_ordinal']
        self.save(role)

    def usage(self, usage):
        self.check(force=True)
        with self.service.connection() as connection:
            tasks.record_attempt(connection, self.rid, self.token, usage['role'], usage['attempt'], usage)

    def save(self, step):
        self.check(force=True)
        self.state.update(step=step, updated_at=utc_now(), model_attempts=self.calls)
        write_json(self.folder/'state.json', self.state)
        with self.service.connection() as connection:
            tasks.observe(connection, self.rid, self.state, owner_token=self.token)
        print(f'run={self.rid} step={step} model_attempts={self.calls}', flush=True)

    def checkpoint(self, step, document, fingerprint):
        self.check(force=True)
        path=self.folder/'checkpoints'/f'{step}.json'
        write_json(path, document)
        with self.service.connection() as connection:
            tasks.checkpoint(connection, self.rid, self.token, step, path.relative_to(self.root).as_posix(),
                             hashlib.sha256(path.read_bytes()).hexdigest(), fingerprint)
        self.service.fault_hook('after_checkpoint', role=step, run_id=self.rid)

    def load_checkpoint(self, row, fingerprint):
        if row.get('workflow_fingerprint')!=fingerprint:
            raise SourceError('Checkpoint workflow fingerprint differs; reuse refused')
        path=(self.root/row['artifact_path']).resolve()
        owners=(self.root/'runs'/self.rid/'owners').resolve()
        if not path.is_relative_to(owners):
            raise SourceError('Checkpoint escaped this research owner storage')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['artifact_sha256']:
            raise SourceError('Checkpoint fingerprint differs; reuse refused')
        return _read(path), path.parent.parent

    def file_manifest(self, names):
        return {name: hashlib.sha256((self.folder/name).read_bytes()).hexdigest() for name in names}

    def restore_files(self, document, source, allowed):
        if not isinstance(document.get('files'), dict) or set(document['files'])!=set(allowed):
            raise SourceError('Checkpoint artifact list differs from its finite contract')
        for name, sha in document['files'].items():
            path=(source/name).resolve()
            if not path.is_relative_to(source.resolve()):
                raise SourceError('Checkpoint artifact path escaped owner storage')
            raw=path.read_bytes()
            if len(raw)>2_000_000 or hashlib.sha256(raw).hexdigest()!=sha:
                raise SourceError('Checkpoint artifact bytes differ from verified checkpoint')
            write_atomic(self.folder/name, raw)

    def row(self):
        with self.service.connection() as connection:
            return tasks.get(connection, self.rid)


def _role_names(folder, role):
    names=[f'steps/{role}.json']
    for attempt in (1,2):
        for kind in ('input','output','error'):
            name=f'steps/{role}-attempt-{attempt}-{kind}.json'
            if (folder/name).is_file(): names.append(name)
    return names


def _previous_attempts(execution, role, selected, analyses):
    """Preserve consumed requests as untrusted retry material, never as checkpoints."""
    with execution.service.connection() as connection:
        attempts=connection.execute('SELECT * FROM run_attempts WHERE run_id=%s AND role=%s ORDER BY role_attempt',
                                    (execution.rid,role)).fetchall()
    for record in attempts:
        number=record['role_attempt']
        source=execution.root/'runs'/execution.rid/'owners'/str(record['owner_token'])/'steps'
        for kind in ('input','output','error'):
            name=f'{role}-attempt-{number}-{kind}.json'
            path=source/name
            if path.exists():
                data=_read(path, 130000)
                write_json(execution.folder/'steps'/name, data)
        path=execution.folder/'steps'/f'{role}-attempt-{number}-input.json'
        if not path.exists():
            raise SourceError('Consumed model attempt has no saved input; cannot reconstruct provenance')
        data=_read(path,130000)
        messages=data.get('input')
        if (not isinstance(messages,list) or len(messages)!=2 or
            any(not isinstance(m,dict) or set(m)!={'role','content'} or not isinstance(m['content'],str) for m in messages)
            or [m['role'] for m in messages]!=['developer','user']):
            raise SourceError('Previous attempted input has unexpected instruction messages')
        expected=prompt_for(role,execution.task['ticker'],execution.task['as_of_date'].isoformat(),
                            execution.task['question'],selected[role],analyses)
        payload=json.loads(messages[1]['content'])
        clean={k:v for k,v in payload.items() if k not in {'previous_attempt_validation_error',
              'previous_attempt_output_untrusted','previous_attempt_output_omitted'}}
        if (data.get('model')!=execution.task['model'] or data.get('input_sha256')!=digest(messages)
            or messages[0]!=expected[0] or clean!=json.loads(expected[1]['content'])):
            raise SourceError('Previous attempted input differs from current frozen evidence and prior results')
        error=execution.folder/'steps'/f'{role}-attempt-{number}-error.json'
        if not error.exists():
            write_json(error, {'error':'Previous call has no committed verified checkpoint; explicit recovery may consume another call',
                              'phase':'recovery_uncommitted', 'output_file':None})
    return len(attempts)


def execute_recoverable(service, task, config, auth):
    if task.get('workflow_config_fingerprint')!=configuration_fingerprint():
        raise SourceError('Queued workflow configuration changed; create a new research request')
    with Execution(service, task) as ex:
        if auth is not None:
            if auth.choose_model(task['model'])!=task['model']:
                raise SourceError('Queued model is unavailable; no automatic model switch')
            ex.check(force=True)
        rows={r['step']:r for r in ex.row()['steps'] if r['status']=='completed' and r.get('artifact_path')}
        input_fp=digest([task['request_fingerprint'], task['workflow_config_fingerprint'], 'inputs'])
        frozen_input=next((rows[s] for s in ('data_import','company_news','filing_text','financials') if s in rows),None)
        if frozen_input is not None:
            checkpoint, source=ex.load_checkpoint(frozen_input, input_fp)
            ex.restore_files(checkpoint, source, ['input_bundle.json','analysis_plan.json'])
            inputs=_read(ex.folder/'input_bundle.json')
            audit_inputs(service.root, inputs)
            selected,plan=build_analysis_plan(inputs,task['model'],task['billing_mode'])
            if _read(ex.folder/'analysis_plan.json')!=plan:
                raise SourceError('Frozen input plan differs from current analysis configuration')
            if any(inputs.get(k)!=v for k,v in [('ticker',task['ticker']),('cik',task['cik']),
                   ('as_of_date',task['as_of_date'].isoformat()),('question',task['question'])]):
                raise SourceError('Frozen input identity differs from the persisted research task')
            ex.state['dataset_id']=checkpoint['dataset_id']
            # Any one of these checkpoints was written only after the whole
            # audited bundle committed. A crash between their short commits
            # must not silently refresh those already frozen inputs.
            for step in ('financials','filing_text','company_news','data_import'):
                if step not in rows:
                    ex.checkpoint(step,checkpoint,input_fp)
        else:
            inputs=collect_inputs(service.root,task['ticker'],task['cik'],task['as_of_date'],task['question'],config,
                       refresh=task['options']['refresh'], news_days=task['options']['news_days'],budget=ex,
                       transports=service.transports,progress=ex.save,
                       evidence_kind='controlled_test' if service.transports else 'live_api')
            audit_inputs(service.root,inputs)
            write_json(ex.folder/'input_bundle.json',inputs)
            ex.save('data_import')
            with service.connection() as connection:
                ex.state['dataset_id']=domain.import_bundle(connection,service.root,inputs)
            selected,plan=build_analysis_plan(inputs,task['model'],task['billing_mode'])
            write_json(ex.folder/'analysis_plan.json',plan)
            document={'dataset_id':ex.state['dataset_id'],
                      'files':ex.file_manifest(['input_bundle.json','analysis_plan.json'])}
            # A source phase becomes reusable only after the complete bundle was audited and imported.
            for step in ('financials','filing_text','company_news','data_import'):
                ex.checkpoint(step,document,input_fp)
        ex.state['workflow_fingerprint']=plan['workflow_fingerprint']
        analyses={}
        with ResponsesAnalyst(config.get('OPENAI_API_KEY') if task['billing_mode']=='api_key' else None,
                task['model'],ex.folder/'steps',ex,transport=service.transports.get('model'),plan_auth=auth,
                usage_observer=ex.usage,fault_hook=service.fault_hook) as analyst:
            for role in ROLES:
                fingerprint=digest([plan['workflow_fingerprint'],role,analyses])
                if role in rows:
                    document,source=ex.load_checkpoint(rows[role],fingerprint)
                    names=list(document.get('files',{}))
                    allowed={f'steps/{role}.json'} | {f'steps/{role}-attempt-{a}-{k}.json'
                                                    for a in (1,2) for k in ('input','output','error')}
                    if not set(names)<=allowed or f'steps/{role}.json' not in names:
                        raise SourceError('Role checkpoint artifact names differ from role scope')
                    ex.restore_files(document,source,names)
                    result=_read(ex.folder/'steps'/f'{role}.json')['result']
                    validate_result(result,selected[role])
                    analyses[role]=result
                    ex.check(force=True)
                    continue
                start=_previous_attempts(ex,role,selected,analyses)
                if start>=2:
                    raise SourceError('This role already consumed both attempts; create a new research request')
                ex.save(role)
                analyses[role]=analyst.analyze(role,task['ticker'],task['as_of_date'].isoformat(),task['question'],
                                              selected[role],analyses.copy(),attempt_start=start)
                ex.checkpoint(role,{'files':ex.file_manifest(_role_names(ex.folder,role))},fingerprint)
        ex.save('validation')
        row=ex.row()
        usage=[a['usage'] for a in row['attempts'] if a.get('usage')]
        if len(usage)!=ex.calls:
            raise SourceError('Durable model usage does not cover all consumed attempts')
        summary=inputs['financials']
        status='completed_with_gaps' if (inputs['source_gaps'] or summary['warnings'] or
              not any(e['kind']=='news' for e in inputs['evidence']) or any(a['gaps'] for a in analyses.values())) else 'completed'
        limitations=list(summary['limitations'])+list(summary['warnings'])+inputs['source_gaps']+[
            inputs['news_coverage']['limitation'],
            'Filing excerpts cover at most two filings / two selected sections each; not a full company audit.',
            'Citation and numeric checks do not certify semantic entailment; review important claims manually.',
            'Cancellation cannot recall an accepted provider request; unconfirmed calls may have incurred unknown cost.',
            'Resume preserves the original deadline and the cumulative limit of two attempts per role and eight per run.']
        report={**inputs,'schema_version':REPORT_SCHEMA_VERSION,'run_id':ex.rid,'created_at':utc_now(),
                'status':status,'model':task['model'],'billing_mode':task['billing_mode'],
                'dataset_id':ex.state['dataset_id'],'prompt_version':PROMPT_VERSION,'analyses':analyses,
                'limitations':limitations,'model_attempts':ex.calls,'model_usage':usage,'cost':'unknown',
                'analysis_provenance':plan,'analysis_checks':analysis_checks(analyses,selected),
                'execution_version':EXECUTION_VERSION,'resume_count':task['resume_count']}
        audit_inputs(service.root,report)
        write_atomic(ex.folder/'report.html',render_report(report).encode())
        write_json(ex.folder/'report.json',report)
        write_json(ex.folder/'published.json',{'files':ex.file_manifest(publication_files(ex.folder)), 'published_at':utc_now()})
        ex.state.update(status=status,step='finished',model_attempts=ex.calls,model_usage=usage,
                        report_html='report.html',report_json='report.json')
        write_json(ex.folder/'state.json',ex.state)
        validate_report_directory(service.root,ex.folder,ex.rid)
        validation_fp=digest([plan['workflow_fingerprint'],'validation',analyses])
        if 'validation' not in rows:
            ex.checkpoint('validation',{'files':ex.file_manifest(publication_files(ex.folder))},validation_fp)
        else:
            ex.load_checkpoint(rows['validation'],validation_fp)
        service.fault_hook('before_publication',run_id=ex.rid)
        ex.check(force=True)
        canonical=service.root/'runs'/ex.rid
        names=publication_files(ex.folder)+['state.json','published.json']
        prepared=ex.folder/'promotion'
        # Flush all file data before taking the publication row lock. The
        # fenced transaction contains only finite renames and the DB commit.
        for name in names:
            write_atomic(prepared/name,(ex.folder/name).read_bytes())
        ex.check(force=True)
        with service.connection() as connection:
            with tasks.publication_guard(connection,ex.rid,ex.token):
                # Readers require completed DB status. Each file is atomic, manifest last;
                # files and PostgreSQL are not a distributed atomic transaction.
                (canonical/'published.json').unlink(missing_ok=True)
                (canonical/'steps').mkdir(exist_ok=True)
                for name in names:
                    os.replace(prepared/name,canonical/name)
                for directory in (canonical/'steps',canonical):
                    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                service.fault_hook('after_publication_files',run_id=ex.rid)
                tasks.finish(connection,ex.rid,status,owner_token=ex.token)
        return ex.rid,report
