"""Offline controlled Harness -> evaluation -> candidate -> approval simulation."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab_evaluation_demo import run_demo
from langfuse_adapter import Config,LangfuseAdapter


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--langfuse',action='store_true',help='Explicit opt-in export of synthetic metadata only')
    args=p.parse_args()
    config=Config.from_env() if args.langfuse else None
    if args.langfuse and config is None: p.error('Langfuse credentials and explicit host required; no export started')
    with LangfuseAdapter(config) as sink:
        summary=run_demo(args.output,sink=sink)
        sink.flush()
    print(json.dumps({'summary':summary,'telemetry':sink.stats},ensure_ascii=False,indent=2))


if __name__=='__main__': raise SystemExit(main())
