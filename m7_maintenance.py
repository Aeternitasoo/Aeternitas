"""Offline, finite-file maintenance and verified PostgreSQL disaster recovery.

Operators stop API admission and workers before using these functions. Table
locks reject/serialize database writers; they are not a distributed filesystem
transaction. Only trusted, locally produced backups may be restored.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

import evidence_repository as domain
from analysis_workflow import publication_files
from research_pipeline import validate_report_directory, saved_report
from research_service import ResearchService, ServiceError
from sources.sec import SourceError, write_atomic, write_json

VERSION = 'aeternitas-backup-m7-v2'
ROW_CANONICALIZATION = 'aware-datetime-utc-v2;date-unchanged;decimal-exact;uuid-string'
SUCCESS = {'completed', 'completed_with_gaps'}
RUN_ID = re.compile(r'[0-9a-f]{32}')
SHA = re.compile(r'[0-9a-f]{64}')
TABLES = ('schema_migrations','companies','source_snapshots','retrievals','filings',
          'fact_observations','fact_locations','datasets','dataset_sources','dataset_facts',
          'calculations','calculation_inputs','evidence_versions','dataset_evidence',
          'research_runs','run_steps','run_attempts')
BASE_NAMES = ('state.json','published.json','input_bundle.json','analysis_plan.json','report.html','report.json')
STEP_NAMES = ('financials','filings','news','data_import','validation','starting')
ROLES = ('fundamentals','events','risk','synthesis')


def _json(value):
    # PostgreSQL timestamptz represents an instant, independently of a source
    # or restore session's TimeZone. datetime must precede its date base class.
    if isinstance(value, datetime):
        if value.utcoffset() is None: raise SourceError('Naive database timestamp cannot be canonicalized')
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date): return value.isoformat()
    if isinstance(value, (Decimal, UUID)): return str(value)
    raise TypeError('Unsupported backup value')


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=_json).encode()).hexdigest()


def _hash(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _storage_name(name):
    """Only paths emitted by the existing source adapters / finite run contracts."""
    if re.fullmatch(r'snapshots/companyfacts/[0-9]{10}/[0-9a-f]{64}\.json',name): return True
    if re.fullmatch(r'snapshots/web/[0-9a-f]{64}\.bin',name): return True
    if re.fullmatch(r'retrievals/[0-9a-f]{32}\.json',name): return True
    if re.fullmatch(r'extracted/filings/[0-9a-f]{64}-[A-Za-z0-9_-]+\.txt',name): return True
    if not re.match(r'runs/[0-9a-f]{32}/',name): return False
    parts=PurePosixPath(name).parts[2:]
    if len(parts)>=2 and parts[0]=='owners':
        try:
            if str(UUID(parts[1]))!=parts[1]: return False
        except ValueError: return False
        parts=parts[2:]
        if parts and parts[0]=='promotion': parts=parts[1:]
    tail='/'.join(parts)
    if tail in BASE_NAMES: return True
    if re.fullmatch(r'steps/(fundamentals|events|risk|synthesis)(-attempt-[12]-(input|output|error))?\.json',tail): return True
    # A checkpoint name is additionally checked against its SQL hash. Its step
    # label is a bounded identifier, not a file path or credential filename.
    return bool(re.fullmatch(r'(checkpoints/[a-z][a-z0-9_]{0,40}|[a-z][a-z0-9_]{0,40}-legacy-checked)\.json',tail))


def safe_path(root, relative, *, existing=False):
    """Reject traversal and every symlink component, even an internal symlink."""
    root=Path(root).absolute()
    raw=str(relative)
    path=PurePosixPath(raw)
    if (not raw or path.is_absolute() or '..' in path.parts or '.' in path.parts or
        '\\' in raw or ':' in raw or any(ord(c)<32 for c in raw)):
        raise SourceError('Unsafe maintenance relative path')
    if root.is_symlink() or root.resolve()!=root:
        raise SourceError('Maintenance root must be a physical absolute directory')
    candidate=root.joinpath(*path.parts)
    cursor=root
    for part in path.parts:
        cursor/=part
        if cursor.is_symlink(): raise SourceError('Maintenance refuses symbolic links')
    if not candidate.resolve().is_relative_to(root): raise SourceError('Maintenance path escapes root')
    if existing and not candidate.is_file(): raise SourceError('Referenced maintenance file is missing')
    return candidate


def _validate_root(root):
    root=Path(root).absolute()
    if root.is_symlink() or root.resolve()!=root or not root.is_dir():
        raise SourceError('Data root must be an existing physical directory')
    return root


def _tables(connection, schema):
    names={r['table_name'] for r in connection.execute(
        'SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type=%s',
        (schema,'BASE TABLE')).fetchall()}
    if names!=set(TABLES): raise SourceError('Database table set differs from the supported migration contract')


def _rows(connection, *, new_root=None, old_root=None):
    result={}
    for table in TABLES:
        rows=connection.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall()
        if table=='research_runs' and new_root is not None:
            rows=[{**r,'data_root':str(new_root) if r['data_root']==str(old_root) else r['data_root']} for r in rows]
        result[table]={'count':len(rows),'sha256':_digest(sorted(rows,key=lambda r:_digest(r)))}
    return result


@contextmanager
def quiescent(service, *, acknowledged=False):
    if not acknowledged: raise SourceError('Stop API admission and all workers first; pass explicit quiesced acknowledgement')
    with service.connection() as connection:
        _tables(connection,service.schema)
        with connection.transaction():
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute(sql.SQL('LOCK TABLE {} IN SHARE MODE').format(
                sql.SQL(',').join(map(sql.Identifier,TABLES))))
            roots={r['data_root'] for r in connection.execute('SELECT DISTINCT data_root FROM research_runs').fetchall()}
            if roots and roots!={str(service.root)}:
                raise SourceError('Backup requires one data root; other roots must be separately reconciled')
            if connection.execute("SELECT 1 FROM research_runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise SourceError('Queued/running work remains; drain workers before offline maintenance')
            yield connection


def _run_files(root,rid):
    if not RUN_ID.fullmatch(rid): raise SourceError('Invalid persisted run identity')
    prefix=f'runs/{rid}'
    folder=safe_path(root,prefix)
    names=set()
    def generation(rel):
        for name in BASE_NAMES:
            path=safe_path(root,f'{rel}/{name}')
            if path.exists(): names.add(f'{rel}/{name}')
        for role in ROLES:
            candidates=[f'steps/{role}.json']+[f'steps/{role}-attempt-{a}-{kind}.json'
                for a in (1,2) for kind in ('input','output','error')]
            for name in candidates:
                if safe_path(root,f'{rel}/{name}').exists(): names.add(f'{rel}/{name}')
        for step in STEP_NAMES+ROLES:
            for name in (f'checkpoints/{step}.json',f'{step}-legacy-checked.json'):
                if safe_path(root,f'{rel}/{name}').exists(): names.add(f'{rel}/{name}')
    generation(prefix)
    owners=safe_path(root,f'{prefix}/owners')
    if owners.exists():
        for owner in owners.iterdir():
            if owner.is_symlink(): raise SourceError('Owner generation is a symbolic link')
            try: valid=str(UUID(owner.name))==owner.name
            except ValueError: valid=False
            if not valid or not owner.is_dir(): raise SourceError('Unexpected owner generation path')
            generation(f'{prefix}/owners/{owner.name}')
            generation(f'{prefix}/owners/{owner.name}/promotion')
    return names


def _referenced_files(connection,root):
    names=set()
    def bundle_files(report):
        names.update(s['snapshot_path'] for s in report['sources'])
        names.update(f"retrievals/{s['retrieval_id']}.json" for s in report['sources'])
        names.update(e['locator']['normalized_path'] for e in report['evidence'] if e['kind']=='filing')
    for row in connection.execute('SELECT snapshot_path,sha256,size_bytes FROM source_snapshots').fetchall():
        path=safe_path(root,row['snapshot_path'],existing=True)
        if path.stat().st_size!=row['size_bytes'] or _hash(path)!=row['sha256']:
            raise SourceError('Database raw snapshot differs from referenced bytes')
        names.add(row['snapshot_path'])
    for row in connection.execute('SELECT retrieval_id FROM retrievals').fetchall():
        rid=row['retrieval_id']
        if not RUN_ID.fullmatch(rid): raise SourceError('Invalid stored retrieval identity')
        names.add(f'retrievals/{rid}.json')
    for row in connection.execute("SELECT payload FROM evidence_versions WHERE kind='filing'").fetchall():
        names.add(row['payload']['locator']['normalized_path'])
    runs=connection.execute('SELECT run_id,status FROM research_runs ORDER BY run_id').fetchall()
    indexed={row['run_id'] for row in runs}
    for row in runs:
        names.update(_run_files(root,row['run_id']))
        if row['status'] in SUCCESS: saved_report(root,row['run_id'])
    # M1/M2 file publications predate the task table. Preserve their genuine
    # histories without manufacturing later task rows or losing their sources.
    legacy=[]
    runs_folder=safe_path(root,'runs')
    if runs_folder.exists():
        for folder in runs_folder.iterdir():
            if not RUN_ID.fullmatch(folder.name) or folder.name in indexed: continue
            if folder.is_symlink() or not folder.is_dir(): raise SourceError('Legacy run directory is not a physical directory')
            state_path=safe_path(root,f'runs/{folder.name}/state.json')
            if not state_path.is_file(): continue
            state=json.loads(state_path.read_text())
            if state.get('status') not in SUCCESS: continue
            _,report=saved_report(root,folder.name)
            bundle_files(report)
            names.update(_run_files(root,folder.name))
            legacy.append({'run_id':folder.name,'status':report['status'],'ticker':report['ticker']})
    for step in connection.execute('SELECT artifact_path,artifact_sha256 FROM run_steps WHERE artifact_path IS NOT NULL').fetchall():
        path=safe_path(root,step['artifact_path'],existing=True)
        if _hash(path)!=step['artifact_sha256']: raise SourceError('Verified database checkpoint bytes differ')
        names.add(step['artifact_path'])
    # Every name derives from finite task identities or an audited source reference.
    # Keep configuration/credentials out even if a malformed database references one.
    for name in names:
        if not _storage_name(name):
            raise SourceError('Referenced file is outside approved research storage categories')
        if any(p.startswith('.') or 'token' in p.lower() or p.lower().endswith('.env') for p in PurePosixPath(name).parts):
            raise SourceError('Referenced file resembles credentials/configuration')
        safe_path(root,name,existing=True)
    return names,runs,sorted(legacy,key=lambda r:r['run_id'])


def _pg(tool,dsn,args,*,timeout=300):
    env=dict(os.environ)
    # libpq receives credentials through environment, never process arguments/logs.
    mapping={'dbname':'PGDATABASE','host':'PGHOST','port':'PGPORT','user':'PGUSER','password':'PGPASSWORD',
             'sslmode':'PGSSLMODE','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY','sslrootcert':'PGSSLROOTCERT',
             'connect_timeout':'PGCONNECT_TIMEOUT','options':'PGOPTIONS','application_name':'PGAPPNAME',
             'service':'PGSERVICE','passfile':'PGPASSFILE','hostaddr':'PGHOSTADDR'}
    for key,value in conninfo_to_dict(dsn).items():
        if key not in mapping: raise SourceError('Unsupported libpq backup connection option')
        env[mapping[key]]=value
    try:
        result=subprocess.run([tool,*map(str,args)],env=env,capture_output=True,timeout=timeout)
    except (OSError,subprocess.TimeoutExpired):
        raise SourceError(f'{tool} failed or exceeded the offline maintenance deadline') from None
    if result.returncode: raise SourceError(f'{tool} failed; private database diagnostics withheld')
    return result.stdout


def backup(service,destination,*,quiesced_ack=False):
    root=_validate_root(service.root)
    destination=Path(destination).absolute()
    if destination.exists() or destination.resolve().is_relative_to(root):
        raise SourceError('Backup destination must be new and outside the source data root')
    if destination.parent.resolve()!=destination.parent or not destination.parent.is_dir():
        raise SourceError('Backup parent must exist without symbolic links')
    destination.mkdir(mode=0o700)
    try:
        with quiescent(service,acknowledged=quiesced_ack) as connection:
            names,runs,legacy=_referenced_files(connection,root)
            for row in runs:
                if row['status'] in SUCCESS: service.report(row['run_id'])
            snapshot=connection.execute('SELECT pg_export_snapshot() AS id').fetchone()['id']
            _pg('pg_dump',service.config['DATABASE_URL'],['--format=custom','--no-owner','--no-privileges',
                '--schema',service.schema,'--snapshot',snapshot,'--file',destination/'database.dump'])
            files={}
            for name in sorted(names):
                data=safe_path(root,name,existing=True).read_bytes()
                target=safe_path(destination,f'files/{name}')
                write_atomic(target,data)
                files[name]={'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)}
            manifest={'version':VERSION,'row_canonicalization':ROW_CANONICALIZATION,
                'created_at':datetime.now().astimezone().isoformat(),
                'schema':service.schema,'source_root':str(root),'tables':_rows(connection),
                'files':files,'dump':{'sha256':_hash(destination/'database.dump'),'size':(destination/'database.dump').stat().st_size},
                'runs':[{'run_id':r['run_id'],'status':r['status']} for r in runs],
                'legacy_reports':legacy,
                'consistency':'Operator drained admission/workers; SQL tables SHARE locked during snapshot dump and finite file copy; no distributed atomicity claim',
                'excluded':['credentials','configuration','cache indexes','unreferenced diagnostics','arbitrary files'],
                'restore_trust':'Trusted locally produced backup only; manifest is integrity metadata, not an authenticity signature'}
            write_json(destination/'manifest.json',manifest)
            write_atomic(destination/'manifest.sha256',(_hash(destination/'manifest.json')+'\n').encode())
        return {'backup':str(destination),'files':len(files),'runs':len(runs),'legacy_reports':len(legacy),'manifest_sha256':_hash(destination/'manifest.json')}
    except BaseException:
        write_json(destination/'incomplete.json',{'status':'incomplete','do_not_restore':True})
        raise


def verify_backup(folder):
    folder=_validate_root(folder)
    if (folder/'incomplete.json').exists(): raise SourceError('Incomplete backup cannot be restored')
    expected=safe_path(folder,'manifest.sha256',existing=True).read_text().strip()
    if not SHA.fullmatch(expected) or _hash(safe_path(folder,'manifest.json',existing=True))!=expected:
        raise SourceError('Backup manifest fingerprint differs')
    manifest=json.loads((folder/'manifest.json').read_text())
    if (manifest.get('version')!=VERSION or manifest.get('row_canonicalization')!=ROW_CANONICALIZATION
        or set(manifest.get('tables',{}))!=set(TABLES)):
        raise SourceError('Unsupported backup contract')
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',manifest.get('schema','')): raise SourceError('Invalid backup schema')
    dump=safe_path(folder,'database.dump',existing=True)
    if _hash(dump)!=manifest['dump']['sha256'] or dump.stat().st_size!=manifest['dump']['size']:
        raise SourceError('Backup database dump fingerprint differs')
    for name,meta in manifest['files'].items():
        if not _storage_name(name):
            raise SourceError('Backup references an unexpected storage category')
        path=safe_path(folder,f'files/{name}',existing=True)
        if _hash(path)!=meta['sha256'] or path.stat().st_size!=meta['size']:
            raise SourceError('Backup file fingerprint differs')
    return manifest


def restore(folder,destination,dsn,*,trusted_local_backup=False):
    if not trusted_local_backup: raise SourceError('Restore only a trusted locally produced backup')
    manifest=verify_backup(folder)
    folder=Path(folder).absolute(); destination=Path(destination).absolute()
    if destination.exists() or destination.parent.resolve()!=destination.parent or not destination.parent.is_dir():
        raise SourceError('Restore destination must be a new physical directory')
    if str(destination)==manifest['source_root']: raise SourceError('Restore must use a different data root')
    schema=manifest['schema']
    with domain.connect(dsn) as connection:
        existing=connection.execute("SELECT 1 FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') LIMIT 1").fetchone()
        if existing: raise SourceError('Restore requires an independent empty database')
    # pg_restore failures leave an explicitly incomplete target, never a claim of success.
    destination.mkdir(mode=0o700)
    marker=destination/'restore-incomplete.json'
    write_json(marker,{'status':'incomplete','source_manifest_sha256':_hash(folder/'manifest.json')})
    # -d selects direct restore mode; the empty value resolves via PGDATABASE.
    _pg('pg_restore',dsn,['--dbname=','--exit-on-error','--single-transaction','--clean','--if-exists','--no-owner','--no-privileges',folder/'database.dump'])
    with domain.connect(dsn,schema=schema) as connection:
        _tables(connection,schema)
        if _rows(connection)!=manifest['tables']: raise SourceError('Restored database rows differ from backup')
        domain.migrate(connection)  # Verify all existing migration checksums, apply only new migrations.
        with connection.transaction():
            roots={r['data_root'] for r in connection.execute('SELECT DISTINCT data_root FROM research_runs').fetchall()}
            if roots and roots!={manifest['source_root']}: raise SourceError('Restored database contains unexpected historical roots')
            connection.execute('UPDATE research_runs SET data_root=%s WHERE data_root=%s',(str(destination),manifest['source_root']))
    for name in manifest['files']:
        write_atomic(safe_path(destination,name),safe_path(folder,f'files/{name}',existing=True).read_bytes())
    service=ResearchService(destination,{'DATABASE_URL':dsn},schema=schema)
    verified=[]
    for row in manifest['runs']:
        current=service.get(row['run_id'])
        if current['status']!=row['status']: raise SourceError('Restored history status differs')
        if row['status'] in SUCCESS:
            service.report(row['run_id']); verified.append(row['run_id'])
    legacy_verified=[]
    for row in manifest.get('legacy_reports',[]):
        _,report=saved_report(destination,row['run_id'])
        if report['ticker']!=row['ticker'] or report['status']!=row['status']:
            raise SourceError('Restored legacy report identity differs')
        legacy_verified.append(row['run_id'])
    with service.connection() as connection:
        # The only changed historic field is the documented data_root relocation.
        reverse=_rows(connection,new_root=manifest['source_root'],old_root=destination)
        if reverse!=manifest['tables']: raise SourceError('Restored rows changed beyond root relocation')
    result={'status':'verified','source_manifest_sha256':_hash(folder/'manifest.json'),
        'restored_root':str(destination),'history_count':len(manifest['runs']),'completed_reports_verified':verified,
        'legacy_reports_verified':legacy_verified,
        'files_verified':len(manifest['files']),'root_relocation_only':True,'new_source_requests':0,'new_model_requests':0}
    write_json(destination/'restore-verification.json',result)
    marker.unlink()
    return result


def maintenance_scan(service):
    """Read-only inventory. Generations with recovery evidence are never deletion candidates."""
    root=_validate_root(service.root)
    with service.connection() as connection:
        rows=connection.execute('SELECT run_id,status,owner_token FROM research_runs WHERE data_root=%s ORDER BY run_id',(str(root),)).fetchall()
        references={r['artifact_path'] for r in connection.execute('SELECT artifact_path FROM run_steps WHERE artifact_path IS NOT NULL').fetchall()}
    result={'version':'maintenance-m7-v1','mode':'dry_run','reports':[],'generations':[],
        'cleanup_policy':'No automatic deletion; referenced/successful/failed or unknown generations retain historical evidence'}
    for row in rows:
        rid=row['run_id']; folder=safe_path(root,f'runs/{rid}')
        if row['status'] in SUCCESS:
            try: service.report(rid); status='verified'
            except (SourceError,ServiceError,ValueError,OSError,KeyError):
                # Public report errors contain no source credentials; store only class.
                status='integrity_failed'
            result['reports'].append({'run_id':rid,'status':status})
        owners=safe_path(root,f'runs/{rid}/owners')
        if owners.exists():
            for owner in owners.iterdir():
                if owner.is_symlink(): raise SourceError('Owner generation is a symbolic link')
                try: valid=str(UUID(owner.name))==owner.name
                except ValueError: valid=False
                if not valid or not owner.is_dir(): raise SourceError('Unexpected owner generation path')
                rel=owner.relative_to(root).as_posix()
                referenced=any(p.startswith(rel+'/') for p in references)
                empty=not any(owner.iterdir())
                result['generations'].append({'run_id':rid,'generation':rel,'checkpoint_referenced':referenced,
                    'current_owner':str(row['owner_token'])==owner.name,'empty':empty,
                    'safe_cleanup_candidate':empty and not referenced and str(row['owner_token'])!=owner.name and row['status'] not in {'running','queued'},
                    'action':'dry_run_only'})
    return result


def repair_completed(service,rid,*,quiesced_ack=False,backup_folder=None):
    """Restore an exact previously verified publication; never rerun providers/models."""
    if not RUN_ID.fullmatch(rid): raise SourceError('Invalid maintenance run identity')
    root=_validate_root(service.root)
    with quiescent(service,acknowledged=quiesced_ack) as connection:
        row=connection.execute('SELECT * FROM research_runs WHERE run_id=%s AND data_root=%s',(rid,str(root))).fetchone()
        if not row or row['status'] not in SUCCESS: raise SourceError('Only completed local reports may be repaired')
        canonical=safe_path(root,f'runs/{rid}')
        expected=None
        if backup_folder:
            manifest=verify_backup(backup_folder)
            if manifest['source_root']!=str(root): raise SourceError('Repair backup belongs to another original root')
            if not any(r['run_id']==rid and r['status']==row['status'] for r in manifest['runs']):
                raise SourceError('Backup run identity/status differs')
            source=safe_path(backup_folder,f'files/runs/{rid}')
            verify_root=Path(backup_folder)/'files'
            expected=manifest
        else:
            source=None; verify_root=root
            # Only the DB successful current owner is eligible, never a guessed old draft.
            if row['owner_token']:
                candidate=safe_path(root,f'runs/{rid}/owners/{row["owner_token"]}')
                try: validate_report_directory(root,candidate,rid); source=candidate
                except (SourceError,ValueError,OSError,KeyError): pass
            if source is None: raise SourceError('No verified successful owner; provide a verified backup')
        _,report=validate_report_directory(verify_root,source,rid)
        for key in ('run_id','ticker','cik','question','model','billing_mode','dataset_id','status'):
            if report.get(key)!=row[key]: raise SourceError('Repair publication differs from persisted task')
        if report['as_of_date']!=row['as_of_date'].isoformat(): raise SourceError('Repair cutoff differs')
        names=publication_files(source)+['state.json','published.json']
        stage=safe_path(root,f'maintenance/{rid}/{uuid4().hex}')
        for name in names: write_atomic(stage/'replacement'/name,safe_path(source,name,existing=True).read_bytes())
        # Preserve every existing canonical byte before overwriting, including corrupt data.
        preserved=[]
        for name in names:
            old=safe_path(canonical,name)
            if old.exists(): write_atomic(stage/'previous'/name,old.read_bytes()); preserved.append(name)
        source_files={}
        if expected:
            needed={s['snapshot_path'] for s in report['sources']}
            needed.update(f"retrievals/{s['retrieval_id']}.json" for s in report['sources'])
            needed.update(e['locator']['normalized_path'] for e in report['evidence'] if e['kind']=='filing')
            if not needed<=set(expected['files']): raise SourceError('Repair backup omits referenced source files')
            source_files={name:expected['files'][name] for name in needed}
            for name in source_files:
                old=safe_path(root,name)
                if old.exists() and _hash(old)==source_files[name]['sha256']: continue
                if old.exists(): write_atomic(stage/'previous_sources'/name,old.read_bytes())
                write_atomic(old,safe_path(backup_folder,f'files/{name}',existing=True).read_bytes())
        for name in names:
            if name=='published.json': continue
            write_atomic(safe_path(canonical,name),(stage/'replacement'/name).read_bytes())
        write_atomic(canonical/'published.json',(stage/'replacement/published.json').read_bytes())
        service.report(rid)
        result={'status':'repaired_verified','run_id':rid,'source':'verified_backup' if expected else 'verified_successful_owner',
            'preserved_existing_files':preserved,'history_changed':False,'new_source_requests':0,'new_model_requests':0,
            'operation_directory':stage.relative_to(root).as_posix(),'atomicity':'Finite atomic file writes; offline operation, not a DB/filesystem distributed transaction'}
        write_json(stage/'operation.json',result)
        return result
