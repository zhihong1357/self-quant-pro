# 彩排：在合成数据上把试验从头跑一遍

真实数据上每张试验卡只跑一次，跑坏了也算一次试验。所以改了引擎或研究代码之后，先在合成数据上用真正的运行框架跑一遍。合成数据是自己造的，数字可以看、可以公开（决策 0011 只管由第三方数据算出的数）。

这样已经抓到过两个问题（2026-11-26）：
- 阳性对照遇到中途下架的合约会误判；
- 候选 A 每 8 小时重排一次，一年换手约 700 倍。

## 步骤（在本机，不碰谷歌云）

1. 造数据（约 40 秒，约 450 MB）：
   ```
   uv run python scripts/rehearsal/make_lake.py /tmp/rehearse/lake
   ```
2. 把仓库克隆到临时目录，在那里追加要彩排的试验卡（`uv run sqp ledger append <副本>/registry/ledger/trials.jsonl '<JSON>'`），提交，再把这个副本的 main 指到它：
   ```
   git -C <副本> update-ref refs/remotes/origin/main HEAD
   ```
   这一步只改临时副本，不碰真正的仓库。
3. 跑：
   ```
   GITHUB_ACTIONS=true GITHUB_REF=refs/heads/main GITHUB_EVENT_NAME=workflow_dispatch \
     uv run sqp trial run --repo <副本> --local /tmp/rehearse/lake
   ```
   结果在 `/tmp/rehearse/lake/derived/trials/<编号>/`。

再跑一遍之前，删掉存储里的 `derived/` 和 `ledger/`：标记已经写过的试验不会再跑。
