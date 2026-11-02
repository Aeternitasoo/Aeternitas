# Aeternitas Core

面向电力、AI 算力与云计算产业链的 AI 投资研究系统。通过真实来源、受控工具、可追溯报告、长期记忆、执行轨迹和独立评估，研究产业传导、公司盈利及风险。

长期协作规则以 [AGENTS.md](AGENTS.md) 为准；实际历史与验收见唯一 [Progress](AI_Research_System_Progress.md)。Core 软件包版本为 **2.0.0**，不代表成熟自主研究或 RSI 已完成，本版本采用独立干净根提交；旧私有项目历史与Release不属于本仓库。

## 核心架构

| 模块 | 实际入口 |
|---|---|
| Research Engine | research_workflow.py、research_quality.py、research_intelligence.py |
| Harness / Tools / Skills | agent_harness.py、agent_tools.py、agent_skills.py、agent_skills/*/SKILL.md |
| Memory | research_memory.py，可选独立 Mem0 SDK 环境 |
| Observability | research_observability.py、langfuse_adapter.py、research_analysis.FailureDiagnostics |
| RSI | research_rsi.py、research_validation.py、infrastructure_rsi.py、candidate_registry.py |
| Evaluation | lab_evaluation.py、research_quality.py、research_intelligence.py |

详见 [实际架构](docs/ARCHITECTURE.md)、[逐文件分类](docs/CORE_FILE_INVENTORY.md)。保留平铺模块，避免改变来源审计、Prompt 和代码哈希依赖的相邻路径；未机械搬迁到 src/。

## Research Workflow

配置 → 采集或读取冻结来源 → 来源审计与证据组织 → 财务口径检查 → 一次 LLM 综合 → 引用、反证与推翻条件检查 → HTML / JSON → 文件哈希及提交核验。

事实、推断、假设、未知分别记录。quality_policy 将质量机制接入执行路径；严重错误拒绝发布，未经独立语义审核的结果保留 incomplete_research。引文匹配不证明论点正确。行情不是必要输入。

core_cli.py 是当前入口；aeternitas.py 的原公司研究、授权和数据诊断命令作为兼容入口保留。旧价格 Agent、批量价格实验、教学 CLI 和独立前端退出当前版本；备份、旧提交、历史记录和项目外实验保持。A0、Time Machine 和共享行情层未放宽。

## Harness / Tools / Skills

Harness 控制轮数、工具注册、参数、权限、时限、预算和失败状态。模型不能用任意函数名或路径执行代码。Skills 先公开名称和摘要，按需加载 SKILL.md，绑定内容 SHA。现有行业研究、财务分析、证据核验三项技能不授予工具权限。执行控制不是针对恶意 Python 的操作系统安全沙箱。

## Memory

四类记忆：Project、Research、Procedural、Task。记录绑定主体、范围、期间、来源哈希、时间、可信状态和版本。相同观察幂等复用，独立来源或新观察保留；冲突、过期、未来和来源变动影响准入，记忆不替代原始证据。

真实本地 Mem0 2.2.1 持久化与跨进程检索已验证。当前使用 **256 维词法哈希**，不是训练过的语义嵌入；存在语义匹配不足。select_task_memories 由主程序白名单约束，没有默认向每次研究注入记忆。长期写入需可信主程序批准。

检索、注入、实际使用和帮助质量提升分别记录；研究质量改善尚未充分证明。原始模型输出不会自动成为可信事实。Memory 失败时保留无记忆工作流。

## Observability / Langfuse

本地 Recorder 保存 Trace、父子 Span、Generation、工具、版本、Token、耗时和安全错误分类。FailureDiagnostics 区分请求尝试、HTTP、完成事件、响应落盘及最终提交；未知用量和传输结果不填零。

Langfuse 5.0.4 Adapter 已实现及离线测试，**云端 Requires Credentials，未验证远端接收**。遥测只输出元数据白名单，不外发秘密或完整研究正文；导出失败不改变研究结果。本地记录是后备。

## 受控 RSI

轨迹／Evaluation → 失败模式与历史经验 → 改进提案 → 隔离 Candidate → Regression / Development / Holdout → 人工语义审核 → 晋升建议／人工批准／回滚。

已能读取历史失败、生成已知模式隔离候选并保存 Diff 和版本。最新通用运行时改动纳入 Core；未获人工批准的研究 Prompt 候选保持隔离，没有自动晋升。这是早期受控 RSI，未验证通用自主改代码或可靠自主进化。审批密钥不分发。

## Evaluation

确定性检查包括结构、来源哈希、逐字引用、财务口径、权限和版本；独立语义检查判断证据支持、反证相关性及推翻逻辑。相同输入配对比较，开发反馈和独立留出分开。

人类语义审核仍必需。已消费留出不能再称为全新独立证据。小样本、成功提交或检索命中均不证明普遍研究提升。

## 安装

支持 WSL / Linux，Python **3.13.18**。保留已有环境；下面是新环境的可替换路径。

~~~bash
python3.13 -m venv ~/.venvs/aeternitas-core
~/.venvs/aeternitas-core/bin/python -m pip install -r requirements.lock setuptools==80.9.0
~/.venvs/aeternitas-core/bin/python -m pip install --no-build-isolation --no-deps -e .
~/.venvs/aeternitas-core/bin/python -m pip check
~~~

requirements-core.txt 为最小直接依赖；requirements.txt 保留兼容环境固定直接版本；requirements.lock 是此次验证的完整解析版本（含兼容 API、测试、Langfuse）。pyproject.toml 区分 core 与 test / server / observability 可选组。安装验收针对 **源码 editable 安装**；独立 wheel 资源布局尚未验收。

Mem0 必须隔离，主环境 protobuf 7.36.2 与侧车 protobuf 6.33.6 不合并：

~~~bash
python3.13 -m venv ~/.venvs/aeternitas-memory
~/.venvs/aeternitas-memory/bin/python -m pip install -r requirements-memory.lock
~/.venvs/aeternitas-memory/bin/python -m pip check
export AETERNITAS_MEMORY_PYTHON="$HOME/.venvs/aeternitas-memory/bin/python"
~~~

按 .env.example 在本机配置。沿用 ChatGPT OAuth 和已选模型；不自动切换 API Key、提供商或计费。SEC / Finnhub、真实模型和 Langfuse 均为显式在线操作，不由导入、帮助或离线演示触发。

## 运行

~~~bash
aeternitas-core --help
aeternitas-core capabilities
aeternitas-core plan --config config/research_cloud_compute_power.json
~~~

配置样例保留原案例日期，并非最新资料。真实研究需已经检查的质量策略；config/quality_policy.example.json 提供与配置样例主体一致的最小策略（没有人工批准财务指标，因此不能认证数值分析完整性）；冻结输入清单使用 FrozenInputs 的 source_root、bundles（路径及 SHA）。

~~~bash
aeternitas-core research --config /absolute/task.json \
  --quality-policy /absolute/quality-policy.json \
  --frozen-inputs /absolute/frozen-inputs.json \
  --output /absolute/new-runs --execute
aeternitas-core verify /absolute/new-runs/RUN_ID
~~~

最多一次现有 Workflow 模型请求，不自动重试。冻结读取不重新采集来源。可直接使用 run_workflow 与既有 Harness；输入契约见 [Workflow](docs/LAB_RESEARCH_WORKFLOW_V1.md)。

## 离线验收

在仓库根目录、已安装环境执行，临时目录放在仓库外：

~~~bash
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/scripts/offline_guard:$PWD"
python -B -m pytest -q -p no:cacheprovider \
  --ignore=test_m7_postgres_init.py --basetemp=/absolute/new-test-temp
python -B scripts/core_smoke.py --output /absolute/new-core-smoke \
  --memory-python "$AETERNITAS_MEMORY_PYTHON"
python -B scripts/check_core_release.py --output /absolute/new-security-report.json
~~~

Smoke 使用脚本化决策，实际执行 Harness、Skills、Evaluation、Registry 和可选真实本地 Mem0，不是 GPT 研究。没有显式隔离 PostgreSQL 配置的兼容测试跳过；集群初始化测试单列不执行。可选 Mem0 未配置会跳过 SDK 测试。不能将这些状态算作生产验证。

## 当前限制与下一步

- 来源关键词选择可能遗漏经营证据或误取 XBRL 分类标签。
- 财务指标覆盖、独立语义审核不足，部分报告只能为不完整研究。
- 记忆边际价值、长期可靠性与通用 RSI 尚未验证。
- Langfuse 云端未连接；新 Core 数据库／容器部署未重新验收。
- 下一步先复核“经营 backlog 与收购无形资产分类标签混淆”的实际取证问题，再开展冻结独立研究评价，不增加框架替代质量验证。


## 公开分发与复现边界

项目：https://github.com/Aeternitasoo/Aeternitas
旧私人协作记录、实验档案和Release不在本仓库历史中。
此仓库不分发真实模型报告、新闻缓存或授权不明的研究材料。
固定测试包括合成数据、脱敏故障形状，以及有明确来源和复用依据的SEC数值子集；后者没有被重新标记为合成数据。
分发边界见 [NOTICE](NOTICE.md) 和 [来源说明](docs/REFERENCES_AND_LICENSES.md)。

首次体验先执行capabilities、plan和core_smoke，无需真实模型密钥。
请只在本机配置凭据，不提交.env、供应商原件、完整模型输入输出或记忆库。
公开可见不等于授予项目开源许可，当前没有选择项目级开源许可证。
