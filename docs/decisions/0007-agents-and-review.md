# 0007 智能体分工与审查

- 状态：已接受
- 日期：2026-11-24
- 提出：Claude

## 背景

所有者不写代码，所有开发由 AI 智能体完成。Claude Pro 的订阅不到 20 天就到期，之后主力换成 Google 的 Antigravity。不同工具读不同的说明文件，规则一旦分散就会互相矛盾。

## 决定

1. AGENTS.md 是所有智能体的唯一规则源。Claude Code 通过 CLAUDE.md 引用它；Antigravity、Jules、Codex 直接读它。不建其他工具专用的规则文件。
2. 当前主力是 Claude Code；订阅到期后由 Antigravity 接手；Jules 做异步的小任务。
3. 提出者不审自己：写代码的模型不能当这份代码的审查者，优先用另一家的模型审查。
4. 所有者是唯一的合并者。合并进 `main` 即视为所有者批准。
5. Gemini API 只做研究辅助，不当闸门。
6. 每次会话结束都要更新 `docs/agent-handoff.md`。

## 后果

- 所有者是否能用 Codex 还没确认。AGENTS.md 写成通用格式，不依赖任何一个工具。
- 换工具时不需要迁移规则，只需要让新工具先读 AGENTS.md 和交接记录。
