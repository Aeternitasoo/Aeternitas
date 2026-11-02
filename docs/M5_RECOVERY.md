# M5：重复请求、中断与取消

正式入口仍是 CLI → 本机 HTTP API → PostgreSQL 队列 → worker → 本地 HTML / JSON。模型继续使用本机选择的 ChatGPT 订阅授权，不自动切换收费方式。

## 提交与幂等

`run` 会先输出一个非敏感的 idempotency key，再发请求。网络断开时保留这个 key；相同参数配同一个 key 只返回原 run，不重建任务。同 key 改公司、日期、问题、refresh、窗口、deadline、模型或工作流版本会返回 409。新研究请使用新 key。

```bash
.venv/bin/python aeternitas.py run AAPL --as-of 2026-10-23 --deadline 600 --idempotency-key my-aapl-review --wait
.venv/bin/python aeternitas.py status RUN_ID
.venv/bin/python aeternitas.py list --status interrupted
```

日期应改为实际研究截止日，不能选择未来日期；PLTR 等公司必须先有 `config/companies.json` 中的正确 CIK 配置。停止 CLI 等候不会取消后台任务。

## 恢复与不确定调用

```bash
.venv/bin/python aeternitas.py resume RUN_ID
.venv/bin/python aeternitas.py resume RUN_ID --acknowledge-unknown
.venv/bin/python aeternitas.py wait RUN_ID --seconds 600
.venv/bin/python aeternitas.py cancel RUN_ID
```

只有 failed / interrupted 可恢复。恢复沿用原日期、问题、模型、证据检查点、绝对 deadline，以及每角色最多两次、每任务最多八次的持久调用预算，不重新计时。四个角色都有有效检查点时，即使八次已用完，仍可只重建并发布文件。取消的任务不能恢复；已完成任务的取消请求不改变成功状态。

M1–M4 的历史报告继续按原契约读取和验证，没有补写旧检查点。没有 M5 执行版本的历史失败任务不会被静默重跑；需要新研究时创建新 run。任务完成后若文件又遭人为删除 / 篡改，报告会被拦截；这种已完成任务的离线维护与归档修复仍属 M7，不能用 `resume` 绕过状态和预算限制。

每次供应商请求之前，数据库先提交一个 attempt reservation。响应有完成事件及 usage 后记账；响应无效也占次数。429 / 部分临时服务错误最多尝试两次，等待不超过五秒并受总 deadline 约束；永久错误停止。网络结果不明时，正式 worker 不立即盲目重发。再次尝试前须明确确认可能重复消耗订阅额度。已确认的旧调用仍保留在账本中，新出现的不确定调用需要再次确认。

`--acknowledge-unknown` 也适用于响应已经完成、但没有成功提交角色检查点的情况：供应商可能已消耗额度，本地不能把结果当成已保存。费用不能从订阅 token usage 精确换算，报告保留 `cost: unknown`。

## 所有权、文件和资源

worker 在短事务中领取任务，取得随机 owner token、三十秒 lease，并每五秒续约。进程退出后 lease 自然失效，任务转 interrupted；不自动重放不确定的模型调用。同一数据目录最多两个有效 running 任务，默认启动助手仍启动一个 worker。各任务内部四角色保持原有顺序；本模块没有证明并行角色分析更好。

每个 owner 只写 `runs/RUN_ID/owners/OWNER_TOKEN/`。检查点记录相对路径、文件 SHA-256 和工作流指纹。恢复重新验证冻结来源与检查点；旧 owner 的 token 不能记账、续约、保存检查点或发布。来源快照保持内容寻址与引用审计，恢复不会静默换成新新闻。

发布前复核所有来源、数值、引用、模型实际输入输出与 manifest。仅在持有 run 行锁且 owner、lease、取消、deadline 仍有效时，才把有限的已校验文件逐个原子替换到正式目录，并将 manifest 最后写入；随后提交完成状态。HTTP 读者只接受数据库 completed / completed_with_gaps 且文件核验成功的报告。文件系统与 PostgreSQL 不构成分布式原子事务：若写完文件但提交前崩溃，API 仍拒绝发布，显式恢复可以用四个成功检查点重建，不再调用模型。诊断用离线 `verify` 校验文件完整性；正式是否已发布应同时查询任务状态。

复制和刷盘在私有 generation 中提前完成；发布锁内只有有限次文件重命名、目录同步与数据库提交。小负载测量曾发现把逐文件 fsync 放在锁内造成约四分之一秒的事务等待，旧失败记录保留；优化后按同一条件复测，不把测量失败删除或改成成功。

每个数据库调用、HTTP client、文件以及 heartbeat 由其创建方关闭；不在模型或来源网络等待期间持有数据库事务。当前使用短生命周期连接，没有连接池或公网并发承诺。取消 / deadline 让晚到结果不能发布或启动下一角色，但不能召回已被供应商接收的请求。

## 验收记录

实际结果写入 `data/live/m5/`：受控故障与 PostgreSQL 集成回归、预先声明协议的小负载测量、双会话竞争、EXPLAIN 对照，以及真实订阅模型中断恢复分别记录。合成来源 / 模型不会被记作真实接口成功。最终状态与已验收环节在同一份 Progress 中维护。
