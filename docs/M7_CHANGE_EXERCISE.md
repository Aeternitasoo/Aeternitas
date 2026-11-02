# M7 有限改动演练：备份跨时区校验

本次演练针对已有备份 / 恢复路径中的可复现缺陷，保留失败、修复和复验。实现与定位由 AI 助手完成，属于工程演练证据；没有因此将用户的 M8 独立 Debug 或独立实现能力标为通过。

## 触发条件与原失败

备份 v1 用 Python `datetime.isoformat()` 直接序列化数据库行，再计算 SHA-256。PostgreSQL `timestamptz` 表示同一绝对时刻，但驱动返回的时区偏移受连接的 `TimeZone` 影响。例如同一时刻可表示为 `2026-10-24T13:00:00+08:00` 或 `2026-10-24T05:00:00+00:00`；直接序列化的字符串不同，行 hash 随之不同。

实际回归让源连接使用 `Asia/Shanghai`、独立空目标库使用 `UTC`，执行真实 `pg_dump` / `pg_restore` 后触发 `Restored database rows differ from backup`。源数据没有因转时区改变，错误发生在备份后的行完整性表示。原失败为 1 项失败、2.35 秒，保存在本机 `data/live/m7/timezone-before-fix.txt`，没有覆盖为成功。

## 有界修复

改动在 `m7_maintenance.py` 的数据库值规范化与备份契约：

- aware `datetime` 先转 UTC 再 `isoformat()`，并在 `date` 分支之前处理，因为 datetime 是 date 的子类。
- naive `datetime` 缺少可确定的时区，直接拒绝，不猜测源数据库时区。
- `date` 保留日期语义；Decimal 继续精确保留，UUID 继续使用字符串。
- 新备份版本为 `aeternitas-backup-m7-v2`，manifest 声明 `aware-datetime-utc-v2;date-unchanged;decimal-exact;uuid-string`。旧 v1 文件保留；新工具拒绝缺少该契约的旧档，不能改 manifest 冒充可跨时区恢复。

没有更改研究日期、原报告、模型输出、已有 SQL 迁移、CLI / API 请求或分析预算。恢复仍只将任务记录中的数据根迁移到新目录；没有重新生成研究或用新模型结果覆盖原档。

## 复验与真实恢复

`test_m7_maintenance.py::test_backup_restore_rows_are_invariant_under_pg_session_timezone` 保留同样的上海→UTC条件，使用真实 PostgreSQL 和独立空目标库，检查恢复后状态、原始 created_at / as_of_date、报告与供应商调用数均保持一致。修复后的 12 项维护回归全部通过、13.43 秒，记录为 `data/live/m7/timezone-after-fix.txt`。

第三候选包解包后，原完整测试因未指定第二空库跳过了两项恢复场景；后来仅补跑这两项，2 项通过、4.93 秒，原跳过记录保持。补验包括跨时区与带早期无 DB task 报告的独立恢复，未请求真实数据或模型。

原安装停写后实际重新生成 v2 备份，并恢复到独立空库 / 新目录：

| 证据 | 实际结果 |
|---|---|
| `data/live/m7/backup-v2-result.json` | 308 个文件、5 条任务历史、3 份早期文件报告；manifest SHA-256 `4ddea416020c1b812dccda019113db9f9c44097b82f30a06226fcb5b3dc9a3a2` |
| `data/live/m7/restore-result.json` | 308 文件核验，4 份任务完成报告与 3 份早期文件报告均验证；历史 5 条；仅迁移 root；新来源 / 模型请求均为零 |
| `data/live/m7/maintenance-real-restore.json` | 隔离恢复环境的完成报告 HTML 被移走后，HTTP 从 200 变为 409；从成功 owner 原字节修复后回到 200，调用数仍为 6，DB 状态不变，原移走字节保留 |

这些记录验证现有 WSL 原生安装的备份与维护。Compose 使用另一个数据库和 named volumes，是独立安装；本次没有将上述结果改称容器卷备份 / 恢复成功。

## 与早期角色范围改动的关系

本次还保存 M6 的有限改动溯源于 `data/live/m7/provenance/limited-change.json` 和 `role-scope-change.diff`：在 prompt 组装中增加 `input_evidence_scope` 与 `prior_analysis_scopes`，区分角色实际看到的材料与整次研究的来源覆盖。前后真实 run 为 M5 `379073942801443698b541bb7c913124` 与 M6 `1ebad23247724fc7856bdeb11d268c7a`；旧 prompt hash 与新 prompt hash 分别保存，旧报告未改写。

该溯源复用既有 M5 / M6 真实记录，整理材料时没有新增模型调用。它说明一个有限分析改动如何穿过既有共享流程；M7 新发现并修复的跨时区缺陷则说明备份维护的回归如何发现问题。两者都不替代用户独立完成未见变体的 M8 验收。


## 后续发现的容器初始化故障

真实 Docker 启动揭示 PostgreSQL 18 不允许降权 bootstrap superuser。此前全套受控测试没有覆盖 fresh initdb 的管理员身份，因此配置文件存在和迁移测试通过都未发现该问题。现将 bootstrap 管理员与应用角色分开，应用成为数据库 owner 但不具备 superuser / createdb / createrole / replication 权限，管理员 TCP 密码禁用。

新增真实临时 PG18 cluster 回归复现原降权失败，再检查 SCRAM 应用登录、数据库 owner、全部迁移、管理员密码禁用、临时秘密删除和动态 SQL 失败日志脱敏。该修复与 delivery / packaging 必要回归共 12 项通过、6.133 秒，发生在 454 项全套之后，记录独立保留。第一失败容器卷未删除；全新项目重新初始化并执行 smoke，通过后才记录容器成功。
