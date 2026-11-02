"""Single Core entrypoint; planning/help are offline, execution is explicit."""
import argparse
import json
from pathlib import Path

from research_workflow import FrozenInputs, config_checked, run_workflow, verify_workflow


def main(argv=None):
    parser=argparse.ArgumentParser(description="Aeternitas Core: bounded evidence-based investment research")
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("capabilities",help="Offline implementation and validation boundaries")
    plan=sub.add_parser("plan",help="Validate research config offline; no collection or model request")
    plan.add_argument("--config",required=True,type=Path)
    run=sub.add_parser("research",help="Explicit one-request research; requires quality policy")
    run.add_argument("--config",required=True,type=Path)
    run.add_argument("--quality-policy",required=True,type=Path)
    run.add_argument("--profile",type=Path)
    run.add_argument("--frozen-inputs",type=Path,help="JSON source_root + bundles accepted by FrozenInputs")
    run.add_argument("--output",required=True,type=Path)
    run.add_argument("--env",type=Path,default=Path.cwd()/".env")
    run.add_argument("--execute",action="store_true",help="Required consent to real configured model/source calls")
    check=sub.add_parser("verify",help="Recheck saved Workflow report without external calls")
    check.add_argument("folder",type=Path)
    a=parser.parse_args(argv)
    if a.command=="capabilities":
        print(json.dumps({"core_version":"2.0.0","research":"implemented; semantic human review required",
          "memory":"real local Mem0 SDK, optional isolated interpreter; lexical retrieval",
          "langfuse":"adapter tested; requires credentials; cloud not validated",
          "rsi":"isolated candidates and manual approval; no autonomous production apply",
          "historical_replay":"A0/Time Machine unchanged; not automatically certified"},ensure_ascii=False,indent=2))
    elif a.command=="verify":
        verify_workflow(a.folder);print("verified")
    else:
        config=config_checked(json.loads(a.config.read_text()))
        if a.command=="plan":
            print(json.dumps({"config":config,"real_requests":0},ensure_ascii=False,indent=2))
        else:
            if not a.execute:parser.error("research requires --execute; use plan for offline inspection")
            from aeternitas import load_config
            provider=load_config(a.env)
            if provider["AETERNITAS_AUTH"]=="chatgpt" and provider["AETERNITAS_MODEL"]=="auto":
                from chatgpt_auth import ChatGPTAuth
                provider["AETERNITAS_MODEL"]=ChatGPTAuth().status().get("model")
            frozen=json.loads(a.frozen_inputs.read_text()) if a.frozen_inputs else None
            collector=FrozenInputs(**frozen) if frozen else None
            result=run_workflow(config,a.output,provider_config=provider,collector=collector,
                quality_policy=json.loads(a.quality_policy.read_text()),
                profile=json.loads(a.profile.read_text()) if a.profile else None)
            print(str(result))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
