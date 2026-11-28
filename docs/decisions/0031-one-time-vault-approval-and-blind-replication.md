# 0031 一次性金库批准绑定单张试验卡；已用掉的假说族经“四联锁”通道做盲复现

- 状态：已接受
- 日期：2026-11-27
- 提出：Antigravity（吸收并核实外部初稿后定稿）。合并即批准

## 背景

[代际传承](../lineage.md)第 4 节（迁移验收 A）和[决策 0026](0026-controls-first-stricter-selection.md)第 7 条要求：在第三代框架里，用 Binance U 本位永续合约 1 小时 K 线（`um-klines-1h`）重做第二代 `myquant`（固定提交 `feb6a07`）的 `btc-eth-001` 研究（BTC/ETH/SOL 跨资产传导，原结论为否定）。所有者 2026-11-25 决定它必须**盲复现**：只输出“和第二代是否一致、差几个标准误”等核对信息，不输出确认集、金库、前向时段的任何其他数字；并且金库批准只对指定的那一张试验卡有效，用完即作废。

把这件事落进现有框架时，有四个冲突要一次性解决：

1. **现有金库批准可以复用**：`src/sqp/data/pit.py` 的金库检查原来只看批准账本里有没有匹配的 `(universe, hypothesis)`，既没有绑定单张试验卡，也没有在存储桶里写不可覆盖的已消费标记。同一条批准会被后续试验或 GitHub 重跑反复使用。
2. **`btc-eth-001` 横跨四段**：它的窗口（日线开盘 2023-01-01 16:00 UTC 至 2026-11-16 16:00 UTC；小时线 2026-08-20 00:00 UTC 至 2026-11-18 00:00 UTC）跨过 `U1` 的探索、确认、金库和前向四段。现有运行框架每张试验卡只放行单个受保护段，而 `crypto.cross-asset-transmission` 已被标记为前代用掉的族（`U1.toml` 的 `families_burned`，截至 `2026-11-19`）。
3. **盲复现既要读对照值，又不能破坏研究边界**：按[决策 0011](0011-derived-results-stay-private.md)，本仓库的 `registry/inherited/myquant/research/btc-eth-001/trials.jsonl` 剥离了数值结果，只留 6 个元数据字段；但 `registry/inherited/manifest.json` 已经固定了上游 `myquant@feb6a07` 完整文件的提交号、字节数和 SHA-256。同时 `research-boundary` 严禁研究代码读文件或联网，且现有运行框架会把研究代码的控制台输出存进 `console.log`，如果盲复现代码不禁用它，可能把数字写进存储桶。
4. **统计口径要对齐第二代实际跑出的代码**：核对 `myquant` 历史提交 `e992b10`（实际运行 `btc-eth-001` 的 Python 代码）确认：OKX 日线以 **16:00 UTC 为开盘锚点**（不是收盘锚点）；全部控制变量均已滞后一期（无同期泄露）；日频 `BTC→SOL` 控制滞后 `ETH`；Newey–West HAC 使用固定滞后（日频 5、小时频 12）、不乘小样本自由度因子、p 值用双侧正态分布；分块自助法用于第 6 步扣费成本闸的净日收益均值检验，总体结论由 4 条预注册准则共同决定。

## 选项

| 选项 | 做法 | 优点 | 最坏会怎样 |
|---|---|---|---|
| A：放宽普通试验卡支持多段读取 | 把 `segment` 改成列表，靠人工审查防泄密 | 改动少 | 破坏单段隔离不变量；普通新假说可能误配或借此偷读确认集与金库 |
| B：写仓库外独立脚本直接读桶比对 | 绕过 `PointInTime` 和 `run.py` 算完打印“一致” | 不改主框架 | 绕过了待验收的第三代框架本身，起不到迁移验收作用，且无一次性锁 |
| **C：一次性金库批准 + “四联锁”盲复现通道（采纳）** | 全系统升级金库批准为单卡一次性消费；新增受四把锁限制的 `mode = "blind_replication"` 通道；比对与盲输出留在框架层 | 不改任何冻结文件；不削弱 `research-boundary`；永久堵住金库批准复用漏洞；端到端验证第三代真实链路 | 一旦消耗批准后研究代码出错，该批准永久作废，需所有者追加新批准和新试验卡才能重跑（通过前置分区就绪检查与合成彩排把风险降到最低） |

## 决定

1. **全系统升级为一次性、单卡绑定的金库批准（落实决策 0026 第 7 条）**：
   - `registry/ledger/approvals.jsonl` 中每条 `type == "vault"` 的记录必须写明 `universe`、`hypothesis` 和 `trial`（精确对应一张试验卡编号，例如 `"T-0007"`）。每份批准由账本自带的 `seq` 唯一标识。
   - 静态检查（`sqp check`）强制：同一张试验卡不能有两条金库批准；两张试验卡不能共用同一个批准 `seq`；试验卡的 `approval`、`universe`、`hypothesis`、卡号必须与批准记录一一对应。
   - 运行时强制（`src/sqp/data/pit.py` 与 `src/sqp/research/run.py`）：进入金库读取之前，通过只增不改存储的原子写入（谷歌云 `if_generation_match=0` / 本地硬链接）落盘 `ledger/approvals/<seq>.used.json`（含本次运行的一次性随机凭证 `nonce`），并把凭证 `VaultReceipt` 交给 `PointInTime`。`PointInTime` 每次读金库段都独立核对 `origin/main` 批准记录与存储桶中的消费标记及 `nonce`。重复运行或 GitHub Re-run 都会在写标记时立即被拒绝。
2. **引入“四联锁”已烧毁族盲复现通道（`mode = "blind_replication"`）**：
   - 普通试验卡行为完全不变（默认 `mode = "standard"`），并新增静态与运行时拦截：`families_burned` 中的族不能提交普通确认集/金库试验，`vault_burned` 中的族不能提交普通金库试验。
   - 试验卡声明 `mode = "blind_replication"` 时，`segment` 必须填 `"vault"`，禁止 `repeat_of`，且静态检查与运行时同时强制以下四把锁：
     1. **已烧毁族与时间上限锁**：假说卡所属 `family` 必须在目标宇宙（`origin/main`）的 `[exposure] families_burned` 列表中，且试验卡声明的 `max_knowable_time`（带 UTC 时区）不得晚于 `burned_through` 当日终（UTC）。
     2. **冻结上游基线锁**：试验卡必须写明 `inherited = {"source": ..., "path": ...}`，且该条目存在于 `origin/main` 的 `registry/inherited/manifest.json` 中（带固定提交号、字节数与 SHA-256）。
     3. **一次性金库批准锁**：批准记录必须额外精确匹配 `mode = "blind_replication"`、`study`、`max_knowable_time` 与 `inherited`，并在读数据前完成存储桶原子消费。
     4. **硬截断与盲输出锁**：`PointInTime` 仅在四锁齐备时放行跨段读取，并在 `_authorize()` 硬拦截任何 `end > max_knowable_time` 的查询。
3. **三层隔离的盲比对与零泄露保证**：
   - 研究代码（`src/sqp/research/studies/`）继续严格遵守 `research-boundary`，不能读文件、不能联网、不能导入框架层的比对模块 `sqp.research.blind`。新增脱敏返回容器 `BlindMetrics`（打印和转为字符串只返回 `<BlindMetrics redacted>`），盲复现模式强制要求研究代码返回 `BlindMetrics`（拒绝 `Outcome`），普通模式相反。
   - 盲复现模式下，`run.py` 捕获研究代码的控制台输出后**直接丢弃，不写入 `console.log`**；出错时沿用 `_error_name` 只记内置错误类名，绝不打印或落盘错误正文。
   - 框架层受审模块 `src/sqp/research/blind.py` 在消耗批准**之前**先做两项前置检查（均不读行情数据，失败不烧批准）：(a) 检查所需数据集月份与品种的 `.inputs.json` / `.parquet` / `.sha256.json` 是否齐备（防止 2026-11 月度归档未到齐时误烧批准）；(b) 依据 `manifest.json` 锁定的提交号与 SHA-256 获取上游 `myquant` 对照文件（先查私有存储缓存 `raw/gen2/...`，未命中则从 GitHub 固定提交拉取并逐字节核对字节数和 SHA-256，不把第三方派生数值放进本仓库）。
   - `blind.py` 在内存中完成比对后立即丢弃全部 Binance 原始估计量，仅输出严格白名单的盲 `result.json` 与 9 节标准 `report.md`（递归校验不含 `beta`、`se`、`t_statistic`、`p_value`、`r_squared`、`sharpe`、`net_daily_edge` 等违禁键）。公开日志只打印对象名、指纹和判定词（`一致` / `不一致`）。
4. **纯 Python 统计原语与写死的“一致 / 不一致”门槛（零新增依赖）**：
   - 在 `src/sqp/stats.py` 新增纯 Python 标准库实现的统计原语（不新增第三方依赖，符合规则 13）：Householder QR 最小二乘（`ols_fit`）、Bartlett 核 Newey–West HAC（`newey_west_hac`）、偏超前滞后检验（`partial_lead_lag`）、Holm–Bonferroni 校正（`holm_adjust`）、带清洗与禁运期的向前滚动切分与评估（`purged_walk_forward_splits`、`evaluate_walk_forward_forecasts`）、循环分块自助法均值检验（`circular_block_bootstrap_mean_test`）。注：自助法改用标准库 `random.Random(seed)`（非 numpy 生成器），因此盲比对只核对 5% 显著性定性结论是否一致，不要求随机数逐位相同。
   - 按[代际传承](../lineage.md)第 4 节（“结论一致，关键指标的差异在一个标准误以内”），在看到任何 Binance 结果前写死 `btc-eth-001` 的盲判定规则——总体判 `一致` 当且仅当以下四条全部满足：
     1. 总体结论 `supported`、全部 4 条预注册准则、以及 3 组 Holm 校正的拒绝结论，与第二代逐项一致；
     2. 3 组检验（`daily_btc_eth`、`daily_btc_sol`、`hourly_btc_eth`）的无符号系数差 `|β_Binance − β_OKX| / SE_OKX ≤ 1.0`（分母采用已冻结的第二代标准误，报告中仅显示保留 2 位小数的无符号 `delta_se`，不暴露 `SE_Binance` 与方向，防止反推原始系数）；
     3. 3 组检验的样本量偏差均满足 `|n_Binance − n_OKX| / n_OKX ≤ 5%`；
     4. 成本闸分块自助法在 5% 水平下的拒绝结论与第二代一致。
5. **分两步合入，不修改任何冻结文件**：
   - 因为持续集成的 `gate / rules` 始终用 `main` 上的检查器审查 PR，研究代码只能导入已合并进 `main` 的 `sqp.stats` 函数与 `sqp.research.studies` 符号。所以本决策随第一个 PR 先合入框架、检查、盲比对与 `sqp.stats` 纯 Python 统计原语；紧接着第二个 PR 合入 `src/sqp/research/studies/btc_eth_001.py` 和合成数据端到端彩排；等 2026-12 月初 Binance 发布 2026-11 月度归档并同步进存储桶后，再开只追加假说卡 `H-0010`、一次性批准与试验卡的小 PR 正式运行。
   - 试验计数按保守原则照常计入 `tests = 3`，不豁免。

## 后果

- 不修改任何冻结文件（`U1.toml`、`config/`、`registry/inherited/`、`.github/workflows/` 全部保持不动），不引入新的第三方依赖。
- 未来任何真实策略若获批读金库，自动受单卡绑定与存储桶一次性消费锁保护，杜绝一证多用或重跑复用。
- 若未来需要盲复现其他已烧毁族的前代研究，可复用同一套四联锁通道，只需在 `sqp.research.blind` 注册对应的盲比对器。
- 若某次盲复现在消耗批准后因代码缺陷失败，该批准永久作废，须修复后追加新的批准和试验卡重跑。
