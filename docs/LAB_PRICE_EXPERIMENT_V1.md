# VST 首个价格预测探索实验 v1

本入口是独立的 retrospective price-only 实验，不是 Time Machine 的第三种准入模式。A0、market_data、Time Machine 和原研究 CLI 未修改。所有输出携带 exploratory_retrospective_unverified_vintage、strict_replay_eligible=false、include_in_strict_statistics=false。调用方不得纳入严格统计。

## 执行

使用 Python 3.13.18，无新增依赖。输出目录必须不存在，已有目录拒绝覆盖。只读取本地数据，不加载 API Key、不联网、不连接数据库。

```bash
cd /absolute/aeternitas
/absolute/venvs/aeternitas-lab-py313/bin/python -B lab_price_experiment.py \
  --snapshot /absolute/aeternitas-data/datasets/vst/processed-20261028T140411241682Z \
  --output /absolute/aeternitas-data/experiments/my-new-run \
  --acknowledge-retrospective
```

不提供确认标志会拒绝执行。本轮真实执行时另有 Python 审计钩子阻断网络与数据库 socket、项目目录运行时写入；日志在实验外部目录保存。

## 已固定的研究契约

- 输入：1,714条 VST 标准化日线，2020-01-02～2026-10-27。按版本清单核验 CSV/日历哈希，完整交易日集合与 XNYS 日历一致，否则停止整个实验；不把缺日压缩成连续20日、不填价格、不补标签。
- 价格口径：原 CSV 的 Close，不使用 Adj Close 替代 OHLC。目标是 Close[t+20]/Close[t]-1 > 0；等于0记为0。按20个实际交易日索引，不是20个自然日；不含分红、手续费、滑点或总收益，未知复权/拆股会影响经济解释。
- 决策时间：日线后的纽约当地次日零点。prefix 内每条日线必须已结束。这里是明确的回溯假设，不补写 available_at 或 retrieved_at，不声称历史可用性。
- 特征：1/5/20/60交易日收益；MA5/MA20-1；最近20个日收益的样本标准差（不年化）；最近60个收盘价窗口最大回撤（正值）；5日/20日平均成交量比-1。至少61条日线，零成交量分母拒绝。
- 训练截止：2024-01-01T00:00:00-05:00。特征截至当时且20交易日标签已成熟的训练样本才可用；末20个未成熟训练样本排除。训练决策2020-03-30～2023-11-30，926条；最后标签对应2023-12-29日线，按次日零点成熟。
- 验证：2024-01-02～2026-10-27，708个决策，688个成熟标签、末20个无法评分。不随机切分，不训练验证标签，不滚动再训练。按成熟时间清除训练边界重叠；不存在使用验证期后标签训练的情形。
- 模型：频率基线=训练标签正收益比例；传统基线=标准库全批量 L2 逻辑回归。训练集独立拟合均值/总体标准差；固定1000步、学习率0.1、L2=0.01，不惩罚截距，不自动搜索参数。保存最后梯度绝对最大值，未宣称求得精确最优解。价格/标签收益用 Decimal；统计特征及优化用有限浮点。
- 评估：Brier=(p-y)^2均值，Log Loss=二元交叉熵均值，计算日志时p裁剪到[1e-12,1-1e-12]。空评分集返回null，不返回0。未成熟记录不写label。每天的20日目标重叠，另按验证首日锚定、每20交易日取样，报告非重叠敏感性；不计算独立样本显著性或收益率。

## 文件与接口边界

输出 config.json、model.json、training.json、features.json、predictions.json、outcomes.json、report.json、manifest.json；均独占创建。先保存预测文件，再计算验证集结果。predictions.json没有未来标签、收益或诊断。

`features(prefix_bars, cutoff) -> Feature` 只接收已选前缀；`predict(Feature, frozen_model, kind)` 无行情全集/未来结果参数；`outcome(bars, index, asof)` 是单独的评分入口。相同输入/模型/配置具有相同experiment_fingerprint，run ID和执行时间不参与该指纹。来源CSV整体哈希用于版本识别，不是可让模型读取未来行情的输入特征。前缀指纹仅覆盖特征实际使用的历史close/volume。

现有四角色证券研究工作流不等于预测 Research Agent。当前缺少正式不可变预测登记：JSON独占创建与哈希只能防意外覆盖并检测变更，不提供数据库事务、签名或强制不可修改。manifest只校验文件完整性，不证明历史版本认证。

未来 Research Agent 最小接口应接收版本化的准入数据包/截止时间/特征契约，输出目标版本、正收益概率、模型与Prompt版本和输入指纹；正式登记先锁定预测，独立评估器仅在成熟后取结果。Agent不得接收outcomes、未来价格或拒绝诊断。探索与严格的数据包、登记和统计继续分开。本轮未实现Agent或正式登记。

## 本轮结果（仅探索）

| 基线 | 评分样本 | Brier | Log Loss |
|---|---:|---:|---:|
| 历史频率 | 688 | 0.247900 | 0.689119 |
| L2逻辑回归 | 688 | 0.352151 | 1.027914 |

每20日取样35个成熟样本：频率 Brier=0.240242、Log Loss=0.673520；逻辑回归 Brier=0.353830、Log Loss=1.056256。后者本次更差，未因此修改配置。

单只存续股票、今天修订的历史价格、未知发布/抓取/调整版本，以及重叠标签限制了结果；不代表严格无未来信息污染实验，不代表交易收益或可赚钱策略。
