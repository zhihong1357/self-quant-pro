# 假说卡

每个假说一个文件：`H-NNNN.toml`，格式见 [模板](../../docs/templates/hypothesis.toml)。

- 草稿（`status = "draft"`）可以随便改。
- 注册之后（`registered` 及以后），除了 `status` 和 `status_history`，其余内容全部冻结。
- 被否定的假说不删除，改成 `rejected` 或 `retired`。它们是试验计数的一部分，也防止重复劳动。

规则由 `sqp check` 执行，见 [科研规程](../../docs/protocol.md) 第 3 节。
