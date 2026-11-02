# RSI v1.3 Workflow集成与验证

本版将开发预检接入`run_workflow`的显式候选路径。默认旧路径、旧Prompt/契约版本及旧产物保留；不自动晋升。原版97f7375f357148fbb7ba89d03a59b25d，冻结v1.2候选5437f862e4cc4d6d94af1d554c4bcc1d，v1.3开发候选57987833a5884f6b9540461d922a1c07；集成候选Parent指向开发候选，源码哈希、Prompt Diff与配置另行冻结。

## 执行屏障

1. 采集／冻结并通过原`audit_inputs`。
2. 组织证据后，进入`quality_source_financial` Span。`QualityBarrier`将固定Python检查器注册为Harness Tool，参数由主程序绑定，模型不能指定函数、路径或权限。复用Harness进程执行及30秒上限，异常／超时拒绝发布。
3. 执行全文与模型证据链审计，补充有限来源片段，记录未命中、未提取、未传入、未引用与截断。证据ID使用来源哈希和定位，不依赖安装路径。扫描选定的完整SEC文件，不认证已覆盖所有历史文件、分页或表格语义。
4. Financial Metric Contract核验源指针、金额、版本、主体、期间、币种／单位和定义审核。比较需要相同定义且期间可比；AI与非AI、分部与合并、客户合同收入与总收入不允许作为同一指标。输入中未审核的旧数值证据从两组同时去除，旧财务面板原样另存，不再作为权威面板展示。
5. 使用既有ResponsesAnalyst、OAuth、SSE、单请求预算和本地Recorder调用模型。没有另建模型客户端或让模型自由选择工具；这不是新一轮动态Harness技能决策实验。
6. 候选输出Schema强制每条论点含reasoning、counterevidence、counterevidence_status、metric_ids及falsification（观察、逻辑桥梁、weakens/refutes/unresolved、替代解释）。不允许靠省略字段回避校验。
7. `quality_counterevidence_claim_falsification` Span核对支持／反面原文、反证来源、指标绑定和结构。未审核的财务数值论点、未知指标或伪造反证导致拒绝。已检索但未引用的反证明确标记。
8. 语义支持和推翻逻辑未独立审核时只能保存`incomplete_research`，`publication_level=incomplete_only`、`eligible_for_promotion=false`。不能用格式或关键词通过冒充语义正确。
9. `verify_workflow`离线重建输入、财务安全面板、Schema与屏障结果并核对哈希。屏障文件必须进入提交清单；报告中的降级状态不能被移除。拒绝保存failure及quality-failure记录，不生成有效提交。

当前不提供人工覆盖入口，overrides为空。`COMMITTED.json`证明本地不完整研究产物原子提交，不代表研究质量通过或生产晋升。

## 接口及公平对照

`run_workflow(..., quality_policy=policy)`启用强制屏障。`comparison_context=policy`仅供受控基线比较，给原版同一份补充来源、移除同一批未审核数值，保持原版Prompt／输出Schema；不是声称原版原始全流程源码完全未变。旧默认路径仍单独做回归。

候选与基线实际模型输入内容指纹必须相同；Prompt、Schema和后处理屏障是声明的版本差异。这是整体集成版本对照，不能把差异全部归因于单个质量检查器。

policy包括每公司topics、max_hits、checker_seconds、经独立定义审核的metrics和calculations。来源未审核不得填成approved；零个合格指标时不能报告财务准确率100%，只能记录无可评分指标并降级。来源检索完整性、反证覆盖、语义支持和推翻逻辑分别记录，不压成一个虚假的总分。

## 真实实验与预算

`scripts/run_quality_validation.py --root <项目外轮目录> --prepare`先冻结，随后显式`--execute-real`。最多三组、六次请求，每例一次，无自动重试；持久请求账本防止重启后重新发送。保存完整结构化原始响应、响应ID、实际用量、延迟、拒绝及报告。授权配置auto时只沿用OAuth已保存的gpt-6.1-sol选择，不改变配置或计费。

GOOGL／AMD／CEG已消费，不作为新Holdout。新尝试ORCL、AVGO、NEE；NEE被旧财务解析器对2009年度歧义的保守检查阻塞，不为凑样本放宽规则。仅在可用来源上运行，不补造第三组。Langfuse没有凭据时只保留本地Trace。

正式结果、准确证据路径与测试次数见唯一Progress及本轮比较报告。没有独立人工语义审核和足够留出证据，不建议晋升。

## 本轮结果与未通过验收项

候选ID `68c509a9844e453e8d4fe1446f959c28`，Parent为上述v1.3开发候选。两组新任务ORCL／AVGO完成四次真实HTTP200请求，无重试，用量52,925 Token。原版0/2报告；候选1/2保存为不完整研究，正式质量验收两组均未通过。原版两次被原同源引用契约拒绝。NEE源解析失败，未发送其模型请求，余下两次额度未使用。

候选ORCL被反证角色标签规则误拒：两个引用片段已在实际输入中，且逐字匹配，仅因没有`intelligence_purpose=counterevidence`标签被拒。这是新证据契约／评价器缺陷，不能归因于模型引用不存在。冻结版本未根据结果修改。独立评价结论为`reject`，执行器初步汇总的hold意见保留，随后拒绝建议另行追加，不改写原始记录。

模型前离线相关测试113通过。真实实验后增加需求回归，扩大后的最终离线检查482通过、1失败、0跳过；唯一失败为`test_permitted_existing_source_can_supply_counterevidence`，明确保留已知缺陷，不xfail或修改预期。

所有财务定义审核与语义审查尚未完成；实际可评分合格财务指标数为0，不能给出“财务准确率100%”。结构性校验、未知与拒绝机制已经运行，不等于能够自动证明来源完整、财务分析充分或推理成立。

证据目录：`/absolute/aeternitas-data/rsi-v1.3-integration/20261101T043235309018Z/`。`paired-round/quality-comparison.json`为最终独立比较，`paired-round/registry/proposals/metric-coverage.json`记录提案（实际范围为修正来源角色误拒，历史文件名不表示财务实现已完成），`tests-final.txt`记录失败测试。

下一步是隔离修复反证的来源授权与用途标签混淆，保持原文／来源哈希和待审语义约束，通过该需求回归后再冻结新版本。另需经审核的财务定义及新留出，不能沿用本轮任务当全新独立样本。本轮无下一代候选、人工审批或生产晋升。
