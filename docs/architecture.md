# 架构

## 1. 分层

```
数据源
  └─ 原始层：只增不改，记录来源、校验和、下载时间
       └─ 时点层：按"何时可知"查询；时钟上限；金库守卫（拒绝写进 refusals 账本）
            └─ 研究运行框架：一次试验 = 一条账本记录 + 一份标准报告
                 └─ 信号 ──► 统一模型（信号合成 + 风险模型）
                                └─ 成本与容量：资金规模 → 净收益曲线 → 最佳资金区间
                                     └─ 组合与仓位：不爆仓约束
                                          └─ 执行（G7 起）：下单、对账、熔断
                                               └─ 监控：日报、告警、前向账本

治理层（贯穿全部）：注册表、三本账、sqp check、持续集成的两道闸
```

G0 只有治理层。其余各层按[路线图](roadmap.md)逐个阶段加入，每层都有自己的验收条件。

## 2. 目录

现在（G0）：

| 路径 | 内容 |
|---|---|
| `AGENTS.md`、`CLAUDE.md` | 智能体规则 |
| `STAGE` | 当前阶段 |
| `config/` | 治理、科研规程、继承规则的配置（冻结） |
| `docs/` | 宪章、规程、架构、路线图、决策记录等 |
| `registry/` | 继承记录、研究宇宙、假说卡、三本账 |
| `src/sqp/` | 治理检查与研究工具 |
| `tests/` | 测试，含 G0 验收测试 `test_acceptance.py` |
| `.github/` | 持续集成、PR 模板 |

计划：`src/sqp/data/`（G1）、`src/sqp/research/`（G2）、`src/sqp/model/` 与 `src/sqp/capacity/`（G4）、`deploy/`（G5）、`src/sqp/execution/`（G7）。

## 3. 环境分工

| 环境 | 用来做什么 | 费用 |
|---|---|---|
| 所有者的 Mac mini | 智能体工作、本地检查 | 0 |
| GitHub Actions | 持续集成；G1–G4 的定时数据采集和研究运行 | 公开仓库的标准运行器免费 |
| Google Colab | 交互式探索、较重的一次性计算 | AI Pro 附带的额度，以 Colab 页面显示为准 |
| Google AI Studio | 调试提示词；Gemini API 只做研究辅助 | 免费档 |
| 谷歌云 | G1 起：原始数据存储；G5 起：已验收的生产任务 | 每月上限 10 美元 |

## 4. 谷歌云的布局（存储 G1 起，计算 G5 起）

| 需要 | 服务 | 要点 |
|---|---|---|
| 定时运行 | Cloud Scheduler 触发 Cloud Run 作业 | 跑完即停，不常驻 |
| 存储 | Cloud Storage | G1 起；存储桶在 us-central1（免费额度区域）；采集账号只能创建对象，存储层面只增不改 |
| 分析 | BigQuery | 按扫描量计费，每个查询设扫描上限 |
| 密钥 | Secret Manager | 代码只引用名字，不碰明文 |
| 镜像 | Artifact Registry | 设清理策略，只留最近几个版本 |
| 身份 | Workload Identity Federation | GitHub 到谷歌云不发长期 JSON 密钥 |
| 区域 | 存储 us-central1；生产计算 G5 再定 | 要考虑 Binance 主接口拒绝美国 IP，以及跨区流量费（决策 0010） |

G7 之前不开常驻虚拟机。部署流程：PR 合并 → 持续集成通过 → GitHub 的 `production` 环境等所有者点批准 → 部署。费用规则见[成本护栏](cost-guardrails.md)。

## 5. 治理检查（`sqp check`）

配置在 `config/governance.toml`（冻结）。每条违规的输出格式是 `[规则] 文件：说明`。

| 规则 | 检查什么 | 代码 |
|---|---|---|
| `file-size` | 单个文件不超过 1 MB | `checks/files.py` |
| `data-file` | 数据格式文件只能放在 `tests/fixtures/` | `checks/files.py` |
| `secret-file` | `.env`、私钥、服务账号文件等不能提交 | `checks/files.py` |
| `secret-pattern` | 文件内容里像密钥的字符串 | `checks/secrets.py` |
| `secret-history` | PR 里每一次提交加入过的像密钥的内容 | `checks/secrets.py` |
| `order-code` | G7 之前的下单相关代码 | `checks/code.py` |
| `never-code` | 任何阶段的提币和资金划转代码 | `checks/code.py` |
| `stage-value` | `STAGE` 的取值 | `checks/registry.py` |
| `universe-schema` | 研究宇宙的字段与分段连续性 | `checks/registry.py` |
| `universe-exposure` | 前代看过的数据有没有声明 | `checks/registry.py` |
| `hypothesis-schema` | 假说卡的字段；阈值只能更严 | `checks/registry.py` |
| `ledger-chain` | 账本的哈希链 | `checks/registry.py` |
| `derived-results` | `registry/` 下的 JSONL 记录不带数值结果字段 | `checks/registry.py` |
| `decision-format`、`decision-number` | 决策记录的格式与编号 | `checks/decisions.py` |
| `no-delete` | 不能删除的文件 | `checks/history.py` |
| `ledger-append-only` | 账本只能追加 | `checks/history.py` |
| `frozen-file` | 冻结文件的改动要附决策记录 | `checks/history.py` |
| `decision-frozen` | 已合并的决策只能改状态 | `checks/history.py` |
| `hypothesis-frozen` | 注册后的假说卡冻结 | `checks/history.py` |
| `stage-gate` | 阶段一步一步走，并附决策记录 | `checks/history.py` |

`secret-history` 和 `checks/history.py` 里的六条要和基准比对，只在给了 `--base` 时运行；持续集成总是会给。

## 6. 持续集成的两道闸

| 工作流 | 什么时候跑 | 跑谁的代码 | 作用 |
|---|---|---|---|
| `ci`（任务 `tests`） | 每个 PR、每次推到 `main` | PR 自己的 | 格式、静态检查、测试、治理检查 |
| `gate`（任务 `rules`） | 每个 PR | `main` 上的检查器，检查 PR 的内容 | PR 改不了闸门；不执行 PR 里的代码，拿不到任何密钥 |
| `inheritance` | 每周一次、手动、改到继承记录时 | — | 按固定提交重新生成继承记录，逐字节比对 |

为什么要第二道：`ci` 跑的是 PR 自己的代码，一个 PR 理论上可以同时削弱检查器和违反规则。`gate` 用的是 `main` 上已经合并的检查器，PR 碰不到它。第一个 PR（G0）合并之前 `main` 上还没有 `gate`，所以只有 `ci` 一道；之后的每个 PR 都有两道。

**密钥扫描的分工**：GitHub 自带的推送保护在推送那一刻拦下已知格式的密钥（公开仓库免费，要在设置里确认打开）；`sqp check` 再扫当前文件和 PR 里的每一次提交，并且在 `gate` 里由 `main` 的版本执行。没有用第三方扫描器：它们的规则文件和行内豁免注释可以被 PR 自己改掉。

## 7. 语言模型在系统里的位置

Gemini API（AI Studio 的密钥）只做研究辅助：读文献、找反例、起草代码。结论、阈值、准入、交易决定，一律由确定性代码给出。
