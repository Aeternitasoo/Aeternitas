# Aeternitas Core：工程进度

本文件是唯一进度入口。长期项目定位以 [AGENTS.md](AGENTS.md) 为准。

## 当前架构
Research Workflow、Harness & Skills、Memory、Observability、受控RSI、Evaluation。
SEC、新闻、财务、证据、HTML/JSON、A0/Time Machine及公司研究兼容链路保留。

## 已有验证
- Python3.13.18源码editable安装、固定依赖及独立Mem0环境可运行。
- 原Core远程CI：723通过、70跳过；跳过项未算作数据库或生产验证。
- 本地Mem0四类持久化、去重、来源/时间/版本约束和检索已验证，当前为词法哈希。
- Harness、工具权限、预算、按需Skills、Local Recorder和隔离候选机制已有离线验证。

## 能力边界
Langfuse云端Requires Credentials。记忆改善研究质量、独立语义质量和通用自主代码改进尚未验证。
报告仍需独立语义审核，生产候选晋升须人工批准，历史准入规则不放宽。

## 公开版本
已准备229文件的干净单根版本，个人协作记录、私有实验和旧PR/Release历史不随本仓库分发。
旧仓库保留Private且不作为第二个公开项目维护。
SEC数值fixture保留来源与官方复用依据；没有将真实数字改标为合成数据。
本版本须由新仓库CI重新执行回归和离线SDK冒烟；不能把旧提交的CI伪装为新提交已验收。
当前工程问题仍包括来源语义检索和独立研究质量评估；公开发布不代表解决这些问题。

## 2026-11-02 — V2 Stage 1: bounded Langfuse remote acceptance

- Canonical main d62d5a3 synchronized; development isolated on codex/v2-langfuse-validation. No change to visibility, license, production Harness, Memory, RSI candidates or old experimental data.
- **Implemented:** existing infrastructure smoke now uses production Recorder/sanitize/Mapper and locked SDK 5.0.4. One synthetic root, Generation and failed tool are independently checked for remote IDs/parents/types, versions/fingerprints, usage fixture, elapsed time and ERROR state. Flush alone cannot pass. At most ten bounded read queries and a 45-second child lifetime; local append-only records survive remote failure. No complete research IO exported.
- **Tested:** 68 targeted tests passed, 0 failed, 0 skipped (52.98s), including 14 new acceptance tests and existing real SDK in-memory / bounded exporter / Workflow regressions. Initial 66-test run also passed before the final missing-credential and SDK-signature tests.
- **Validated locally:** explicit smoke with existing local configuration returned Requires Credentials, remote_delivery_verified=false and model_requests=0. No Langfuse export was attempted. Evidence under ~/.local/share/aeternitas-lab/v2-langfuse/: tests-first.txt, tests-final.txt and smoke/039863a323e14f4ba0791e2e5dd7e684/langfuse-status.json; ~ denotes the invoking user's home, not a distributable fixture.
- **Requires Credentials / Not Yet Validated:** LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and actual regional/self-host LANGFUSE_BASE_URL are absent. Actual cloud ingestion, remote readback and console inspection have not occurred. Synthetic usage 7/3/10 tests telemetry fields only; actual model tokens/currency cost are null, no model inference took place.
- Official SDK/API references and installed signatures checked; dependencies unchanged. See [stage procedure](docs/V2_LANGFUSE_VALIDATION.md). Remote CI is reported by the feature PR checks; prior main CI is not claimed as this change's result.
- Next: review this stage, configure credentials privately and perform the bounded remote smoke/readback. Mem0 semantic retrieval, ablation, RSI upgrades and integrated V2 remain later stages, not completed by this patch. No automatic merge or production promotion.
