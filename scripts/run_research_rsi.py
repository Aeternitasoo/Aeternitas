"""Execute one prepared frozen RSI trial; no data downloads or model retries."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from langfuse_adapter import Config,LangfuseAdapter
from research_rsi import execute_trial


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared',type=Path,required=True)
    p.add_argument('--execute-real',action='store_true',help='Authorize the frozen, at-most-four model request plan')
    p.add_argument('--langfuse',action='store_true',help='Explicit opt-in content-free export; requires configured credentials')
    args=p.parse_args()
    with LangfuseAdapter(Config.from_env() if args.langfuse else None) as sink:
        summary,path=execute_trial(args.prepared,execute_real=args.execute_real,sink=sink)
        sink.flush()
    print(json.dumps({'summary':summary,'path':str(path),'telemetry':sink.stats},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
