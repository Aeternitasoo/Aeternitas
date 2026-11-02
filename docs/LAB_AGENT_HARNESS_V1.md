# Agent Harness & Skills v1

本模块是执行控制层，长期目标仍以AGENTS.md为准。它不替代Research Workflow、证券研究服务、价格Agent或量化实验；不提供自动修改代码、独立研究质量评分或RSI晋升。

## 实际复用与新增边界

- 复用ResponsesAnalyst.complete_json：认证、HTTP、SSE完成事件及真实返回的模型／Token元数据保持原实现。
- 复用research_observability.Recorder：扩展字段白名单及结构化错误分类，记录根任务、模型、工具准入、执行、验证的父子Span。
- 复用research_workflow的独占文件保存／digest及最终研究契约，复用audit_inputs、财务结果和逐字引用校验。
- 新增有界模型／工具循环、工具注册与权限判断、SKILL.md按需读取、状态日志。没有数据库或新Agent框架。

**工具请求使用结构化JSON决策协议，经原complete_json传输。不是Responses原生function_call／function_call_output协议。** 模型请求Python工具后，Harness实际执行并把结果放回下一轮消息；工具结果明确标为不可信资料。v1没有浏览器、任意代码执行或文件路径工具。

## 调用方式

入口为`agent_harness.run_harness`：

```python
folder = run_harness(
    task, model, registry, SkillRegistry(project_root / "agent_skills"), output_root,
    allowed_tools=frozenset({"skills.load", "research.financial_summary"}),
    permissions=frozenset({"skills.read", "research.read"}),
    result_schema=result_schema,
    result_validator=domain_validator,
    limits=Limits(max_rounds=6, max_tool_calls=8, max_retries=0),
    parent_run_id=None,
)
```

model是宿主传入的可pickle调用器。`ResponsesModel(name, auth="chatgpt")`在子进程中构建原客户端；不自动读取环境配置、不换模型、不降级为API Key。离线测试和演示使用明确标记的controlled_test模型，不能声称验证了真实模型选择工具的能力。

模型每轮返回：

```json
{"decision":{"kind":"tool","call_id":"call-a","name":"skills.load","arguments":{"name":"industry-research"}}}
```

或：

```json
{"decision":{"kind":"final","result":{"summary":"符合本次输出Schema的结果"}}}
```

最终输出先按宿主Schema检查，再调用领域校验器。验证器以异常拒绝结果，不能修改原始模型输出。生产模型暴露research.*工具时必须绑定领域校验器；`agent_tools.research_result_validator`复用原Research Workflow的validate_output。同一来源多段引文的旧阻塞仍保留，没有借Harness放行。

## 工具注册和权限

Tool记录名称、描述、闭合参数JSON Schema、受信Python函数、权限集合、版本、超时、结果字节上限和repeat_safe。ToolRegistry冻结参数及可pickle绑定，按名称准确查找；模型没有Python函数对象、导入器或任意路径调用入口。

参数支持object／array／string／integer／number／boolean／null、enum、范围、字符串和数组长度及pattern。未知Schema关键字在注册时拒绝；不是完整JSON Schema实现。对象必须additionalProperties=false，布尔值不会冒充整数，非有限JSON或超限结果拒绝。

每次请求依次检查：工具登记 → 参数 → 宿主allowed_tools与permissions → 调用身份／预算 → 子进程执行。拒绝的请求也有独立intent文件、准入Span和失败代码。Skill不能修改权限或预算。

首批工具：

| 名称 | 实际能力 | 权限 |
|---|---|---|
| skills.load | 按已登记名称读取一个SKILL.md，并返回内容SHA版本 | skills.read |
| research.financial_summary | 哈希固定的数据别名→原来源审计→既有年度财务摘要 | research.read |
| research.evidence | 同一冻结包按ID读取证据及来源 | research.read |
| research.verify_citations | 原ID归属、逐字摘录与现有唯一引用校验 | evidence.verify |

研究工具只接收别名及证据ID，不接收模型提供的文件路径、URL或下载参数。宿主通过`research_tools({alias: {bundle_path, source_root, sha256}})`固定资源；版本／来源异常立即拒绝。工具不获取新新闻、财务或行情，不连接数据库。

**注册函数、模型适配器和领域校验器是受信宿主代码。** 进程隔离用于终止超时工作，不是为任意敌意Python提供的OS沙箱；新增工具必须由宿主审查权限、参数资源边界及副作用。没有生产代码写入工具。

## 执行预算与失败

安全默认值：最多6轮、8次工具请求、每工具10秒、每模型调用120秒、总任务300秒、零重试；Token上限默认未配置。轮数最多8、工具请求最多64、重试最多2，超出配置范围直接拒绝。

- 模型和工具在spawn子进程中执行，超时时杀死该进程组并等待回收，结果晚到不能发布。总截止时间也限制每个子操作；进程终止有短暂清理时间。
- 相同call_id绑定不同参数会拒绝。repeat_safe工具的相同输入可返回缓存，仍占工具请求预算；不安全工具重复输入直接拒绝。Skill每次显式加载重新检查版本，不复用普通工具缓存；同一次运行中已加载Skill内容改变会拒绝。
- max_retries仅允许已声明repeat_safe、且明确抛出TransientToolError的工具有限重试；每次尝试计入工具预算。模型请求、超时或远端结果不明不自动重试。旧客户端不能可靠分类的HTTP错误不猜测为可重试。
- 只统计适配器实际返回的非负整数Token。未取得总用量时actual_total_tokens=null；配置Token上限后遇到未知用量停止，不把未知当零。Token上限在响应后核算，不能保证供应商不会在单次请求中超出剩余额度。
- 工具／权限／结构错误采取停止策略，不把错误交回模型后无界尝试。完整失败状态、原始模型响应及安全错误分类保留，不生成默认结论。
- 本版运行环境为WSL／POSIX Python3.13；Windows原生执行与崩溃恢复未验证。强制终止父进程可能只留下最后一条状态日志，不提供自动续跑。

## Skills

project/agent_skills包含三份短技能：industry-research、financial-analysis、evidence-verification。它们描述任务方法、输入输出、证据标准及异常处理，实际能力由Python工具提供。

发现只读取有限的name／description前置元数据，不读取全部正文，不预先计算全文哈希。前置字段采用简单字符串或JSON双引号字符串，不实现完整YAML解析；未知字段拒绝。

模型先看到技能名称／描述，需要时调用skills.load。加载时读取最多32KiB正文、计算全文SHA-256，作为Skill Version；本次实际使用的名称／哈希绑定到state、工具结果、后续模型消息和Span。限制目录与名称，拒绝路径穿越和符号链接。新运行可加载新版本；不会修改旧运行记录。

这些是Aeternitas运行时技能，不是安装到用户Codex全局目录的技能。未安装PyYAML；skill-creator通用quick_validate依赖PyYAML而无法运行，未为此添加依赖。本项目自己的元数据、按需读取、版本及权限测试和实际离线演示提供运行验证。

## 本地记录与后续接口

每次独占新run目录：request、每轮模型请求／响应、工具intent／请求／尝试／结果、transitions.jsonl、events.jsonl、state、final及成功COMMITTED。失败没有COMMITTED；verify_run核验文件清单及SHA。没有覆盖或自动恢复旧任务。

状态包括created、running、awaiting_model、executing_tool、completed、failed、budget_exhausted。记录Run／Parent Run、输入输出指纹、Harness／Prompt／工具注册／结果Schema／组件代码版本、实际加载的Skill版本。参数与结果在本地完整保存，Span仅记录参数哈希、工具／Skill标识、用量、耗时和固定错误分类；sink异常隔离。sink仍为同步接口，必须有界，尚无后台发送队列。

Langfuse：可把harness根Span、harness_model、harness_tool_admission、harness_tool及harness_validation映射到服务端Trace／Generation；SDK、配置、导出、补传与真实集成均未实现。

Evaluation／RSI：evaluation_result=null、evaluation_status=not_run，明确未评分。独立评估器仍需绑定提交结果、版本与指标；未来还缺改进提案、隔离候选、独立测试、晋升／回滚审批与留出验证。没有自动修改或晋升能力，不能把可读执行记录当成RSI闭环。

## 离线演示

```bash
/absolute/venvs/aeternitas-lab-py313/bin/python -B scripts/demo_harness.py \
  --bundle /absolute/path/company-input-bundle.json \
  --sha256 VERIFIED_BUNDLE_SHA256 \
  --source-root /absolute/path/original/data \
  --output /absolute/path/new-output-root
```

这些路径来自宿主CLI参数；不会开放给模型。演示使用固定离线决策，按需要加载三个Skill，执行财务读取、证据读取、引文核验，再保存受控演示结果。不是新研究报告、真实模型验证或质量评分。--help及导入没有网络请求。
