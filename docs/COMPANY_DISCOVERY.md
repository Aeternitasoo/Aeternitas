# 输入公司代码并研究

这是 v1.0.0 后的本地扩充。入口仍是 WSL 终端，报告为 HTML / JSON。

```bash
cd /absolute/aeternitas
# 确保本机 API 和 worker 运行；已经运行时不会重复启动
.venv/bin/python scripts/local_app.py start
# 可选：只识别公司，不调用模型，不需要启动本机 API
.venv/bin/python aeternitas.py resolve PLTR
# 正式研究：自动查公司、获取财务 / 原文 / 新闻、四步分析、校验和报告
.venv/bin/python aeternitas.py run PLTR --deadline 1200 --wait --wait-seconds 1250
```

把 PLTR 换成 NVDA、AMZN 等代码即可，无需编辑 config/companies.json。
不传 --as-of 时用北京时间当天；也可以给 --as-of YYYY-MM-DD、--question "你的问题"。
run 会先返回 run ID；后台 worker 推进任务，--wait 只负责等候，完成后打印可在 Windows 浏览器打开的本机 HTTP 报告链接。
停止等候不会取消研究。查进度：`status RUN_ID`，回看报告：`report RUN_ID`，离线核验：`verify RUN_ID`，查历史：`list --company PLTR`。
提交结果不明时沿用终端打印的 --idempotency-key 与相同参数，避免重复提交。

识别：去空格、转大写，BRK.B 转 BRK-B，查询 SEC 官方代码目录并取得 CIK。
目录缓存一天，保存字节、哈希和获取时间；`resolve PLTR --refresh` 强制刷新目录。
run 的 --refresh 刷新研究资料；目录单独刷新，避免每次研究重复下载全市场目录。
原 AAPL、MSFT 映射保留为本地覆盖，显示 source=local_override；其余有效代码按 SEC 目录自动发现。
没有公司代码默认值，也不会把不认识的代码替换成苹果。

支持边界：公司必须能在 SEC 目录中识别，且有当前解析器支持的 US-GAAP / USD 年度财务和 10-K / 10-Q 原文。识别成功只表示身份已确认，资料覆盖不足会明确失败，不能保证所有代码都有完整研究结果。
港股、A 股、其他海外交易所、私营企业、仅 IFRS / 20-F 披露暂未接入；代码不在 SEC 目录时不会调用模型。多股权类别共享一个 CIK 的跨类别重复导入仍受现有数据库限制，见设计记录。
公司名称 / 中文名搜索还未提供；输入交易代码。股票改名、退市或历史代码不在当前目录时不会猜测历史身份。

所有正常研究继续使用已有模型授权与数据配置。没有新增付费供应商，也没有自动切换 API 计费。目录识别失败不会提交研究任务；上游超时、无效代码和格式错误分别返回结构化错误。

受控与真实验证记录保存在本机 data/live/company-discovery/，进度见同一份 AI_Research_System_Progress.md。


## 本机真实验收（2026-10-24）

PLTR / Palantir 自动发现及真实四角色分析已通过；报告 run `3da0d5cde17441a4aa35a26eed7cfdc2`，状态 completed_with_gaps。首次模型连接中断，用户明确确认恢复同一任务后成功，共六次尝试（包括未知结果与综合结构修复），沿用订阅授权 / gpt-6.1-sol。

HTML / JSON HTTP 读取、verify、六项年度财务与原始财报 inline-XBRL 对照、财务 / 计算 / 新闻 / 原文各一条来源抽查通过。NVDA、GOOGL 仅核查身份查询，不宣称真实报告已运行。完整受控回归另记 471 passed，未访问外部供应商；不能替代上述真实验证。

[查看本机 PLTR 报告](http://127.0.0.1:8767/research-runs/3da0d5cde17441a4aa35a26eed7cfdc2/report.html)。本机服务须运行；研究资料局限以报告中的缺口为准。
