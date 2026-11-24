# 0006 技术栈：Python 3.12 和 uv

- 状态：已接受
- 日期：2026-11-24
- 提出：Claude，参考所有者转述的另一个 AI 的建议（Linux、Python）

## 背景

第二代一套是 TypeScript（myquant），一套是 Python（finance-research），精力被分散。量化研究的工具（数值计算、统计、数据处理）以 Python 为主；生产环境（GitHub Actions、Cloud Run）是 Linux。

## 决定

1. 只用 Python 3.12，运行环境是 Linux；所有者本机是 macOS，同样能跑。
2. 用 uv 管理依赖和锁文件；ruff 做格式和静态检查；pytest 做测试。
3. G0 没有运行时依赖。之后每新增一个依赖，都要在 PR 里写理由。

## 后果

- myquant 的 TypeScript 代码不能直接复用，只参考思路。
- 依赖少，换智能体、换机器时更不容易出问题。
