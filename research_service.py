"""Shared research operations used by the HTTP app and the durable queue worker."""

import json
import re
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import evidence_repository as domain
import run_repository as tasks
from chatgpt_auth import ChatGPTAuth
from company_identity import CompanyError, normalize_ticker, resolve_company
from research_pipeline import run_research, saved_report
from sources.sec import SourceError


TERMINAL = {'completed','completed_with_gaps','failed','interrupted','cancelled'}
STATUSES = TERMINAL | {'queued','running'}
PROJECT_ROOT = Path(__file__).resolve().parent


class ServiceError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def valid_id(run_id):
    if not re.fullmatch(r'[0-9a-f]{32}',run_id):
        raise ServiceError(400,'invalid_run_id','Run ID must be 32 lowercase hexadecimal characters')


def validate_request(ticker, as_of_date, question, refresh=False, news_days=14, deadline=300):
    companies = json.loads((PROJECT_ROOT/'config/companies.json').read_text())
    try:
        ticker = normalize_ticker(ticker)
    except CompanyError as exc:
        raise ServiceError(400, exc.code, str(exc)) from None
    try:
        cutoff = date.fromisoformat(as_of_date)
    except (ValueError,TypeError):
        raise ServiceError(422,'invalid_date','Use an ISO date: YYYY-MM-DD') from None
    if cutoff > datetime.now(ZoneInfo('Asia/Shanghai')).date():
        raise ServiceError(422,'future_date','Research cutoff cannot be in the future')
    if not question.strip() or len(question)>1200 or any(ord(c)<32 and c not in '\n\t' for c in question):
        raise ServiceError(422,'invalid_question','Question must contain 1–1200 printable characters')
    if not 1<=news_days<=30 or not 1<=deadline<=1800:
        raise ServiceError(422,'invalid_budget','News window 1–30 days; deadline 1–1800 seconds')
    return dict(ticker=ticker,cik=companies.get(ticker, {}).get('cik'),as_of_date=cutoff.isoformat(),question=question.strip(),
                refresh=refresh,news_days=news_days,deadline=deadline)


class ResearchService:
    def __init__(self, root, config, *, schema='public', runner=None, transports=None, fault_hook=None):
        self.root = Path(root).resolve()
        self.config = dict(config)
        # Formal queued research always imports through the same database, even if an empty
        # environment value was used for an earlier run-local / collect diagnostic.
        self.config['DATABASE_URL'] = self.config.get('DATABASE_URL') or domain.DEFAULT_DATABASE
        self.schema = schema
        self.runner = runner
        self.transports = transports or {}
        self.fault_hook = fault_hook or (lambda event, **details: None)

    def connection(self):
        return domain.connect(self.config.get('DATABASE_URL') or domain.DEFAULT_DATABASE,schema=self.schema)

    def create(self, *, idempotency_key=None, **request):
        from recoverable_pipeline import configuration_fingerprint
        request = validate_request(**request)
        billing = 'chatgpt_plan' if self.config['AETERNITAS_AUTH']=='chatgpt' else 'api_key'
        model = self.config['AETERNITAS_MODEL']
        if model == 'auto' and billing == 'chatgpt_plan':
            model = ChatGPTAuth().status().get('model')
        if not model:
            raise ServiceError(503,'model_not_configured','Run aeternitas.py login and select a model first')
        # Resolve outside the database transaction and before any model work.
        identity = self.resolve(request['ticker'])
        request.update(ticker=identity['ticker'], cik=identity['cik'])
        key=idempotency_key or uuid4().hex
        rid=uuid4().hex
        try:
            with self.connection() as connection:
                row = tasks.create(connection,rid,request,self.root,billing,model,
                                   idempotency_key=key,workflow_config_fingerprint=configuration_fingerprint(), company_identity=identity)
        except tasks.RepoConflict as exc:
            raise ServiceError(409,'idempotency_conflict',str(exc)) from None
        except ValueError as exc:
            raise ServiceError(422,'invalid_idempotency_key',str(exc)) from None
        row.update(idempotency_key=key,reused=row['run_id']!=rid)
        return self.links(row)

    def resolve(self, ticker, *, refresh=False):
        overrides = json.loads((PROJECT_ROOT/'config/companies.json').read_text())
        try:
            return resolve_company(ticker, self.root, self.config.get('SEC_CONTACT'), overrides=overrides,
                                   transport=self.transports.get('company_directory'), refresh=refresh)
        except CompanyError as exc:
            status = 503 if exc.code in {'company_lookup_unavailable', 'sec_contact_missing', 'invalid_company_directory'} else 400
            raise ServiceError(status, exc.code, str(exc)) from None

    @staticmethod
    def links(row):
        rid = row['run_id']
        options = row.pop('options', {})
        if options.get('company_identity'):
            row['company_identity'] = options['company_identity']
        for step in row.get('steps',[]):
            # Paths include the private fencing token. Callers can inspect
            # checkpoint hashes and status without learning worker ownership.
            step['checkpoint_saved']=bool(step.get('artifact_path') and step.get('artifact_sha256'))
            step.pop('artifact_path',None)
        row['model_usage']=[{**{k:u[k] for k in ('role','attempt','status','elapsed_seconds') if k in u},
                              'usage':({k:u['usage'][k] for k in ('input_tokens','output_tokens','total_tokens')
                                        if k in u['usage']} if isinstance(u.get('usage'),dict) else None)}
                            for u in row.get('model_usage',[])]
        row['links'] = {name:f'/research-runs/{rid}/{suffix}' for name,suffix in
                        [('report_html','report.html'),('report_json','report.json'),('evidence','evidence')]}
        row['links']['status'] = f'/research-runs/{rid}'
        return row

    def listing(self, *, ticker=None, status=None, limit=50, offset=0):
        if ticker is not None:
            try:
                ticker=normalize_ticker(ticker)
            except CompanyError as exc:
                raise ServiceError(400,exc.code,str(exc)) from None
        if status is not None and status not in STATUSES:
            raise ServiceError(422,'invalid_status','Unknown status filter')
        with self.connection() as connection:
            return [self.links(row) for row in tasks.listing(connection,ticker=ticker,status=status,limit=limit,offset=offset,root=self.root)]

    def get(self, run_id):
        valid_id(run_id)
        with self.connection() as connection:
            row = tasks.get(connection,run_id,root=self.root)
        if row is None:
            raise ServiceError(404,'run_not_found','Research run does not exist')
        return self.links(row)

    def cancel(self, run_id):
        valid_id(run_id)
        with self.connection() as connection:
            row=tasks.request_cancel(connection,run_id,root=self.root)
        if row is None:
            raise ServiceError(404,'run_not_found','Research run does not exist')
        return self.links(row)

    def resume(self, run_id, *, acknowledge_unknown=False):
        from recoverable_pipeline import configuration_fingerprint
        valid_id(run_id)
        try:
            with self.connection() as connection:
                tasks.recover_expired(connection,self.root)
                row=tasks.resume(connection,run_id,root=self.root,workflow_config_fingerprint=configuration_fingerprint(),
                                 acknowledge_unknown=acknowledge_unknown)
        except (tasks.RepoConflict,tasks.OwnershipError,tasks.AttemptBudgetError) as exc:
            raise ServiceError(409,'resume_refused',str(exc)) from None
        if row is None:
            raise ServiceError(404,'run_not_found','Research run does not exist')
        return self.links(row)

    def report(self, run_id):
        row = self.get(run_id)
        if row['status'] not in {'completed','completed_with_gaps'}:
            raise ServiceError(409,'report_not_ready',f"Report unavailable; run status is {row['status']}")
        try:
            folder = self.root/'runs'/run_id
            for name in ('state.json','report.html','report.json','input_bundle.json','published.json'):
                if not (folder/name).resolve().is_relative_to(self.root/'runs'):
                    raise SourceError('Publication path escapes run storage')
            folder,report = saved_report(self.root,run_id)
        except (SourceError,ValueError,OSError,KeyError):
            raise ServiceError(409,'report_integrity_failed','Saved report failed integrity checks; publication withheld') from None
        if report.get('dataset_id') != row['dataset_id']:
            raise ServiceError(409,'dataset_mismatch','Report and database dataset identities differ')
        for key in ('run_id','ticker','cik','as_of_date','question','model','billing_mode'):
            value=row[key].isoformat() if hasattr(row[key],'isoformat') else row[key]
            if report.get(key)!=value:
                raise ServiceError(409,'task_report_mismatch','Report and persisted task identities differ')
        return folder,report

    def evidence(self, run_id, evidence_id=None):
        row = self.get(run_id)
        if not row['dataset_id']:
            raise ServiceError(409,'evidence_not_ready','Research inputs have not been imported yet')
        with self.connection() as connection:
            if evidence_id is None:
                rows = connection.execute('''SELECT ev.payload FROM dataset_evidence de
                  JOIN evidence_versions ev ON ev.version_id=de.version_id AND ev.evidence_id=de.evidence_id
                  WHERE de.dataset_id=%s ORDER BY ev.evidence_id''',(row['dataset_id'],)).fetchall()
                return [r['payload'] for r in rows]
            try:
                result = domain.evidence(connection,row['dataset_id'],evidence_id)
            except ValueError:
                result = None
        if result is None:
            raise ServiceError(404,'evidence_not_found','Evidence ID is not present in this research dataset')
        return result

    def execute(self, task):
        rid = task['run_id']
        token=task['owner_token']
        previous=None
        def observe(state):
            nonlocal previous
            with self.connection() as connection:
                # Compatibility for existing injected M3/M4 controlled runners. Formal
                # service execution uses the isolated M5 engine below.
                current=tasks.get(connection,rid)
                while current['model_attempts']<state.get('model_attempts',0):
                    tasks.reserve_attempt(connection,rid,token,state['step'].removeprefix('model:'))
                    current=tasks.get(connection,rid)
                for usage in state.get('model_usage',[]):
                    pending=next((a for a in current['attempts'] if a['role']==usage['role'] and
                                  a['role_attempt']==usage['attempt'] and a['status']=='reserved'),None)
                    if pending:
                        tasks.record_attempt(connection,rid,token,usage['role'],usage['attempt'],usage)
                tasks.observe(connection,rid,state,owner_token=token)
            mark=(state['step'],state['status'],state.get('model_attempts',0))
            if mark!=previous:
                print(f'run={rid} step={mark[0]} status={mark[1]} model_attempts={mark[2]}',flush=True)
                previous=mark
        try:
            config=dict(self.config)
            billing='chatgpt_plan' if config['AETERNITAS_AUTH']=='chatgpt' else 'api_key'
            if billing != task['billing_mode']:
                raise SourceError('Billing configuration changed after submission; no automatic provider switch')
            config['AETERNITAS_MODEL']=task['model']
            auth=None
            if billing=='chatgpt_plan':
                auth=ChatGPTAuth()
                # Catalog refresh and token renewal occur inside the renewable lease.
            if self.runner is None:
                from recoverable_pipeline import execute_recoverable
                execute_recoverable(self,task,config,auth)
                return
            options=task['options']
            self.runner(self.root,task['ticker'],task['cik'],task['as_of_date'],task['question'],config,
                        refresh=options['refresh'],news_days=options['news_days'],seconds=options['deadline'],
                        run_id=rid,state_observer=observe,plan_auth=auth)
            _,report=saved_report(self.root,rid)
            with self.connection() as connection:
                observe(dict(step='finished',status='running',model_attempts=report['model_attempts'],
                             model_usage=report['model_usage'],dataset_id=report['dataset_id']))
                # Explicitly mark only the artifacts just validated by saved_report.
                from analysis_workflow import digest
                from sources.sec import write_json
                folder=self.root/'runs'/rid/'owners'/token
                folder.mkdir(parents=True,exist_ok=True)
                for step in tasks.get(connection,rid)['steps']:
                    path=folder/f"{step['step']}-legacy-checked.json"
                    write_json(path,{'report_sha256':digest(report),'step':step['step']})
                    import hashlib
                    tasks.checkpoint(connection,rid,token,step['step'],path.relative_to(self.root).as_posix(),
                                     hashlib.sha256(path.read_bytes()).hexdigest(),task['workflow_config_fingerprint'])
                tasks.finish(connection,rid,report['status'],owner_token=token)
        except BaseException as exc:
            # Unknown exceptions (including database connection failures) never leak their text.
            message=str(exc) if isinstance(exc,SourceError) else type(exc).__name__
            for key in ('OPENAI_API_KEY','FINNHUB_API_KEY','SEC_CONTACT','DATABASE_URL'):
                secret=self.config.get(key)
                if secret: message=message.replace(secret,'[redacted]')
            status='interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed'
            try:
                with self.connection() as connection:
                    tasks.recover_expired(connection,self.root)
                    row=tasks.get(connection,rid)
                    if row and row['status']=='running':
                        tasks.finish(connection,rid,status,error=message[:500],owner_token=token)
            except (tasks.OwnershipError,SourceError):
                # Cancellation, a newer owner or an unavailable database must never
                # be overwritten by the old worker's error handler.
                pass
            if status=='interrupted':
                raise

