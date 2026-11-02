> 历史阶段记录：以下状态仅适用于文中日期。当前 Core、环境和入口以 README.md、docs/ARCHITECTURE.md 及唯一 Progress 最新记录为准；退役脚本不再作为当前入口。

# Research Workflow v1 与可观测性接口

本模块服务于AGENTS.md规定的持续性投资研究主线。它复用原采集器、来源审计、证据选择、ResponsesAnalyst和HTML渲染器；不是固定价格预测Agent的更名，也不替代原CLI、四角色流程、数据库任务服务或量化实验。

## 本轮边界

可复用流程：配置 → 逐公司采集／读取冻结包 → 来源审计与证据整理 → 一次LLM综合 → 结构／分类／逐字引用校验 → HTML／JSON与独占提交清单。公司、CIK、报告主体、问题、研究日期、章节与申报搜索词均可更换，不在实现中固定MSFT／NVDA／VST。不需要行情。

本轮只做离线测试，不请求真实LLM、SEC或Finnhub，不安装依赖、不连接数据库。离线Mock响应不能作为新的真实研究成果。

## 复用、抽取和剩余的一次性逻辑

| 部分 | 实际处理 |
|---|---|
| 数据获取 | 直接调用research_inputs.collect_inputs：SEC年度财务、至多两份申报、Finnhub公司新闻摘要；原超时、白名单与限流保留 |
| 来源核验 | 原audit_inputs逐一核对获取回执、原始字节SHA、财务JSON位置及计算、规范化申报片段、新闻响应 |
| 证据组织 | 原select_evidence按新问题排序既有证据；另从已经核验并保存的申报全文中，按配置词提取至多四个定位片段／公司。不是完整语义检索，首处命中可能不相关或漏检 |
| 多公司记录 | 证据ID加股票前缀，原ID、CIK、来源、发布时间、获取时间、locator与哈希保留；输入绑定来源版本和locator指纹 |
| 综合分析 | 使用原ResponsesAnalyst.complete_json及现有认证方式，一次请求、无自动重试，复用原Budget；没有新增模型客户端 |
| 结果校验 | 章节顺序与配置一致；每节一至四条，区分fact／inference／assumption／unknown；事实与推断须有引用。复用既有数字限制、ID、唯一引用和逐字摘录校验；不证明语义支持充分 |
| 报告 | 在原research_report中增加可选多公司财务面板和分类章节；原四角色报告分支保留，不用字符串拼接整页或另建模板系统 |
| 上次案例的额外现金流／资本开支提取和inline特殊复核 | 仍保留为案例证据，不把一次性人工主体／数值审查变成自动认证。通用结构化财务仍主要为既有年度收入、营业利润、净利润及比率；配置申报词可找到相关原文，不能保证复现上次全部精选数字 |
| 跨期结论、RAG、Memory、Langfuse、RSI | 本轮没有实现 |

## 输入与调用

入口是可测试的Python库接口，不新增CLI子命令。配置示例在`config/research_cloud_compute_power.json`；其中日期固定为原案例日期，不代表今天已更新来源。配置不得放凭据。

```python
import json
from pathlib import Path
from research_workflow import FrozenInputs, run_workflow, verify_workflow

config = json.loads(Path("config/research_cloud_compute_power.json").read_text())
# bundles由调用方提供，每个symbol绑定已有公司包的绝对路径和经确认的SHA-256。
collector = FrozenInputs(source_root=original_source_root, bundles=bundles)
# 离线测试必须注入已有ResponsesAnalyst + httpx.MockTransport。
# 未注入client_factory时会使用provider_config中的现有客户端配置，届时会发起真实请求。
folder = run_workflow(config, output_root, collector=collector,
                      client_factory=offline_client_factory)
assert verify_workflow(folder)
```

`FrozenInputs`不复制、不改写原来源目录；核对调用方提供的包SHA、股票、CIK、日期及新闻窗口，再执行原audit_inputs。更换问题可以重新组织同一冻结证据；冻结包原有问题与元数据保持原样。来源目录必须保留，才能再次审计。原始包的获取时间不算本次网络请求，冻结读取的request_attempts为零。

未来如需实时采集，使用`evidence_kind="live_api"`并省略collector，将原CLI的`load_config()`结果以provider_config参数传入；模型必须与配置完全相同，认证只接受现有明确配置的chatgpt或api_key，无自动回退、换模型或计费切换。不要运行此路径来代替本轮离线验收。

主体名称默认要求与财务来源完全一致。已审查的名称异常可以通过accepted_fact_names及identity_note显式记录，但只是调用方的审查声明，不是自动验证法律主体／合并范围。默认拒绝未说明的名称差异。每个新版本的关键主体与数字仍需独立核查。

输入上限沿用现有客户端的90,000字节；超过时在请求前拒绝，并要求缩小范围。最多六个主体、八个章节；一次模型预算；不恢复或覆盖同名运行目录。不是完整行业扫描器，不支持没有SEC收入覆盖的标的或行业指标适配器。

## 输出与失败记录

每次新建独立目录，保存config、八个执行模块的代码SHA、各公司原包、evidence、selection、input、input-manifest、request／Prompt模板及实际消息哈希、model-attempt、raw-response、model-metadata、report.json／report.html、validation、state、events.jsonl及COMMITTED.json。

input_fingerprint绑定实际提供的研究包与来源／locator版本；output_fingerprint绑定输入、Prompt、模型元数据及结构化结果。运行ID和创建时间独立保存。COMMITTED内的文件哈希覆盖展示报告和其他正式记录。独占创建防止静默覆盖，不是签名不可变Registry。

**只有存在COMMITTED且verify_workflow通过才算正式成功。** 提交前重新核对来源、实际输入、模型响应与报告绑定。失败保留failure.json、安全错误类型／分类及events；无效输出保留raw-response，不用默认结论替代。报告写入或最终复核失败可能留下未提交的report／state，它们不是正式结果。不要只凭state中的完成字样发布报告。

复核使用`verify_workflow(folder)`，不会请求网络或模型；与旧单公司CLI verify的契约不同，不应将新报告交给旧saved_report。

## 可观测性与Langfuse边界

已实现本地`research-events-v1`事件和可选`sink(event)`接口。一个run_id对应一个trace_id；每个span有稳定的父子关系、开始／结束事件、顺序、UTC时间及本地测量耗时。

| 本地位置 | 记录与未来映射 |
|---|---|
| research | 任务根Trace；run ID、配置指纹、模型及最终状态 |
| source_collection | 每公司采集／冻结读取Span；公司、来源数量、现有回执里的请求尝试数。不是每个HTTP请求的实时Span，失败前的完整请求数／HTTP状态可能未知 |
| evidence_organization | 来源审计、关键词整理Span；证据数量和输入指纹 |
| llm_synthesis | 可映射为generation；请求模型、Prompt版本／哈希、输入指纹、已有客户端提供的响应ID／完成状态／HTTP状态及Token |
| output_validation | 结构、概率以外的研究分类和逐字引用校验Span（本流程不生成概率预测） |
| report_save | 报告保存Span，输出指纹；最后本地发布校验仍是正式提交门槛 |
| 错误 | 类型、固定错误分类、失败阶段；识别本地预算耗尽／截止时间。不记录原始异常、供应商正文、请求头或凭据 |

原complete_json现在额外返回响应正文实际提供的response_id、完成状态、HTTP状态及非负整数Token。未提供的用量为null；金额费用为null，不根据模型价格估算。模型版本仍为供应商声明，非独立版本认证。原客户端对失败做了脱敏，部分HTTP／超时细节无法取得，记录未知，不靠猜测填补。

默认不向sink传Prompt、问题正文、原文、模型原文、完整URL、环境配置或异常消息；只发送字段白名单内的运行元数据。完整研究内容留在本地记录。sink收到深拷贝，抛错只增加export_errors，不改变输入、结果或输出指纹。sink应负责有界排队／发送，不能无限阻塞；本轮没有实现后台队列。必要的本地审计文件写入失败仍阻止发布，以免无记录地完成研究。

**尚未接入Langfuse。** 没有导入SDK、配置密钥、发出遥测、运行Langfuse服务、实现SDK适配器、重试／离线补传或界面验收。上述为本地事件契约及映射设计，不声称兼容某个未核实的SDK版本。

下一步可以在明确部署方式和数据发送范围后，核实当前官方SDK／Python3.13兼容性，只新增sink适配器，把本地事件映射到Langfuse。先做不发网的映射／失败隔离测试，再获准做一次受控集成验证；无需改写研究流程。不能把本轮Mock测试说成Langfuse集成成功。

## 时间和研究质量边界

保持原A0、Time Machine及预测分支不变；本流程始终记录strict_historical_replay=false，不提供严格历史回放或前瞻价格预测认证。日期过滤、来源SHA和引用匹配各有用途，不互相替代，也不证明结论正确或投资回报。
