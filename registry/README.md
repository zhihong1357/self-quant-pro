# 注册表

系统的记忆。这里的东西只增不改，由 `sqp check` 和持续集成强制执行。

| 目录 | 内容 | 格式 | 规则 |
|---|---|---|---|
| `inherited/` | 从前两代继承的试验记录与文件指纹 | 机器生成（JSON、原样复制的 JSONL） | 冻结；只能由 `sqp inherit scan` 生成，`sqp inherit verify` 核对 |
| `universes/` | 研究宇宙：品种范围、分段边界、前代暴露 | 人写（TOML） | 冻结；新宇宙可以新增 |
| `hypotheses/` | 假说卡 | 人写（TOML） | 注册后冻结；不能删除 |
| `ledger/` | 试验、拒绝、批准三本账 | 机器写（JSONL，哈希链） | 只能在末尾追加 |

约定：人写的用 TOML，机器生成的用 JSON 或 JSONL。

## 三本账

| 文件 | 记什么 | 谁写 |
|---|---|---|
| `ledger/trials.jsonl` | 每一次在真实数据上输出了绩效或显著性指标的计算 | G2 起由运行框架自动写 |
| `ledger/refusals.jsonl` | 被时点接口拒绝的请求（越过时钟上限、没批准就读金库） | G1 起由数据层自动写 |
| `ledger/approvals.jsonl` | 所有者的批准：开金库、进模拟盘、进实盘 | 所有者让智能体追加，所有者合并 PR 才生效 |

## 账本格式

每行一个规范 JSON 对象（键排序、无空格、UTF-8），带这几个自动字段：

| 字段 | 含义 |
|---|---|
| `seq` | 序号，从 1 开始 |
| `prev` | 上一条的 `hash`；第一条是 64 个 0 |
| `at` | UTC 时间 |
| `hash` | 除 `hash` 之外整条记录的 SHA-256 |

追加：`uv run sqp ledger append registry/ledger/approvals.jsonl '{"kind": "vault_open", "hypothesis": "H-0003"}'`

写错了不能改旧行，只能追加一条更正记录，引用要更正的 `seq`。
