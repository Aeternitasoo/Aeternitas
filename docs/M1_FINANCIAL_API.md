# M1 第一批：真实财务数据入口

这批代码改变一个实际行为：不再先手工下载几份 JSON，再统计条数；现在输入公司，程序按配置请求 SEC companyfacts，保存原始响应，从中选择两个年度的财务事实并计算指标。

这是完整研究系统的数据输入部分。新闻、申报原文章节、LLM、PostgreSQL、网页、worker 尚未接入；不能把这个阶段叫成熟 v1 或完整 AI 投研报告。

## 运行

在原来的项目目录、已激活的 venv 中：

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python fetch_financials.py AAPL --refresh
```

终端会要求输入自己的联系邮箱，仅发给 SEC 作为 User-Agent，不保存到结果，不需要发给导师。这不是 API key。无交互环境可在本机设置 `SEC_CONTACT`。不要把个人配置提交到 Git。

首次运行与 `--refresh` 都会实际访问接口；再次运行不带 refresh，可使用 24 小时内的缓存，终端明确显示“缓存命中”和原获取时间：

```bash
python fetch_financials.py AAPL
python fetch_financials.py MSFT --refresh
```

换新公司通过 `config/companies.json` 配 CIK；当前支持范围仍以 AAPL / MSFT 验收。换公司不等于已经验证其会计口径 / taxonomy。日期过滤示例：`python fetch_financials.py AAPL --as-of 2025-10-01`。

SEC companyfacts 是真实数据 API，不需要访问 key；它与之前 submissions 提供的申报目录不同。官方说明：[SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)。访问需标识工具并控制频率，参考 [SEC Developer Resources](https://www.sec.gov/about/developer-resources)。不要循环高频刷新或并行运行大量下载。

## 输出在哪里

- `data/live/snapshots/companyfacts/<CIK>/<SHA256>.json`：API 返回的原始字节，按内容指纹保存。
- `data/live/retrievals/<ID>.json`：每次成功获取的 URL、时间、hash、请求尝试数；即使刷新后内容没变，也保留新的获取记录。
- `data/live/cache/companyfacts-<CIK>.json`：最近成功获取的索引，不是正式报告或唯一历史。
- `data/live/financials/<公司>-<ID>.json`：本次选出的事实、计算、缺口和来源记录。每次生成新文件，不覆盖旧结果。

终端最后直接打印本次结果与原始响应的位置。`data/live/` 默认不进 Git，防止运行历史淹没代码 diff；旧教学快照和原测试没有删除。

每个事实保留：tag（来源字段）、unit（单位）、期间、filed_at（披露日）、accession（申报编号）、JSON pointer（在原响应中的精确位置）、fact ID。指标保存公式、输入 fact IDs、舍入与不可计算原因。

## 三个模块分别干什么

| 文件 | 职责 | 理解词 |
|---|---|---|
| `sources/sec.py` | HTTP 请求、响应验证、缓存、快照和获取记录 | 把外部资料可靠地拿回来 |
| `financial_facts.py` | 选择年度 / 观察版本、核对基本口径、计算 | 决定拿回来的数字怎么用 |
| `fetch_financials.py` | 读命令参数、调用上述模块、展示并保存结果 | 把流程接起来的入口 |

旧 `research.py` 继续保留为整数 JSON CLI。旧 `filing_parsing.py` 的五条限制暂未迁移，新的财务路径不依赖它；正式披露目录与历史分页仍待后续接入，不能说已经修好。

## 已实现的选择规则与限制

1. 仅使用 us-gaap、USD、10-K / 10-K/A 中时长 330–400 天的 duration fact；instant 和季度值不作为全年利润。
2. 先按披露日不晚于截止日期过滤，再对同一期间选择最新已知观察版本；不用 `fy` 直接判断事实年份，因为比较年度也可能放在当前财年的申报中。
3. 收入使用配置在代码里的三个已知 tag 依次作为映射；同期间优先更新的披露，日期相同再用 tag 优先级。来源 tag 会保留。
4. 以最新两个收入年度为锚；最新年度缺利润时明确缺失，不静默退到一个更旧但齐全的年度。
5. 金额在 JSON 解码时保留 Decimal，再以十进制字符串写入结果，避免浮点数损失。零值不等于缺失。
6. 营收同比要求年度相接、时长差不超过 7 天、收入 tag 一致；零 / 负基数不输出普通增长率。利润率不混用不同 accession 的利润与收入。需要人工核对的情形标记原因。
7. 以上是保守的程序检查，仍不能单独证明会计政策 / 合并范围完全不变；需接原申报并人工核对。历史接口是今天取得的聚合观察，日期筛选不等于交易级时点重建。

网络采用每次 I/O timeout、最多三次暂时错误重试、下载大小和经过时间检查。所有重定向拒绝，代码不接受任意 URL。强制刷新失败就明确报错，不把旧缓存冒充新获取。

这个同步 client 不保证对已阻塞 socket 的硬实时取消，也没有持久化全工作流调用预算、worker ownership 或 DB 事务恢复；这些仍在 M5。原始文件、获取记录和缓存指针之间不是一个跨文件事务，后续 M5 / M7 补孤儿扫描与恢复。

## 本次验证如何理解

新增测试使用明确标识的 synthetic 财务数据与 HTTPX MockTransport。它们验证缓存 / 刷新、变化响应、错误、财务期间、缺失、零、重述、精度、单位与计算，不是 SEC live 成功证据，更不是用户独立完成的证明。

2026-10-19：导师独立环境 Python 3.12.14、pytest 9.1.1、HTTPX 0.28.1，执行 `python -m pytest -q`，48 passed（保留原九项，新增三十九项）。`fetch_financials.py --help` 正常。

用户原 Python 3.14.4 保持不变，需在本机执行同一测试。尚未替用户运行 live 下载；真实公司响应的 tag、年度覆盖和网络可用性，以本机首次运行输出为准。SEC 抓取被拒绝时应保留 HTTP 状态并排查，不绕过封锁或伪装已经取得数据。

来源 / parser 测试通过，只能给成熟度 MAT-02 / 03 / 05–08 增加部分证据；整项仍待 live、原申报 / DB / 报告链验收。下一批继续接新闻与 LLM，最终形成 M1 的一份完整 HTML / JSON 研究报告。
