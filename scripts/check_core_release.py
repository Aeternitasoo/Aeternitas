"""Read-only release audit. Reports locations/patterns, never matched values."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

PATTERNS={
 "openai_key": re.compile(rb'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b'),
 "github_token": re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
 "jwt":re.compile(rb'\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b'),
 "private_key":re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
 "credential_url":re.compile(rb'https?://[A-Za-z0-9_-]{3,}:[^/\s@]{8,}@'),
}
BLOCKED_COMPONENTS={'.env','.venv','memory-data','memory-store','private-approval','approval-authority','data','__pycache__'}
MAX_FILE_BYTES=5*1024*1024
# Reviewed negative redaction fixture only; any byte change invalidates exemption.
SYNTHETIC_FIXTURES={('test_lab_evaluation.py','credential_url'):'276ab799449360b71599edaadd8d96d9d607bcc55c664cd026e60e040e21da01'}


def matches(data):
    return [{'pattern':name,'line':data.count(b'\n',0,m.start())+1}
            for name,pattern in PATTERNS.items() for m in pattern.finditer(data)]


def audit(root,*,history=True):
    root=Path(root).resolve()
    def git(*args):return subprocess.check_output(['git','-C',str(root),*args])
    names=git('ls-files','--cached','--others','--exclude-standard','-z').decode().split('\0')
    files=[];issues=[];synthetic=[]
    for name in sorted(set(names)-{''}):
        p=root/name
        if not p.exists():continue
        if p.is_symlink():issues.append({'path':name,'reason':'symlink'});continue
        if set(Path(name).parts)&BLOCKED_COMPONENTS or p.name=='approval-key':
            issues.append({'path':name,'reason':'local_or_sensitive_path'})
        data=p.read_bytes()
        if len(data)>MAX_FILE_BYTES:issues.append({'path':name,'reason':'oversize','bytes':len(data)})
        for m in matches(data):
            if SYNTHETIC_FIXTURES.get((name,m['pattern']))==hashlib.sha256(data).hexdigest():
                synthetic.append({'path':name,**m,'review':'pinned negative redaction fixture'})
            else:issues.append({'path':name,**m})
        files.append({'path':name,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
    history_issues=[];blobs=0
    if history:
        seen=set()
        for row in git('rev-list','--objects','--all').decode().splitlines():
            sha,_,name=row.partition(' ')
            if sha in seen:continue
            seen.add(sha)
            if git('cat-file','-t',sha).strip()!=b'blob':continue
            blobs+=1
            data=git('cat-file','blob',sha)
            for m in matches(data):
                if SYNTHETIC_FIXTURES.get((name,m['pattern']))==hashlib.sha256(data).hexdigest():
                    synthetic.append({'blob':sha,'path':name,**m,'review':'pinned negative redaction fixture'})
                else:history_issues.append({'blob':sha,'path':name,**m})
    return {'files':files,'issues':issues,'history_blobs_scanned':blobs,
            'history_issues':history_issues,'reviewed_synthetic_findings':synthetic,'pattern_scan_is_not_complete_secret_detection':True,
            'passed':not issues and not history_issues}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--current-only',action='store_true')
    a=p.parse_args();result=audit(a.root,history=not a.current_only)
    with a.output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='files'},indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
