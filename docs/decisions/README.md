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
| [0019](0019-monthly-research-tables.md) | 把原始归档整理成研究用的月度表，每行带"何时可知"；只增不改的分块 |
| [0020](0020-point-in-time-interface.md) | 时点接口：只能读到当时已经知道的数据；确认集要注册，金库要批准，都以合并为准 |
| [0021](0021-exploration-profile.md) | 探索集画像：只描述、只读探索集、一条命令能重算，结果只放私有存储桶 |
| [0022](0022-pit-hardening-after-review.md) | Gemini 独立审查后加固时点接口；写明信任边界：硬边界是存储桶的凭据 |
| [0023](0023-g1-accepted.md) | G1 验收，STAGE 进入 G2；三项未完成的交付移到 G2 |
| [0024](0024-import-gen1-recordings.md) | 每天导入第一代的录制数据；只读令牌放在只许 main 使用的环境里 |
| [0025](0025-trials-registered-before-they-run.md) | 试验先在仓库登记、所有者合并后才能跑；每张试验卡只跑一次 |
| [0026](0026-controls-first-stricter-selection.md) | 选第一个确认检验之前先做阳性对照；选择规则收紧；成本按吃单；尽早开始前向计时 |
| [0027](0027-no-api-recording-forward-from-archives.md) | 不从 Binance 交易接口录数据；前向数据先用公开归档的月度文件；实盘的交易场所和数据到 G7 之前一起定 |
| [0028](0028-prepare-second-universe.md) | 开始准备第二个研究宇宙：历史长、所有者能合法交易、换手低；首选美国上市的 ETF |
