"""Offline-readable HTML, rendered from validated structured data, not model HTML."""

from html import escape

from research_runtime import public_url


def _financial_section(summary, indexed, h, number_link, heading="核心财务 · USD"):
    rows = []
    for annual in summary["annual_periods"]:
        values = []
        for metric in ("revenue", "operating_income", "net_income"):
            fact = annual["facts"][metric]
            values.append(number_link(h(fact['value']), f"V-{fact['fact_id'][:16]}") if fact else "缺失")
        for metric in ("operating_margin", "net_margin"):
            value = annual[metric]
            values.append(number_link(h(value['value']) + '%', indexed[f"{metric}:{annual['period_end']}"])
                          if value['value'] is not None else h(value['unavailable_reason']))
        rows.append("<tr><td>" + h(annual["period_start"] + " → " + annual["period_end"]) + "</td>"
                    + "".join(f"<td>{v}</td>" for v in values) + "</tr>")
    growth = summary["revenue_yoy"]
    growth_text = h(growth["value"]) + "%" if growth["value"] is not None else h(growth["unavailable_reason"])
    if growth['value'] is not None:
        growth_text = number_link(growth_text, indexed['revenue_yoy'])
    return f'<section><h2>{h(heading)}</h2><div class="scroll"><table><thead><tr><th>年度期间</th><th>收入</th><th>营业利润</th><th>净利润</th><th>营业利润率</th><th>净利润率</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div><p>营收同比：{growth_text}。数值由 Python / Decimal 计算。</p></section>'


def render_report(report):
    def h(value):
        return escape(str(value), quote=True)

    def link(url):
        try:
            public_url(url)
        except ValueError:
            return h(url)
        return f'<a href="{h(url)}" rel="noreferrer noopener">原始来源</a>'

    summary = report["financials"]
    indexed={e['title']:e['id'] for e in report['evidence'] if e['kind']=='calculation'}
    def number_link(text,evidence_id):
        return f'<a href="#{h(evidence_id)}" title="查看事实、公式与原始来源">{text}</a>' if report.get('analysis_provenance') else text
    panels = report.get('financial_panels')
    if panels is None:
        financial_html = _financial_section(summary, indexed, h, number_link)
    else:
        financial_html = ''.join(_financial_section(p['summary'],
            {e['title']: e['id'] for e in report['evidence']
             if e['kind'] == 'calculation' and e.get('symbol') == p['symbol']}, h, number_link,
            p['report_entity'] + ' · ' + p['symbol'] + ' · USD') for p in panels)
    sections = []
    labels = {"fundamentals": "基本面分析", "events": "新闻事件", "risk": "风险审阅", "synthesis": "综合判断"}
    for role, label in (() if report.get('research_sections') is not None else labels.items()):
        result = report["analyses"][role]
        claims = []
        for claim in result["claims"]:
            cites = " ".join(f'<a href="#{h(c["evidence_id"])}">[{h(c["evidence_id"])}]</a>' for c in claim["citations"])
            quotes = "".join(f'<blockquote>{h(c["quote"])}</blockquote>' for c in claim["citations"])
            claims.append(f'<li><span class="tag">{h(claim["stance"])}</span> {h(claim["text"])} {cites}{quotes}</li>')
        gaps = "".join(f"<li>{h(g)}</li>" for g in result["gaps"])
        sections.append(f'<section><h2>{label}</h2><ul>{"".join(claims)}</ul>'
                        + (f'<h3>信息缺口</h3><ul>{gaps}</ul>' if gaps else "") + "</section>")
    if report.get('research_sections') is not None:
        classification = {'fact': '事实', 'inference': '推断', 'assumption': '假设', 'unknown': '未知'}
        for section in report['research_sections']:
            claims = []
            for claim in section['claims']:
                cites = ' '.join(f'<a href="#{h(c["evidence_id"])}">[{h(c["evidence_id"])}]</a>' for c in claim['citations'])
                quotes = ''.join(f'<blockquote>{h(c["quote"])}</blockquote>' for c in claim['citations'])
                claims.append(f'<li><span class="tag">{classification[claim["classification"]]}</span> '
                              f'{h(claim["text"])} {cites}{quotes}</li>')
            sections.append(f'<section><h2>{h(report["section_titles"][section["topic"]])}</h2>'
                            f'<ul>{"".join(claims)}</ul></section>')
    sources = []
    for evidence in report["evidence"]:
        source = evidence["source"]
        sources.append(f'<details id="{h(evidence["id"])}"><summary>{h(evidence["id"])} · {h(evidence["title"])}</summary>'
                       f'<p>{link(evidence["source_url"])} · 发布 / 提交：{h(evidence["published_at"])}'
                       f' · 获取：{h(source["fetched_at"])} · 缓存：{h(source["cache_hit"])}</p>'
                       f'<pre>{h(evidence["text"])}</pre><p>快照 SHA-256：{h(source["sha256"])}</p></details>')
    limitations = "".join(f"<li>{h(x)}</li>" for x in report["limitations"])
    badge = {"live_api":"真实接口研究",
             "frozen_live_evidence":"冻结真实来源 · 本次真实模型分析（未重新获取上游数据）"}.get(
                 report["evidence_kind"], "受控测试 / 回放样例，非真实 AI 研究")
    if report.get('model_execution') == 'injected_unverified':
        badge = ('冻结真实来源 · 注入模型响应，未验证真实调用' if report['evidence_kind'] == 'frozen_live_evidence'
                 else '受控测试 / 注入模型响应，非真实 AI 研究')
    billing = ("注入客户端（测试／调用方式见记录）" if report.get('billing_mode') == 'injected_client' else
               "ChatGPT 订阅授权" if report.get("billing_mode") == "chatgpt_plan" else "独立 API key")
    dataset_line = (f'<p>证据数据集：<code>{h(report["dataset_id"])}</code>（PostgreSQL）</p>'
                    if report.get("dataset_id") else "")
    analysis_line=''
    if report.get('analysis_provenance'):
        plan=report['analysis_provenance']
        role_rows=''.join(f'<tr><td>{h(labels[role])}</td><td>{h(meta["selected_count"])}</td>'
                         f'<td>{h(plan["role_prompts"][role]["version"])}</td></tr>'
                         for role,meta in plan['role_manifests'].items())
        analysis_line=(f'<section><h2>分析检查与版本</h2><p>结构、数字格式、公司 / 日期 / 来源范围、引用 ID 和逐字摘录：程序检查通过。'
                       f'上方论点属于模型判断；摘录是否充分支持判断及研究质量仍需人工核对。</p>'
                       f'<p>流程版本：{h(plan["workflow_version"])} · 结果契约：{h(plan["result_schema_version"])}</p>'
                       f'<p>运行指纹：<code>{h(plan["workflow_fingerprint"])}</code></p>'
                       f'<div class="scroll"><table><tr><th>角色</th><th>实际输入证据数</th><th>Prompt 版本</th></tr>{role_rows}</table></div>'
                       f'<p>检索仅在本次已收集的申报片段中按关键词 / 元数据排序，不代表读过完整财报。'
                       f'逐论点的 fact / calculation IDs、原文 hash 和实际输入绑定保存在 JSON 报告。</p></section>')
    identity_html = ''.join('<p>' + h(s['symbol'] + ': ' + s['report_entity']
                         + ('；来源名称口径说明：' + s['identity_note'] if s.get('identity_note') else '')) + '</p>'
                         for s in report.get('identity_notes', []))
    title = '产业研究' if panels is not None else summary['company_name']
    return f'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>Aeternitas · {h(report['ticker'])}</title>
<style>body{{font:16px/1.7 system-ui,sans-serif;max-width:1050px;margin:36px auto;padding:0 22px;color:#162d38;background:#f5f7f8;overflow-wrap:anywhere}}h1,h2{{line-height:1.3}}section,details,.card{{background:white;border:1px solid #dae2e5;border-radius:10px;padding:20px;margin:18px 0}}table{{width:100%;border-collapse:collapse;font-size:14px;overflow-wrap:normal}}td,th{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}.scroll{{overflow:auto}}.tag{{font-size:12px;background:#e8f0f2;padding:3px 6px;border-radius:4px}}blockquote{{border-left:3px solid #628b96;margin:12px 0;padding-left:14px;color:#455b66}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 system-ui}}a{{color:#086a7b;overflow-wrap:anywhere}}summary{{cursor:pointer;font-weight:600}}.muted{{color:#536872}}@media print{{body{{background:white}}details{{break-inside:avoid}}}}</style>
<header><p class="muted">AETERNITAS 1.0 · RESEARCH</p><h1>{h(title)} · {h(report['ticker'])}</h1>
<p><span class="tag">{badge}</span> <span class="tag">{h(report['status'])}</span></p>
<p>研究问题：{h(report['question'])}</p><p>截止日：{h(report['as_of_date'])}（Asia/Shanghai，日期级）<br>研究编号：{h(report['run_id'])}</p></header>
{identity_html}{financial_html}
{''.join(sections)}{analysis_line}
<section><h2>范围与局限</h2><ul>{limitations}</ul><p>新闻结果：{h(report['news_coverage']['state'])}；本轮选取 {h(report['news_coverage']['selected_rows'])} 条，使用供应商摘要。</p>
<p>模型入口：{billing} · 模型：{h(report['model'])} · Prompt：{h(report['prompt_version'])} · 调用尝试：{h(report['model_attempts'])}。实际用量 / 费用以账号记录为准，未核对部分记为 unknown。</p>{dataset_line}</section>
<h2>证据与原始引用</h2>{''.join(sources)}
<footer class="muted"><p>引用 ID 与摘录已做程序校验；摘录是否充分支持解释仍需人工核查。本报告不执行交易。</p></footer></html>'''
