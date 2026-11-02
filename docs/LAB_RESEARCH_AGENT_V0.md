# Aeternitas Research Agent v0

本轮是单次研究调用的最小Agent，不是自主多步工具系统。版本核验 → 白名单输入准入 → 固定Prompt → 已有模型客户端 → 概率／引用校验 → 独占本地记录。没有接入新闻、财务、网络工具、交易或自动优化。

## 复用与运行边界

复用 `ResponsesAnalyst` 的客户端、现有 ChatGPT plan 授权对象、SSE完成事件解码和 Budget。为该客户端新增 `complete_json` 方法；原 `analyze` 四角色路径保持原样。每个Agent运行只有一次请求，失败不修复重试、不切换付费API。是否重试结果不明的真实请求应另行审查。

允许的本地工具只有 `market_data.load_csv`、`time_machine.run`、`lab_price_experiment.features`，由程序按固定顺序调用；LLM本身没有工具分发器。模型返回函数调用／其他能力会被拒绝。工具失败时不会继续调用模型。输入文件和CLI帮助均不会自动调用真实API。

## 输入契约

`run_agent(client, request, source, output_root)` 接收已配置的现有 `ResponsesAnalyst`，不自行读取凭据。

request必须且只能包含：

```python
from research_agent import TARGET
request = {
    'experiment_id': 'my-experiment',
    'symbol': 'VST',
    'cutoff_at': '2024-06-29T00:00:00-04:00',
    'target_spec': dict(TARGET),
    'experiment_mode': 'exploratory',
}
```

固定目标沿用价格实验版本：`Close[t+20个交易日]/Close[t]-1 > 0`，零收益标签为0，不含分红；预测锚点为最后提供的已结束日线Close，不表示能按该收盘价成交。

- exploratory：source只包含snapshot路径和独立固定的manifest_sha256。核对标准化CSV和日历哈希，检查交易日覆盖，筛出当地次日零点已结束、没有已知晚发布时间的日线，计算最后61条的8项既有价格特征。该路径明确为回溯假设，不是A0例外；保留未知发布时间、抓取时间、复权状态。它不具备严格模式的历史抓取证据。日历仅用于版本化的交易日质量检查，不冒充当年已知版本。
- historical_replay：source只包含csv、dataset_sha256、calendar、calendar_sha256。必须重新执行真实Time Machine strict路径并得到ready及strict_timing_passed，拒绝未知／晚时间和未认证调整。模型只取得准入日线；完整Time Machine清单只保留其指纹，不传递候选、排除或诊断信息。
- prospective：现使用独立的[前瞻快照准入v1](LAB_PROSPECTIVE_SNAPSHOT_V1.md)。source只包含snapshot和manifest_sha256，必须证明本次请求前的接收、冻结、来源／身份／价格口径及交易日质量；不设置严格历史通过。旧csv/calendar参数不能直接用于prospective。cutoff不在未来且距执行不超过24小时。

模型输入由程序新建白名单包，只包含请求标识、固定目标、模式、截止时间、61条OHLCV及其原有时间／调整状态、8条计算证据、来源版本、输入指纹和限制。多余请求字段直接拒绝，CSV额外列由原加载器拒绝。文件全集哈希只用作版本身份，不把截止后的价格、标签、评分、训练结果或原始文件正文发送给模型。

## 模型输出与本地记录

LLM仅返回probability_positive、support、opposition、uncertainties、gaps。概率须为有限数字[0,1]，不能为bool或字符串；至少一条论据引用实际证据ID和原文摘录，不确定性及缺口必须明确。无法获得另一方向的依据时该方向可为空。无效JSON、重复字段、缺失字段、捏造ID、错误引文、拒绝或未完成响应均失败，不产生默认概率。

程序绑定target_spec、decision_cutoff、input_fingerprint、source_version、model_identity、Prompt版本／模板／实际请求哈希、generated_at、experiment_mode和唯一prediction_id；不相信模型自报这些字段。模型确切后端版本若未返回记为null，返回的model只作供应商声明。`probability`和`decision_date`为与旧实验对接的别名，`model=research_agent`；没有未来标签，status=pending_evaluation。没有实现自动到期评分调度。

每个预测目录以独占mkdir预留：

- request.json：实际白名单输入、原Prompt模板／Schema、版本和哈希、准入回执指纹。
- prediction.json：通过校验的预测、证据目录和记录内容指纹。
- COMMITTED.json：请求及预测文件哈希；只有该标记有效且verify_record通过才能视为已提交。
- failure.json：失败或结果不明的保留记录；不能作为成功预测。相同prediction_id拒绝重复运行，新运行创建新ID。

verify_record不依赖后来更新的Prompt文本，使用保存的模板校验。独占文件及哈希防意外覆盖和检查损坏，不等于签名、WORM或正式不可变Registry；有文件写权限的操作者仍可改写并重算哈希。

所有v0记录均保持strict_replay_eligible=false、include_in_strict_statistics=false、llm_pretraining_leakage_excluded=false。即使声明输入通过严格时间检查，也不能由此证明LLM不存在预训练知识泄漏。引用字面匹配只能确认来源绑定，不能自动证明解释正确或排除模型自由文本幻觉。

## 最小离线演示

该CLI硬编码MockTransport，无真实模型开关；0.55是明确的演示响应，不是模型预测或默认错误回退。示例输入为已固定的真实VST版本，输出目录必须在源码仓库外：

```bash
cd /absolute/aeternitas
/absolute/venvs/aeternitas-lab-py313/bin/python -B research_agent.py \
  --snapshot /absolute/aeternitas-data/datasets/vst/processed-20261028T140411241682Z \
  --manifest-sha256 8b607fd1e7feec5863a4878f1f935656319ec93073db5a31eb37044b5b3fd077 \
  --cutoff 2024-06-29T00:00:00-04:00 \
  --experiment-id vst-agent-v0-offline-mock \
  --output-root /absolute/aeternitas-data/agent-v0/manual-mock-ledger
```

后续真实调用只能通过库接口注入原项目已授权的客户端；本轮没有读取授权、调用真实模型、连接数据库或下载数据，也没有验证真实模型输出质量。尚未完成正式预测登记、前瞻性运行、Brier对比实验、盈利或自动改进。
