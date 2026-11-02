# Aeternitas Lab：历史行情层 v1

实施日期：2026-10-28。Python 3.13.18，沿用 httpx、pytest 和标准库，无新依赖或数据库迁移。

## 状态与数据路径

- 已实现：Finnhub Stock Candles 日线适配、CSV 读取、统一 DailyBar、A0 时间准入、日/周/月查询和 CLI。
- 已测试：固定合成数据、MockTransport、项目外临时目录，阻断真实网络和数据库连接。
- 已验证范围：本机 CSV → Decimal 标准化 → 质量/时间检查 → 日/周/月 JSON 输出。
- 尚未验证：真实 Finnhub 请求、账户行情权限、实际历史长度与供应商时间标签。未发起真实探测。

路径为 `CSV / Finnhub resolution=D → MarketDataset[DailyBar] → query(cutoff) → 1d / 本地1w / 本地1mo`。不调用供应商 W/M 接口，不生成预测、不计算总收益。

## 官方接口核查

官方网页 `https://finnhub.io/docs/api/stock-candles` 在读取工具中未返回可读正文；核对了 Finnhub 官方客户端和 OpenAPI：

- [官方 OpenAPI](https://github.com/Finnhub-Stock-API/finnhub-go/blob/master/api/openapi.yaml)：GET `/stock/candle`，参数 symbol、resolution、from、to；时间为 UNIX 秒。日线按拆股调整，盘中数据不调整；“单次一个月”限制描述的是盘中数据，不能套用于日线。
- [官方响应模型](https://github.com/Finnhub-Stock-API/finnhub-go/blob/master/docs/StockCandles.md)：o/h/l/c/v/t 数组，s 为 ok 或 no_data。
- [官方 Python 客户端](https://github.com/Finnhub-Stock-API/finnhub-python/blob/master/finnhub/client.py)：stock_candles 传递上述四项参数。

公开规范未承诺每只证券/账户的日线历史深度，也没有逐根数据的首次发布时间、调整版本及分红总收益证明。本实现不承诺某套餐可用，不试图用 adjusted=false 获得未经确认的原始日线。401/403 明确失败，实际权限待用户授权探测后核实。

## 模型与质量

`market_data.DailyBar` 是 frozen dataclass。价格用 Decimal，成交量用非负整数；字段包括 symbol、exchange、timezone、trade_date、OHLCV、currency、provider、source_version、source_id、available_at、availability_basis、retrieved_at、adjustment、data_mode 和原始交易时间戳。

严格 ISO 日期；时间必须带时区并归一为 UTC。拒绝零/负价格、NaN/Infinity、不合理 OHLC 顺序、负数/小数成交量、重复日记录、异常字段及数组长度不一致；无数据有独立错误，不返回“成功空数据”。同一查询不混合交易所、时区、币种、供应商、数据模式或调整类型。范围包括起止日期，返回按交易日排序。

CSV 采用 fixture 中的明确列名。可用时间空值只能配 availability_basis=unknown；没有抓取时间会在准入时排除。CSV 文件 SHA-256、路径和每条来源版本保留。CSV 声明的时间并不是独立的供应商历史认证。

Finnhub 复用现有 `WebSources`、FINNHUB_API_KEY、X-Finnhub-Token、快照和脱敏错误机制。只扩展固定 URL 白名单，未开放任意 URL。正常响应在保存前校验，拒绝凭据回显；请求参数/来源回执不含 key。现有 20 秒以内单次超时、最多三次重试、有限 Retry-After 和总预算继续生效；每次 candles 请求（含重试）额外间隔一秒。CLI 总预算60秒、响应≤5MiB、最多20000根；一次 D 请求，不假定服务无截断，完整性另由日历诊断。

## 时间与调整政策

复用 `point_in_time.parse_timestamp`，并将原 A0 的已知时间比较抽成 `time_boundary_issues`，原 A0 与行情层共用：available_at、retrieved_at 均应已知且不晚于 cutoff；抓取不能早于精确可用时间。原 SEC 日级保守政策及 A0 输出含义保留。

- `strict` 为默认：未知可用时间、晚抓取、晚发布、调整版本不明均排除。v1 只准入时间合格的 raw 声明记录；所有复权类型暂不用于严格回放。
- `captured` 是显式的已抓取版本查看模式：仍拒绝晚抓取及已知晚发布记录，但允许查看未知发布时间/复权行情，保留 available_at=null、警告和 strict_replay_eligible=false。它不是历史版本认证，不提供“猜测发布延迟”的开关。
- Finnhub 日线固定标记 split_adjusted、availability_basis=unknown、available_at=null。抓取时间只表示本机观察到这一版本。默认严格查询会排除这些记录。
- 保留供应商 epoch；trade_date 按调用者明确提供的交易所时区转换。实际标签语义未在线确认；CLI 默认 XNAS / America/New_York / USD 是调用方可覆盖的描述，不是自动证券身份发现。
- 日线保守等待整个当地交易日期结束；周线为周一至周日，月线为自然月，均等待下一当地日期边界后才视为已结束。此规则可能比实际收盘更晚，不凭空推断盘中/半日市收盘时间。

每次查询输出 records、rejected、source、coverage、日历版本及 prediction_created=false；unavailable 是明确状态，CLI 退出2。无数据/错误参数退出1；成功有记录退出0。

## 周/月完整性

完整周期必须提供独立版本化 SessionCalendar JSON：exchange、timezone、coverage_start/end、sessions、available_at、retrieved_at、source、version。

模块核对日历身份、覆盖和历史可用时间，以 sessions 精确集合检查缺失日，不把自然日或周一至周五当成交易日。日历是调用方声明，模块不认证它来自真实交易所。缺整段周期也有诊断；部分查询范围缺少周期所需交易日时不算完整。没有日历、日历覆盖不足、缺日或周期未结束时默认拒绝周/月输出；`--include-incomplete` 可显式查看部分结果，complete=false 且附原因。

聚合：首根 Open、最大 High、最小 Low、末根 Close、Volume 求和。保留输入日期/来源、聚合身份哈希及日历；可用/抓取字段表示最晚输入依赖，另存 period_closed_at 与 eligible_not_before（含周期结束和输入边界的保守准入时刻），不把纯计算的边界称作供应商首次发布时间。未知输入可用时间时，eligible_not_before 仍为 null。调整类型不变，不额外加分红。

## 本机离线演示

仓库 fixture 的 DEMO、XSIM 和日历都是合成测试材料，不代表真实行情或真实交易所日历：

```bash
cd /absolute/aeternitas
/absolute/venvs/aeternitas-lab-py313/bin/python -B aeternitas.py market DEMO \
  --csv tests/fixtures/market_daily_synthetic_v1.csv \
  --start 2025-01-01 --end 2025-02-28 --cutoff 2025-03-03T12:00:00Z \
  --interval 1mo --calendar tests/fixtures/market_calendar_synthetic_v1.json
```

换成 `--interval 1d` / `1w` 查看日/周线。`--output /指定的新路径.json` 仅创建新文件，拒绝覆盖既有文件。CSV 和 --help 不读取账户配置或联网。

以下是**在线入口说明，本轮未执行**。运行前确认账户权限与额度；不自动切换供应商/套餐：

```bash
/absolute/venvs/aeternitas-lab-py313/bin/python -B aeternitas.py market-check AAPL \
  --start 2025-01-02 --end 2025-01-03 --exchange XNAS --timezone America/New_York --currency USD
```

该入口复用本机 FINNHUB_API_KEY，保存脱敏来源快照；接口成功也明确 strict_replay_eligible=false。需要查看已取得的数据，可显式运行 `market AAPL --finnhub --time-policy captured` 并提供日期、cutoff；周/月完整结果仍需相应日历。不会自动执行此命令。

当前不支持：真实交易日历下载、历史证券标识、行情自动分页/覆盖补齐、复权 vintage 认证、拆股/分红/总收益、价格预测和数据库行情表。本次不修改 Docker、CI、启动脚本和研究报告前端。

独立行情来源尚未登记为 PostgreSQL 业务数据集，未验收纳入 M7 的引用式备份。请保留指定输出和行情快照；不能把原研究备份当作已覆盖新行情文件的证明。
