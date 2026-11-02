# M2：事实、新闻、日期与 PostgreSQL

M2 的五个验收环节来自原模块任务：两家公司同链路、年度财务契约、新闻 / 申报日期与更新、真实数据库、来源回查与版本化回归。仍只做两个可比年度，不扩充季度推导、TTM、HTTP API 或 worker。

## 本机用法

当前 WSL 已安装 PostgreSQL 18.6、psycopg 3.3.3，数据库 `aeternitas`，角色 `sosei` 无 superuser / createdb / createrole 权限。经 Unix socket 使用系统用户的 peer 认证；项目 `.env` 中的 `DATABASE_URL` 已配置，没有新增密码，也不用提供给助手。

```bash
.venv/bin/python aeternitas.py db-init
.venv/bin/python aeternitas.py collect MSFT --refresh
.venv/bin/python aeternitas.py data-list
.venv/bin/python aeternitas.py data-facts MSFT
.venv/bin/python aeternitas.py data-calculations DATASET_ID
.venv/bin/python aeternitas.py data-evidence DATASET_ID EVIDENCE_ID
.venv/bin/python aeternitas.py data-import RUN_ID
```

`collect` 真实获取来源、选择年度事实和证据、核验并原子入库，**不调用模型**。`data-import` 可接已验证的旧 run，或 `--bundle` 指定 `collect` 保存的来源包，均不访问外部供应商。它不会修改旧报告。

配置 `DATABASE_URL` 后，研究在模型分析前入库，报告保存相同 `dataset_id`。模型接收同一份已验证的来源包。M3 已新增数据库任务 / 步骤历史、API 和 worker，正式 `run` 走 API；原同步入口为 `run-local`，见 [M3 说明](M3_APPLICATION.md)。DB 失败会停止，不能跳过入库继续伪装完整成功。显式 `DATABASE_URL=""` 只供 collect / run-local 来源文件诊断；不是正式任务的无数据库回退。

迁移只运行项目 `migrations/` 中的 SQL，已应用文件会核对 SHA-256；修改表结构须新增迁移，不能改写已应用的迁移。系统包安装可按 Ubuntu 官方仓库的 `postgresql-18 postgresql-client-18` 完成，再创建非特权本机角色与其拥有的数据库；本次已实际完成，不需要重新安装。这里尚不是 M7 的干净环境交付验收。

## 数据怎么保存

- `companies` 保存公司身份；`filings` 区分申报 accession、表名、披露日与文档名。
- `source_snapshots` 保存 URL / 查询 / hash 对应的内容身份；`retrievals` 保留每次获取的时间、刷新标记和尝试数。相同 bytes 的新获取可以是新 receipt，但不重复创建相同申报事实。
- `fact_observations` 保存公司、tag、USD、期间、申报版本与精确 NUMERIC 值。`fact_locations` 将原 fact ID / JSON pointer 映射到观察版本，允许不同完整响应保留同一个业务观察的不同位置。
- `datasets` 及其关联表固定某个截止日选取的事实、计算和证据。新申报 / 修订创建新观察；旧数据集不随新选择改写。同一申报、同期间、同 tag 发生不同值会拒绝，要求来源审查。
- `calculations` / `calculation_inputs` 保存公式、输入 fact IDs、舍入规则、值或明确的 unavailable reason。缺失事实没有伪造的零值行；真实零值和亏损会保留。
- `evidence_versions` / `dataset_evidence` 固定实际引用的文字、日期、原始位置、解析版本和来源。跨公司事实 / 证据绑定同时由应用校验和复合外键阻止。

文件和网络验证在入库事务之前完成；一个来源包在短事务中整批提交。主键、唯一、NOT NULL、CHECK、外键与参数化 SQL 保护数据；失败回滚整个批次，不在网络或模型等待时持有事务。

## 财务与日期契约

三个指标为 revenue、operating_income、net_income，映射顺序见 `financial_facts.METRIC_TAGS`。只选 10-K / 10-K/A、标准 us-gaap、USD、年度 duration；按实际期间而不是申报的 `fy` 选择，截止日按 `filed` 过滤。重述只在其披露后进入选择。相同 tag / USD 并不足以保证会计政策完全相同；跨 tag、非连续期间、零或负基数、混合申报利润率均有明确保护。

SEC 将符合条件的非自定义、全公司事实纳入 companyfacts；仍保留原 tag、期间和申报定位。[SEC API 说明](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

`filing_fact_audit.InlineFacts` 是审查辅助，检查选定值在已保存原始 HTML 中是否存在匹配的内嵌 XBRL：相同公司、期间、USD、无分部维度、明确 scale / sign。它只支持当前 tag 和数值变换；不宣称是通用 XBRL 解析器，不支持的情况不会算作匹配成功。

申报按披露日选入，reportDate 仅作为报告期间；历史页跳过完全晚于截止日的页面，优先最近的相关历史，在原有最多四页预算内查询。找不到年度 accession、仅一份文档或章节不足均显示缺口。

新闻按 Asia/Shanghai 的日期窗口筛选。按供应商 ID 或规范 URL 去重，去除 fragment / 已知跟踪参数，保留文章身份查询参数；重复观察优先最新记录。仅保存供应商标题 / 摘要，显示 requested window、实际最早 / 最晚返回时间和选取数量；这些不证明新闻覆盖穷尽。来源失败、有效空结果和覆盖缺口分别处理。

## 验证与证据

```bash
# 默认回归不调用外部接口；真实 DB 测试需要明确指定，使用独立临时 schema。
.venv/bin/python -m pytest -q
AETERNITAS_TEST_DATABASE='dbname=aeternitas host=/var/run/postgresql' \
  .venv/bin/python -m pytest -q
.venv/bin/python scripts/audit_m2.py AAPL_RUN_ID MSFT_RUN_ID
```

真实 DB 测试验证约束、参数化查询、Decimal 精确往返、零 / 缺失、更新后旧数据保留、跨公司拒绝，并在事实插入后由 DB 触发器制造失败，核对整批回滚及随后可再次导入。测试仅清理自己创建的随机 schema，不清空项目数据。[PostgreSQL 约束](https://www.postgresql.org/docs/18/ddl-constraints.html)、[事务](https://www.postgresql.org/docs/18/tutorial-transactions.html)

`tests/fixtures/m2_sec_annual_v1.json` 冻结两家公司已回查的公开 SEC 观察子集，只供回归；数组位置 / hash 与完整原响应不同，保留原始来源 hash 作为溯源说明，不能伪装为 live 输入。新闻摘要不复制进版本化公开 fixture。

本机两家公司的实测、财报 / 数据库交叉审查及运行编号在同一份 Progress；完整机器可读验收记录为 `data/live/m2/acceptance.json`。真实 HTTP、模型和数据库验证与合成故障测试分开记录，测试数量不用于计算模块进度。
