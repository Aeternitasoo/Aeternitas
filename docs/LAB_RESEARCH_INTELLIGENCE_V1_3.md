# RSI v1.3：研究输入与推理契约开发版

本版是显式调用的离线预检和隔离候选，不替换生产Workflow，不修改v1.1/v1.2候选、旧评分、财务选择器或严格时间规则。三家v1.2任务已消费，只能用于开发诊断，不能再次充当独立留出。

## 实现与边界

`research_intelligence.completeness`首先复用`audit_inputs`核验来源，再对冻结SEC规范化全文检索。每个检索词单独记录，避免一个宽泛命中掩盖同组另一个概念的缺失。返回来源哈希、定位、实际命中次数、截断状态、补充证据及指纹。补充文本沿用现有45,000字节上限，每词最多6段，不自动注入模型。

状态包括：来源存在尚未提取、已提取未传入、已传入未引用、已引用但语义未审。未命中记为`not_found_in_scanned_sources`，不等于已经证明来源不存在；完整性恒为未认证。表格的数字、主体、期间和含义仍需专门提取及复核，不能凭关键词计分为完整覆盖。没有引用也不等于模型内部从未阅读，因此使用的是可观测的“未引用”状态。

反面证据使用同一检索器、显式的counterevidence检索主题，记录来源及未命中。它仍是词法候选检索，不是已经完成自动反证识别或语义RAG。

`validate_metric`要求指标名称、定义ID与文字、主体/分部、期间、币种/单位、GAAP口径、合并范围、源JSON指针和版本指纹、金额及定义审核状态。源金额、期间和accession必须一致。`financial_growth`先核验两侧来源，再检查口径、连续期间与分母；定义待审核时拒绝。第一版只接受直接USD观察，不隐式换成百万美元或其他币种。`comparable_growth`是已核验记录的低层计算函数，不能代替源校验。

定义审核不能由标签优先级自动批准。原生产`METRIC_TAGS`继续保留，因而旧路径的CEG口径风险尚未自动修复；新入口必须显式采用本契约，后续才可在独立测试后选择是否替换旧选择器。

`claim_review`验证支持/反面引用、反证检索记录，以及“观察—逻辑桥梁—弱化/推翻程度—替代解释”的推翻条件结构。它不判断语义真伪：entailment、因果及可证伪逻辑均返回待独立审核，不能自动晋升。现有Workflow Schema不包含完整dossier，本轮不冒充已接上生产输出契约。

## 离线入口

```bash
/absolute/venvs/aeternitas-lab-py313/bin/python -B scripts/audit_research_intelligence.py \
  --source-root /absolute/frozen/source/root \
  --bundle /absolute/bundle.json --run /absolute/saved/workflow/run \
  --topics /absolute/topics.json --output /absolute/new-audit.json
```

topics为`[{"id":"cash-flow","terms":["cash provided by operating"],"purpose":"support"}]`，也可指定counterevidence。输出文件必须尚不存在。导入和帮助不发送网络请求。

## 真实来源复核与候选

CEG冻结Companyfacts的同一10-K accession `0001868275-26-000032`中，`Revenues`为2025年25,533M、2024年23,568M；`RevenueFromContractWithCustomerExcludingAssessedTax`为22,663M、18,964M。前者变化约8.34%，后者约19.51%。这证明两套数值和旧选择器优先级的差异，不代表已经完成所有公司财务定义的自动认证。

新源审计在三家公司均发现关键词命中未进入原模型证据包；精确结果及定位保存在项目外，包含大量截断提示。这是检索遗漏的可观测证据，不是自动语义质量分数。

使用既有CandidateRegistry保存受控规则生成的Prompt候选，Parent仍为冻结v1.1候选。候选强调读取预检、财务定义核验、反证与合理推翻条件；原有输出Schema不放宽。版本关系、Diff、反馈与源码哈希独立保存。生成过程保留一个未运行的初稿，最终版本绑定加上字节预算后的实现，初稿标记superseded，不删除。

正式候选`57987833a5884f6b9540461d922a1c07`，Parent `5437f862e4cc4d6d94af1d554c4bcc1d`。没有模型请求、没有质量评分或晋升，不能称为真实研究质量已改善。程序化规则提案也不等于LLM自主诊断整个系统。

项目外证据根：`/absolute/aeternitas-data/rsi-v1.3/20261101T042205087475Z/`。

下一步在不修改候选的前提下，先将预检、通过审核的指标及Claim dossier显式接入受控实验输入/输出路径，完成冻结开发样本检查，再制定新的独立留出和真实调用预算。尚未进行新的真实配对研究、人审或生产集成。
