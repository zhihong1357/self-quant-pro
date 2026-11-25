# 开通谷歌云（G1，只开存储）

一次性的设置，约 15 分钟。做完以后，GitHub 上的数据任务能把数据写进你的私有存储桶。全程不发任何长期密钥，所以谁也拿不到能长期使用的钥匙，包括智能体。依据是[决策 0015](decisions/0015-g1-starts-now.md)。

界面上的按钮名称可能和这里略有出入。卡住了就截图发给智能体。

## 你需要准备

- 订阅 Google AI Pro 的那个 Google 账号。
- 一张能网上付款的 Visa 或 Mastercard，借记卡、信用卡都行。开结算账号必须绑卡。
  - G1 只用存储，预计在免费额度之内，每月 0 美元。
  - 就算超出，也先由每月 10 美元的赠金抵扣。

## 第 1 步：开结算账号，领每月 10 美元赠金（约 5 分钟）

先试 A；A 里找不到入口，再用 B。

**A. 在 Google AI Studio 里领**（谷歌 2026-11-13 上线的新流程）
1. 打开 aistudio.google.com，用 AI Pro 的账号登录。
2. 左边栏会提示有没有可以领取的赠金，点进去，弹出 **Claim Google Cloud Credits** 窗口。
3. 按提示建开发者档案，再建结算账号（这一步绑卡）。
4. 最后点 **Redeem and Continue**。以后每个月的赠金会自动加到这个结算账号。
5. 如果页面要求**预付 5 美元**：那是用赠金抵扣 Gemini API 费用的条件（谷歌官方博客，2026-11-13），G1 只用存储，用不到。先不付，改走 B；拿不准就截图发给智能体。

**B. 在 Google Developer Program 的权益页领**
1. 打开 program.google.com/my-benefits。
2. 找到谷歌云赠金，在下拉框里选你的结算账号。
3. 勾上 **Always use this billing account**（以后每月自动加到这个账号），点应用。

**领完核对一下**：打开 console.cloud.google.com/billing，点你的结算账号，再点左边 **Credits**（赠金）。能看到一条 Google Developer Program 的赠金，就成功了。

领赠金不是 G1 的硬前提。硬前提只有一个：有一个开着的结算账号。

## 第 2 步：建一个专门的项目（约 2 分钟）

1. 打开 console.cloud.google.com/projectcreate。
2. **Project name** 填 `self-quant-pro`。
3. 下面会自动生成一个 **Project ID**，例如 `self-quant-pro-123456`。把它记下来，第 3 步要用。
4. **Billing account** 选第 1 步那个结算账号。**Location** 不用改。
5. 点 **Create**。

如果建项目时没看到选结算账号的地方：打开 console.cloud.google.com/billing/projects，在这个项目那一行点右边的三个点 → **Change billing** → 选第 1 步的账号。

## 第 3 步：在 Cloud Shell 里运行一行命令（约 5 分钟）

Cloud Shell 是谷歌云自带的网页终端，什么都不用装。

1. 打开 shell.cloud.google.com/?show=terminal。第一次打开，如果弹出授权窗口，点 **Authorize**。
2. 复制下面这一整行。把最后的 `你的项目ID` 换成第 2 步记下的 Project ID，粘贴进终端，按回车：

   ```
   curl -fsSLo sqp-setup.sh https://raw.githubusercontent.com/zhihong1357/self-quant-pro/main/scripts/gcp/setup-storage.sh && bash sqp-setup.sh 你的项目ID
   ```

   这一行先下载脚本，下载成功才运行。如果显示 `curl: (22) ... 404`，通常是这份说明所在的 PR 还没合并：先合并，再运行。
3. 屏幕上会显示项目 ID 和编号，并问"确认无误请输入 yes 回车："。**看到这句话才输入** `yes`。
   - 如果没看到这句话，终端就回到了 `$` 提示符，说明脚本没运行起来。这时不要输入 yes：在终端里单独输入 yes，会不停地打印 y（按 Ctrl+C 停止）。截图发给智能体。
4. 中途如果又弹出 **Authorize Cloud Shell**，点 **Authorize**。
5. 最后会打印三行，样子如下：

   ```
   GCP_PROJECT_ID=self-quant-pro-123456
   GCP_WIF_PROVIDER=projects/123456789012/locations/global/workloadIdentityPools/github/providers/self-quant-pro
   SQP_BUCKET=self-quant-pro-123456-sqp-data
   ```

   把这三行原样发给智能体。它们不是密钥：只有本仓库 `main` 分支上的工作流能用这个身份，别人拿到这三行也什么都做不了。

这个命令可以重复运行，已经建好的会保留。中途出错，就把报错截图发给智能体。

## 第 4 步（只在脚本提示时才做）：在网页上建预算提醒

结算账号的币种如果不是美元，脚本建不了预算，会提示你做这一步。

1. 打开 console.cloud.google.com/billing → 点你的结算账号 → 左边 **Budgets & alerts** → **Create budget**。
2. 类型选 **Alerts only**（只提醒），名字填 `sqp-monthly`。
3. **Projects** 选 `self-quant-pro`；**Credits**（赠金）下面的勾全部取消，这样按原价计算。
4. 金额填按当天汇率折合 10 美元左右的数。
5. 提醒档位：10%、50%、90%、100%。
6. 点 **Finish**。

## 脚本做了什么

| 建了什么 | 设置 | 为什么 |
|---|---|---|
| 启用 6 个接口 | IAM、STS、Cloud Storage、结算、预算、资源管理 | 后面几步要用；启用接口本身不收费 |
| 存储桶 `<项目ID>-sqp-data` | us-central1，标准存储，统一权限，禁止公开访问 | us-central1 有永久免费额度；数据不能被公开 |
| 身份池 `github` 和提供方 `self-quant-pro` | 只接受 GitHub 签发的令牌，并且必须同时满足四条：仓库编号 1468841217、所有者编号 265740839、分支 `main`、由定时或手动触发 | 编号不会被别人重新注册；PR 触发的任务拿不到身份 |
| 存储桶权限 | 上面这个身份只有"新建对象"和"读对象" | 不能删除，也就不能覆盖：原始数据只增不改，由谷歌云强制 |
| 预算提醒 `sqp-monthly` | 每月 10 美元，按原价，1、5、9、10 美元时发邮件 | 有异常早知道 |

没有建服务账号，也没有任何密钥文件。

## 为什么不建"封顶预算"

封顶预算（spend cap）到顶时只暂停 Cloud Run、Gemini API 这类按量计费的服务，不暂停存储（谷歌官方文档，2026-11-24 核对）。G1 只用存储，建了也管不到。

存储的费用靠三层控制：
- 数据量小：最小切片估计不到 1 GB，免费额度是每月 5 GB。
- 代码自己限量：上传前先检查存储桶的总量。
- 预算提醒。

G5 开始用 Cloud Run 时，再建封顶预算。

## 常见问题

- **要不要建服务账号、下载 JSON 密钥？** 不要。我们用的方式不发密钥。以后任何人或任何智能体让你下载谷歌云的 JSON 密钥，都拒绝。
- **我能自己看存储桶里的东西吗？** 能：console.cloud.google.com/storage → 点存储桶的名字。你是项目所有者，能看也能删；GitHub 的工作流只能新建和读取。
- **以后每月要做什么？** [成本护栏](cost-guardrails.md)第 4 节的五分钟检查。
