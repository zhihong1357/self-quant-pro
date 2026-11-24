# 所有者简报

给所有者看的。不需要会写代码。

## 1. 你的角色

| 你做 | 你不做 |
|---|---|
| 拍板：读决策记录，同意了就合并 | 调试代码 |
| 合并 PR（你是唯一能合并的人） | 在谷歌云上试跑任何东西 |
| 在网页上做少量设置（每次都会有逐步说明） | 把任何密钥贴进聊天、PR 或文件 |
| 每月看一次谷歌云账单（G5 起） | 给智能体交易所密钥 |

## 2. 每个 PR 怎么看（约 10 分钟）

1. **看检查结果**：PR 页面下方的检查全部是绿色对勾。有红叉就不合并，把红叉的说明复制给智能体。
2. **读"改了什么"**：PR 描述的第一段应当是你能看懂的中文。看不懂就让智能体重写，这是它的责任。
3. **读新增的决策记录**：文件列表里 `docs/decisions/` 下的新文件。这是你真正在拍板的东西。
4. **看"所有者需要做什么"**：按里面的步骤做。
5. **合并**：点 "Merge pull request"，选 "Create a merge commit"。

## 3. 第一次合并之后要做的设置（约 10 分钟，只做一次）

顺序很重要：**先合并第一个 PR，再做这些设置**。否则第一个 PR 会因为等不到 `gate` 检查而无法合并。

**A. 只允许合并提交**
1. 打开仓库 → 上方 **Settings** → 左边 **General**。
2. 往下找到 **Pull Requests**。
3. 勾选 **Allow merge commits**；取消勾选 **Allow squash merging** 和 **Allow rebase merging**。
4. 勾选 **Automatically delete head branches**。

**B. 保护 main 分支**
1. **Settings** → 左边 **Rules** → **Rulesets** → **New ruleset** → **New branch ruleset**。
2. **Ruleset name** 填 `main`；**Enforcement status** 选 **Active**。
3. **Target branches** → **Add target** → **Include default branch**。
4. 勾选：**Restrict deletions**、**Block force pushes**。
5. 勾选 **Require a pull request before merging**（"Required approvals" 保持 0：你不能批准自己的 PR，合并本身就是你的批准）。
6. 勾选 **Require status checks to pass**，再勾选 **Require branches to be up to date before merging**；点 **Add checks**，搜索并加入 `tests` 和 `rules`。
7. 点 **Create**。

**C. 打开推送保护**
1. **Settings** → 左边 **Advanced Security**（有的界面叫 **Code security**）。
2. 找到 **Secret Protection**，打开；再打开下面的 **Push protection**。公开仓库免费。
3. 打开之后，任何人推送已知格式的密钥都会在推送那一刻被拦下。

界面的按钮名称可能和这里略有不同。找不到就截图给智能体。

## 4. 永远不要做的事

- 把 API 密钥、交易所密钥、密码贴进聊天、PR、Issue 或任何文件。
- 在 `Settings` 里关掉上面的保护来"让 PR 先合进去"。检查不过，就让智能体修。
- 跳过决策记录直接让智能体改 `STAGE`。

## 5. 接下来要你决定的事

| 什么时候 | 决定什么 |
|---|---|
| 现在 | 读决策 0001–0009，合并 G0 的 PR，做第 3 节的设置 |
| 约 2026-12-08 | finance-research 怎么办：G1 接管它的数据代码，还是从头写 |
| G1 开头 | 原始数据放哪里：Hugging Face 私有数据集，还是谷歌云存储 |
| G4 结束 | 仓库继续公开还是转私有 |
| G5 开始 | 开通谷歌云（会有逐步说明） |
| G6 开始前 | 你能承受的最大亏损：写成一个具体数字 |
| G7 | 是否实盘、用多少钱起步 |

## 6. 怎么跟智能体说话

- 开新会话时说："先读 AGENTS.md 和 docs/agent-handoff.md，然后告诉我现在在哪个阶段、下一步是什么。"
- 让它审查别人的工作时说："你是审查者，不是作者。找出致命问题，按严重程度排序。"
- 它说"完成了"时问："检查全过了吗？交接记录更新了吗？"
