"""Short queue transactions. Connections and checkpoint byte validation belong to callers.

Formal workers must pass owner_token. Omission is a historical controlled-runner
adapter only; it is not an authorization mechanism for the HTTP service.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import PurePosixPath
import re
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

PUBLIC = '''run_id,ticker,cik,as_of_date,question,billing_mode,model,status,step,
model_attempts,model_usage,dataset_id,error,options,created_at,updated_at,started_at,finished_at,
cancel_requested,deadline_at,resume_count,lease_until,request_fingerprint,workflow_config_fingerprint'''
ROLES=('fundamentals','events','risk','synthesis')
TERMINAL={'completed','completed_with_gaps','failed','interrupted','cancelled'}

class RepoConflict(ValueError): pass
class OwnershipError(ValueError): pass
class RunCancelled(OwnershipError): pass
class RunDeadline(OwnershipError): pass
class AttemptBudgetError(ValueError): pass

def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()

def _sha(value,name):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value): raise ValueError(f'Invalid {name} fingerprint')
    return value

def _token(value):
    try: return str(UUID(str(value)))
    except (ValueError,TypeError,AttributeError): raise OwnershipError('Invalid owner token') from None

def _lease(value):
    if type(value) not in (int,float) or not 0<value<=600: raise ValueError('Lease seconds must be positive and at most 600')
    return value

def create(connection,run_id,request,root,billing,model,*,idempotency_key=None,request_fingerprint=None,workflow_config_fingerprint=None,company_identity=None):
    if idempotency_key is not None and (not isinstance(idempotency_key,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',idempotency_key)):
        raise ValueError('Invalid idempotency key')
    fingerprint=_sha(request_fingerprint or _digest({'request':request,'billing':billing,'model':model,'workflow_config_fingerprint':workflow_config_fingerprint}),'request')
    if workflow_config_fingerprint is not None: _sha(workflow_config_fingerprint,'workflow config')
    with connection.transaction():
        row=connection.execute('''INSERT INTO research_runs
          (run_id,ticker,cik,as_of_date,question,options,data_root,billing_mode,model,idempotency_key,request_fingerprint,workflow_config_fingerprint)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT (data_root,idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING run_id''',
          (run_id,request['ticker'],request['cik'],request['as_of_date'],request['question'],Jsonb({**{k:request[k] for k in ('refresh','news_days','deadline')}, **({'company_identity':company_identity} if company_identity else {})}),
           str(root),billing,model,idempotency_key,fingerprint,workflow_config_fingerprint)).fetchone()
        if row is None:
            row=connection.execute('SELECT run_id,request_fingerprint FROM research_runs WHERE data_root=%s AND idempotency_key=%s',(str(root),idempotency_key)).fetchone()
            if row['request_fingerprint']!=fingerprint: raise RepoConflict('Idempotency key already belongs to different research inputs')
        identity=row['run_id']
    return get(connection,identity)

def get(connection,run_id,*,root=None):
    where='run_id=%s';values=[run_id]
    if root is not None: where+=' AND data_root=%s';values.append(str(root))
    row=connection.execute(f'SELECT {PUBLIC} FROM research_runs WHERE {where}',values).fetchone()
    if row is not None:
        row['steps']=connection.execute('''SELECT step,ordinal,status,model_attempts,error,started_at,finished_at,
          artifact_path,artifact_sha256,workflow_fingerprint,verified_at FROM run_steps WHERE run_id=%s ORDER BY ordinal''',(run_id,)).fetchall()
        row['attempts']=connection.execute('''SELECT role,role_attempt,total_ordinal,status,usage,started_at,finished_at
          FROM run_attempts WHERE run_id=%s ORDER BY total_ordinal''',(run_id,)).fetchall()
    return row

def listing(connection,*,ticker=None,status=None,limit=50,offset=0,root=None,data_root=None):
    if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0: raise ValueError('Invalid list bounds')
    clauses=[];values=[]
    for column,value in (('data_root',data_root if data_root is not None else root),('ticker',ticker),('status',status)):
        if value is not None: clauses.append(f'{column}=%s');values.append(str(value))
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    return connection.execute(f'SELECT {PUBLIC} FROM research_runs{where} ORDER BY created_at DESC,run_id DESC LIMIT %s OFFSET %s',[*values,limit,offset]).fetchall()

def claim(connection,root,worker_id,lease_seconds=30,*,run_id=None):
    _lease(lease_seconds)
    with connection.transaction():
        # Serialize only the millisecond-scale capacity check plus claim. This
        # lock is released before any source/model work or publication begins.
        connection.execute('SELECT pg_advisory_xact_lock(71002105)')
        active=connection.execute("""SELECT count(*) AS n FROM research_runs WHERE data_root=%s AND status='running'
          AND lease_until>clock_timestamp() AND deadline_at>clock_timestamp()""",(str(root),)).fetchone()['n']
        if active>=2: return None
        where="status='queued' AND data_root=%s AND NOT cancel_requested"
        values=[str(root)]
        if run_id is not None: where+=' AND run_id=%s';values.append(run_id)
        row=connection.execute(f'SELECT * FROM research_runs WHERE {where} ORDER BY created_at,run_id FOR UPDATE SKIP LOCKED LIMIT 1',values).fetchone()
        if row is None: return None
        row=connection.execute("""UPDATE research_runs SET status='running',step='starting',worker_id=%s,owner_token=%s,
          lease_until=clock_timestamp()+(%s*interval '1 second'),
          deadline_at=coalesce(deadline_at,clock_timestamp()+((options->>'deadline')::int*interval '1 second')),
          started_at=coalesce(started_at,clock_timestamp()),finished_at=NULL,updated_at=clock_timestamp()
          WHERE run_id=%s RETURNING *""",(worker_id,uuid4(),lease_seconds,row['run_id'])).fetchone()
        row['owner_token']=str(row['owner_token'])
        row['lease_seconds']=lease_seconds
    return row

def _owner(connection,run_id,owner_token,*,lock=False,allow_cancel=False,compatibility=False):
    row=connection.execute('SELECT * FROM research_runs WHERE run_id=%s'+(' FOR UPDATE' if lock else ''),(run_id,)).fetchone()
    if row is None: raise OwnershipError('Research run does not exist')
    if row['cancel_requested'] and not allow_cancel: raise RunCancelled('Research cancellation requested')
    if row['status']!='running': raise OwnershipError('Research run is not running')
    if owner_token is None and compatibility: owner_token=row['owner_token']
    if row['owner_token'] is None or _token(owner_token)!=str(row['owner_token']): raise OwnershipError('Research owner is stale')
    # Evaluate time after acquiring a row lock, not in the SELECT target list
    # before LockRows can wait. Otherwise a waiter could revive an expired lease.
    now=connection.execute('SELECT clock_timestamp() AS now').fetchone()['now']
    if row['lease_until'] is None or row['lease_until']<=now: raise OwnershipError('Research ownership lease expired')
    if row['deadline_at'] is not None and row['deadline_at']<=now: raise RunDeadline('Research deadline exceeded; no further calls or publication')
    row['owner_token']=str(row['owner_token'])
    return row

def assert_owner(connection,run_id,owner_token,*,allow_cancel=False):
    return _owner(connection,run_id,owner_token,allow_cancel=allow_cancel)

def renew(connection,run_id,owner_token,lease_seconds=30):
    _lease(lease_seconds)
    with connection.transaction():
        _owner(connection,run_id,owner_token,lock=True)
        return connection.execute("""UPDATE research_runs SET lease_until=clock_timestamp()+(%s*interval '1 second'),updated_at=clock_timestamp()
          WHERE run_id=%s RETURNING lease_until""",(lease_seconds,run_id)).fetchone()

def observe(connection,run_id,state,*,owner_token=None):
    step=state['step'].removeprefix('model:')
    if step=='finished': step='validation'
    failed=state['status']=='failed';count=state.get('model_attempts',0)
    if type(count) is not int or not 0<=count<=8: raise AttemptBudgetError('Global model attempt limit is eight')
    with connection.transaction():
        row=_owner(connection,run_id,owner_token,lock=True,compatibility=owner_token is None)
        connection.execute('''INSERT INTO run_steps(run_id,step,ordinal,status,model_attempts,error,finished_at,owner_token)
          VALUES (%s,%s,(SELECT coalesce(max(ordinal),0)+1 FROM run_steps WHERE run_id=%s),%s,%s,%s,CASE WHEN %s THEN clock_timestamp() END,%s)
          ON CONFLICT(run_id,step) DO UPDATE SET
            status=CASE WHEN run_steps.status='completed' THEN run_steps.status ELSE excluded.status END,
            model_attempts=greatest(run_steps.model_attempts,excluded.model_attempts),
            error=CASE WHEN run_steps.status='completed' THEN run_steps.error ELSE excluded.error END,
            finished_at=CASE WHEN run_steps.status='completed' THEN run_steps.finished_at ELSE excluded.finished_at END,owner_token=excluded.owner_token''',
          (run_id,step,run_id,'failed' if failed else 'running',max(count,row['model_attempts']),state.get('error'),failed,row['owner_token']))
        connection.execute('''UPDATE research_runs SET step=%s,model_attempts=greatest(model_attempts,%s),model_usage=%s,
          dataset_id=coalesce(%s,dataset_id),updated_at=clock_timestamp() WHERE run_id=%s''',
          (step,count,Jsonb(state.get('model_usage',row['model_usage'])),state.get('dataset_id'),run_id))

def reserve_attempt(connection,run_id,owner_token,role):
    if role not in ROLES: raise ValueError('Unknown analysis role')
    with connection.transaction():
        row=_owner(connection,run_id,owner_token,lock=True)
        if connection.execute("SELECT 1 FROM run_steps WHERE run_id=%s AND step=%s AND status='completed'",(run_id,role)).fetchone():
            raise RepoConflict('Verified role checkpoint already exists; reuse it instead of another call')
        n=connection.execute('SELECT count(*) AS n FROM run_attempts WHERE run_id=%s AND role=%s',(run_id,role)).fetchone()['n']
        if row['model_attempts']>=8 or n>=2: raise AttemptBudgetError('Model attempt budget exhausted (eight global, two per role)')
        if connection.execute("SELECT 1 FROM run_attempts WHERE run_id=%s AND status='reserved'",(run_id,)).fetchone():
            raise RepoConflict('A model attempt is already reserved')
        result=connection.execute("""INSERT INTO run_attempts(run_id,role,role_attempt,total_ordinal,owner_token,status)
          VALUES (%s,%s,%s,%s,%s,'reserved') RETURNING *""",(run_id,role,n+1,row['model_attempts']+1,row['owner_token'])).fetchone()
        connection.execute('UPDATE research_runs SET model_attempts=model_attempts+1,updated_at=clock_timestamp() WHERE run_id=%s',(run_id,))
    result['owner_token']=str(result['owner_token'])
    return result

def _usage(connection,run_id):
    connection.execute("""UPDATE research_runs SET model_usage=coalesce((SELECT jsonb_agg(usage ORDER BY total_ordinal)
      FROM run_attempts WHERE run_id=%s AND usage IS NOT NULL),'[]'::jsonb),updated_at=clock_timestamp() WHERE run_id=%s""",(run_id,run_id))

def record_attempt(connection,run_id,owner_token,role,role_attempt,usage):
    if not isinstance(usage,dict): raise ValueError('Attempt usage must be an object')
    supplied=usage.get('status','unknown')
    status='completed' if supplied=='completed' else 'unknown' if supplied in {'unknown','network_unknown','unconfirmed_error','reserved'} else 'failed'
    with connection.transaction():
        _owner(connection,run_id,owner_token,lock=True)
        row=connection.execute('SELECT * FROM run_attempts WHERE run_id=%s AND role=%s AND role_attempt=%s FOR UPDATE',(run_id,role,role_attempt)).fetchone()
        if row is None or str(row['owner_token'])!=_token(owner_token): raise OwnershipError('Attempt belongs to a different owner')
        if row['status']!='reserved': raise RepoConflict('Attempt outcome is already recorded')
        connection.execute('UPDATE run_attempts SET status=%s,usage=%s,finished_at=clock_timestamp() WHERE run_id=%s AND role=%s AND role_attempt=%s',
          (status,Jsonb(usage),run_id,role,role_attempt))
        _usage(connection,run_id)
    return get(connection,run_id)

def checkpoint(connection,run_id,owner_token,step,artifact_path,artifact_sha256,workflow_fingerprint):
    _sha(artifact_sha256,'artifact');_sha(workflow_fingerprint,'workflow');token=_token(owner_token)
    path=PurePosixPath(str(artifact_path))
    if (not isinstance(artifact_path,str) or len(artifact_path)>1200 or path.is_absolute() or '..' in path.parts or '\\' in artifact_path or ':' in artifact_path
        or not any(part in {token,token.replace('-','')} for part in path.parts)):
        raise ValueError('Checkpoint must reference a relative artifact in this owner workspace')
    with connection.transaction():
        row=_owner(connection,run_id,token,lock=True)
        old=connection.execute('SELECT * FROM run_steps WHERE run_id=%s AND step=%s FOR UPDATE',(run_id,step)).fetchone()
        if old and old['status']=='completed' and (old['artifact_sha256']!=artifact_sha256 or old['workflow_fingerprint']!=workflow_fingerprint):
            raise RepoConflict('Existing verified checkpoint has different bytes or workflow')
        if step in ROLES:
            latest=connection.execute('SELECT status FROM run_attempts WHERE run_id=%s AND role=%s ORDER BY role_attempt DESC LIMIT 1',(run_id,step)).fetchone()
            if latest is None or latest['status']!='completed': raise RepoConflict('Role checkpoint requires a completed recorded model attempt')
        connection.execute("""INSERT INTO run_steps(run_id,step,ordinal,status,model_attempts,finished_at,owner_token,
            artifact_path,artifact_sha256,workflow_fingerprint,verified_at)
          VALUES (%s,%s,(SELECT coalesce(max(ordinal),0)+1 FROM run_steps WHERE run_id=%s),'completed',%s,clock_timestamp(),%s,%s,%s,%s,clock_timestamp())
          ON CONFLICT(run_id,step) DO UPDATE SET status='completed',error=NULL,finished_at=clock_timestamp(),owner_token=excluded.owner_token,
            artifact_path=excluded.artifact_path,artifact_sha256=excluded.artifact_sha256,workflow_fingerprint=excluded.workflow_fingerprint,verified_at=excluded.verified_at""",
          (run_id,step,run_id,row['model_attempts'],token,artifact_path,artifact_sha256,workflow_fingerprint))
    return get(connection,run_id)

@contextmanager
def publication_guard(connection,run_id,owner_token):
    with connection.transaction(): yield _owner(connection,run_id,owner_token,lock=True)

def _unknown(connection,run_id):
    connection.execute("""UPDATE run_attempts SET status='unknown',finished_at=clock_timestamp(),
      usage=jsonb_build_object('role',role,'attempt',role_attempt,'status','network_unknown','usage',NULL,
        'reason','Worker stopped or cancellation occurred; whether provider charged this call is unknown')
      WHERE run_id=%s AND status='reserved'""",(run_id,))
    if connection.execute('SELECT 1 FROM run_attempts WHERE run_id=%s LIMIT 1',(run_id,)).fetchone(): _usage(connection,run_id)

def finish(connection,run_id,status,*,error=None,owner_token=None):
    if status not in TERMINAL: raise ValueError('Invalid terminal research status')
    with connection.transaction():
        _owner(connection,run_id,owner_token,lock=True,compatibility=owner_token is None)
        connection.execute("""UPDATE research_runs SET status=%s,error=%s,finished_at=clock_timestamp(),updated_at=clock_timestamp(),
          step=CASE WHEN %s IN ('completed','completed_with_gaps') THEN 'finished' ELSE step END,lease_until=NULL WHERE run_id=%s""",(status,error,status,run_id))
        if status not in {'completed','completed_with_gaps'}:
            connection.execute("UPDATE run_steps SET status=%s,error=%s,finished_at=clock_timestamp() WHERE run_id=%s AND status='running'",
                               ('interrupted' if status=='cancelled' else status,error,run_id))
            _unknown(connection,run_id)
    return get(connection,run_id)

def request_cancel(connection,run_id,*,root=None):
    with connection.transaction():
        row=connection.execute('SELECT * FROM research_runs WHERE run_id=%s'+(' AND data_root=%s' if root is not None else '')+' FOR UPDATE',
                               (run_id,str(root)) if root is not None else (run_id,)).fetchone()
        if row is None: return None
        if row['status'] not in TERMINAL:
            connection.execute("""UPDATE research_runs SET cancel_requested=true,status='cancelled',finished_at=clock_timestamp(),
              updated_at=clock_timestamp(),lease_until=NULL,error='Cancellation requested; in-flight billing may be unknown' WHERE run_id=%s""",(run_id,))
            connection.execute("UPDATE run_steps SET status='interrupted',finished_at=clock_timestamp() WHERE run_id=%s AND status='running'",(run_id,))
            _unknown(connection,run_id)
    return get(connection,run_id)

def _interrupt(connection,run_id,error):
    connection.execute("""UPDATE research_runs SET status='interrupted',error=%s,finished_at=clock_timestamp(),
      updated_at=clock_timestamp(),lease_until=NULL WHERE run_id=%s""",(error,run_id))
    connection.execute("UPDATE run_steps SET status='interrupted',error=%s,finished_at=clock_timestamp() WHERE run_id=%s AND status='running'",(error,run_id))
    _unknown(connection,run_id)

def recover_expired(connection,root):
    with connection.transaction():
        rows=connection.execute("""SELECT run_id FROM research_runs WHERE data_root=%s AND status='running'
          AND (lease_until IS NULL OR lease_until<=clock_timestamp() OR deadline_at<=clock_timestamp()) FOR UPDATE SKIP LOCKED""",(str(root),)).fetchall()
        for row in rows: _interrupt(connection,row['run_id'],'Worker lease or deadline expired; explicit resume required; possible external billing is unknown')
    return len(rows)

def interrupt_orphans(connection,root):
    """Historical single-worker startup adapter. M5 workers use recover_expired."""
    with connection.transaction():
        rows=connection.execute("SELECT run_id FROM research_runs WHERE data_root=%s AND status='running' FOR UPDATE",(str(root),)).fetchall()
        for row in rows: _interrupt(connection,row['run_id'],'Previous worker stopped; explicit resume required; possible external billing is unknown')
    return len(rows)

def resume(connection,run_id,*,workflow_config_fingerprint=None,acknowledge_unknown=False,root=None):
    with connection.transaction():
        row=connection.execute('SELECT * FROM research_runs WHERE run_id=%s'+(' AND data_root=%s' if root is not None else '')+' FOR UPDATE',
                               (run_id,str(root)) if root is not None else (run_id,)).fetchone()
        if row is None: return None
        if row['status'] not in {'failed','interrupted'}: raise RepoConflict('Only failed or interrupted research can be resumed')
        if row['workflow_config_fingerprint']!=workflow_config_fingerprint: raise RepoConflict('Workflow/configuration version changed; create new research')
        if row['cancel_requested']: raise RunCancelled('Cancelled research cannot be resumed')
        now=connection.execute('SELECT clock_timestamp() AS now').fetchone()['now']
        if row['deadline_at'] is not None and row['deadline_at']<=now: raise RunDeadline('Original deadline expired; resume cannot extend its budget')
        complete_roles=connection.execute("SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step=ANY(%s) AND status='completed' AND artifact_sha256 IS NOT NULL",(run_id,list(ROLES))).fetchone()['n']
        if row['model_attempts']>=8 and complete_roles!=len(ROLES): raise AttemptBudgetError('Global model call budget is exhausted')
        unknown=connection.execute("""SELECT 1 FROM run_attempts a WHERE a.run_id=%s
          AND (%s::timestamptz IS NULL OR a.started_at>%s::timestamptz) AND
          (a.status IN ('unknown','reserved') OR (a.status='completed' AND NOT EXISTS
            (SELECT 1 FROM run_steps s WHERE s.run_id=a.run_id AND s.step=a.role AND s.status='completed'
              AND s.artifact_sha256 IS NOT NULL))) LIMIT 1""",
          (run_id,row['unknown_acknowledged_at'],row['unknown_acknowledged_at'])).fetchone()
        if unknown and not acknowledge_unknown: raise RepoConflict('Previous call outcome/billing is unknown; explicit acknowledgement required')
        connection.execute("""UPDATE research_runs SET status='queued',step='queued',finished_at=NULL,error=NULL,owner_token=NULL,
          worker_id=NULL,lease_until=NULL,resume_count=resume_count+1,updated_at=clock_timestamp(),
          unknown_acknowledged_at=CASE WHEN %s THEN clock_timestamp() ELSE unknown_acknowledged_at END WHERE run_id=%s""",(acknowledge_unknown,run_id))
    return get(connection,run_id)
