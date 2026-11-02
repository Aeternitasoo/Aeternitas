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
