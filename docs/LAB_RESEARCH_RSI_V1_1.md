> 历史阶段记录：以下状态仅适用于文中日期。当前 Core、环境和入口以 README.md、docs/ARCHITECTURE.md 及唯一 Progress 最新记录为准；退役脚本不再作为当前入口。

# RSI v1.1：冻结真实研究上的候选试验

本版本复用 Research Workflow、ResponsesAnalyst、ChatGPT OAuth、Recorder、LangfuseAdapter、Evaluation 和 Candidate Registry。它不扩展投资研究业务，不重建 Harness，也不自动写入生产 Skills、Prompt、权限或预算。

## 真实记录与数据集

`research_rsi.source_record` 只盘点明确提供的运行目录，保存源路径、Run ID、文件SHA、实际模型执行依据、Harness／Skill／Prompt版本、提交状态、已知失败及适用指标。真实模型标记来自已知本地执行记录和宿主提供的来源声明，不冒充服务端签名认证；受控模型明确标为false，未知来源保持unknown／null。

本轮最终数据集来自五条已有记录：

- Development：Microsoft／NVIDIA／Vistra原产业研究成功案例、后续真实GPT Workflow失败响应，以及冻结真实Microsoft数据上的离线Harness演示。最后一条使用Mock模型，不是新的真实模型研究。
- Regression：已提交的PLTR公司研究，检查全部既有论点的契约兼容性。它是归档重放，不是新增LLM执行，也不作为产业语义提升证据。
- Holdout：已保存的AAPL独立冻结研究输入，运行原版及候选版。只有一个独立公司／任务组，不是大型或从未被项目接触过的外部盲测集。

相关任务按同一来源家族分组，不能跨split；相同输入／输出内容哈希不能跨split。`freeze_real_dataset` 复用既有三组版本清单，在模型请求前冻结任务、请求计划、评估门槛和SHA。开发分析只接受Development，拒绝Holdout。版本比较仍由原 `compare_versions`／Registry完成。

最早选中的AAPL M1旧包缺少后来增加的新闻公司关联元数据。原始字节审计通过，但原证据选择器拒绝其进入Workflow。该预检、第一版数据集和候选全部保留；在零模型请求时改用通过同一准入规则的较新已存AAPL包，生成新的数据集版本。候选内容哈希保持完全相同，没有按模型表现重新选数据或改变规则。

## 自动失败分析与唯一候选

`analyze_research_traces` 读取真实响应、发送的Schema、输入证据、状态及本地Trace，分别统计未知ID、非逐字引文、超长引文、相同片段重复，以及同源不同片段。问题关联源Run、引文哈希、论点位置和代码位置。

实际开发问题分为：

1. **证据契约粒度**：旧校验要求同一论点内证据ID唯一，不能表示同一财报证据的多个不同片段。它是契约设计问题，不归咎于网络或一概归咎于LLM。
2. **Schema与本地规则不一致**：原Workflow发送的Schema没有表达引文长度、数量等本地限制，这是另一个潜在实现缺口。本次旧真实响应的22处引文均逐字匹配且长度合法，实际触发拒绝的是4处同源不同片段；没有把超长引文当作本次已观察到的原因。超长片段仍由新旧契约拒绝。

Skill方法或Prompt文字单独不能修改校验器规则。因此受控提案选择一个版本化的研究契约候选，包含 `contracts/research.json` 和 `prompts/research.md` 两份数据文件，以及与之匹配的可信宿主校验实现。不是让Agent减少引用以规避问题，也不允许候选执行Python代码。

| 约束 | 原版 | 显式候选v2 |
| --- | --- | --- |
| 引用身份 | evidence_id | evidence_id + quote |
| 同源不同逐字片段 | 拒绝 | 接受 |
| 完全相同的重复片段 | 拒绝 | 拒绝 |
| 每段长度 | 12–300字符 | 保持12–300字符 |
| 每论点不同来源 | 最多3个 | 保持最多3个 |
| 每论点片段 | 最多3段 | 最多6段，总计不超过1200字符 |
| 未知ID、非逐字引文、自由编造数值 | 拒绝 | 继续拒绝 |
| 发给模型的Schema | 原版 | 明确章节／论点／片段及长度限制 |

`research_contract_profile.py` 只接受两种固定规范，不接受任意权限、预算、路径或放宽的数值。v2复用原校验器检查每个片段、正文、分类和缺口；只对片段身份和数量进行显式版本化。旧真实失败响应不修改、不截短，旧契约继续拒绝，原Run不补提交。其不同同源片段符合v2共同检查，不代表原失败任务已成功，也不证明结论语义正确。

Schema范围核对依据：[OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)。来源数量、逐字匹配、重复片段及累计引文长度仍由本地检查，Schema不能替代领域校验。

## Workflow接入和证据绑定

新增的可选 `run_workflow(..., profile=...)` 使用冻结Profile的Prompt、Schema和明确版本。省略profile时仍走原版，旧报告按原路径核验。

Profile被保存到 `execution-profile.json`，代码SHA包含新校验模块；报告、实际消息、Schema、Prompt哈希和契约版本互相绑定，`verify_workflow`重新核验。Profile不进入模型的数据证据包，改Prompt／Schema不会改变输入内容指纹。

`prepare_frozen_input` 复用整个Workflow直至模型边界，随后明确中止；不调用模型、不伪造响应、不生成报告，`model_attempts=0`。它用于核对原输入指纹、来源审计、字节预算和冻结包兼容性。

真实执行继续使用原ResponsesAnalyst与OAuth客户端。仅加HTTP事件钩子记录脱敏的模型请求次数、状态和输入／请求指纹，不重写SSE、认证或错误处理。读取冻结数据，不请求SEC／新闻／行情。

## 请求预算与重启

`RequestLedger` 最多四次模型HTTP发送，每个预定case／版本只能发送一次，不自动重试。计数在发送前独占落盘；重新启动不能重置预算或重发已有case。冻结计划本轮只安排三次：开发候选、AAPL原版、AAPL候选。已保存的开发基线不计入新请求额度。

预算账本区分发送意图、HTTP状态和最终结构化响应；请求结果不明不等于没有消耗额度。认证／不可重试HTTP错误会停止后续发送，留下明确的未执行记录，不填默认结论。执行journal可重用已保存结果及哈希，但不能借resume追加预算。

```bash
cd /absolute/aeternitas
/absolute/venvs/aeternitas-lab-py313/bin/python -B scripts/run_research_rsi.py \
  --prepared /absolute/path/to/prepared.json --execute-real
```

命令导入及 `--help` 不连接服务。prepared.json引用已冻结的数据集、Registry版本及输入指纹，不含凭据。不要对同一已冻结任务替换输入、Prompt或请求计划。没有 `--execute-real` 时不发送模型请求。

## 独立指标与晋升

`evaluate_workflow` 在模型执行之后独立读取记录。双方同时适用预先固定的共同检查：发布提交、输入指纹、结构、逐字引用、引用长度与来源、证据覆盖、模型身份和实际用量／耗时。另记录各自版本的契约接受结果，以及旧契约结果，避免把历史失败标签改成成功。

引用准确率衡量原文匹配；证据覆盖只衡量需要引用的论点是否有引用，证据利用率是被引用ID占输入ID的比例。它们都不认证因果、语义支持、研究深度或投资价值。当前综合Workflow不执行工具调用或加载Skills，对这两项记录为不适用，而不是虚构100%正确率。

PLTR回归对全部原论点进行兼容性重放，不删截论点以迁就不同的章节上限。其本次执行没有LLM请求，新增模型Token为0；旧运行用量独立保留，不当成0。新模型用量只取客户端元数据，缺失时null，货币成本保持未知。开发旧响应的Token不加进本轮新调用消耗。

本轮预先固定：新增Token差不超过2000、每例不超过180秒、至少修复一例、没有新失败；真实研究晋升还要求至少两个独立Holdout任务组及独立人工语义复核。两个任务组也仅是最低覆盖条件，不构成统计保证。当前只有一个组，尚无人类语义验收，不能将一次修复当作普遍改善。

结果交给原Registry生成建议，保留diff、Parent、原因、风险、完整评估和数据集版本。即使确定性检查改善，也不自动晋升；原生产默认和原版release选择保持。人工审批机制未绕过，未模拟生产批准。

## Langfuse与未完成事项

SDK／Adapter延用v1，无新依赖。当前凭据缺失，真实服务端Smoke Test为 **Requires Credentials**，继续保留本地Recorder。离线内存Exporter或Mock测试不等于服务端接收成功。

尚需更大的独立真实样本、人工语义评估、重复对照／随机性控制、实际费用核验和长期运行观察。真实Research Workflow尚未通过新Harness进行真实模型工具循环，本轮不声称这一能力已验证；不实现任意源码自修改、自动部署或无人审批的生产晋升。

本轮准确运行结果、路径、实际Token和保护核验记录在唯一Progress及项目外试验清单中。

## 本轮真实验证

2026-10-30，沿用gpt-6.1-sol和原OAuth订阅客户端，实际发送3次、全部HTTP200完整响应、无重试，新增Token共49,797，货币费用未知。候选跨行业报告和AAPL报告均提交并重新通过verify_workflow；AAPL原版响应保留为验证失败，无提交。

| 样本 | 原版 | 候选 |
| --- | --- | --- |
| 跨行业开发：报告提交 | 失败（复用历史基线） | 成功 |
| 跨行业开发：Token | 28,523（历史，不计本轮） | 28,955 |
| 跨行业开发：整任务耗时 | 113.69秒 | 176.54秒 |
| AAPL留出：报告提交 | 失败 | 成功 |
| AAPL留出：逐字匹配 | 17/17 | 17/17 |
| AAPL留出：需引用论点覆盖 | 100% | 100% |
| AAPL留出：输入证据利用率 | 52% | 44% |
| AAPL留出：Token | 10,236 | 10,606 |
| AAPL留出：整任务耗时 | 72.94秒 | 83.09秒 |
| PLTR归档兼容性回归 | 通过 | 通过 |

候选跨行业输出有9处、AAPL输出有5处同源不同片段，均经过新契约的逐字及边界检查。AAPL原版也有2处合法不同片段，却被原唯一ID规则拒绝；观察支持契约误拒绝修复，不支持语义质量普遍提高。

固定的Token／耗时和确定性回归门槛通过，但Registry建议 **reject／暂不晋升**：只有一个Holdout组，且独立人工语义门槛未通过（尚未审查）。原版保持active，没有生产晋升、回滚或审批模拟。候选证据利用率下降、耗时增加均原样记录，不用任务完成掩盖代价。

相关离线回归430通过、0失败、0跳过；其中17项为本轮新增。开发期出现两项测试断言问题后修正，日志保留；不将中间失败日志删除或冒充最终结果。Langfuse服务端Smoke未执行，仍为Requires Credentials。
