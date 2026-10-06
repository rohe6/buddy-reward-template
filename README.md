# Buddy 每日任务 · GitHub Actions 自动化

每天定时（目标北京时间 **07:50**，另有说明见下方「为什么实际快到下午一点才收到」）
在 GitHub 的免费 runner 上跑一次：
**先签到 → 再先领已到家的旅行奖励 → 再判断要不要派新的一趟**，结果推送到飞书。

不需要常开电脑。云端 runner 上没有任何本机登录态，凭证由本机刷新脚本导出后放进仓库 Secret。

---

## 目录结构

```
buddy-reward-gha/
├── .github/workflows/buddy-reward.yml   # 定时任务定义（cron + 手动触发）
├── scripts/
│   ├── refresh_credentials.py   # 本机跑：导出本机登录态 → 注入仓库 Secret
│   ├── cloud_runner.py          # 云端跑：签到 + 旅行，产出 result.json
│   ├── feishu_notify.py         # 云端跑：把结果推飞书（签到/猫猫分两块）
│   └── wb_api.py                # 共用 HTTP 客户端（纯标准库）
├── references/endpoints.md      # 接口契约（实测记录，含坑）
└── requirements-refresh.txt     # 只有本机刷新脚本需要装依赖
```

云端脚本零第三方依赖（只用 Python 标准库），所以 workflow 里不需要 `pip install`，跑得也快。

---

## 最快的一条路：一条命令自动部署

不想手动建仓库、配 Secret 的话，本机跑这个（需要 Python 3.8+ 和一个 GitHub 令牌）：

```bash
python3 deploy.py
```

它会引导你登录一次 WorkBuddy、粘贴一次 GitHub 令牌，
然后自动建仓库、上传代码、写入 Secret、跑一次验证、把结果发飞书。
之后再跑一次就等于「续期凭证」。

> `deploy.py` 不在本仓库里，它是从本仓库代码**生成**的单文件分发版本——
> 这样发给别人时只需要一个文件，不用教他 clone。
> 自己生成一份：在仓库根目录跑
>
> ```bash
> python3 build_share_kit.py     # 产物在 dist/ 下
> ```
>
> 生成规则见 `share_kit/deploy_template.py`（模板）和 `build_share_kit.py`（打包器）。
> 改了仓库里的脚本记得重新生成一次，`dist/` 已在 `.gitignore` 里。

---

## 手动部署：四步

### 1. 建仓库、推代码

**仓库根目录必须是 `buddy-reward-gha/` 这一层**（workflow 里写的是 `scripts/cloud_runner.py`），
不要把上一层的 `out/`、`.workbuddy/` 一起推上去。

方式 A —— 有 `gh` CLI（最省事，建仓库和推送一步到位）：

```bash
cd buddy-reward-gha
git init -b main && git add -A && git commit -m "Buddy daily reward automation"
gh repo create buddy-reward --private --source=. --push
```

没装 `gh` 的话：`winget install GitHub.cli`（装完开个新终端，`gh auth login` 走一遍）。

方式 B —— 纯 git（本机已经 `git init -b main` 并 `git add` 过了）：

```bash
# 1) 先在网页上建一个**空仓库**（千万不要勾 Add a README / .gitignore / license）
#    https://github.com/new
# 2) 配一次 git 身份（本机目前没配过，不配就 commit 不了）
git config user.name  "你的名字"
git config user.email "你的邮箱"        # 建议用 GitHub 账号绑定的邮箱，提交才会算到你头上
# 3) 提交并推送
git commit -m "Buddy daily reward automation"
git remote add origin https://github.com/<你的账号>/<仓库名>.git
git push -u origin main
```

首次 `push` 走 HTTPS 时，Git 凭据管理器会弹窗让你登录 GitHub，登录一次之后就不用再输了。

**三条容易踩的坑：**

- 分支名必须是 `main`（或你仓库真正的默认分支）。`schedule` **只在默认分支上触发**，
  workflow 文件不在默认分支上，cron 永远不会跑。
- 第 1 步如果误勾了「Add a README」，远端会有一次你本地没有的提交，`push` 会被拒。
  先 `git pull --rebase origin main` 再 `git push -u origin main` 即可。
- 仓库要开着 Actions（新建仓库默认是开的）。fork 过来的仓库默认**不跑** schedule，
  需要手动进 Actions 页点一次 Enable。

### 2. 建飞书机器人，拿 Webhook

飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook 地址。
如果勾了「签名校验」，把签名密钥也留下。

### 3. 本机导出凭证并注入 Secret

```bash
pip install -r requirements-refresh.txt

# 先确保网页版登录态是活的：打开 https://www.workbuddy.cn/profile/growth-center 确认已登录；
# 如果用的是 Buddy 旅行技能，先在技能里跑一次 buddy_travel.py --login
python3 scripts/refresh_credentials.py --repo <你的账号>/<仓库名> --secret FEISHU_WEBHOOK
```

`--secret FEISHU_WEBHOOK` 表示把当前环境变量 `FEISHU_WEBHOOK` 的值也一并写进去：

```bash
export FEISHU_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/xxxx'
export FEISHU_SIGN_SECRET='xxxx'   # 没开签名校验就别设
```

脚本行为：

1. 打开本机 Chromium profile，读出 `workbuddy.cn` 域下的会话 Cookie
2. **只读校验一次**（调 `checkin-status`，不产生任何领取动作），校验不过就中止，不落盘不上传
3. 写入本机凭据文件 `~/.workbuddy/buddy-reward-gha/credentials.env`（权限 600，且在仓库外）
4. 注入 Secret：有 `gh` CLI 就走 `gh secret set`（值走 stdin，不进命令行和日志）；
   没有 `gh` 但有 `GH_TOKEN` 就走 GitHub REST（libsodium sealed box 加密）；
   都没有就打印手动配置步骤，不自动上传

### 4. 手动跑一次验证

仓库 → Actions → `Buddy daily reward` → Run workflow。
看到绿色 ✅、飞书收到卡片，就说明通了。之后每天 09:00 自动跑。

---

## 任务逻辑（严格按需求）

```
签到（幂等）
  └─ daily-checkin 返回 code=0        → success（+100 积分 / 连续天数）
  └─ 返回 code=10001 或"已签到"       → already_checked（成功，不报错）

旅行
  ├─ 查状态
  ├─ state = arrived / returned / finished
  │    └─ 先 claim 领掉到家的奖励
  │         └─ 领完复查状态
  │              ├─ idle 且 daily_limit_reached=false → depart 派新的一趟
  │              └─ daily_limit_reached=true          → 今日已派过，不派（claimed_then_skip）
  ├─ state = idle
  │    ├─ daily_limit_reached=false → depart
  │    └─ daily_limit_reached=true  → 跳过
  └─ state = traveling → 不重复派遣
```

失败隔离：**签到成功即算成功**。旅行任何一步失败（含 401）只影响飞书卡片里猫猫那一块，
不影响签到的结论，也不改变退出码。

退出码约定：

| 情况 | 进程退出码 | workflow 结果 |
|---|---|---|
| 签到成功 / 今日已签到 | 0 | ✅ 绿 |
| 签到失败 | 1 | ❌ 红（方便一眼发现） |
| 旅行失败、签到成功 | 0 | ✅ 绿（卡片里标出猫猫失败） |
| 凭证失效 | 1 | ❌ 红（卡片提示去刷新凭证） |

---

## 飞书卡片长什么样

签到和猫猫是两个独立区块，中间有分割线，谁成功谁失败一眼看清：

```
🎁 Buddy 加油站 · 签到
✅ 签到成功 · +100 积分 · 连续第 4 天
─────────────────────
🐱 Buddy 旅行
✅ 已领回到家的旅行奖励 · +8 积分（古镇客栈）
🈵 今日已派过一趟，不再派遣
记录号 10400621
─────────────────────
运行时间 2026-09-28T18:48:16+08:00（UTC+8） · 签到成功 · 猫猫正常
```

卡片颜色：两个都正常 = 绿；签到成功但猫猫失败 = 橙；签到失败或凭证失效 = 红。

想本地预览卡片长什么样（不发出去）：

```bash
python3 scripts/feishu_notify.py out/result.json --dry-run
```

---

## 凭证会过期，怎么续

网页版会话有效期实测约 1–2 周。过期后飞书卡片会变成红色并提示「需要刷新凭证」，
本机重跑一次刷新脚本即可。

刷新脚本会把新值**直接走 REST API 写进仓库 Secret**，不需要手动复制粘贴。
取 GitHub 令牌的通道按顺序尝试：

1. 环境变量 `GH_TOKEN` / `GITHUB_TOKEN`
2. **Windows 凭据管理器**（直接 `CredRead`，target `git:https://github.com`）
3. `git credential fill` 兜底

推荐 2 号通道的原因：这台机器上没有 `gh` CLI、也没有 `GH_TOKEN`，
而 `git credential fill` 在无人值守场景会弹出凭据管理器窗口并**一直挂到超时**。
只要之前用浏览器登录过一次 `git push`，凭据管理器里就存着一个 scope 含
`repo, workflow` 的令牌，直接读它最快也最稳。

> **别在网页上手动粘贴 Secret。** Cookie 值有数千个字符（本机实测约 6700），GitHub 的输入框
> 很容易在粘贴时被悄悄截断。截断后的值**仍然非空**，Secret 看起来是配置好的，
> 接口却只会回一个 `HTTP 401` —— 和「会话过期」长得一模一样，极难排查。
> 确实需要手动粘时，粘完**务必用 workflow 里的 Preflight 步骤核对长度与指纹**。

### Preflight：Secret 自检

workflow 在真正调接口之前会先跑一步 `Preflight - check WB_COOKIE shape`，
只打印长度和 SHA-256 前 8 位指纹（和刷新脚本输出的是同一个值，可以直接对）：

```
WB_COOKIE: 长度=6709 指纹=1a2b3c4d
```

- **长度 < 1000** 或缺 `KEYCLOAK_SESSION` → 直接判失败并提示重新注入，
  不会再去调接口拿一个误导性的 401
- 想确认 Secret 里到底是不是本机那份 → 比对这个指纹即可

用 systemd timer / Windows 任务计划程序定期跑刷新脚本，可以做到接近免维护。

---

## 安全约定

- 明文 Cookie **只在内存 → 本机凭据文件 → GitHub Secret** 之间流转
- 绝不写入仓库目录，`.gitignore` 已挡掉 `*.local.json`、`credentials.env` 等
- 日志/输出里只有长度和 SHA-256 前 8 位指纹，不回显值本身
- workflow 里凭证只从 `secrets.*` 注入环境变量；GitHub 会自动对日志做掩码
- 云端只用 Python 标准库，不引入任何第三方包，减少供应链面

⚠️ 本机自测时**不要**直接 `cat` 或用不加引号的 `source` 打开 `credentials.env`——
Cookie 头里带 `;` 和空格，会被 shell 当成命令分隔符，把明文打到终端。
脚本已经用单引号把值包好了，正常 `set -a; . "$FILE"; set +a` 是安全的；
更稳妥的做法是直接给 `WB_COOKIE_FILE` 指向该文件：

```bash
WB_COOKIE_FILE=~/.workbuddy/buddy-reward-gha/credentials.env \
  python3 scripts/cloud_runner.py --out out/result.json
```

---

## 常见问题

**Q：为什么不是读客户端 token？**
桌面端 5.6.0 起把本地 `accessToken` 换成了 `$wbEncrypted` 加密信封，本机已经取不到明文
（5.6.2 实测仍是加密格式）。所以刷新脚本导出的是网页版会话 Cookie ——
它同时能覆盖签到和旅行两组接口，反而是更省事的一条路。细节见 `references/endpoints.md`。

**Q：一天只跑一次，会不会漏领？**
不会漏领，只是会晚一天。今天的 run 派出新的一趟，猫猫 1–4 小时后到家，
明天的 run 会先把它领掉。想当天就领，可以给 workflow 再加一条 cron
（例如 `0 13 * * *`，北京时间 21:00）；脚本幂等，多跑没有副作用。

**Q：为什么 `checkin-status` 说 `today_checked_in: false`，但签到却成功了？**
这个字段历史上就不可靠，本次实测也复现了。所以幂等判断只认 `daily-checkin` 的返回体。

**Q：卡片报「凭证被网关拒绝（HTTP 401）」，但我本机明明是登录状态？**
401 是网关层直接拒绝，有两种原因，长得完全一样：

1. Cookie 真的过期了
2. Secret 里的值不完整（手动粘贴时被截断）

区分办法就是看 Preflight 打印的长度与指纹：长度只有几百、或指纹和本机对不上，
说明是第 2 种 —— 在本机重跑刷新脚本重新注入即可（不要再去网页上粘一次）。
两者都吻合，才是第 1 种，需要重新登录 WorkBuddy 后再刷新。

**Q：`daily-checkin` 明明失败了，为什么脚本还判成功？**
因为「今天已签到」时接口返回的是 **HTTP 400** 加 `{"code":10001}`，不是 200。
判断依据是响应体里的业务码，HTTP 状态码只用来识别 401。见 `references/endpoints.md`。

**Q：为什么重复调用签到不算失败？**
今日已签到时服务端返回的是 **HTTP 400**，业务码 `10001` 藏在响应体里
（`{"code":10001,"msg":"今天已签到，请明天再来"}`）。因为 HTTP 状态码骗人，
云端脚本的判断依据是 body 里的业务码，HTTP 码只用来识别 401。

**Q：为什么 cron 写的是 `50 23 * * *`，而不是直接写北京时间的 07:50？**
GitHub 的 schedule 默认按 **UTC** 计算，北京时间是 UTC+8，所以
07:50 北京 = **23:50 UTC（前一天）**。注意 UTC 的日期比北京早一天，翻日志时别搞混。
GitHub 现在也支持给 schedule 加 IANA 时区，写成下面这样两者等价：

```yaml
on:
  schedule:
    - cron: "50 7 * * *"
      timezone: "Asia/Shanghai"
```

**Q：说好早上 07:50，为什么实际快到下午一点才收到飞书卡片？**
因为**不准时的是 GitHub 的 cron 调度器本身**，跟我们怎么写没关系。

官方文档只承诺「尽力而为」：`The schedule event can be delayed during periods of
high loads of GitHub Actions workflow runs … some queued jobs may be dropped`。
而从 **2026-08 下旬**起出现了大面积的平台侧积压，官方社区讨论
[#206019](https://github.com/orgs/community/discussions/206019) 和
[#208924](https://github.com/orgs/community/discussions/208924)
里大量仓库报告**每天晚 3~10 小时**，并且这些运行的
`created_at == run_started_at`（秒级相同）—— 说明是**调度事件本身晚创建**，
不是排队等 runner，所以换分钟数、加时区、换分支都治不了。

本仓库实测：9/29 那次还准（只晚 44 秒），**9/30 起每天稳定落在 05:08~05:41 UTC，
也就是恒定晚 5 小时左右**。

结论：

- 这条 cron 只能保证「**每天跑一次**」，具体时刻不可控。**签到不会漏**
  （脚本幂等，晚点跑照样把当天的奖励领掉），只是飞书卡片来得晚。
- 想**准点**，得绕开 cron 调度器，用外部定时器去调 `workflow_dispatch`
  —— 它走实时事件通道，不经过 cron 队列，实测几秒内就开始跑。做法见下一节。
- workflow 里加了一步 `Log trigger and start time`，会把本次是被哪条 cron 触发、
  几点开始打印出来，方便以后核对晚了多久。

---

## 准点触发（可选）

如果确实需要每天准点跑（比如希望卡片一早就到），思路是：**别指望 cron，
用另一个定时器去打 `workflow_dispatch` 接口**。它走实时事件通道，不经过 cron 队列，
实测几秒内就开始跑。

两种挂法，按「愿不愿意把令牌交给第三方」选：

### 方案 A：本机任务计划程序（令牌不出本机，推荐）

仓库里有现成的 `scripts/dispatch_once.py`，本机（或任何能跑 Python 的机器）这样调用：

```bash
export GH_DISPATCH_TOKEN=<你的令牌>
python3 scripts/dispatch_once.py --repo <owner>/<repo>
```

先手动跑一次看有没有 `已触发`，然后交给系统定时器：

- **Windows**：任务计划程序 → 新建任务 → 触发器「每天 07:50」→ 操作「启动程序」，
  程序填 `pythonw.exe` 的完整路径，参数填 `scripts\dispatch_once.py --repo <owner>/<repo>`，
  并在「设置」里勾上**「如果错过了计划开始时间，请尽快启动任务」**（电脑那时没开机会补跑）。
- **Linux/macOS**：`systemd timer` 或 `cron`，每天 07:50 跑同一行命令。

令牌放在本机（环境变量或凭据文件），不会离开这台机器。代价是电脑得在那个点开着。

### 方案 B：第三方定时服务（不需要本机开机）

在 cron-job.org / Cloudflare Workers cron / Google Cloud Scheduler 里建一个
每天 07:50（时区选 **Asia/Shanghai**，很多服务默认 UTC，那就是 15:50）的 HTTP 请求。
`--show-config` 会把该填的东西打印出来，不用自己拼：

```bash
python3 scripts/dispatch_once.py --repo <owner>/<repo> --show-config
```

成功标志是 **HTTP 204**。接口只负责「叫醒」，执行结果去 Actions 页面看。

⚠️ **令牌是凭据。** 放在第三方服务上，等于把「触发这个仓库工作流」的权限交给它：
请单独建一个 **fine-grained PAT**，只授权这一个仓库、只勾 `Actions: write`，
不要图省事用 classic PAT 的 `repo` 全权限。fine-grained PAT 有有效期上限，
到期后定时器会开始返回 401 —— 记得续。

### 和 cron 并存时不会收到两张卡

cron 留着当兜底（第三方定时器挂了、或本机没开机时它还能救一次）。两者并存时，
cron 那次通常已经无事可做，所以 workflow 在 **`schedule` 触发时给通知脚本加了
`--quiet-noop`**：只有「签到幂等跳过 **且** 旅行今日已派完」这种纯确认才不推送。

**失败永远不会被静默** —— 任何 401、签到失败、旅行异常照常发卡片。
判据见 `scripts/feishu_notify.py` 的 `is_noop()`，写得很窄，宁可多发也不漏报。

---

## 常见问题（续）

**Q：定时任务会不会自己停掉？**
会，两种情况：**公开仓库**里 60 天没有任何仓库活动，schedule 会被自动禁用（会先发邮件提醒，
去 Actions 页点 Enable workflow 就能恢复）；fork 来的仓库默认不跑 schedule。
私有仓库不受 60 天规则影响，但每天一次约 30 秒的运行会消耗免费额度（每月 2000 分钟，够用）。

**Q：cron 时间到点了为什么没跑，或者晚了好几个小时？**
按顺序排三处：① workflow 是否在**默认分支**上；② Actions 是否被禁用；
③ **是不是又撞上 GitHub 的调度积压**（自 2026-08 起这是常态，晚 3~10 小时都算「正常」，
详见上面那条 FAQ，不是配置问题）。
前两点排除后，用 `workflow_dispatch` 手动跑一次即可确认脚本本身没问题；
workflow 第一步 `Log trigger and start time` 会打印它实际是几点开始的，
把那个时间和 cron 一对，就知道晚了多久。

**Q：可以在 GitHub 上直接看到凭证吗？**
不能。Secret 写入后只能覆盖、不能读回。万一怀疑泄漏，去飞书 / WorkBuddy 网页版
退出登录，再重跑刷新脚本注入新会话即可。
