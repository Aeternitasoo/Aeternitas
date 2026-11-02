"""Aeternitas: local task API/worker CLI plus explicit source and offline audit tools."""

import argparse
import getpass
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from chatgpt_auth import ChatGPTAuth
from research_pipeline import run_research, saved_report
from research_report import render_report
from sources.sec import SourceError, write_atomic
from research_service import ServiceError


PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_REQUIRED = ("SEC_CONTACT", "FINNHUB_API_KEY")
CONFIG_KEYS = (*SOURCE_REQUIRED, "OPENAI_API_KEY", "AETERNITAS_MODEL", "AETERNITAS_AUTH", "DATABASE_URL")


def required(config):
    return SOURCE_REQUIRED + (("OPENAI_API_KEY",) if config["AETERNITAS_AUTH"] == "api_key" else ())


def load_config(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ValueError("Invalid .env syntax; use KEY=value (no shell commands)")
            value = value.strip()
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            if not isinstance(value, str) or "\n" in value or "\r" in value:
                raise ValueError("Configuration values must be single-line text")
            values[key] = value
    for key in CONFIG_KEYS:
        if key in os.environ:
            values[key] = os.environ[key].strip()
    values.setdefault("AETERNITAS_AUTH", "chatgpt")
    if values["AETERNITAS_AUTH"] not in {"chatgpt", "api_key"}:
        raise ValueError("AETERNITAS_AUTH must be chatgpt or api_key")
    values.setdefault("AETERNITAS_MODEL", "auto" if values["AETERNITAS_AUTH"] == "chatgpt" else "gpt-4.1-mini")
    return values


def configure(path, force=False, provider="chatgpt"):
    if not sys.stdin.isatty():
        raise ValueError("Run configure in your own interactive terminal")
    print("模型入口：ChatGPT 登录授权（订阅额度，仍受账号资格 / 限额约束）。" if provider == "chatgpt"
          else "模型入口：OpenAI Platform API key，独立 API 计费（你明确选择了此模式）。")
    print("新闻与 SEC 配置只写入本机，不用发到聊天；没有新闻 key 请先取得，不要乱填。")
    values = load_config(path) if path.exists() else {}
    previous = values.get("AETERNITAS_AUTH")
    values["AETERNITAS_AUTH"] = provider
    for key, label in (("SEC_CONTACT", "SEC 联系邮箱："), ("FINNHUB_API_KEY", "Finnhub API key（隐藏输入）：")):
        if force or not values.get(key):
            values[key] = (getpass.getpass(label) if key.endswith("KEY") else input(label)).strip()
    if provider == "api_key":
        if force or not values.get("OPENAI_API_KEY"):
            values["OPENAI_API_KEY"] = getpass.getpass("OpenAI API key（隐藏输入）：").strip()
        values["AETERNITAS_MODEL"] = input("API 模型 [gpt-4.1-mini]：").strip() or "gpt-4.1-mini"
    elif previous != "chatgpt" or force or values.get("AETERNITAS_MODEL") == "gpt-4.1-mini":
        values["AETERNITAS_MODEL"] = "auto"
    values.setdefault("AETERNITAS_MODEL", "auto")
    if any(not values[key] for key in required(values)):
        raise ValueError("Credentials cannot be empty; no file written")
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Configuration cannot contain newlines")
    write_atomic(path, ("\n".join(f"{key}={json.dumps(value)}" for key, value in values.items()) + "\n").encode())
    path.chmod(0o600)
    print("本机数据源配置已保存；ChatGPT 模式不用填写 OpenAI API key。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Aeternitas 1.0 · local research tasks, evidence and reports")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data/live")
    parser.add_argument("--api-url", default="http://127.0.0.1:8767", help="Local API URL; loopback only")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("configure", help="Write local credentials interactively")
    setup.add_argument("--force", action="store_true")
    setup.add_argument("--provider", choices=("chatgpt", "api_key"), default="chatgpt")
    login = sub.add_parser("login", help="Continue with ChatGPT and choose an account-provided model")
    login.add_argument("--new-account", action="store_true")
    login.add_argument("--if-needed", action="store_true", help="Reuse a locally connected session when possible")
    sub.add_parser("auth-status", help="Check local ChatGPT login; no token output")
    sub.add_parser("logout", help="Clear active local tokens; remote revocation not confirmed")
    sub.add_parser("models", help="Read selected ChatGPT account's model catalog")
    sub.add_parser("doctor", help="Check local configuration; no network or paid calls")
    readiness = sub.add_parser("audit-readiness", help="Verify an archive and check input timing offline; creates no prediction")
    readiness.add_argument("run_id")
    readiness.add_argument("--cutoff", required=True, help="Explicit aware timestamp, e.g. 2026-10-24T12:00:00Z")
    importing_csv = sub.add_parser('import-vst-csv', help='Offline user-provided Yahoo CSV import; no credentials or network')
    importing_csv.add_argument('csv', type=Path)
    importing_csv.add_argument('--output-root', type=Path, default=(Path.home() / '.local/share/aeternitas-lab/datasets/vst'))
    importing_csv.add_argument('--calendar', type=Path, help='Optional real XNYS session calendar; missing coverage is reported')
    importing_csv.add_argument('--expected-sha256')
    importing_csv.add_argument('--cutoff', action='append', default=None)
    machine = sub.add_parser("time-machine", help="Offline version-pinned CSV inputs and A0 admission manifest; no prediction")
    machine.add_argument("symbol", type=str.upper)
    machine.add_argument("--csv", required=True, type=Path)
    machine.add_argument("--start", required=True)
    machine.add_argument("--end", required=True)
    machine.add_argument("--cutoff", required=True, help="Historical cutoff with explicit seconds and timezone")
    machine.add_argument("--types", nargs="+", choices=("1d", "1w", "1mo"), default=["1d"])
    machine.add_argument("--calendar", type=Path)
    machine.add_argument("--dataset-sha256", help="Refuse a CSV version different from this SHA-256")
    machine.add_argument("--calendar-sha256", help="Refuse a calendar version different from this SHA-256")
    machine.add_argument("--mode", choices=("strict", "assumed"), default="strict")
    machine.add_argument("--assumption", action="append", default=[], help="Required in assumed mode; recorded verbatim")
    machine.add_argument("--output", type=Path, help="Create a new manifest JSON; never overwrite")
    for market_name in ("market", "market-check"):
        market = sub.add_parser(market_name, help=("Query CSV or explicitly fetch Finnhub daily/weekly/monthly candles"
            if market_name == "market" else "Explicit ONLINE Finnhub candles probe; uses account quota and saves source snapshots"))
        market.add_argument("symbol", type=str.upper)
        market.add_argument("--start", required=True)
        market.add_argument("--end", required=True)
        market.add_argument("--exchange", default="XNAS", help="Caller-supplied MIC; verify for the requested listing")
        market.add_argument("--timezone", default="America/New_York", help="Caller-supplied exchange timezone")
        market.add_argument("--currency", default="USD")
        market.add_argument("--output", type=Path, help="Create a new JSON file; existing destinations are refused")
        if market_name == "market":
            source = market.add_mutually_exclusive_group(required=True)
            source.add_argument("--csv", type=Path)
            source.add_argument("--finnhub", action="store_true", help="Explicit ONLINE request using existing Finnhub key")
            market.add_argument("--interval", choices=("1d", "1w", "1mo"), default="1d")
            market.add_argument("--cutoff", required=True, help="Aware ISO timestamp; required even for online inspection")
            market.add_argument("--time-policy", choices=("strict", "captured"), default="strict",
                                help="captured permits observation of captured adjusted/unknown-publication data, never PIT certification")
            market.add_argument("--calendar", type=Path, help="Explicit versioned session-calendar JSON for aggregate completeness")
            market.add_argument("--include-incomplete", action="store_true")
    for name in ("run", "run-local", "collect"):
        help_text={"run":"Submit a background research task through the local API",
                   "run-local":"Explicit synchronous pipeline for diagnostics (calls the configured model)",
                   "collect":"Fetch sources without any model call"}
        run = sub.add_parser(name, help=help_text[name])
        run.add_argument("company", type=str.upper)
        run.add_argument("--as-of", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
        run.add_argument("--question", default="最近已披露的经营表现有什么变化？近期重要事件、支持与反对依据以及信息缺口是什么？")
        run.add_argument("--refresh", action="store_true")
        run.add_argument("--news-days", type=int, default=14)
        run.add_argument("--deadline", type=int, default=300)
        if name == "run":
            run.add_argument("--idempotency-key", help="Reuse this key and identical arguments after an uncertain submit; changed arguments conflict")
            run.add_argument("--wait", action="store_true", help="Poll the queued task until it finishes")
            run.add_argument("--wait-seconds", type=int, default=360)
    resolve = sub.add_parser("resolve", help="Identify a ticker through SEC; no model calls")
    resolve.add_argument("company")
    resolve.add_argument("--refresh", action="store_true")
    serve = sub.add_parser("serve", help="Start the loopback task API; no model work in this process")
    serve.add_argument("--port", type=int, default=8767)
    worker = sub.add_parser("worker", help="Execute queued research using the existing pipeline")
    worker.add_argument("--once", action="store_true", help="Claim at most one queued task and exit")
    sub.add_parser("db-init", help="Apply checked domain schema migrations")
    sub.add_parser("data-list", help="List imported evidence datasets")
    importing = sub.add_parser("data-import", help="Atomically import verified saved evidence; no external calls")
    importing.add_argument("run_id", nargs="?")
    importing.add_argument("--bundle", type=Path)
    facts = sub.add_parser("data-facts", help="Query facts joined to original source locations")
    facts.add_argument("company", type=str.upper)
    facts.add_argument("--dataset")
    evidence = sub.add_parser("data-evidence", help="Inspect one immutable dataset evidence version")
    evidence.add_argument("dataset_id")
    evidence.add_argument("evidence_id")
    calculations = sub.add_parser("data-calculations", help="Query formulas and their exact input fact IDs")
    calculations.add_argument("dataset_id")
    listing = sub.add_parser("list", help="Query persisted research tasks through the local API")
    listing.add_argument("--company",type=str.upper)
    listing.add_argument("--status")
    listing.add_argument("--limit",type=int,default=50)
    listing.add_argument("--offset",type=int,default=0)
    for name in ("status", "report", "evidence", "wait", "cancel", "resume", "verify", "replay"):
        command = sub.add_parser(name,help={"status":"Query persisted state, steps and errors",
            "report":"Verify publication and print local HTML / JSON URLs",
            "evidence":"Query this research dataset's evidence",
            "wait":"Wait for an existing task; does not cancel it on timeout",
            "cancel":"Request cancellation; an external request already sent may still consume quota",
            "resume":"Resume an eligible task with its saved successful steps and remaining budget",
            "verify":"Recheck saved sources, numbers, citations and hashes offline",
            "replay":"Render a saved report offline without new model calls"}[name])
        command.add_argument("run_id")
        if name == "evidence": command.add_argument("evidence_id",nargs="?")
        if name == "wait": command.add_argument("--seconds",type=int,default=360)
        if name == "resume":
            command.add_argument("--acknowledge-unknown", action="store_true",
                help="Acknowledge that retrying an unconfirmed external call may consume quota again")
    args = parser.parse_args(argv)
    try:
        if args.command == 'import-vst-csv':
            from yahoo_csv import cli
            args.cutoff = args.cutoff or ['2022-06-30T16:01:00-04:00', '2024-06-28T16:01:00-04:00']
            return cli(args)
        if args.command == "time-machine":
            from time_machine import cli
            return cli(args)
        if args.command in {"market", "market-check"}:
            from market_data import cli
            config = None if args.command == "market" and args.csv else load_config(PROJECT_ROOT / ".env")
            return cli(args, config)
        if args.command == "audit-readiness":
            from point_in_time import audit_saved_run
            print(json.dumps(audit_saved_run(args.data_dir, args.run_id, args.cutoff), ensure_ascii=False, indent=2))
            return 0
        if args.command == "configure":
            configure(PROJECT_ROOT / ".env", args.force, args.provider)
            return 0
        if args.command in {"login", "auth-status", "logout", "models"}:
            auth = ChatGPTAuth()
            if args.command == "auth-status":
                print(json.dumps(auth.status(), ensure_ascii=False, indent=2))
            elif args.command == "logout":
                print(auth.logout())
            elif args.command == "models":
                for model in auth.models():
                    print(model["slug"], model["display_name"])
            else:
                status = auth.status()
                if not args.if_needed or args.new_account or not status["connected"] or not status["plan_permission"]:
                    auth.login(new_account=args.new_account)
                model = auth.choose_model(interactive=True)
                print(f"ChatGPT 授权和模型选择已保存：{model}。尚未发起研究模型调用。")
            return 0
        config = load_config(PROJECT_ROOT / ".env")
        if args.command == "resolve":
            from research_service import ResearchService
            print(json.dumps(ResearchService(args.data_dir,config).resolve(args.company,refresh=args.refresh),ensure_ascii=False,indent=2))
            return 0
        if args.command in {"serve","worker"}:
            from research_service import ResearchService
            service=ResearchService(args.data_dir,config)
            if args.command == "serve":
                import uvicorn
                from local_api import create_app
                if not 1024<=args.port<=65535: raise ValueError("Port must be 1024–65535")
                uvicorn.run(create_app(service),host="127.0.0.1",port=args.port,access_log=False,proxy_headers=False)
            else:
                from research_worker import work
                work(service,once=args.once)
            return 0
        if args.command in {"run","list","status","report","evidence","wait","cancel","resume"}:
            from api_client import APIClient, request_key
            from research_service import validate_request, valid_id
            from urllib.parse import quote
            payload=None
            if args.command == "run":
                payload=validate_request(args.company,args.as_of,args.question,args.refresh,args.news_days,args.deadline)
                payload.pop("cik")
                if not 1<=args.wait_seconds<=3600: raise ValueError("Wait seconds must be 1–3600")
                key=request_key(args.idempotency_key)
            elif args.command != "list": valid_id(args.run_id)
            with APIClient(args.api_url) as client:
                if args.command == "run":
                    print(f"幂等键：{key}；提交结果不明时，以相同参数和 --idempotency-key {key} 重试。",flush=True)
                    result=client.create(payload,idempotency_key=key)
                    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
                    if not args.wait: return 0
                    result=client.wait(result['run_id'],args.wait_seconds)
                elif args.command == "list":
                    params={k:v for k,v in dict(ticker=args.company,status=args.status,limit=args.limit,offset=args.offset).items() if v is not None}
                    result=client.request('GET','/research-runs',params=params)
                elif args.command == "wait":
                    if not 1<=args.seconds<=3600: raise ValueError("Wait seconds must be 1–3600")
                    result=client.wait(args.run_id,args.seconds)
                elif args.command == "status": result=client.request('GET',f'/research-runs/{args.run_id}')
                elif args.command == "cancel": result=client.request('POST',f'/research-runs/{args.run_id}/cancel')
                elif args.command == "resume":
                    if args.acknowledge_unknown:
                        print("已确认：重试结果不明的外部调用可能再次消耗额度；累计调用预算仍生效。",flush=True)
                    result=client.request('POST',f'/research-runs/{args.run_id}/resume',
                                          json={'acknowledge_unknown':args.acknowledge_unknown})
                elif args.command == "report":
                    result=client.request('GET',f'/research-runs/{args.run_id}/report.json')
                    print(f"HTML 报告：{client.url}/research-runs/{args.run_id}/report.html")
                    print(f"JSON 报告：{client.url}/research-runs/{args.run_id}/report.json")
                    print("已通过保存报告、来源与引用核验。")
                    return 0
                else:
                    suffix='/'+quote(args.evidence_id,safe='') if args.evidence_id else ''
                    result=client.request('GET',f'/research-runs/{args.run_id}/evidence{suffix}')
            print(json.dumps(result,ensure_ascii=False,indent=2))
            if args.command in {'run','wait'} and result.get('status') in {'completed','completed_with_gaps'}:
                # The report endpoint performs the full saved-output audit.
                with APIClient(args.api_url) as report_client:
                    report_client.request('GET',f"/research-runs/{result['run_id']}/report.json")
                    print(f"HTML 报告：{report_client.url}/research-runs/{result['run_id']}/report.html")
                    print(f"JSON 报告：{report_client.url}/research-runs/{result['run_id']}/report.json")
            return 1 if args.command in {'run','status','wait'} and result.get('status') in {'failed','interrupted','cancelled'} else 0
        if args.command == "db-init" or args.command.startswith("data-"):
            import evidence_repository as repository
            dsn = config.get("DATABASE_URL") or repository.DEFAULT_DATABASE
            # Resolve and verify files before opening a transaction/connection.
            bundle = None
            if args.command == "data-import":
                if bool(args.run_id) == bool(args.bundle):
                    raise ValueError("Provide exactly one run ID or --bundle path")
                if args.run_id:
                    folder, _ = saved_report(args.data_dir,args.run_id)
                    bundle = json.loads((folder / "input_bundle.json").read_text(encoding="utf-8"))
                else:
                    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
            with repository.connect(dsn) as connection:
                if args.command == "db-init":
                    repository.migrate(connection)
                    result = {"schema": "M2 evidence + M3 tasks + M5 recovery", "status": "ready"}
                elif args.command == "data-import":
                    result = {"dataset_id": repository.import_bundle(connection,args.data_dir,bundle)}
                elif args.command == "data-list":
                    result = repository.list_datasets(connection)
                elif args.command == "data-facts":
                    result = repository.facts(connection,args.company,args.dataset)
                elif args.command == "data-calculations":
                    result = repository.calculations(connection,args.dataset_id)
                else:
                    result = repository.evidence(connection,args.dataset_id,args.evidence_id)
            print(json.dumps(result,default=repository.json_value,ensure_ascii=False,indent=2))
            return 0
        if args.command == "doctor":
            missing = [key for key in required(config) if not config.get(key)]
            for key in required(config):
                print(f"{key}: {'已配置' if config.get(key) else '缺失'}")
            print(f"模型入口：{config['AETERNITAS_AUTH']}")
            login_missing = False
            if config["AETERNITAS_AUTH"] == "chatgpt":
                status = ChatGPTAuth().status()
                login_missing = not (status["connected"] and status["plan_permission"] and status["model"])
                print("ChatGPT：" + ("本机已有授权和模型选择" if not login_missing else "需要运行 login"))
            print(f"模型：{config['AETERNITAS_MODEL']}")
            print("本检查没有访问供应商；配置存在不代表凭据有效或账号有权限。")
            return 1 if missing or login_missing else 0
        if args.command in {"verify", "replay"}:
            folder, report = saved_report(args.data_dir, args.run_id)
            if args.command == "replay":
                report["evidence_kind"] = "replay_saved_output"
                write_atomic(folder / "report-replay.html", render_report(report).encode())
                print(f"已回放已保存分析，无新 API / 模型调用：{folder / 'report-replay.html'}")
            else:
                print("来源字节、财务重算、原文位置、引用摘录与报告指纹通过；论点语义仍需人工复核。")
            return 0
        cutoff = date.fromisoformat(args.as_of)
        if cutoff > datetime.now(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError("--as-of cannot be in the future")
        if not 1 <= args.news_days <= 30 or not 1 <= args.deadline <= 1800:
            raise ValueError("News window must be 1–30 days; deadline 1–1800 seconds")
        if not args.question.strip() or len(args.question) > 1200:
            raise ValueError("Question must contain 1–1200 characters")
        from research_service import ResearchService
        # Same deterministic discovery as formal API tasks; no private config edits.
        identity = ResearchService(args.data_dir,config).resolve(args.company)
        args.company = identity['ticker']
        cik = identity['cik']
        print(f"已识别：{args.company} / CIK {cik} / {identity.get('name') or identity['source']}",flush=True)
        needed = SOURCE_REQUIRED if args.command == "collect" else required(config)
        missing = [key for key in needed if not config.get(key)]
        if missing:
            raise ValueError("缺少本机配置：" + ", ".join(missing) + "；先运行 python aeternitas.py configure")
        if args.command == "collect":
            from research_inputs import collect_inputs
            from research_pipeline import audit_inputs
            from research_runtime import Budget
            from sources.sec import write_json
            from uuid import uuid4
            inputs = collect_inputs(args.data_dir,args.company,cik,cutoff,args.question,
                                    config,refresh=args.refresh,news_days=args.news_days,budget=Budget(args.deadline),
                                    progress=lambda step: print(f"[M2] {step}",flush=True))
            audit_inputs(args.data_dir,inputs)
            path = args.data_dir / "bundles" / f"{args.company}-{uuid4().hex}.json"
            write_json(path,inputs)
            print(f"来源包：{path}（无模型调用）")
            if config.get("DATABASE_URL"):
                from evidence_repository import connect, import_bundle
                with connect(config["DATABASE_URL"]) as connection:
                    print("数据集：" + import_bundle(connection,args.data_dir,inputs))
            return 0
        auth = None
        if config["AETERNITAS_AUTH"] == "chatgpt":
            auth = ChatGPTAuth()
            config["AETERNITAS_MODEL"] = auth.choose_model(config["AETERNITAS_MODEL"])
        run_options = {"plan_auth": auth} if auth is not None else {}
        print("模型入口：ChatGPT 订阅授权（无付费 API 自动回退）" if auth is not None else "模型入口：独立 API key，按 API 计费", flush=True)
        run_id, report = run_research(args.data_dir, args.company, cik,
                                      cutoff, args.question, config, refresh=args.refresh,
                                      news_days=args.news_days, seconds=args.deadline,
                                      progress=lambda step: print(f"[研究] {step}", flush=True), **run_options)
        saved_report(args.data_dir, run_id)
        folder = args.data_dir / "runs" / run_id
        print(f"\n研究编号：{run_id}\n状态：{report['status']}\n模型调用尝试：{report['model_attempts']}（上限 8）")
        print(f"HTML 报告：{folder / 'report.html'}\n结构化结果：{folder / 'report.json'}")
        print(f"来源核对：python aeternitas.py verify {run_id}")
        print("本次报告已自动完成保存后的来源 / 引用核对；verify 可再次检查。")
        print("请打开报告抽查一个财务值和一条新闻论点；自动引用检查不等于所有解释正确。")
        return 0
    except KeyboardInterrupt:
        if args.command == 'wait' or (args.command == 'run' and args.wait):
            print("\n已停止等候；后台任务没有因此取消。需要取消时使用 cancel RUN_ID。",file=sys.stderr)
        else:
            print("\n操作已停止；未把中断当作成功。", file=sys.stderr)
        return 130
    except (ServiceError, SourceError, ValueError, OSError, KeyError, EOFError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
