#!/usr/bin/env python3
"""Back up a drained local installation; never includes credential files."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aeternitas import load_config
from m7_maintenance import backup
from research_service import ResearchService
from sources.sec import SourceError

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('data/live'))
    p.add_argument('--env',type=Path,default=Path('.env'))
    p.add_argument('--schema',default='public')
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--quiesced',action='store_true',help='API admission and all workers stopped after draining the queue')
    a=p.parse_args()
    try: result=backup(ResearchService(a.root,load_config(a.env),schema=a.schema),a.destination,quiesced_ack=a.quiesced)
    except (SourceError,ValueError,OSError) as exc: print(type(exc).__name__+': '+str(exc),file=sys.stderr);return 1
    print(json.dumps(result,ensure_ascii=False));return 0

if __name__=='__main__': raise SystemExit(main())
