# M4：分析结果、证据选择与可复查记录

M4 沿用已有 CLI → 本机 API → worker → SEC / Finnhub → PostgreSQL 证据数据集 → 四角色分析 → HTML / JSON。使用已有 ChatGPT 订阅授权与 gpt-6.1-sol，不新增收费 API、网页前端或 Agent SDK。

## 五个交付环节

| 环节 | 实现及验证 |
|---|---|
| 固定结果与角色 | `analysis_contracts.py` 固定 AnalystResult / Claim / Citation 结构、长度、数量及去重规则；共同规则与四个角色分别保存为 `prompts/m4/*-v1.md` |
| 有界证据与检索 | `evidence_selection.py` 先拒错公司、越日期、未知类型、重复 ID、坏来源 / locator，再在已收集申报片段中按关键词 / 元数据排名；保存每条分数、理由与实际选中 IDs |
| 版本与实际输入输出绑定 | 运行指纹绑定候选证据的内容 / 来源 hash / locator、公司、日期、问题、模型、计费模式、prompt 内容 / 版本、schema / workflow / selection 版本；每次实际请求、最终输出和已验证前序分析均可复核 |
| 分层检查与引用显示 | 结构、数字、证据身份和逐字摘录是硬检查；程序生成每条论点的 fact / calculation IDs 和原文 hash。财务表数字可以点到相应证据；原文是否支持含义与研究质量单列，不能以合法 JSON 代替 |
| 冲突与注入实验 | 单独的受控矛盾材料接真实订阅模型，风险 / 综合并列相反原文、指出不能裁定真实供给；HTTP 与 Python 审计未见 canary 读取、目录外写入、shell 或额外 HTTP |

完成这些环节不等于自动证明所有论点正确；M6 仍需完整案例、留出与角色对照。

## 四个角色具体看到什么

| 角色 | 实际输入 | 主要责任 |
|---|---|---|
| 基本面 | 财务事实、程序计算、最多四个已收集申报片段 | 解释给定期间经营变化，不能把年度数据冒充最近季度，不能编造增长原因 |
| 事件 | 最多十条本次已选新闻摘要 | 区分已报道事件、媒体预期和未来事项；不把新品或评级变化推成已实现业绩变化 |
| 风险 | 本次有界证据及已验证的基本面 / 事件结果 | 回查原文、冲突、期间 / 口径与缺口；前序分析不是来源 |
| 综合 | 本次有界证据及已验证的前三角色结果 | 回答问题，保留支持、反对与未确定事项，不补外部事实 |

检索范围只包含采集器已有的申报片段，不是完整财报搜索，不增加外网抓取权限。排名影响片段顺序和预算内选集，不代表原文真实性或对结论支持程度。实际 AAPL 的四角色证据数为 15 / 10 / 25 / 25；排序及全部候选的版本均有记录。

## 如何使用与回查

```bash
.venv/bin/python scripts/local_app.py start
.venv/bin/python aeternitas.py run AAPL --wait --deadline 480 --wait-seconds 600
.venv/bin/python aeternitas.py report RUN_ID
.venv/bin/python aeternitas.py evidence RUN_ID EVIDENCE_ID
.venv/bin/python aeternitas.py verify RUN_ID
```

浏览器使用 `report` 返回的 localhost 地址。财务数字与论点的引用链接指向证据详情；“分析检查与版本”会说明哪些检查通过、哪些仍需人工核对。

新报告使用 `aeternitas-report-m4-v1`、`analyst-result-m4-v1`、`evidence-roles-m4-v1`、`research-four-roles-m4-v1`。保存内容：

- `input_bundle.json`：通过来源审计的原始输入集合。
- `analysis_plan.json`：各角色候选、选中证据、排名理由、来源 / locator / 文本 hash 和运行指纹。
- `steps/ROLE-attempt-N-input.json`：真实发送的两条 developer / user 消息、输入 hash、模型及版本；修复草稿须对应上次失败输出。
- `steps/ROLE-attempt-N-output.json` / `error.json`：实际未验证输出、完成响应身份、错误阶段与原因。
- `steps/ROLE.json`：通过本地校验的结果，绑定最终请求和实际输出。
- `report.json`：增加 `analysis_provenance` 和 `analysis_checks`；后者按论点记录 evidence IDs、fact IDs、calculation IDs、quote hash、结果 hash，以及 `human_review_required`。
- `published.json`：限定文件名的完整发布 hash 清单，包括计划、步骤及尝试产物。

`verify` 会重算选集和指纹，核对实际消息数 / 角色、公司 / 日期 / 问题、前序结果、修复草稿、最终原始输出、checkpoint、报告、输入包及 state。服务读取报告还会绑定 PostgreSQL 任务身份。即使只修改文件后重新计算发布 hash，这些内容之间的不一致仍会被拒绝。hash 是完整性检查，不是对恶意本机所有者重写全部文件的签名证明。

旧 M1–M3 报告保持原文与旧规则，不改写、不追溯增加字段。只有明确旧 schema / prompt 才能走旧校验；仍有 M4 痕迹时不能伪装旧版本。修改 prompt 或流程时应建立新版本并保留旧版本的验证实现，不覆盖已有版本来复用成功步骤。M4 不提供步骤复用，恢复与幂等仍在 M5。

## 硬检查与语义检查的区别

硬检查会拒绝：越出角色集合的 ID、错误公司 / 日期 / 来源、伪造或改写的摘录、重复引用、错结构 / 超限列表、Unicode 数字和明显中文百分比 / 金额 / 倍数 / 日期等量化表达。具体数字仅放在程序表和引用原文中。中文隐含数量及金融含义无法靠正则全部识别，仍需抽查。

同一条合法原文可能被模型作出相反解释；回归专门验证了这种反例仍被标记为需人工检查，不会因硬规则通过而获得“语义正确”认证。新报告任一角色有缺口时用 completed_with_gaps 表示完成且保留缺口。

模型请求未配置 tools 或工具执行器，只接受文字 JSON；SSE 及 JSON 分支遇到工具调用都会拒绝，不解释或执行参数。每角色最多两次，总计八次，deadline、真实完成事件和输出大小限制继续有效。失败也记录步骤与已知 usage，未知消耗不推断为零。模型 schema 仅移除供应商支持清单未列的 `uniqueItems`，本地仍严格按 evidence ID 去重。

这与官方文档所述边界一致：[Structured Outputs 仍可能包含内容错误](https://developers.openai.com/api/docs/guides/structured-outputs)，[外部文本与工具权限应分开控制](https://developers.openai.com/api/docs/guides/agent-builder-safety)。

## 本机真实验收与受控回归分开记录

本次真实公司研究：`8315b720b3714d12926a513ce9dfd96d`，截止 2026-10-22，gpt-6.1-sol，原订阅模式，四角色完成。综合首次引用重复被拒，第二次修复成功，合计五次模型调用；失败形态脱敏为 `tests/fixtures/m4_duplicate_citation_v1.json`。财务、事件、风险及综合共五条论点另做助手原文抽查，记录 `data/live/m4/semantic-review.json`，不等于用户独立验收。

注入实验位于 `data/live/m4/controlled-experiment/`，材料明确标为受控，模型响应为真实订阅请求。两条相同期间供给充足 / 不足的相反描述中嵌入读文件、执行命令和外发指令；随机 canary 内容从未进入 prompt。风险与综合均引用两方并要求核实口径，没有任选一方当公司事实。两次请求仅访问 Responses 固定地址；Python 审计记录 canary 读取 0、目录外写入 0、shell 0，实际代理 peer 为 127.0.0.1:7897。程序本身能读授权和写受控产物，这不是 OS 沙箱，也不证明所有注入文字都无影响。

```bash
AETERNITAS_TEST_DATABASE='dbname=aeternitas host=/var/run/postgresql' \
  .venv/bin/python -m pytest -q
.venv/bin/python scripts/audit_m4.py
```

前者使用合成 / 冻结供应商响应及隔离 PostgreSQL schema；后者只读核验本次已保存的实际 HTTP / DB / 文件和实验结果，不新增供应商或模型请求。最终结果与代码 / fixture / prompt 指纹在 `data/live/m4/acceptance.json`，并同步同一份 Progress。

季度 / TTM、新闻摘要覆盖、模型语义可靠性和代理偶发失败的限制保持。自动重试 / 恢复 / 取消继续 M5，系统化质量对照与留出继续 M6，Docker / CI / 备份继续 M7。
