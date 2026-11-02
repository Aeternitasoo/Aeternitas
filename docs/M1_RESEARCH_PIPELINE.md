# Aeternitas 1.0 · M1 完整研究入口

终端输入公司，执行 SEC 财务 → 精选财报原文 → Finnhub 公司新闻摘要 → 基本面 / 事件 / 风险 / 综合四步模型分析 → 带来源的 HTML / JSON 报告。

**当前默认模型入口已经改为官方 Sign in with ChatGPT。** 符合条件的账号可授权使用其 ChatGPT 计划；不要求 OpenAI Platform API key，不自动切换到单独计费的 API。真实资格、可用模型、权限、地区与额度由 OpenAI 实际返回决定。测试通过不表示你的账号已成功调用。

## 本机真实验收（2026-10-21）

M1 参考链路 6 / 6 环节已贯通。AAPL run `102d168a94d14d43894c7530e5e91c09`：四步真实 completed、四次尝试、约一百九十八秒；已生成 HTML / JSON，独立 verify / replay 与财务、计算、新闻、风险来源抽查通过。证据保存于该 run 的 `acceptance.json`；模拟回归另记，不能代替这条真实记录。

当次仍观察到代理连接 / 协议失败，失败 run 原样保留，没有伪装成功。报告的量化比较为两个完整年度，最近季度数值是明确缺口；MSFT 和更完整质量 / 可靠性验收留给 M2–M7。完整记录见 [Progress](../AI_Research_System_Progress.md)。

## 在已有仓库继续

本仓库的财务、研究、登录、缺头 SSE 与输出诊断补丁已应用。不要再次执行旧 patch；保留 `.venv`、`.env`、用户目录的 OAuth 授权和已有研究数据。

已有授权和数据源配置时，可直接执行：

```bash
export NO_PROXY="localhost,127.0.0.1,::1,${NO_PROXY:-}"
export no_proxy="$NO_PROXY"
.venv/bin/python aeternitas.py doctor
.venv/bin/python aeternitas.py run-local AAPL --refresh --deadline 900
```

当前进程需要继承本机有效代理；2026-10-21 实测为 `http://127.0.0.1:7897`。HTTPX 不将 `127.*` 当作 loopback 绕过项，因此明确列出上述地址。统一脚本也已补齐；不把代理或凭据写进报告。

## 本次运行时你需要做的事

1. 终端运行测试，随后只询问缺失的 SEC 联系邮箱和 Finnhub 新闻 API key；已填写的值默认保留。新闻 key 不属于 ChatGPT，需要你自己的 Finnhub 凭据。
2. 程序显示 Continue with ChatGPT，并尝试打开本机浏览器。浏览器没有打开时，把终端显示的官方登录地址复制到本机浏览器。登录自己的账号，选择 workspace，并查看 / 批准 Aeternitas 的 ChatGPT 计划用量授权。不用发账号密码、登录回调地址或任何 token 给助手。
3. 保持 WSL 终端开启。浏览器授权后通过 `http://127.0.0.1:随机端口/auth/callback` 返回本机；成功后回终端。这里是登录回调，不是新建网页产品。授权默认三分钟超时，可重新执行 login。
4. 终端读取该账号的实际模型目录，让你输入一个编号。不会沿用旧版固定的 gpt-4.1-mini，也不假设 Plus 拥有所有模型。目录里出现某模型并不证明这一请求的所有能力已获允许，首次研究调用才验证实际兼容性。
5. 脚本执行 AAPL 真实研究，自动核对保存后的来源 / 引用并打印报告路径。打开 HTML，人工核对一个财务值、一条新闻论点。正常但无新闻覆盖的报告带缺口，不能冒充新闻链已经完整验收。

## 独立命令

```bash
python aeternitas.py configure --provider chatgpt
python aeternitas.py login
python aeternitas.py auth-status
python aeternitas.py models
python aeternitas.py doctor
.venv/bin/python aeternitas.py db-init
.venv/bin/python scripts/local_app.py start
python aeternitas.py run AAPL --refresh --wait --wait-seconds 1000 --deadline 900
python aeternitas.py list
python aeternitas.py status RUN_ID
python aeternitas.py verify RUN_ID
python aeternitas.py replay RUN_ID
```

`login` 明确发起重新授权，适用于令牌撤销 / 登录失败后的恢复；`login --if-needed` 只在本机未连接时登录，统一脚本用后者。`login --new-account` 在浏览器选择另一个账号并保留旧账号记录；本轮没有完整多账号管理 UI。模型目录与选择按活动账号保存，执行中账号改变会停止，不能悄悄用另一个账号的额度。

`auth-status` / `doctor` 只检查本机状态，不向终端打印令牌、不产生模型调用；本地显示已连接不代表额度充足或远端授权未被撤销。`models` 访问官方模型目录，不发起推理。

报告位于 `data/live/runs/RUN_ID/report.html` 和 `report.json`，HTML 可直接用浏览器打开，无需网页服务器。`verify` 再查原始 hash、Decimal 财务重算、原文位置、引用 ID / 原句与报告指纹。`replay` 只重渲染已验证的保存结果，不获取新数据或重新调用模型，明确标为回放。

## 凭据、额度和失败

- 数据源配置保存在项目 `.env`，Git 忽略。默认 `AETERNITAS_AUTH=chatgpt`、`AETERNITAS_MODEL=auto`。旧 OpenAI key 即使还在文件中，也不会用于 ChatGPT 模式的请求。
- OAuth 状态保存在用户目录 `~/.config/aeternitas/`，目录 700、凭据文件 600，项目、补丁和报告不包含它们。只使用官方授权 / token / Responses 接口，不读取 ChatGPT Cookie 或已有 Codex 登录文件。
- 登录验证 state、PKCE、ID-token 签名 / issuer / audience / expiry / nonce，以及实际返回的计划授权 scope。身份登录成功不等于允许使用订阅额度。账号信息不能由未经签名验证的 JWT 推断。
- 签名公钥 / 目录等 GET 的网络异常最多尝试三次；HTTP 权限错误不会盲重试，旋转令牌的 POST 不自动重复发送。
- 令牌将过期时刷新；同一会话使用进程锁串行更新，旋转的 refresh token 与 access token 一起原子写回。临时网络失败保留凭据；确定失效的 refresh token 会清除令牌，保留已签发的注册 ID供重新登录。
- `logout` 清除本机活动账号的令牌，保留 host / 注册信息；本轮未实现远端撤销调用。若要撤销远端授权，在 ChatGPT 设置中断开 Aeternitas；程序会明确说明撤销未确认。
- 订阅额度不足、用户不符合资格、权限或能力不支持会停止，不自动改用 API key，也不编造额度刷新时间。错误保留 HTTP 状态、已识别的错误 code / param / request ID，避免记录原始敏感响应正文。
- 正常四次模型调用，每角色最多两次、全局八次尝试。订阅用量错误不盲目自动重试。输入最多九万字节，默认研究 deadline 三百秒，可 `--deadline 900`，最多一千八百秒。单次读取等待至多一百八十秒，连接至多九十秒，均受开始请求时剩余 deadline 限制；读取、重试和发布前再次校验总 deadline。传输错误记录异常类别和耗时，不记录可能携带敏感信息的异常正文。
- 官方订阅接口要求流式返回且不支持 `max_output_tokens`。本机因此限制收到的文字 / 事件 / 总字节并在超限或 deadline 后关闭请求；这不是供应商端生成 token 的硬上限，也不保证远端立刻停止消耗。不会沿用旧版“订阅每次最多 2200 output tokens”的不实保证。
- 流结束、失败、incomplete、额度错误或只有部分文字都不等于完成；只在收到 `response.completed` 并通过本地结构 / 引用检查后使用该结果。usage 按实际返回记录，未知部分不伪造费用。
- 已观察到本机真实推理返回 HTTP 200、缺少 `Content-Type`，但正文以 SSE 事件开头。对此只兼容缺失的响应头，仍用有字节 / 时间上限的事件解码器读取到有效 `response.completed`；明确声明其他媒体类型的响应仍拒绝。HTML、普通 JSON、坏事件、中断或失败不会因缺少响应头而当作成功。每次成功读取记录 `response_content_type` 和 `stream_header_missing`，便于回查；尚未据此确认响应头是在上游还是网络中间环节丢失。
- 2026-10-21 最小真实请求进一步确认：正文存在于 `response.output_text.delta` / `done`，而 `response.completed.output=[]`。旧代码丢弃正文事件，只读末尾数组，导致两次空文本与 JSON 首字符报错。现在按 output/content 索引聚合、核对 done / item / 末尾快照一致性；仅在有效完成事件后重建缺失正文，记录 `stream_output_reconstructed`。空完成、中断、失败、拒绝、冲突与超限仍拒绝。该观察只确认响应形态，不判断由上游还是代理造成。
- 输出不合格时记录具体字段 / 原因，例如 `claims[0].text` 含自由数字、引用 ID 超范围、引文长度或逐字匹配失败。完成响应的文字另存 `steps/ROLE-attempt-N-output.json`，标为未验证草稿，最多六万四千字节；它不是正式分析或报告。修复请求携带具体错误和最多一万二千字节的不可信草稿，不将草稿当来源，不修改原始证据。总输入超限时省略草稿、保留错误与证据；每角色仍最多两次。提示版本为 evidence-roles-m1-v3，明确每条论点一至三条引用，优先简洁的重要判断。这增强定位和修复条件，不保证每次模型都能自动修好。
- 可运行 `.venv/bin/python scripts/probe_model_stream.py` 发起一次独立的最小真实订阅请求，检查固定 JSON 的解析与完成事件，诊断保存至 `data/live/diagnostics/minimal-model.json`。这会使用订阅额度，不是离线测试；pytest 不访问真实供应商。诊断仅保存固定测试文字、事件类别、形态与汇总用量，不保存凭据。
- 若你日后明确选择标准 API，可以运行 `configure --provider api_key` 并配置 Platform key；此模式单独计费。统一脚本始终明确选择 chatgpt，不默默改变收费方式。

## 原有数据与发布边界仍保持

原文最多两份、每份最多两个章节 / 各两千字符；最多补查四份历史目录。新闻按 Asia/Shanghai 截止日筛选，最多三十天窗口 / 十条摘要，不抓文章全文、不宣称新闻覆盖完整。原始响应、hash、归一化位置、获取时间与来源保留；refresh 真实请求，失败不静默使用过期缓存。

每条模型判断必须引用当前角色允许的证据 ID 及逐字摘录，错误结果先修复再进入下游。AI 自由叙述不含阿拉伯数字，金额 / 比例由程序表格展示；这些检查不保证所有语义和中文数字正确，重要论点仍需人工复核。外部材料没有 shell、任意网络或交易工具。报告转义不可信内容并禁用脚本。

M3 已将正式 run 改为本机 API 入队、独立 worker 执行，任务与步骤进入 PostgreSQL；原同步诊断入口改名 run-local。文件发布与 DB 不是原子事务，中断仍可能留下未发布文件；重新创建任务可能再次消耗额度，自动恢复 / 幂等 / 取消继续 M5。完整操作见 [M3 说明](M3_APPLICATION.md)。Docker / CI 和完整版本发布继续后续模块，受控测试不代表用户独立能力。

## 官方核查入口（2026-10-20）

- [Sign in with ChatGPT：注册与登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [账号、会话与刷新](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [模型目录与流式调用](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [订阅接口限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [错误与恢复](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)
- [SEC API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [Finnhub 公司新闻](https://finnhub.io/docs/api/company-news)
