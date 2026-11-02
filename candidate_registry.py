"""Append-only local candidates and manually approved release selection.

Only an external release pointer changes; production repo files are never
written. This is a trusted-host control, not an OS sandbox for hostile Python.
Models/Skills receive neither these functions nor the approval authority key.
"""
from contextlib import contextmanager
import difflib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

from evidence_repository import digest
from lab_evaluation import compare_versions, read, load_split
from research_runtime import utc_now
from research_workflow import save, sha

VERSION='candidate-registry-v1'


def checked_files(files,kind):
    patterns={'skill':r'skills/[a-z][a-z0-9-]{0,62}/SKILL\.md',
              'prompt':r'prompts/[a-z][a-z0-9_-]{0,62}\.md',
              'research_contract':r'(?:contracts/research\.json|prompts/research\.md)',
              'harness':r'harness/[a-z][a-z0-9_]{0,62}\.py'}
    if kind not in patterns or not files or len(files)>16: raise ValueError('Invalid candidate scope')
    for name,text in files.items():
        if not isinstance(name,str) or not re.fullmatch(patterns[kind],name): raise ValueError('Candidate path / scope refused')
        if not isinstance(text,str) or not 0<len(text.encode())<=65536: raise ValueError('Candidate content size')
    if sum(len(t.encode()) for t in files.values())>262144: raise ValueError('Candidate size limit')
    if kind=='research_contract':
        from research_contract_profile import checked,LEGACY
        spec=json.loads(files['contracts/research.json'])
        checked({'spec':spec,'instruction':files['prompts/research.md'],
                 'prompt_version':'industry-research-v1' if spec==LEGACY else 'industry-research-multi-excerpt-v2'})


class CandidateRegistry:
    def __init__(self,root,*,authority_root,scope='research',create=False):
        self.root=Path(root);self.authority=Path(authority_root);self.scope=scope
        if self.root.is_symlink() or self.authority.is_symlink(): raise ValueError('Registry symlink')
        if create:
            self.root.mkdir(parents=True,exist_ok=False)
            for name in ('versions','activations','events','comparisons','approvals'): (self.root/name).mkdir()
            self.authority.mkdir(parents=True,exist_ok=False,mode=0o700)
            fd=os.open(self.authority/'approval-key',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream: stream.write(os.urandom(32))
            save(self.root/'registry.json',{'version':VERSION,'scope':scope,'created_at':utc_now()})
        if read(self.root/'registry.json')['scope']!=scope: raise ValueError('Registry scope differs')

    @contextmanager
    def lock(self):
        with (self.root/'.lock').open('a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(stream,fcntl.LOCK_UN)

    def version(self,identity):
        if not re.fullmatch(r'[a-f0-9]{32}',identity): raise ValueError('Invalid version ID')
        folder=self.root/'versions'/identity
        m=read(folder/'manifest.json')
        expected={'manifest.json','diff.json',*('files/'+n for n in m['files'])}
        actual={str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file()}
        if actual!=expected or any(p.is_symlink() for p in folder.rglob('*')): raise ValueError('Candidate inventory changed')
        for name,pin in m['files'].items():
            if sha(folder/'files'/name)!=pin: raise ValueError('Candidate content changed')
        if sha(folder/'diff.json')!=m['diff_sha256']: raise ValueError('Candidate diff changed')
        if m['version_id']!=identity: raise ValueError('Candidate identity differs')
        return m,folder

    def event(self,version_id,status,**data):
        save(self.root/'events'/(utc_now().replace(':','').replace('.','')+'-'+uuid4().hex+'.json'),
             {'version_id':version_id,'status':status,'at':utc_now(),**data})

    def create_version(self,files,*,kind,parent=None,reason,failure_analysis=None,
                       dataset_fingerprint=None,proposal_metadata=None):
        checked_files(files,kind)
        original={}
        if parent:
            base,folder=self.version(parent)
            if base['kind']!=kind: raise ValueError('Candidate kind differs')
            original={n:(folder/'files'/n).read_text() for n in base['files']}
            if set(files)!=set(original): raise ValueError('Candidate may not expand file scope')
            if not failure_analysis or failure_analysis['split']!='development' or not failure_analysis.get('proposal'):
                raise ValueError('Development-only proposal required')
            if dataset_fingerprint not in failure_analysis['dataset_fingerprints']: raise ValueError('Proposal dataset differs')
        identity=uuid4().hex;folder=self.root/'versions'/identity;folder.mkdir()
        self.event(identity,'proposed',parent=parent)
        hashes={};diffs={}
        for name,text in files.items():
            path=folder/'files'/name;path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as stream: stream.write(text.encode())
            hashes[name]=sha(path)
            diffs[name]=''.join(difflib.unified_diff(original.get(name,'').splitlines(True),text.splitlines(True),
                                                   fromfile='parent/'+name,tofile='candidate/'+name))
        save(folder/'diff.json',diffs)
        manifest={'version_id':identity,'registry_version':VERSION,'parent_version':parent,'kind':kind,
            'reason':reason,'failure_analysis':failure_analysis,'dataset_fingerprint':dataset_fingerprint,
            'proposal_metadata':proposal_metadata,
            'files':hashes,'diff_sha256':sha(folder/'diff.json'),'created_at':utc_now(),
            'content_fingerprint':digest(hashes),'scope':self.scope,
            'execution_policy':'data_only' if kind!='harness' else 'archive_only_requires_separate_trusted_runner'}
        save(folder/'manifest.json',manifest)
        self.event(identity,'candidate_created',manifest_sha256=sha(folder/'manifest.json'))
        return identity

    def _history(self):
        rows=[];previous=None
        for p in sorted((self.root/'activations').glob('*.json')):
            row=read(p)
            if row['previous_record_sha256']!=previous or row['generation']!=len(rows): raise ValueError('Activation history changed')
            self.version(row['version_id'])
            if sha(self.root/'versions'/row['version_id']/'manifest.json')!=row['version_manifest_sha256']:
                raise ValueError('Active manifest changed')
            rows.append(row);previous=sha(p)
        return rows,previous

    def active(self):
        rows,_=self._history()
        return rows[-1]['version_id'] if rows else None

    def initialize(self,identity):
        with self.lock():
            m,folder=self.version(identity)
            if self.active() is not None or m['parent_version'] is not None: raise ValueError('Registry already initialized')
            self._activate(identity,'initialize',None,None)

    def _activate(self,identity,action,receipt,comparison_sha):
        rows,previous=self._history();_,folder=self.version(identity)
        row={'generation':len(rows),'version_id':identity,'action':action,'at':utc_now(),
             'previous_record_sha256':previous,'previous_version':rows[-1]['version_id'] if rows else None,
             'version_manifest_sha256':sha(folder/'manifest.json'),'approval_id':receipt['approval_id'] if receipt else None,
             'comparison_sha256':comparison_sha}
        save(self.root/'activations'/f'{len(rows):010d}.json',row)
        return row

    def record_comparison(self,identity,baseline,candidate,*,policy,dataset_root=None):
        m,folder=self.version(identity)
        if m['kind']=='harness': raise ValueError('Executable Harness candidates need a separate trusted isolation runner')
        if m['scope']!=policy['scope']: raise ValueError('Promotion scope differs')
        if dataset_root is None: raise ValueError('Frozen dataset path required')
        dataset_root=Path(dataset_root).resolve()
        if sha(dataset_root/'manifest.json')!=m['dataset_fingerprint']:
            raise ValueError('Frozen dataset manifest changed')
        if read(dataset_root/'manifest.json')['policy']!=policy:
            raise ValueError('Promotion policy changed after dataset freeze')
        expected={}
        for split in ('regression','holdout'):
            cases,_=load_split(dataset_root,split,m['dataset_fingerprint'])
            expected.update({(split,c['id']):c for c in cases})
        for reports in (baseline,candidate):
            actual={(e['criteria']['split'],e['criteria']['case_id']) for e in reports}
            if actual!=set(expected): raise ValueError('Frozen evaluation cases missing / extra')
            for evaluation in reports:
                c=evaluation['criteria'];case=expected[(c['split'],c['case_id'])]
                if case['kind']=='scripted_task' and c.get('matches')!={'/'+k:v for k,v in case['expected'].items()}:
                    raise ValueError('Frozen expected output changed')
        if any(e['subject_version']!=identity for e in candidate) or any(e['subject_version']!=m['parent_version'] for e in baseline):
            raise ValueError('Comparison version mismatch')
        if any(e['created_at']<m['created_at'] for e in candidate): raise ValueError('Candidate evaluated before freeze')
        if any(e['criteria']['dataset_fingerprint']!=m['dataset_fingerprint'] for e in baseline+candidate):
            raise ValueError('Frozen dataset differs')
        if any(not e.get('permission_budget_fingerprint') for e in baseline+candidate):
            raise ValueError('Evaluator policy binding missing')
        if any(e['created_at']<m['created_at'] for e in baseline if e['criteria']['split']=='holdout'):
            raise ValueError('Holdout inspected before candidate freeze')
        self.event(identity,'evaluating')
        result=compare_versions(baseline,candidate,policy=policy)
        result['candidate_manifest_sha256']=sha(folder/'manifest.json')
        result['candidate_id']=identity
        result['dataset_root']=str(dataset_root)
        # Preserve full independent reports, not a model-supplied quality claim.
        result['baseline_reports']=baseline;result['candidate_reports']=candidate
        path=self.root/'comparisons'/(uuid4().hex+'.json');save(path,result)
        self.event(identity,'awaiting_manual_approval' if result['eligible'] else 'rejected',comparison_sha256=sha(path))
        return result,path

    def _receipt(self,action,identity,comparison_sha,*,simulated):
        if action not in {'promote','rollback'}: raise ValueError('Unknown approval action')
        if simulated and self.scope!='controlled_fixture': raise ValueError('Simulation forbidden outside fixture registry')
        value={'approval_id':uuid4().hex,'action':action,'target_version':identity,'current_version':self.active(),
               'comparison_sha256':comparison_sha,'at':utc_now(),'scope':self.scope,'simulated':simulated}
        value['signature']=hmac.new((self.authority/'approval-key').read_bytes(),digest(value).encode(),hashlib.sha256).hexdigest()
        path=self.root/'approvals'/(value['approval_id']+'.json');save(path,value)
        return path

    def approve_interactively(self,action,identity,comparison_sha):
        if not sys.stdin.isatty(): raise ValueError('Manual approval requires an interactive terminal')
        expected=action+' '+identity+' '+comparison_sha
        answer=input('Review frozen reports. Type exactly: '+expected+'\n')
        if answer!=expected: raise ValueError('Manual approval declined')
        return self._receipt(action,identity,comparison_sha,simulated=False)

    def fixture_approval(self,action,identity,comparison_sha):
        """Only controlled fixture demo/tests; never proof of actual human approval."""
        return self._receipt(action,identity,comparison_sha,simulated=True)

    def apply(self,action,identity,receipt_path,comparison_path):
        with self.lock():
            m,folder=self.version(identity);result=read(comparison_path);pin=sha(comparison_path)
            # Comparison must be an immutable local registry artifact.
            if Path(comparison_path).resolve().parent!=(self.root/'comparisons').resolve(): raise ValueError('Foreign comparison')
            r=read(receipt_path);signature=r.pop('signature',None)
            expected=hmac.new((self.authority/'approval-key').read_bytes(),digest(r).encode(),hashlib.sha256).hexdigest()
            if (not isinstance(signature,str) or not hmac.compare_digest(signature,expected)
                    or r['scope']!=self.scope or (r['simulated'] and self.scope!='controlled_fixture')
                    or r['action']!=action or r['target_version']!=identity or r['current_version']!=self.active()
                    or r['comparison_sha256']!=pin): raise ValueError('Manual approval missing / invalid / stale')
            history,_=self._history()
            if any(row['approval_id']==r['approval_id'] for row in history): raise ValueError('Approval already used')
            if not result['eligible']: raise ValueError('Failed candidate cannot be promoted')
            if sha(Path(result['dataset_root'])/'manifest.json')!=result['dataset_fingerprint']:
                raise ValueError('Frozen dataset changed after evaluation')
            if action=='promote':
                if (result['candidate_id']!=identity or m['parent_version']!=self.active()
                        or sha(folder/'manifest.json')!=result['candidate_manifest_sha256']):
                    raise ValueError('Candidate / active parent changed')
                # Recompute recommendation from preserved independent evaluations.
                computed=compare_versions(result['baseline_reports'],result['candidate_reports'],policy=result['policy'])
                if not computed['eligible']: raise ValueError('Independent evaluation gate failed')
                for evaluation in result['baseline_reports']+result['candidate_reports']:
                    from lab_evaluation import verify_pins
                    verify_pins(evaluation['run_path'],evaluation['run_files'])
            elif action=='rollback':
                if identity not in {row['version_id'] for row in history} or self.active()!=result['candidate_id']:
                    raise ValueError('Rollback target not a prior active version')
            else: raise ValueError('Unknown action')
            row=self._activate(identity,action,r,pin)
            self.event(identity,'approved' if action=='promote' else 'rolled_back',approval_id=r['approval_id'],
                       comparison_sha256=pin,simulated_approval=r['simulated'])
            return row

    def active_files(self):
        identity=self.active()
        if identity is None: raise ValueError('No active release')
        return self.version(identity)[1]/'files'
