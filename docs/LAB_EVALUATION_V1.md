# Langfuse 与独立 Evaluation v1

真实研究上的后续试验与显式多片段契约见 [RSI v1.1](LAB_RESEARCH_RSI_V1_1.md)。本页保留v1离线演示范围；不把历史演示改写成真实服务或研究质量验证。

本轮提供真实可配置的 SDK 适配器和可运行的离线候选闭环。没有 Langfuse 凭据，尚未向 Cloud／自托管服务发送 Trace。合成任务通过不代表研究质量或真实 LLM 得到改善。

## 现有能力的组合

`run_harness` 和 `run_workflow` 原有 `sink` 参数直接接受 `LangfuseAdapter`，本地 `Recorder` 仍先追加事件。复用原 OAuth、SSE、模型客户端、工具权限、Skills 按需加载和成功记录核验。没有更换模型、认证或研究流程。旧 M6 评估器继续保留；研究输出复核调用现有 `research_workflow.validate_output`／引用契约。

新增模块：

| 文件 | 职责 |
| --- | --- |
| `langfuse_adapter.py` | 有界进程队列、SDK 映射、脱敏、Flush／Shutdown 超时 |
| `lab_evaluation.py` | 独立执行评分、固定数据集、失败分析、同样本版本比较 |
| `candidate_registry.py` | 隔离候选、差异、人工审批、追加式晋升及回滚记录 |
| `lab_evaluation_demo.py` | 固定合成任务和受控改进模板，实际调用原 Harness |
| `scripts/demo_evaluation.py` | 默认完全离线的闭环入口 |
| `scripts/manage_candidates.py` | 可信宿主终端上的人工审批入口 |

## Langfuse 配置和生命周期

已固定官方 `langfuse==5.0.4`，官方 PyPI 要求 Python >=3.11、<4，支持当前 Python 3.13。SDK 使用 `start_observation`、`create_score`、`flush`、`shutdown`。参考：[PyPI](https://pypi.org/project/langfuse/5.0.4/)、[Python SDK](https://python.reference.langfuse.com/langfuse)、[官方埋点文档](https://langfuse.com/docs/observability/sdk/instrumentation)。

显式设置环境变量（不要把实际值写入源码、命令历史、报告或聊天）：

- `LANGFUSE_PUBLIC_KEY`
- `LANGFUSE_SECRET_KEY`
- `LANGFUSE_BASE_URL`，也兼容 `LANGFUSE_HOST`。必须显式指定 Cloud 区域或自托管地址；不默认选择地区。远程地址要求 HTTPS，本机 localhost 可用 HTTP。

默认不读取凭据或连接服务。宿主显式构造：

```python
from langfuse_adapter import Config, LangfuseAdapter

with LangfuseAdapter(Config.from_env()) as export:
    folder = run_harness(..., sink=export)
    result, path = evaluate_run(folder, frozen_criteria, evaluation_root, sink=export)
    export.flush()
```

也可 `run_workflow(..., sink=export)`。缺少任一配置时 `Config.from_env()` 返回 None，适配器禁用，不创建进程；配置格式错误会明确报错。不要把 Langfuse 接入当作启动研究的隐式前置条件。

每次导出使用独立进程，默认最多排队128条命令；Recorder 只做非阻塞入队，队满计数丢弃。SDK内部也限制批队列；HTTP超时2秒，HTTPX不主动重试连接。SDK／OTel自身有限重试仍受进程生命周期约束。Flush和Shutdown默认各最多3秒，超时终止导出进程，绝不重试研究或模型调用。不自动补发本地历史轨迹，避免重复及越权上传。

root observation 对应一次任务；模型阶段为 generation，工具准入／执行、Skill加载、证据工具、验证及报告阶段沿用本地父子结构。Run ID 稳定映射到 SHA-256 前32位 Trace ID；父任务 ID、响应 ID、调用 ID 默认仅导出哈希。本地记录保留原始对应关系。实际耗时使用 Recorder 的 `elapsed_seconds`；SDK显示的 Span 时间是导出进程处理时间，离线重放时不能替代实际执行耗时。

只导出白名单标量：模型和版本、Skill哈希、工具名、状态、真实用量、耗时、固定错误分类、输入输出指纹。没有原始 Prompt、财报正文、证据摘录、文件路径、工具参数正文或原始异常消息。评价只导出数值指标和 Evaluation ID；原始依据留在本地。SDK mask同时作用于metadata，因此保留已过滤的扁平元数据，其他内容全部遮蔽。

`flush()` 成功只表示本地SDK处理结束，不足以证明服务端接收。`remote_delivery_verified` 不由本地调用自动改成 true。服务端连通性必须另以合成 Trace 和实际服务端证据验证；当前为 **Requires Credentials**。

## 独立评估

`evaluate_run(run_dir, criteria, output_root, expected_files=..., subject_version=...)` 读取已固定的运行文件；不调用模型，不修改原 Run 或 COMMITTED。成功记录额外使用原 `verify_run`；失败记录保留失败状态，不冒充成功。

预设 criteria 包含 case_id、split、dataset_fingerprint，以及需要的工具／Skill、期望状态、输出JSON投影、可选引用契约、Token／耗时阈值。它只能由宿主从冻结数据集提供，不接收 Agent 自评分。

评估项：完成状态、记录完整性、Trace父子关系、实际执行工具的注册／参数／权限、失败请求与执行、所需工具和Skill、Skill正文哈希、结构化输出、确定性期望值、研究分类与逐字引用、客户端实际Token、耗时及失败类别。人工语义判断始终标为 `requires_human_review`；引用有效不等于金融结论成立。工具选择质量仅能针对预先声明的要求评分。

总体分仅在明确给出正权重、且被计分项均已知时生成。未提供权重为 null。Token不可得时为null，费用未核实为null；不估算成真实成本。旧失败GPT响应可以通过 `evaluate_saved_research` 离线复核，保留 `actual_outcome=rejected`，它通过的是“应该拒绝”的回归断言。

## 数据集和版本对照

`freeze_dataset` 在新目录写入 Regression／Development／Holdout 的固定JSON及SHA-256清单，要求三组非空；拒绝重复ID和内容相同的案例。`load_split` 要求显式 split 及预先固定的manifest哈希，只读取指定分组。结构性去重不能证明语义上没有相近案例，正式研究留出集仍须独立策划。

开发失败分析只接受Development结果，拒绝Regression／Holdout。候选先冻结，再运行Regression，最后打开Holdout。版本比较要求相同案例、criteria、数据集，以及相同权限／预算；输出各指标差异、修复／新增失败、Token和耗时变化，版本和完整报告均留存。可比较Harness／Skill／Prompt／工具注册表／配置的独立评估记录，不自动执行任意版本源码。

晋升策略必须在数据集冻结时固定。演示仅在 `controlled_fixture` 范围允许未知Token成本，同时要求至少修复两例、无新增失败、所有候选案例通过、每例耗时小于预设阈值。研究范围默认不接受未知Token成本放行；真实研究还需要研究质量标准和人工复核，不能把演示策略照搬为生产策略。

## 候选版本与人工控制

候选包含Parent、文件副本及哈希、统一差异、原因、Development失败记录、数据集版本、创建时间和状态事件。仅允许预设路径：`skills/<name>/SKILL.md`、`prompts/<name>.md`、`harness/<name>.py`。候选不能扩张父版本文件范围。Skill／Prompt作为数据交给既有Harness执行；Python Harness候选目前仅归档，自动执行及晋升会拒绝，仍需后续单独建设可信隔离运行器。

状态保留 `proposed → candidate_created → evaluating → awaiting_manual_approval → approved / rejected`；回滚单独记录。生产仓库文件完全不写入。被选中的版本只通过项目外的追加式activation记录生效，宿主需要显式使用 `active_files()`；没有暗中切换现有研究命令。原版、候选、评估、审批、晋升和回滚记录全部保留。

审批要求真实交互终端，输入动作、版本ID和对照报告哈希。签名绑定当前版本和报告，拒绝伪造、过期及重复审批。审批密钥仅供宿主程序使用，不交给工具或模型。`manage_candidates.py --registry ... --authority ... review --comparison ...` 可先查报告；`promote`／`rollback` 额外指定 `--version` 后才进入人工确认。

演示的 `fixture_approval` 仅在创建时声明为controlled_fixture的外部注册库有效，审批记录明确 `simulated=true`。这不是用户对生产晋升的批准。整个控制层依赖可信宿主和文件权限，不是可对抗任意本地Python代码的安全沙箱；Langfuse、Agent和Skills均没有被注册审批／写生产代码的工具。

## 运行最小闭环

使用一个不存在的新项目外目录：

```bash
cd /absolute/aeternitas
/absolute/venvs/aeternitas-lab-py313/bin/python -B scripts/demo_evaluation.py \
  --output /absolute/aeternitas-data/rsi-demo/unique-run
```

默认不连接任何服务。只有显式 `--langfuse` 且完整环境配置存在时才导出合成演示元数据。本轮没有执行该在线选项。

闭环是：固定三组共六个合成案例 → 原Harness实际执行两条Development任务 → 独立评估发现未调用验证工具 → 失败统计生成受控模板提案 → 创建隔离Skill候选 → 两条Regression和两条Holdout分别运行原版／候选 → 独立对照 → fixture人工审批模拟 → 晋升指针 → 模拟审批回滚。共十次真实离线Harness运行、二十四次Mock模型轮次，零真实LLM请求。

输出包括dataset、runs、evaluations、failure-analysis.json、registry中的原版／候选／差异／报告／审批／activation历史、summary.json及哈希清单。审批密钥不进入公开清单。不覆盖旧目录；重复相同输出路径会拒绝。

## 尚未实现／验证

- Langfuse服务端真实接收、账户权限和长期导出稳定性。
- 针对真实产业结论的可靠语义评分、真实成本价格核验和更大独立留出集。
- 任意Harness源码候选的隔离执行、自动部署及生产晋升。
- 自动LLM生成改进、统计显著性验证、持续调度、多租户审批或不可篡改外部审计服务。
- 真实模型在新Skill／Prompt下的改善。既有真实响应的重复引用失败仍保留，未在本轮修复。

本轮不改变A0、Time Machine、量化目标／评分、OAuth、SSE、已有实验或原始数据。
