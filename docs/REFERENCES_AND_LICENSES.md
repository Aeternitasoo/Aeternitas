# 来源、归属与分发边界

Aeternitas为AI辅助工程项目。设计曾参考TradingAgents、ai-hedge-fund和FinRobot的职责划分；参考不等于复制或独立发明。

- TradingAgents：https://github.com/TauricResearch/TradingAgents （上游许可须按实际采用版本核实）
- ai-hedge-fund：https://github.com/virattt/ai-hedge-fund
- FinRobot：https://github.com/AI4Finance-Foundation/FinRobot

本候选没有vendor依赖源码或wheel。依赖由固定requirements安装，分别保留其自身许可。
没有完成逐行相似度或所有权法律认证；所有者仍须确认不存在雇主/客户限制和未授权复制。

SEC事实、公司申报、Finnhub/Yahoo等供应商资料和模型报告不能因为程序能够获取就被默认获准再分发。
本公开版本含代码、文档、合成测试输入、已脱敏的故障形状，以及下列明确标为公开SEC派生数据的数值子集；不把真实数字重新标成合成数据。
- tests/fixtures/m2_sec_annual_v1.json：SEC companyfacts年度数值子集及原始来源标识。
- evals/m6/development/D01.json：同一公开来源的数值回归案例，保留derived_public_snapshot标记和来源。
SEC官方FAQ说明政府制作内容及EDGAR公开申报内容可免费访问及复用（核查：2026-11-02）：
https://www.sec.gov/about/webmaster-frequently-asked-questions
这不包括该页列举的图片等例外，也不作为其他供应商资料的授权。
私人实验原件、完整模型输入输出、真实研究报告、新闻缓存及授权不明的第三方材料留在私有归档，不随此版本分发。
旧Git历史、PR及Release已隔离在不同repository ID的Private归档；本公开仓库从无父提交快照建立，不复制这些内容。

项目许可决定见 [NOTICE](../NOTICE.md)。本文件不替所有者自动选择开源许可证。
