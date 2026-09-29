# 接口契约（以实际请求为准）

> 本文件里所有路径、方法、请求体、返回字段，都是本机抓包 + 真实调用验证过的，
> 不是网上流传的版本。验证时间：2026-09-28，WorkBuddy 桌面端 5.6.2。

## 0. 一句话结论：用哪套接口、走哪套鉴权

| 能力 | 主机 | 鉴权 |
|---|---|---|
| Buddy 加油站签到 | `https://www.workbuddy.cn` | **网页版会话 Cookie** |
| Buddy 旅行（派猫猫） | `https://www.workbuddy.cn` | **网页版会话 Cookie** |

两点和常见说法不一样，务必记牢：

1. **签到不一定要打 `copilot.tencent.com`。** 桌面端内部确实调用
   `POST https://copilot.tencent.com/billing/meter/checkin-status` 与 `.../daily-checkin`
   （见 `resources/extensions/edge-sync/server/index.cjs` 的 `getCheckinStatus` / `claimDailyCheckin`），
   但同一个 `/billing/meter/*` 前缀在 `www.workbuddy.cn` 下**同样提供**，且用网页版 Cookie 就能调通。
   实测 `POST https://www.workbuddy.cn/billing/meter/daily-checkin` 返回 `code:0, data.credit:100`。
   → 云端只用一套凭证（Cookie）就能同时覆盖签到和旅行。

2. **客户端 `accessToken` 现在不能用。** 桌面端 5.6.0 起把
   `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`
   里的 `auth.accessToken` 换成了 `{"$wbEncrypted":1,"envelope":"…"}` 加密信封，
   本机已经拿不到明文 token（5.6.2 实测仍是加密格式）。
   → 刷新脚本导出的是**网页版会话 Cookie**，不是客户端 token。

## 1. 请求通用要求

| 项 | 值 |
|---|---|
| 认证 | `Cookie: <完整网页版会话 Cookie 头>` |
| 必需头 | `Accept`、`User-Agent`（浏览器串）、`Referer: https://www.workbuddy.cn/profile/growth-center`、`Origin: https://www.workbuddy.cn`、`X-Client-Platform: web` |
| 不需要 | `Authorization`、`X-User-Id`（这套接口不用它们） |
| 请求体 | `Content-Type: application/json`，签到/领奖/派遣均为 `{}` 或指定字段 |

> 提前说一句坑：用 Python `urllib` 只带 `Cookie` 头时曾在网关拿到过 `401 openresty/APISIX`；
> 补齐 `Referer` / `Origin` / `X-Client-Platform` / 浏览器 UA 后稳定 200。
> 所以 `wb_api.py` 固定发这套头，别精简。

## 2. Buddy 加油站签到

| 步骤 | 方法 | 路径 | 请求体 |
|---|---|---|---|
| 查状态 | POST | `/billing/meter/checkin-status` | `{}` |
| 执行签到 | POST | `/billing/meter/daily-checkin` | `{}` |

真实返回（2026-09-28）：

```json
// checkin-status
{"code":0,"msg":"OK","data":{"active":false,"today_checked_in":false,"streak_days":0,
 "daily_credit":0,"today_credit":0,"week_progress":[false,false,false,false,false,false,false],
 "action_button":{"show":false,"text":"","action":""}}}

// daily-checkin（首次签到成功）
{"code":0,"msg":"OK","data":{"credit":100,"streak_days":4,"is_streak_day":false}}

// daily-checkin（同日重复调用）
HTTP/1.1 400
{"code":10001,"msg":"今天已签到，请明天再来","requestId":"20bb9b97-0fc4-4908-b425-d729af4acbce"}
```

⚠️ 两个必须记住的坑：

1. **`today_checked_in` 不可信。** 上面第一次运行时它返回 `false`、`streak_days:0`，
   但紧接着的 `daily-checkin` 返回 `code:0 / credit:100`，说明当天确实签成了。
   `active:false`、`daily_credit:0` 同样不代表活动关闭。
   **幂等判断只认 `daily-checkin` 的 body，不要用 `today_checked_in` 做短路依据。**

2. **重复签到时 HTTP 状态码是 400，不是 200。** 业务码 `code=10001` 藏在 400 的响应体里。
   如果按「HTTP 200 才算成功」来写，每天第一次之外的所有调用都会被误判成失败。
   → 必须**只看 body 里的业务码**，HTTP 状态码只用来识别 401（凭证失效）。

签到按自然日结算，每天 100 积分，连续第 7 天 1000 积分。

## 3. Buddy 旅行（派猫猫）

| 步骤 | 方法 | 路径 | 请求体 |
|---|---|---|---|
| 查状态 | GET | `/activity/growth/buddy/travel/status` | — |
| 派遣 | POST | `/activity/growth/buddy/travel/depart` | `{"location_id":1}` |
| 领取 | POST | `/activity/growth/buddy/travel/claim` | `{}` |

真实返回（2026-09-28，从「古镇客栈」归来）：

```json
// travel/status
{"code":0,"msg":"OK","data":{"state":"arrived","buddy_id":7558713,"record_id":10400621,
 "location":{"id":4,"code":"ancient_town","name":"古镇客栈","duration_hours":3},
 "depart_at":1790565421,"arrive_at":1790576221,"server_now":1790592496,
 "daily_limit_reached":true,"duration_hours":3,"reward_credit":8}}

// travel/claim
{"code":0,"msg":"OK","data":{"state":"idle","record_id":10400621,
 "reward_credit":8,"letter":{...}}}

// travel/status（claim 之后立刻复查）
{"code":0,"msg":"OK","data":{"state":"idle","record_id":0,"location":null,
 "arrive_at":0,"server_now":1790592498,"daily_limit_reached":true,"reward_credit":0}}
```

`state` 取值与动作：

| state | 含义 | 动作 |
|---|---|---|
| `idle` | 空闲 | `daily_limit_reached=false` → depart；`=true` → 今日已派过，跳过 |
| `traveling` | 在路上 | 不重复派遣 |
| `arrived` / `returned` / `finished` | 已归来 | claim |

关键坑：

- `daily_limit_reached` 由服务端按自然日算。**claim 不会把它清掉**：上面 claim 之后复查仍是 `true`，
  所以「领完奖励」不等于「能再派一趟」。本项目据此实现「先领、再判、今天已派就不派」。
- 倒计时用 `arrive_at - server_now`，不要用本机时间（有钟差）。
- 旅行时长 1–4 小时随机，地点 `1=咖啡馆`、`4=古镇客栈` 等，以 `travel/config` 为准。
- 接口位于 `www.workbuddy.cn`（网页版成长中心），活动改版/下线时会直接返业务错误码。

## 4. 凭证获取方式（刷新脚本）

本机导出的是浏览器里 `www.workbuddy.cn` 域下的会话 Cookie，主要几条：

| Cookie | 作用 |
|---|---|
| `KEYCLOAK_SESSION` | Keycloak 会话标识 |
| `session` / `session_2` | 站点会话主体（最长，约 4 KB / 2 KB） |
| `qcloud_from` / `sensorsdata2015jssdkcross` / `_gcl_au` | 埋点与来源，顺带带上无害 |

导出途径（`refresh_credentials.py`）：

1. 默认打开 `~/.workbuddy/buddy-travel-data/browser_profile`（Buddy 旅行技能的持久化 profile）
2. 也可以用 `--profile` 指定其它 Chromium profile，或用 `--cookie-file` 直接喂 Cookie
3. 导出后先做一次只读校验（`checkin-status`），校验不过就不落盘、不上传

会话有效期实测约 1–2 周，过期后 runner 会返回 `credential_expired: true`，
飞书通知里会明确写「需要刷新凭证」，本机重跑一次刷新脚本即可。

## 5. 没有使用的接口

以下传闻中的接口**本项目一律不调用**：

- `copilot.tencent.com` 下的签到接口：客户端 token 已加密，云端用不了；且 `www.workbuddy.cn` 有等价接口
- 任何需要 `X-User-Id`、`Authorization: Bearer <客户端 token>` 的调用
- 成长中心其它只读接口（`/activity/growth/energy`、`/buddy/list`、`/lottery/*` 等）：与本任务无关
