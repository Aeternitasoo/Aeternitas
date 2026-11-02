# Aeternitas Core：实际架构

本页描述当前实现；AGENTS.md 是唯一长期定位，Progress 保存完整执行史。

| 核心 | 代码 | 边界 |
|---|---|---|
| Research Engine | research_workflow / quality / intelligence / inputs / report；financial_facts、filing_source、news_source、sources.sec | 配置化研究、冻结来源、质量策略、HTML/JSON；完整性与语义不自动认证 |
| Harness & Skills | agent_harness、harness_contracts、agent_tools、agent_skills；agent_skills/*/SKILL.md | 注册工具、权限、JSON契约、预算、按需加载哈希；非恶意代码沙箱 |
| Memory | research_memory；requirements-memory.lock | 真实 Mem0 2.2.1、本地Qdrant/SDK历史；四类来源绑定、幂等、版本/冲突/过期；词法检索 |
| Observability | research_observability、langfuse_adapter、research_analysis.FailureDiagnostics | 本地父子轨迹、Generation、实际用量/延迟、安全失败快照；云端Requires Credentials |
| RSI | research_rsi、research_validation、infrastructure_rsi、candidate_registry | 文件/Trace/Evaluation分析、模板提案、隔离候选、比较、人审晋升/回滚；无通用自改 |
| Evaluation | lab_evaluation、research_quality、research_intelligence、test_* | 固定输入、独立分组、结构/来源/财务/执行检查；语义需独立复核 |

## 执行和质量屏障

Core CLI → 配置及显式在线授权 → 原采集器或FrozenInputs → audit_inputs → organize / QualityBarrier.prepare → 原 ResponsesAnalyst → QualityBarrier.challenge / 结构引用校验 → 报告保存 → 完整性提交。

行业研究不要求行情。新闻是供应商摘要，财务主体和期间由契约约束，客户合同收入不等于合并总收入。Source Completeness 区分来源不存在、未提取、未传入和未使用。字面匹配不等于支持对应论点；自动检索与人工审核仍存在缺口。

Recorder 与可选遥测独立，导出异常不改变研究结果；必要本地审计持久化失败须阻止提交。失败诊断追加 diagnostics/event 文件，HTTP成功不等于报告提交成功。

## 版本整合

本轮整合最新隔离运行时中已测试的记忆去重、主程序范围选择、utility状态和失败诊断，排除实验 ablation 依赖及个人历史绝对路径。通用候选构建函数接受开发记录路径、父版本及受限源文件清单，不执行候选Python。

研究 Prompt 未因整合自动晋升；旧生产基线和历史候选保留在原项目外Registry。Evaluation／人工晋升与 Git 源码整理是不同操作。

## 结构与保留项

模块平铺，内部通过相邻源码、Prompt和配置计算SHA，也存在循环导入。保持源码 editable 安装，避免 import shim、重复包或无法复核的批量搬迁。独立 wheel 资源布局另行验收。

A0 / Time Machine、market_data、yahoo_csv 为共享工具。旧四角色公司研究、证据库、API/worker、迁移和维护仍由兼容入口/审计复用，保留原行为；数据库和容器未在本轮真实复验。

旧价格 Agent、批量预测、预测专用prospective入口、教学程序、独立前端、一次性对照及旧tar发布器移出当前树。完整备份与旧Git提交保留，逐文件见 CORE_FILE_INVENTORY；不确认用途的历史文档留存，但不驱动当前开发计划。

## 环境和数据边界

- Core：Python3.13.18，requirements.lock固定兼容环境，pyproject区分core/test/server/observability。
- Mem0：独立3.13，protobuf6.33.6；主环境protobuf7.36.2。不合并或迁移数据库到pgvector。
- 本地SDK不作LLM推断，词法哈希不产生云端Embedding费用。
- .env、OAuth、审批密钥、记忆库、DB、真实来源和未脱敏模型输入输出留在仓库外/忽略目录。
- A0 / Time Machine拒绝条件不变；抓取时间不冒充历史可用时间，日期过滤不等于严格回放。
- Langfuse 无凭据保持 Requires Credentials，不用 Mock 证明远程集成。
