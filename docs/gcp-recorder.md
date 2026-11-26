# 开通 Binance 录制任务（谷歌云东京区）

一次性的设置，你要操作约 10 分钟；第一次录制会在后台再跑约 30 分钟。依据是[决策 0027](decisions/0027-api-recording-in-tokyo.md) 和 [0029](decisions/0029-recorder-30-minute-rows.md)。

做完以后，每天 UTC 00:17 会有一个小任务在谷歌云的东京区自动运行：从 Binance 的公开行情接口录合约列表、资金费率、30 分钟 K 线和每 30 分钟的持仓量，原样存进你的私有存储桶。它不用 Binance 账号，也不能下单。

界面上的按钮名称可能和这里略有出入。卡住了就截图发给智能体。

## 你需要准备

- G1 时设置好的谷歌云项目 `self-quant-pro`（[开通谷歌云](gcp-setup.md)）。
- 每月 10 美元的谷歌云赠金已经激活。预计每月花费约 0 美元：Cloud Run 和 Cloud Scheduler 的免费额度之内（推测，置信度中）。

## 第 1 步：在 Cloud Shell 里运行一行命令（约 5 分钟）

1. 打开 shell.cloud.google.com/?show=terminal。如果弹出授权窗口，点 **Authorize**。
2. 复制下面这一整行，粘贴进终端，按回车：

   ```
   curl -fsSLo sqp-recorder.sh https://raw.githubusercontent.com/zhihong1357/self-quant-pro/main/scripts/gcp/setup-recorder.sh && bash sqp-recorder.sh self-quant-pro
   ```

   如果显示 `curl: (22) ... 404`，通常是这份说明所在的 PR 还没合并：先合并，再运行。
3. 屏幕上会问"确认无误请输入 yes 回车："。**看到这句话才输入** `yes`。
4. 中途如果又弹出 **Authorize Cloud Shell**，点 **Authorize**。
5. 最后它会试连一次 Binance，约 2 分钟。日志里出现"状态码：200（200 表示这里能录）"，就是东京连得上。
6. 看到"已经开始。你现在可以关掉 Cloud Shell。"就完成了。

这个命令可以重复运行，已经建好的会保留或更新。中途出错，就把报错截图发给智能体。

## 第 2 步：建封顶预算（约 3 分钟）

Cloud Run 按用量收费。封顶预算到了上限，只暂停 Cloud Run，不删除任何东西；G1 建的预算提醒照旧。

1. 打开 console.cloud.google.com/billing → 点你的结算账号 → 左边 **Budgets & alerts** → **Create budget**。
2. 选 **Spend cap enforcement**（封顶），不是 **Alerts only**。名字填 `sqp-cloud-run-cap`，点 **Next**。
3. **Project** 选 `self-quant-pro`，**Service** 选 **Cloud Run**，点 **Next**。
4. **Target amount** 填 `3`（美元；结算币种不是美元，就填约等于 3 美元的数），点 **Finish**。

找不到 Spend cap 选项就截图发给智能体。这一步不影响录制。

## 第 3 步（约 30 分钟后）：确认第一次录制成功

任选一种：

- 回到 Cloud Shell，运行 `bash sqp-recorder.sh self-quant-pro logs`。最后几行有"写入：exchange-info、premium-index、funding、klines-30m、open-interest-30m"，就是成功了。
- 或者打开 console.cloud.google.com/run/jobs → 点 `binance-recorder`：执行记录最新一行是绿色对勾，就是成功了。

然后告诉智能体"录制成功"，或者把日志截图发过去。

## 以后

- 每天自动运行，不用管。哪天失败了，第二天会把缺的时间段一起补上（持仓量最多补一个月）。
- 想暂停：console.cloud.google.com/cloudscheduler → `binance-recorder-daily` → **Pause**；恢复点 **Resume**。
- 想看日志：在 Cloud Shell 里运行 `bash sqp-recorder.sh self-quant-pro logs`。脚本文件不在了，就重新运行第 1 步那行命令。
- 日志里有一行"存储桶已用约……，上限……"。用到 4,000 MB 左右就告诉智能体：要写一份决策提高上限（预计 2027 年才会到）。

## 脚本做了什么

| 建了什么 | 设置 | 为什么 |
|---|---|---|
| 服务账号 `binance-recorder` | 存储桶上只有"新建对象"和"读对象" | 不能删除，也就不能覆盖：录下的数据只增不改，由谷歌云强制 |
| Cloud Run 任务 `binance-recorder` | 东京区；官方的 `python:3.12-slim` 镜像；每次运行从 main 取一个固定提交的代码，用锁定的依赖运行 | 东京连得上 Binance；没有要维护的系统，也没有对外开放的端口；代码只经由你合并的 PR 改动 |
| Cloud Scheduler `binance-recorder-daily` | 每天 UTC 00:17 | 在 00:00 的资金费结算之后 |
| 第一次运行 | 补最近 29 天 | 持仓量的历史，接口只保留一个月 |

没有建任何密钥文件。

## 常见问题

- **试连显示 451？** 东京区被 Binance 拒绝了。截图发给智能体，换一个区域。
- **日志里有 418？** Binance 暂时封了这个地址，任务自己会停下，第二天再试。
- **要不要下载服务账号的 JSON 密钥？** 不要。任务在谷歌云里直接用这个身份，不需要密钥。任何人或任何智能体让你下载谷歌云的 JSON 密钥，都拒绝。
