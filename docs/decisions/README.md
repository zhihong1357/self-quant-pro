# 决策记录

每份决策一个文件，编号连续，格式见 [模板](../templates/decision.md)。合并进 `main` 即视为所有者批准；之后只能改"状态"一行。

| 编号 | 决定 |
|---|---|
| [0001](0001-generational-rebuild.md) | 新仓库建第三代，只继承三样不可再生的东西 |
| [0002](0002-public-repo-no-license.md) | 仓库暂时公开，不附开源许可 |
| [0003](0003-environments-and-cloud-cap.md) | 环境分工，谷歌云每月 10 美元封顶 |
| [0004](0004-research-scope-and-u1.md) | 研究范围多领域开放，U1 沿用第二代的边界 |
| [0005](0005-live-trading-gate.md) | 实盘闸门 |
| [0006](0006-python-uv.md) | 技术栈：Python 3.12 和 uv |
| [0007](0007-agents-and-review.md) | 智能体分工与审查 |
| [0008](0008-cross-generation-accounting.md) | 跨代记账与数据暴露 |
| [0009](0009-one-model-capital-range.md) | 一个统一模型，输出最佳资金区间 |
| [0010](0010-owner-decisions-2026-11-24.md) | 所有者的四项决定：数据放谷歌云存储、仓库继续公开、风险上限由使用者设定、起步资金暂定 1,000 美元 |
| [0011](0011-derived-results-stay-private.md) | 由第三方数据算出的数值不进公开仓库；Binance Vision 数据不能直接用于实盘 |
| [0012](0012-no-self-collection-on-critical-path.md) | 研究的关键路径不依赖自己采集的数据；目前不做实盘 |
| [0013](0013-u1-exposure-by-evidence.md) | U1 的暴露声明按实际看过的数据修正：资金费率族的确认集可用 |
| [0014](0014-published-results-count-as-exposure.md) | 已发表的结果也算暴露：大币期现 carry 不花确认集 |
| [0015](0015-g1-starts-now.md) | G1 现在开始，不等 finance-research；谷歌云只开存储，身份只能新建和读取 |
| [0016](0016-model-knowledge-counts-as-exposure.md) | 预训练语言模型见过的数据也算暴露：语言模型特征在 U1 上只能前向检验 |
| [0017](0017-storage-config-and-env-check.md) | 存储配置写进仓库；新增手动触发的环境自检工作流 |
| [0018](0018-mirror-binance-vision.md) | Binance 公开归档原样存进存储桶，逐个校验；每周的数据同步工作流 |
