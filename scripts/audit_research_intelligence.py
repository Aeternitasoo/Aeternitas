"""Offline opt-in preflight over frozen Workflow inputs; never calls a model."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research_intelligence import completeness
from research_workflow import save


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',required=True)
    p.add_argument('--bundle',required=True)
    p.add_argument('--run',required=True)
    p.add_argument('--topics',required=True,help='Explicit JSON list of research and counterevidence topics')
    p.add_argument('--output',required=True,help='New file; never overwritten')
    a=p.parse_args();read=lambda path:json.loads(Path(path).read_text())
    r=Path(a.run)
    audit=completeness(a.source_root,read(a.bundle),read(r/'evidence.json'),read(r/'raw-response.json'),read(a.topics))
    save(Path(a.output),audit)
    print(json.dumps({'output':a.output,'complete':audit['complete'],'topics':len(audit['topics'])}))


if __name__=='__main__':main()
