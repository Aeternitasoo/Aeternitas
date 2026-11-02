# VST 离线 Yahoo CSV 导入

本功能不下载行情、不读凭据、不访问网络或数据库。真实用户 CSV 尚未提供；验收仅覆盖合成 fixture。

使用现有 Python 3.13.18，只需传入文件路径：

```bash
/absolute/venvs/aeternitas-lab-py313/bin/python -B /absolute/aeternitas/aeternitas.py import-vst-csv /你的绝对路径/VST.csv
```

来源声明是“用户提供的 Yahoo Finance CSV”，不是程序认证的 Yahoo 原始文件。调用者应确保文件确属 VST / NYSE / USD。入口固定该身份，不根据七列价格推断证券。

必需列：Date、Open、High、Low、Close、Adj Close、Volume。允许大小写、表头前后空格、下划线，以及 Adjusted Close 别名；额外列、缺列、重复/歧义列拒绝。日期只接受 YYYY-MM-DD。空白/null/非数值、重复日期、非正价格、异常 OHLC、负数/小数成交量均报告行号，失败时不发布部分标准化结果。不会把缺失值当零。日期倒序会显式排序并记录来源行号；原字节始终不变。

每次创建独立目录：

`/absolute/aeternitas-data/datasets/vst/<UTC时间>-<随机标识>/`

- raw/input.csv：原文件逐字节副本。
- normalized/market.csv：沿用 market_data CSV schema，OHLC 使用 Close 口径；source_version 关联原文件 SHA-256。
- normalized/prices.csv：Date、Close、Adj Close 与原始行号，保留两种价格口径，不推导调整因子。
- quality_report.json：错误、警告、缺失交易日、异常及转换记录。
- validation/time-machine-1.json、time-machine-2.json：原 Time Machine 默认对2022-06-30及2024-06-28收盘后的严格检查。
- manifest.json：输入与输出 SHA-256、处理版本、范围、记录数、处理时间及来源声明。

失败快照只保存原文件、质量报告和清单，不生成标准化行情或时间清单。文件不存在、超5MiB等无法安全接收的输入直接报错；不会覆盖任何旧快照。

可选参数：

- `--calendar /绝对路径/日历.json`：符合现有 SessionCalendar 的真实 XNYS 日历。导入时保存该日历字节并绑定哈希。缺少日历时，交易日完整性明确标为未验证，不用周一至周五替代交易所日历。
- `--cutoff 2022-06-30T16:01:00-04:00`：自定带时区时点，可重复指定，替代默认两个时点。
- `--expected-sha256 <已知哈希>`：输入变化时生成失败记录。
- `--output-root /项目外的新目录`：选择其他快照根目录。

日历不足或包含非交易日记录会明确失败；覆盖范围内缺日和零成交量有警告。相邻有效记录 Close 变化超过50%只标待复核，不判定是公司行为，不修复价格，也不声称已实现统计异常检测。

本 CSV 不带可信发布时间和下载时间，因此标准化 available_at、retrieved_at 留空，availability_basis=unknown，adjustment=unknown。processed_at 只是本次处理时间，不是下载或历史发布时间。Yahoo 常见调整语义不能代替对这份文件的认证。

严格 Time Machine 将返回 blocked，并保留未知可用时间、缺少抓取记录、调整版本未知、未结束日 K 等原因。即使日期过滤成功，也没有严格历史回放认证。没有日历时还会报告覆盖未验证。导入成功退出0表示格式/标准化完成，不能解释为严格准入成功；质量失败退出1。

对同一标准化文件、相同 cutoff 和日历重复调用现有 time-machine，可比对 input_fingerprint 和 manifest_fingerprint；不同导入目录的运行元数据与处理时间不属于这些内容指纹。原始输入哈希通过 source_version 进入标准化记录，标准化 CSV 哈希通过 Time Machine dataset 绑定，导入 manifest 再列出各文件哈希。

本轮未修改 A0、行情模型、Time Machine、新闻或财务模块，也没有安装依赖。共享时间比较核心保留；本导入器的 NYSE、CSV列与调整口径仅用于该入口。
