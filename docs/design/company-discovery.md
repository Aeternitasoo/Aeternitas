# 公司代码自动发现：TradingAgents 参考与本地取舍

2026-10-24；v1.0.0 发布后的用户授权扩充，原发布标签及 M6 冻结验收记录不改写。

实际读取官方 TauricResearch/TradingAgents 的 main 分支，固定 commit
`e8f5503e9b86d5c2710b0c76b1f686703968c9df`（v0.7.0）。

- [CLI 输入与语法检查](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/cli/prompts.py)：输入 ticker，不按两家公司白名单限制。
- [代码规范化](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/dataflows/symbols.py)：先统一供应商代码格式；语法通过不代表数据存在。
- [SEC 适配器的 cik_for](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/dataflows/vendors/sec_edgar.py)：下载 SEC company_tickers.json，精确匹配 ticker 得到 CIK，目录缓存一天。
- [Yahoo 基本面](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/dataflows/vendors/yahoo/fundamentals.py)：将规范化 symbol 交给 yfinance；空响应显式失败。上游广泛市场覆盖来自这些供应商适配器，不是任意字符串都能研究。

我们采用输入规范化、官方目录确定公司、缓存和显式失败的机制，自行实现于 company_identity.py；没有复制上游源码或引入其框架 / yfinance 依赖。上游 Apache-2.0 许可证及既有参考说明仍见 REFERENCES_AND_LICENSES.md。

本次延续 SEC + Finnhub 与既有 ChatGPT 订阅，先扩展到 SEC 目录能识别且现有 US-GAAP / USD 年度及 10-K/10-Q 解析器能覆盖的发行人。港股 / A 股 / 私营企业、仅 IFRS 或 20-F 等材料不是本次已支持能力；不得对失败静默换数据源、换计费或让模型猜 CIK。

识别发生在创建任务前，十二秒预算、最多三次有界来源尝试（同步 I/O 按剩余时间设超时，不承诺硬取消正在阻塞的 socket）；任务本身的 deadline 从 worker 领取时起算，原最多八次模型尝试保持。失败不入队、不调用模型。目录来源限定一个固定 HTTPS 地址，响应保存为带哈希的不可变快照，缓存一天；refresh 失败不使用旧数据。config/companies.json 保留为显式本地覆盖，不自动写入源代码配置。

CLI 先检查语法 / 日期 / 问题；正式公司的解析由共享服务在 SQL 事务之外完成。任务保存 ticker、CIK 与识别凭据（options.company_identity），worker 直接使用这份确定身份，不重新解析。幂等指纹基于规范化后的研究输入和 CIK；目录获取时间 / cache_hit 不影响相同请求的幂等性。list 查询只检查代码格式，不访问上游。

多股权类别：BRK.B 规范为 SEC 的 BRK-B；现有数据库仍以 CIK 绑定单一主 ticker。同一数据库先后导入同 CIK 的另一股权类别仍会在 data_import 明确拒绝（模型调用前），未宣称本次解决该身份模型扩充。完整全市场覆盖与多类别身份模型为后续工作。
