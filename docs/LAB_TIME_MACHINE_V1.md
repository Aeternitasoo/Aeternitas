# Aeternitas Lab：Time Machine v1

实施日期：2026-10-28。仅离线读取，不读取凭据，不调用网络、模型或数据库，不创建预测。

## 实际能力与边界

- Implemented：指定证券、范围、历史截止时间、行情类型和 CSV 版本；复用行情层与 A0；生成可核对的实验输入清单。
- Tested：Python 3.13.18，固定合成数据和项目外临时目录；测试进程阻断所有网络/数据库 socket，并限制写入位置。
- Validated：实际 CLI 完成 CSV → A0 → 日/周/月输入 → 排除诊断 → JSON 清单 → 两次指纹比对。此处验证的是离线流程。
- 尚未验证：真实供应商历史版本认证、真实交易日历、真实 Finnhub 权限；未发起在线请求。CSV 声明时间与 raw 标签不构成独立的供应商认证。

v1 的数据集入口为已有 `market_data.load_csv`，数据类型只支持行情 `1d`、`1w`、`1mo`。不接入 SEC/新闻数据集，不自动收集数据，也不读取研究报告作为历史预测。未来的研究任务只应取得 `content.research_inputs`；清单中的 candidates、excluded_candidates、diagnostics 属于审计信息，不能作为额外研究数据。

## Python 接口

```python
from time_machine import run, save_manifest, verify_manifest

manifest = run(
    "/absolute/aeternitas/tests/fixtures/market_daily_synthetic_v1.csv",
    symbol="DEMO", start="2025-01-01", end="2025-02-28",
    cutoff="2025-03-03T12:00:00Z", data_types=("1d", "1w", "1mo"),
    calendar_path="/absolute/aeternitas/tests/fixtures/market_calendar_synthetic_v1.json",
    # expected_dataset_sha256="此前记录的64位SHA-256",  # 可选，指定后版本不符立即失败
    # expected_calendar_sha256="此前记录的日历SHA-256",
)
assert verify_manifest(manifest)
# 可选：指定尚不存在的路径；已有文件一律拒绝覆盖。
# save_manifest(manifest, "/指定目录/新的实验输入清单.json")
```

路径、文件 SHA-256 与来源版本共同标识所选数据集。加载器对**同一次读取的字节**计算 SHA 并解析；数据损坏、重复记录、无效时间戳、版本不匹配或参数无效时立即抛出错误，不发布部分清单。合法查询但无匹配证券、存在准入排除或周期缺失时，返回明确的 blocked/limited 清单。

## CLI

```bash
cd /absolute/aeternitas
/absolute/venvs/aeternitas-lab-py313/bin/python -B aeternitas.py time-machine DEMO \
  --csv /absolute/aeternitas/tests/fixtures/market_daily_synthetic_v1.csv \
  --start 2025-01-01 --end 2025-02-28 --cutoff 2025-03-03T12:00:00Z \
  --types 1d 1w 1mo \
  --calendar /absolute/aeternitas/tests/fixtures/market_calendar_synthetic_v1.json
```

默认输出 JSON；`--output /指定的新文件.json` 创建清单；`--dataset-sha256` 与 `--calendar-sha256` 可锁定输入版本。`--help` 无文件写入、配置读取或网络请求。DEMO、XSIM、价格和日历均为合成 fixture，不能作为真实金融研究结论。

退出码：ready 为0，limited/blocked 为2，非法参数/来源/文件错误为1（argparse 用法错误为2）。退出2时仍可能成功生成受限清单，应查看 admission_status，而非仅凭退出码判断原因。

## 时间与模拟策略

所有截止时间必须带明确时区，归一到 UTC；交易日期和交易所时区保留。生成时间只属于运行元数据，不参与数据可用时间判断。

- `strict` 默认沿用行情 `strict`：可用时间与抓取时间都必须已知且 ≤ cutoff，抓取不早于精确可用时间；仅凭交易日不准入。非 raw 或未知调整 vintage 排除。
- `assumed` 必须提供至少一条非空 `--assumption`。它复用现有行情 `captured` 模式，允许查看已抓取但历史发布时间未知/调整 vintage 未认证的数据；保留原 available_at=null、调整类型、警告和假设，所有输出的 strict_replay_eligible=false，strict_timing_passed=false。**已知晚发布、晚抓取、缺抓取时间仍被拒绝**，不创建新 A0 例外。
- 周/月始终沿用行情层的交易日历集合、覆盖范围、日历本身的时间准入和完整周期检查。Time Machine 不开放 include_incomplete；缺日或周期未结束时不提供部分周/月线。
- 没有覆盖整个查询范围的合格日历时，即便日线通过时间检查，范围完整性仍标为 session_coverage_unverified，整体 limited；周/月通常 blocked。

`ready` 表示本次声明的输入与完整性检查无排除；`limited` 表示有可用输入但存在排除/覆盖缺口；`blocked` 表示没有可提供的输入。严格模式仅在 ready 时 strict_timing_passed=true，它表示声明输入的时间检查，不表示供应商历史版本认证。assumed 模式的 ready 仅表示显式假设下的数据准备完成。

Finnhub 日线在现有适配器中为 split_adjusted、available_at=null；这些数据仍不满足严格回放条件。需要可追溯的原始价格历史版本和实际历史可用/抓取证据，不能用今天的抓取时间补写过去的首次发布时间。

## 清单与可复现性

沿用已有 SHA-256 来源标识和 `evidence_repository.digest` 的排序 JSON 哈希，不另建原始数据快照库，不新增数据库表。输入清单包含准入后的规范化记录，引用原 CSV/日历路径和 SHA；不再次复制源文件。

- `run_metadata`：run_id、生成时间、源文件/日历绝对路径。它们不进入内容指纹；同一文件搬迁不改变实验内容身份。
- `input_fingerprint`：规范化查询、策略及版本、整个 CSV 字节 SHA、所选候选记录哈希、来源版本、日历内容与 SHA。文件字节/来源版本变化会改变输入身份，包含被拒绝候选的变化；它不是仅对准入价格计算的哈希。
- `manifest_fingerprint`：整个 content 的确定性指纹，进一步包含实际输出、逐类型覆盖、诊断和状态。等价时区截止点与不同类型参数顺序得到相同内容。
- 每条输出有 record_id 和 daily_input_ids；周/月线可以回溯到日线候选哈希。保留 Decimal 字符串、零成交量、调整类型、来源、可用/抓取时间、周期结束边界。
- summary 的 candidate_count/admitted_daily_count/rejected_candidate_count 按唯一候选日线统计；同一日线被多个周期使用不重复计数。output_record_count 按所有类型输出数计。诊断可能涉及缺失交易日和多个周期，diagnostic_count/issue_counts 不是被拒绝日线数量。各类型排除数另列 results_by_type。

`verify_manifest` 核对内容与输出记录的哈希；不读取原文件、不认证发布历史，也不是数字签名。要检测本地源文件后来变化，使用原清单 SHA 再次 run，并比较指纹。保留源文件和日历版本才能完整重建所有候选；清单自身已保留本次实际准入的规范化内容。运行元数据未纳入校验，不具备防篡改认证。

## 本次验收

Python 3.13.18：226 passed / 0 failed / 0 skipped，其中 Time Machine 45、行情54、A0 32、已有边界6、财务26、证据50、HTTP/新闻13。未运行数据库、线上供应商或真实模型测试。

实际 CLI：相同输入两次得到相同 input_fingerprint / manifest_fingerprint、不同 run_id；日线6、周线3、月线2。提前至2025-01-04T05:01:00Z时准入2条、拒绝4条，limited；未知发布时间且 split_adjusted 的合成数据严格模式0条准入/6条拒绝，显式 assumed 模式仍保留 null 时间和非严格标记。

机器记录及可复查 JSON：`/absolute/aeternitas-data/time-machine-v1/20261028T084248003857Z/`，包含测试命令/日志、cli-demo/commands.json、两次清单、早期排除和未知时间演示、最终文件保护核查。

当前不支持预测登记、收益评价、记忆、自我优化、自动历史版本发现或在线获取。新清单尚未纳入原 M7 数据库/引用式备份验收。下一阶段先确定可用的版本化实验样本和预测契约；本轮结束后停止。
