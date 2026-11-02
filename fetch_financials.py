"""Fetch financial input for the research workflow; no LLM call is made yet."""

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from company_identity import normalize_ticker, resolve_company
from financial_facts import build_financial_summary
from sources.sec import SecClient, SourceError, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fetch live SEC annual financial facts")
    parser.add_argument("company", type=str.upper, help="US filing issuer ticker, e.g. PLTR")
    parser.add_argument("--as-of", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    parser.add_argument("--refresh", action="store_true", help="Bypass cache and request SEC")
    parser.add_argument("--data-dir", type=Path, default=Path("data/live"))
    args = parser.parse_args(argv)
    try:
        cutoff = date.fromisoformat(args.as_of)
        if cutoff > datetime.now(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError("--as-of cannot be in the future")
        companies = json.loads(
            (Path(__file__).parent / "config/companies.json").read_text(encoding="utf-8")
        )
        args.company = normalize_ticker(args.company)
        contact = os.environ.get("SEC_CONTACT", "").strip()
        if not contact and sys.stdin.isatty():
            contact = input("输入你的联系邮箱（仅用于 SEC 请求，不用发给我）：").strip()
        if not contact:
            raise ValueError("Set SEC_CONTACT locally or run in an interactive terminal")
        identity = resolve_company(args.company,args.data_dir,contact,overrides=companies)
        args.company, cik = identity["ticker"], identity["cik"]
        with SecClient(args.data_dir, contact) as client:
            snapshot = client.companyfacts(cik, refresh=args.refresh)
        summary = build_financial_summary(raw=snapshot.data, cik=cik,
                                          as_of_date=cutoff.isoformat(), source=snapshot.reference())
        output = args.data_dir / "financials" / f"{args.company}-{uuid4().hex}.json"
        write_json(output, summary)
    except (SourceError, ValueError, OSError, KeyError, EOFError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"公司：{summary['company_name']} ({args.company})")
    print(f"来源：{'缓存命中' if snapshot.cache_hit else 'SEC API 真实请求'}")
    print(f"获取时间：{snapshot.metadata['fetched_at']}")
    print(f"来源编号：{snapshot.metadata['retrieval_id']}")
    print(f"分析截止日：{summary['as_of_date']}（日期级）")
    for period in summary["annual_periods"]:
        print(f"\n年度期间：{period['period_start']} → {period['period_end']}")
        for metric, label in (("revenue", "收入"), ("operating_income", "营业利润"), ("net_income", "净利润")):
            fact = period["facts"][metric]
            print(f"  {label}：USD {fact['value']}" if fact else f"  {label}：缺失")
        for key, label in (("operating_margin", "营业利润率"), ("net_margin", "净利润率")):
            value = period[key]
            print(f"  {label}：{value['value']}%" if value["value"] is not None
                  else f"  {label}：不可计算 ({value['unavailable_reason']})")
    growth = summary["revenue_yoy"]
    print(f"\n营收同比：{growth['value']}%" if growth["value"] is not None
          else f"\n营收同比：不可计算 ({growth['unavailable_reason']})")
    for warning in summary["warnings"]:
        print(f"注意：{warning}")
    print(f"\n完整结果：{output}")
    print(f"原始响应：{args.data_dir / snapshot.metadata['snapshot_path']}")
    print("本轮是财务数据输入，尚不是 AI 研究报告；财务可比口径仍需回查原申报。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
