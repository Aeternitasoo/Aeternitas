#!/usr/bin/env python3
"""Restore a trusted local backup to an independent empty DB and new data root."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from m7_maintenance import restore
from sources.sec import SourceError

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backup',type=Path,required=True)
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--database-env',default='AETERNITAS_RESTORE_DATABASE',help='Name of environment variable containing the empty target DB DSN')
    p.add_argument('--trusted-local-backup',action='store_true')
    a=p.parse_args()
    try:
        dsn=os.environ.get(a.database_env)
        if not dsn: raise SourceError('Target database environment variable is required')
        result=restore(a.backup,a.destination,dsn,trusted_local_backup=a.trusted_local_backup)
    except (SourceError,ValueError,OSError) as exc: print(type(exc).__name__+': '+str(exc),file=sys.stderr);return 1
    print(json.dumps(result,ensure_ascii=False));return 0

if __name__=='__main__': raise SystemExit(main())
