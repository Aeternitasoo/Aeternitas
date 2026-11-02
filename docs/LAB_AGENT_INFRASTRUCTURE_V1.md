> 历史阶段记录：以下状态仅适用于文中日期。当前 Core、环境和入口以 README.md、docs/ARCHITECTURE.md 及唯一 Progress 最新记录为准；退役脚本不再作为当前入口。

# Agent Infrastructure：Oracle修复、Langfuse与Mem0/RSI连接

项目长期目标和基础设施优先级以AGENTS.md为准。本文是实现说明，不另立项目目标或进度文档。

## Oracle契约

来源授权由主程序绑定的冻结输入及其内容身份决定，Evidence Role不授予权限。supporting/counterevidence/contextual与具体论点的关系分别记录；语义关系依然待独立审核。重复检查按论点、角色、证据ID和原文片段进行；不同角色复用不会被统一误拒，同角色无意义重复仍拒绝。原失败测试不改动、不跳过。

修复候选使用已有CandidateRegistry。保持原Prompt/Schema字节不变，以运行时修复附件表达源码改动：manifest的proposal_metadata绑定runtime_path/runtime_sha256和完整runtime_diff，RSI读取时检查这两个哈希。Parent为68c509a9844e453e8d4fe1446f959c28。不能仅检查Prompt Diff便断言运行时没有变化。旧版本与旧评分不改写，没有生产晋升。

## Langfuse

继续使用langfuse==5.0.4及research_observability.Recorder。本轮增加质量屏障、memory_read/write、rsi_analysis和candidate_record的导出名称；补充Candidate Version、Evaluation ID、Memory ID指纹与重试数量。Workflow和Harness允许主程序传入observability_context，元数据进入同一套Trace，不另建事件系统。

模型/工具/Skill、Prompt、输入输出指纹、耗时、父子关系及真实Token沿用现有字段。工具原始参数保存在本地受控运行记录，远端只传参数哈希；来源、Prompt全文、记忆正文不导出。新增本地密钥模式脱敏，导出仍有第二层白名单。错误、队列溢出和flush超时不改变研究结果；本地Recorder始终保留。

`.env.example`列出LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY、LANGFUSE_BASE_URL。检查入口：

```bash
/absolute/venvs/aeternitas-lab-py313/bin/python -B scripts/check_agent_infrastructure.py --output-root /absolute/external/path
```

无凭据返回Requires Credentials，并保留本地检查Trace。有凭据才执行有进程期限的合成Span/Generation Smoke及服务端Observation读取。flush成功不等于已读取服务端；未读回不设remote_delivery_verified。合成Generation不发模型请求，也不虚构Token用量。

本轮未配置凭据，因此服务端实际连接尚未验证。官方SDK说明：[观测与读取API](https://python.reference.langfuse.com/langfuse)、[评分关联](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk)。

## 真实Mem0本地侧车

使用真实mem0ai==2.2.1的Memory.add(infer=False)、get_all和search。唯一记忆存储为SDK管理的本地Qdrant及SQLite history；没有自建第二套记忆数据库，也没有启动Qdrant服务器。主环境不导入Mem0，MemoryAdapter通过指定解释器和有超时的本地JSON调用连接。

实测发现Mem0要求protobuf<7，与主环境已固定的protobuf7.36.2冲突。完全隔离Python3.13侧车，不降级主环境；完整实际解析依赖固定在requirements-memory.lock。之前失败安装的日志保留。试验性Ollama构造器会尝试查询本地模型，故已移除该额外包，最终配置不用Ollama。

Mem0默认使用外部模型和嵌入（[官方快速开始](https://docs.mem0.ai/open-source/python-quickstart)）。本模式明确禁用LLM推断，用本地256维词法哈希向量替换嵌入组件，不下载模型、不使用项目密钥、不访问付费API。SDK必需的占位提供商客户端只在构造时初始化，配置的是非凭据标记和无服务的loopback地址，操作前替换为HashEmbedding/NoInference；固定SDK版本，并用全Socket阻断测试防止隐式网络访问。

这是实际Mem0持久化和检索，不是Mock Mem0；但也不是训练过的语义嵌入，不能把词法命中当成语义记忆质量。未来换语义嵌入需要另行兼容性、预算和检索评价。

官方[pgvector适配](https://docs.mem0.ai/components/vectordbs/dbs/pgvector)可连接PostgreSQL，但需要vector扩展及额外配置。本轮不迁移现有PostgreSQL、不创建扩展或更改证据表，因此不能宣称现有数据库上的Mem0已验证。侧车是当前已经跑通的最小路径。

### 记忆契约与使用

四类memory_type：project、research、procedural、task。每条记录包含memory_id（与SDK engine_id分别记录）、entity、scope、period、assertion_key、classification、sources路径/SHA、observed_at、created_at、confidence、origin、verified、version、supersedes/parent及可选expires_at。

可信写入必须由主程序显式trusted=True并核验来源；origin=model_output即使带trusted也不会成为可信记忆。这里verified表示主程序认可的来源和记录，不是Mem0或LLM自动证明语义真伪。Harness只暴露memory.search，不暴露可信写权限。

更新以追加新版本和supersedes实现，不覆盖旧记录。公司、主体、scope、期间、类型和事实/推断分类必须兼容；低权威、旧观察或未验证内容不能替代已验证记录。冲突不自动择优，冲突事实从可用结果中排除并返回冲突键。过期、未来创建/观察及来源哈希变化的内容不可用；到期的新版本不会使旧版自动复活。

检索必须指定类型、entity、scope、period，可进一步限定classification和as_of。authoritative_keys用于排除已有当前权威输入覆盖的记忆；记忆结果始终authoritative=false，不能覆盖新的原始披露。容量上限1000记录会明确拒绝，而非静默遗漏；尚未实现生产级分页和高容量索引管理。

MemoryAdapter.search在侧车缺失、异常或超时时返回memory_unavailable和空结果，现有Workflow无需Mem0也能运行。来源缺失的写入则显式失败，不能假装保存成功。现有A0/Time Machine规则不改动；记忆的时间过滤也不认证严格历史版本。

## RSI和可运行示例

`memory_tool`把固定范围的检索注册到既有Harness，只有memory.read权限，范围绑定指纹进入Tool Registry版本。没有模型自授权限或让记忆修改生产代码。

`infrastructure_rsi.review`核验既有真实运行/评价的文件哈希，调用既有analyze_failures处理已消费开发反馈，检索可信procedural记忆，再绑定已有Oracle运行时修复及其Diff。提案/推荐写回同一个CandidateRegistry格式，不重建登记器。当前是已知失败模板与已验证经验的联动，不是LLM自主识别任意未知故障或自主改代码。

示例入口：

```bash
python -B scripts/demo_memory_rsi.py --root /absolute/validation/root --memory-python /absolute/isolated/bin/python
```

该验收示例依赖本轮root里的oracle-replay.json、oracle-candidate.json与registry，且使用明确指定的历史SEC/真实运行记录；不是任意目录一键启动生产研究的入口。它写入四类真实来源记忆，验证跨进程读取，用脚本化（非LLM）决策运行Harness的真实memory.search工具，然后将三个既有GPT执行评价/Trace、Mem0经验与修复候选连接。保存本地Trace、独立Evaluation和待人审建议。

没有新的GPT请求、模型Token或前后研究质量成绩。四类检索命中不是“有记忆提高研究质量”的证据；无记忆/有记忆的真实研究A/B与长期RSI改进仍未验证。下一步应固定检索评价及独立研究样本后再执行有预算的真实比较，不直接晋升。
