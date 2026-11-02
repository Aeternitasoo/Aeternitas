#!/usr/bin/env python3
"""Read-only maintenance scan, or explicit offline repair of one completed run."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aeternitas import load_config
from m7_maintenance import maintenance_scan,repair_completed
from research_service import ResearchService
from sources.sec import SourceError

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('data/live'))
    p.add_argument('--env',type=Path,default=Path('.env'))
    p.add_argument('--schema',default='public')
    p.add_argument('--repair-run')
    p.add_argument('--backup',type=Path)
    p.add_argument('--quiesced',action='store_true')
    a=p.parse_args()
    try:
        service=ResearchService(a.root,load_config(a.env),schema=a.schema)
        result=repair_completed(service,a.repair_run,quiesced_ack=a.quiesced,backup_folder=a.backup) if a.repair_run else maintenance_scan(service)
    except (SourceError,ValueError,OSError) as exc: print(type(exc).__name__+': '+str(exc),file=sys.stderr);return 1
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0

if __name__=='__main__': raise SystemExit(main())
