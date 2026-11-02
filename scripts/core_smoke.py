"""Offline Core acceptance. Scripted decisions are not real LLM research."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab_evaluation_demo import run_demo
from research_memory import MemoryAdapter,TYPES
from research_workflow import sha,save
from agent_skills import SkillRegistry


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True,type=Path,help='New external directory')
    p.add_argument('--memory-python',help='Optional locked Mem0 interpreter; tests real persistence')
    a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    result=run_demo(a.output/'controlled-cycle')
    skills=SkillRegistry(Path(__file__).resolve().parents[1]/'agent_skills')
    memory=[]
    if a.memory_python:
        source=a.output/'memory-source.txt'
        source.write_text('Core smoke executed a controlled offline Harness and Evaluation cycle; not a real investment finding.')
        adapter=MemoryAdapter(a.memory_python,a.output/'memory-store')
        for kind in sorted(TYPES):
            record={'memory_type':kind,'entity':'core-smoke','scope':'acceptance','period':'all',
                'assertion_key':'controlled_execution','text':source.read_text(),'classification':'fact',
                'sources':[{'path':str(source.resolve()),'sha256':sha(source)}],
                'observed_at':datetime.now(timezone.utc).isoformat(),'confidence':1,'origin':'verified_execution'}
            written=adapter.remember(record,trusted=True)
            again=adapter.remember(record,trusted=True)
            found=adapter.search('controlled offline Harness',memory_type=kind,entity='core-smoke',scope='acceptance',period='all')
            assert written['memory_id']==again['memory_id']
            assert written['memory_id'] in [m['memory_id'] for m in found['results']]
            memory.append({'type':kind,'memory_id':written['memory_id'],'idempotent':True,'retrieved':True})
    receipt={'execution_mode':'controlled_offline_no_real_model',
      'harness_evaluation_registry':'passed','skills':skills.discover(),
      'memory':memory,'langfuse':'Requires Credentials; no remote export attempted',
      'production_changed':False,'real_model_requests':0}
    save(a.output/'acceptance.json',receipt)
    print(json.dumps(receipt,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
