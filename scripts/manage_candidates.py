"""Trusted human terminal gate for an external, isolated release registry."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from candidate_registry import CandidateRegistry
from lab_evaluation import read
from research_workflow import sha


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--registry',type=Path,required=True)
    p.add_argument('--authority',type=Path,required=True)
    p.add_argument('--scope',choices=['research','controlled_fixture'],default='research')
    p.add_argument('action',choices=['status','review','promote','rollback'])
    p.add_argument('--version')
    p.add_argument('--comparison',type=Path)
    args=p.parse_args()
    registry=CandidateRegistry(args.registry,authority_root=args.authority,scope=args.scope)
    if args.action=='status':
        print(json.dumps({'active':registry.active(),'scope':args.scope}));return 0
    if args.comparison is None:p.error('--comparison is required')
    result=read(args.comparison)
    print(json.dumps({k:v for k,v in result.items() if k not in {'baseline_reports','candidate_reports'}},indent=2))
    if args.action=='review':return 0
    if not args.version:p.error('--version is required')
    receipt=registry.approve_interactively(args.action,args.version,sha(args.comparison))
    row=registry.apply(args.action,args.version,receipt,args.comparison)
    print(json.dumps(row,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
