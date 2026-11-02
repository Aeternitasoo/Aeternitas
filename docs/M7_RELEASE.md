# M7：本机可审阅发布候选

M7 的工程目标是离开当前会话后仍能启动、检查和维护。代码、数据库、不可变文件、运行说明和发布权限必须作为一件交付物审阅；仅写 Compose / CI 或生成备份文件不能算验收成功。

目前准备的版本名为 `1.0.0-rc.local`。它是本机候选，绑定独立本机候选提交，尚无远程 tag / release。原工作树保留无remote，隔离发布快照已连接用户确认的私有 `Sosei-gpu/Aeternitas`，暂不授予开源许可证。旧本机候选保持原样，最终v1包与对应提交、真实CI及release单独绑定。

## 交付文件

| 文件 | 审阅内容 |
|---|---|
| `RELEASE_CHECKLIST.md` | 工程、数据、运行与发布权限的逐项条件，以及现存限制 |
| `docs/ARCHITECTURE.md` | 实际 CLI → API / service → PostgreSQL / worker → 来源 / 模型 → 报告边界 |
| `docs/REFERENCES_AND_LICENSES.md` | 固定官方上游 commit、许可证、有限归属核查和材料分发边界 |
| `examples/AAPL/`、`examples/MSFT/` | 从原真实报告裁剪的 HTML / JSON 展示，无上游全文或新闻缓存 |
| `examples/run-manifest.json` | 真实 run 身份、日期、原报告 hash、模型请求与已知 usage；展示样例 hash |
| `scripts/package_release.py` | 本机白名单候选包、逐文件 hash、路径与凭据形态拒绝；不发布 |

运行、Compose / CI、备份与恢复的实际命令及结果由本次 M7 运维实现同步补齐 README 与 Progress。尚无结果的项目保持待验收，不从配置文件存在推断已经成功。

## 样例与验证

直接用浏览器打开 `examples/AAPL/report.html` 或 `examples/MSFT/report.html` 即可离线展示，不调用任何数据源或模型。JSON 标记 `display_only_redacted`，不使用正式报告 schema，不包含输入包、四角色全部尝试、原始快照或 publication manifest。完整原报告的 SHA-256 在运行清单中，原 run 保留本机，可通过正式 `report` / `verify` 入口校验。两种验证不能混淆。

样例只留下公开财务事实和确定性计算，以及最终综合中仅引用这两类证据的原模型论点。其余综合论点、新闻和财报文本被排除；公开数值的 tag、期间、单位、accession、JSON pointer、来源 URL 和来源 hash 保留。新闻 / 财报全文没有被替换成杜撰内容，也没有为了凑成完整报告重调模型。

本机源码候选包排除 .env、OAuth、数据库备份、全文原始缓存、私有日志 / 消息、个人 Progress / Module Plan、旧补丁、.git 与留出输入。包中保留运行与测试代码，不捆绑 pip 依赖源码或 wheel。`PACKAGE_MANIFEST.json` 说明未选项目许可、未选发布访问设置与工作树快照状态；它不是发布批准。

```bash
.venv/bin/python -m pytest -q test_m7_release.py
.venv/bin/python scripts/package_release.py \
  --output data/live/m7/aeternitas-1.0.0-rc.local.tar.gz
```

对外发布前应审阅最终准确包及其 manifest、M7 实际运行记录、项目许可和目标访问设置，再制作对应提交 / tag。用户未选择这些项之前，本机准备工作可以继续，远程写入保持未执行。

## 已执行的候选包核查

首个预检包安全解包后的完整回归实际为 450 通过、1 失败、1 跳过。失败指出白名单漏收录 `test_chatgpt_auth.py` 使用的 `scripts/complete_m1.sh`；补入该无凭据的既有订阅入口后，在第二预检包独立解包目录中执行相关六项回归全部通过，失败原日志保留。不能把第一次完整回归改写成全部通过。

第二预检包另在 `/tmp` 全新目录、独立 PostgreSQL schema 中运行其自带 `scripts/smoke_m7.py`：实际 API / worker 以四次合成模型调用完成报告，六份迁移重复执行保持一致，API 重启后历史与校验报告一致；无真实模型授权时新研究明确拒绝。整个过程真实供应商请求为零。此结果验证包内运行链路，不替代正式库备份恢复、真实接口记录或远程 CI 执行。

第三预检包包含 v2 备份与 `pytest.ini`，安全解包后的完整回归实际为 451 通过、2 跳过，134.60 秒，退出码零；跳过的两项明确需要第二个独立空测试数据库，不能计成已执行通过。另在工作树完善了打包的逐组件 `O_NOFOLLOW` 读取与导出前完整原档校验，六项打包安全 / 样例回归通过；这些仅打包与导出改动和第三包的完整回归版本分别记账。

随后为第三包单独提供第二个空测试数据库，仅补跑原先跳过的两项：实际 pg_dump / 独立库与迁移目录恢复（含早期无 task 报告）、上海时区源库到 UTC 目标库的 v2 行 hash 核对，2 项通过、0 跳过，4.93 秒，真实供应商调用为零。原完整运行的两项跳过记录保持原样，补验单独保存为 `data/live/m7/package-review-3-restore-tests.txt`；测试结束清除其拥有的 schema，未触碰正式历史。

两家公司展示页已用本机浏览器实际读取并查看截图：1280 像素视口中页面宽 1265，十个表格数值逐一核对，所有保留引用锚点存在，离线裁剪说明可见。最终候选还需在其余 M7 文件稳定后重新打包并记录准确 hash；中间预检包仅作故障与修复证据。


## 最终本机候选的验证边界

全新容器项目的 PostgreSQL 18 初始化、六份迁移、API / worker 与四步合成 smoke 已实际运行。原配置将 bootstrap superuser 直接降权的失败保留；修复为独立管理员与普通应用账号后，在新卷重新初始化。原生 WSL 的 308 文件备份 / 隔离恢复与容器安装分别记账，容器卷备份恢复未实测。

454 项本机全套回归后，PG18 账号初始化修复另跑 12 项必要回归通过；新增 `test_m7_postgres_init.py` 与 `config/postgres-init.sh` 均进入最终包。最终包单独解包到新目录，以全新虚拟环境验证正式 API / worker 的合成流程与持久历史。最终准确包 hash、本机独立 Git 提交及工作树保留证明在本机 `data/live/m7/release-candidate.json`；这些私有运维记录不随源码包分发。

本机快照是可审阅候选，不是远程 CI 成功或发布批准。用户明确选择仓库、可见性与许可证之后，仍须对对应提交实际执行 GitHub CI、建立 v1.0.0 tag / release 并留存结果，才可签完 M7。


## 用户选择后的私有 v1 构建

用户已明确选择 `Sosei-gpu/Aeternitas` 私有仓库和不新增项目开源许可。原本机候选、最初无作业启动的远程CI失败与后续修复均留存，后者的实测根因为 GitHub Actions 不区分 env 名大小写，`NO_PROXY` / `no_proxy` 相撞。现仅保留大写，固定SHA-256的actionlint验证实际复现原失败并通过修复，纳入工作流。

`--version 1.0.0 --private-release-repository Sosei-gpu/Aeternitas` 生成的manifest如实记录private和无开源授予，只表示本机构建，不伪造远程发布或CI成功。最终Git提交的每个文件须与该包字节一致；远程CI的head SHA、tag提交、release资产hash必须同时匹配。对应记录见私有仓库的v1.0.0 Release及本机最终取证。M8另验。
