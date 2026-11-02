"""Run one frozen RSI v1.2 paired round; at most six sends and no retries."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from langfuse_adapter import Config,LangfuseAdapter
from research_validation import execute_round


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared',type=Path,required=True)
    p.add_argument('--execute-real',action='store_true')
    p.add_argument('--langfuse',action='store_true')
    args=p.parse_args()
    with LangfuseAdapter(Config.from_env() if args.langfuse else None) as sink:
        result,path=execute_round(args.prepared,execute_real=args.execute_real,sink=sink)
        sink.flush()
    print(json.dumps({'summary_path':str(path),'summary':result,'telemetry':sink.stats},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
