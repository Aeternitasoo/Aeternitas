# 最小前瞻行情快照准入 v1

## 两种证据问题

历史回放仍走 A0 / Time Machine strict，发布时间、历史抓取时间和调整vintage要求完全不变。前瞻模式独立证明：这一版输入已在本次决策与请求前被系统接收、冻结并检查。接收今天的历史文件，不会让它变成当年已知输入；原行available_at / retrieved_at / adjustment从不补写。

前瞻通过标记为prospective_admission_passed=true、statistics_class=prospective_observed_snapshot；strict_timing_passed、strict_input_timing_passed、strict_replay_eligible、include_in_strict_statistics仍为false。模型预训练知识隔离不获认证。

## 接口和产物

新增prospective_snapshot.py：

- create_snapshot(csv_path, dataset_sha256=..., raw_files=..., calendar_path=..., calendar_sha256=..., source_type=..., source_description=..., attestations=..., proof_files=..., request=..., output_root=...)。
- assess(folder, manifest_sha256=..., request=..., request_started_at=None) -> (准入回执, 合格日线元组)。任何关键失败均返回blocked和空元组；解析/哈希失败也不能返回部分输入。

request复用Research Agent的五个字段：experiment_id、symbol、cutoff_at、target_spec、experiment_mode=prospective。目标对象直接复用research_agent.TARGET，沿用价格实验TARGET_VERSION，没有另建收益或期限定义。

接收前规划一个决策时点，接收和冻结必须在该时点之前完成，再开始请求。系统内部时钟生成created_at、received_at、frozen_at；它们不是可传入的历史事实。测试通过monkeypatch固定时钟，只用于明确标注的合成fixture。

output_root建议项目外，每次生成独占S-UUID目录：

- raw/source-N.bin：原始文件的逐字节副本，带各自原路径和SHA-256。
- normalized/market.csv：已有行情CSV原样副本，直接使用现有DailyBar/load_csv校验，不修改或重算来源字段。
- raw/calendar.json：已固定哈希的SessionCalendar。
- evidence/*.bin：本地审核所依据的证据文件，固定原始字节和SHA-256。
- quality_report.json：OHLCV、重复、交易日覆盖、缺失／非交易日、未结束／未来记录排除数量；它不是来源认证或新鲜度通过凭证。
- manifest.json：snapshot_id、symbol、exchange、currency、timezone、source_type、来源说明及审核、received_at、original_downloaded_at、data_cutoff、price_field=Close、adjustment_status、原始文件哈希、标准化内容指纹、日历版本、质量引用、created_at、frozen_at、完整request和代码版本。
- SEALED.json：绑定manifest哈希、快照ID和冻结时间；数据文件先fsync，再固定冻结边界并写入绑定清单和封存标记。未封存的中断快照不能通过。

本轮v1的原始下载时间固定保留null；非null参数会被拒绝，因为尚未实现下载时间证据核验。received_at只说明系统本次已收到这些字节，绝不代替供应商历史首次发布时间。调用方固定manifest_sha256，避免只读取可变路径。

## 来源、身份、价格和日历证据

attestations必须分别包含source、identity、price_basis、calendar四个角色。每项字段为：status（verified或unknown）、reviewer、reviewed_at、proof_ids、claims。unknown可归档，但不能准入。

verified也不能只有一个布尔标记：必须有审核者、带时区且不晚于接收的审核时间、固定哈希的非空证据文件及匹配角色。不得仅拿原行情CSV或日历本身的哈希当独立审核证据。claims需与实际输入匹配：

- source：source_type、description。
- identity：symbol、exchange、currency、timezone。
- price_basis：price_field=Close、adjustment_status。
- calendar：exchange、timezone、version。

proof_files格式为 `{证据ID: {path, sha256, roles}}`；raw_files为 `[{path, sha256}]`。资料可以由人工或项目审核流程确认后提供；代码验证审核声明、对应字段与证据字节绑定，不会自动理解文件正文或独立证明供应商真实性。合成单元测试的证据只证明规则可运行，不是实际供应商认证。

未知复权口径会被拒绝；已说明的split_adjusted或total_return_adjusted不冒充raw，前瞻可研究决策时已经冻结的该版本，但后续评分仍必须维持目标价格口径。没有拆股证据不代表没有拆股。本轮不实现公司行为调整、预测或评分。

## 准入顺序

1. 核验固定清单、封存标记、原始／标准化／日历／证据／质量文件哈希；复用现有CSV和Decimal质量验证。
2. 核验实验、目标、股票及决策时点绑定；复核来源、身份、Close口径及日历审核。
3. 要求 created_at ≤ received_at ≤ frozen_at ≤ cutoff ≤ request_started_at ≤ checked_at，截止时间距检查不超过24小时。请求开始时刻由run_agent在进入prepare前实际采集。独立assess默认以本次检查开始为请求边界。
4. 复用既有日K保守结束规则：纽约当地次日零点才视为结束，不改成提前认定收盘。用版本化交易日历查找已完成会话；日历必须覆盖截止当地日期的前一天，不能用自然日或周一至周五推断真实交易日。
5. 最后完整行情日必须等于日历给出的最近已完成会话；缺会话、缺日历覆盖、少于61日、OHLCV错误或已知晚时间均拒绝。原始输入中的未结束／未来日线只留在原档审计，不能进入特征或模型输入。
6. 返回最后61条合格日线；Agent复用现有特征函数，绑定snapshot_id、manifest_sha256、输入指纹及准入分类。诊断、证据审核材料和排除信息不传入LLM。

这是本地哈希和审核声明机制，不是签名或不可改写存储。操作者能修改文件并重算全部哈希时，不能靠该机制认证其诚实性；预先固定的外部manifest哈希和审核来源仍是必要信任边界。

## Agent接入及旧入口

prospective的source改为 `{snapshot: 新前瞻快照目录, manifest_sha256: 固定SHA}`。旧的csv/calendar四字段输入不再能直接进入prospective；historical_replay仍保留原四字段并执行原strict路径。exploratory仍使用原processed快照及其独立策略。

本轮只测试离线契约，绝不读取授权或执行真实模型。原CLI仍是明确的Mock-only探索演示，不会因新增前瞻路径而自动变成真实推理入口。
