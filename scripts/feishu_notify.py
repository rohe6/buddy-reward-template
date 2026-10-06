#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu_notify.py — 把 cloud_runner.py 的结果推送到飞书（自定义机器人 Webhook）

签到与猫猫分开成两个独立区块，一眼能分清谁成功谁失败。

环境变量：
    FEISHU_WEBHOOK       自定义机器人 webhook 地址（必填）
    FEISHU_SIGN_SECRET   若机器人开启了「签名校验」，填这里

用法：
    python3 feishu_notify.py result.json
    python3 feishu_notify.py --text result.json     # 用纯文本消息替代卡片
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request

CST = datetime.timezone(datetime.timedelta(hours=8))

ENV_WEBHOOK = "FEISHU_WEBHOOK"
ENV_SECRET = "FEISHU_SIGN_SECRET"


def _ts(unix: int | None) -> str:
    if not unix:
        return "未知时间"
    return datetime.datetime.fromtimestamp(unix, CST).strftime("%m-%d %H:%M")


def _dur(seconds: int | None) -> str:
    if seconds is None:
        return "未知"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return "{} 小时 {} 分".format(h, m)
    if m:
        return "{} 分 {} 秒".format(m, s)
    return "{} 秒".format(s)


# ---------------------------------------------------------------------------
# 两个区块的文案
# ---------------------------------------------------------------------------
def checkin_lines(c: dict) -> tuple[str, str]:
    """返回 (标题, 正文)。"""
    status = c.get("status")
    if status == "success":
        bits = ["**✅ 签到成功**"]
        if c.get("credit") is not None:
            bits.append("+{} 积分".format(c["credit"]))
        if c.get("streak_days") is not None:
            bits.append("连续第 {} 天".format(c["streak_days"]))
        return "🎁 Buddy 加油站 · 签到", " · ".join(bits)
    if status == "already_checked":
        streak = "，连续 {} 天".format(c["streak_days"]) if c.get("streak_days") is not None else ""
        return "🎁 Buddy 加油站 · 签到", "**☑️ 今日已签到**（幂等跳过，无重复领取）{}".format(streak)
    if status == "status_only":
        return "🎁 Buddy 加油站 · 签到", "**🔍 仅查询** · 今日已签到：{} · 连续 {} 天".format(
            c.get("today_checked_in"), c.get("streak_days"))
    return "🎁 Buddy 加油站 · 签到", "**❌ 签到失败**\n{}".format(c.get("reason") or "未知原因")


def travel_lines(t: dict) -> tuple[str, str]:
    status = t.get("status")
    loc = t.get("location") or "未知地点"

    if status in ("claimed", "claimed_then_departed", "claimed_then_skip"):
        claimed = t.get("claimed") or {}
        body = ["**✅ 已领回到家的旅行奖励** · +{} 积分（{}）".format(
            claimed.get("reward_credit"), claimed.get("location") or loc)]
        extra = {
            "claimed_then_departed": "**🚀 接着派出了新的一趟** → {}，预计 {} 回家".format(
                loc, _ts(t.get("arrive_at"))),
            "claimed_then_skip": "**🈵 今日已派过一趟，不再派遣**",
        }.get(status)
        if extra:
            body.append(extra)
        body.append("记录号 {}".format(claimed.get("record_id") or "—"))
        return "🐱 Buddy 旅行", "\n".join(body)

    if status == "departed":
        return "🐱 Buddy 旅行", "**🚀 已派出新的一趟**\n目的地：{} · 预计 {} 回家 · 记录号 {}".format(
            loc, _ts(t.get("arrive_at")), t.get("record_id") or "—")

    if status == "traveling":
        return "🐱 Buddy 旅行", "**🧳 猫猫在路上**\n目的地：{} · 还有{}到家 · 记录号 {}".format(
            loc, _dur(t.get("remaining_seconds")), t.get("record_id") or "—")

    if status == "daily_limit_reached":
        return "🐱 Buddy 旅行", "**🈵 今日旅行次数已完成**\n不重复派遣，等明天再来。"

    if status == "status_only":
        return "🐱 Buddy 旅行", "**🔍 仅查询** · 当前状态：{} · 今日已派：{}".format(
            t.get("state"), t.get("daily_limit_reached"))

    return "🐱 Buddy 旅行", "**⚠️ 处理失败（不影响上面的签到结论）**\n{}".format(
        t.get("reason") or "未知原因")


# 「猫猫这边没什么新动作」的状态：今天已经派过一趟，或者猫猫还在路上。
# 都没到能领奖励的时候，所以这次运行在旅行侧没有新信息。
NOOP_TRAVEL_STATUSES = frozenset({"daily_limit_reached", "traveling"})


def is_noop(result: dict) -> bool:
    """这次运行是不是「纯确认」——没新做任何事，也没出错。

    用途：外部定时器准点触发 + cron 兜底并存时，兜底那次通常是纯确认
    （签到幂等跳过、旅行今日已派完），没必要把同一件事再发一张卡片。

    **判据刻意收得很窄**：只认 already_checked + {daily_limit_reached, traveling}。
    任何失败、任何凭证问题、以及形状不认识的 status_only 都不算 noop ——
    宁可多发一张卡，也不能把该报的静默掉。
    """
    c = result.get("checkin") or {}
    t = result.get("travel") or {}
    if result.get("credential_expired"):
        return False
    if not (c.get("ok") and t.get("ok")):
        return False
    return (c.get("status") == "already_checked"
            and t.get("status") in NOOP_TRAVEL_STATUSES)


def build_card(result: dict) -> dict:
    c = result.get("checkin") or {}
    t = result.get("travel") or {}

    checkin_ok = bool(c.get("ok"))
    travel_ok = bool(t.get("ok"))
    expired = bool(result.get("credential_expired"))

    if expired:
        template, title = "red", "Buddy 每日任务 · 凭证失效"
    elif checkin_ok and travel_ok:
        template, title = "green", "Buddy 每日任务 · 全部完成"
    elif checkin_ok:
        template, title = "orange", "Buddy 每日任务 · 签到完成，猫猫待处理"
    else:
        template, title = "red", "Buddy 每日任务 · 签到失败"

    ct, cb = checkin_lines(c)
    tt, tb = travel_lines(t)

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "**{}**\n{}".format(ct, cb)}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "**{}**\n{}".format(tt, tb)}},
    ]
    if expired:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            "**🔑 需要刷新凭证**\n本机网页会话已过期。请在常开电脑上重新运行 "
            "`refresh_credentials.py` 注入新凭证。"}})

    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content":
        "运行时间 {}（UTC+8） · 签到{} · 猫猫{}".format(
            result.get("run_at", "-"),
            "成功" if checkin_ok else "失败",
            "正常" if travel_ok else "失败")}]})

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
            "elements": elements,
        },
    }


def build_text(result: dict) -> dict:
    c = result.get("checkin") or {}
    t = result.get("travel") or {}
    ct, cb = checkin_lines(c)
    tt, tb = travel_lines(t)
    head = "Buddy 每日任务" + (" · 全部完成" if c.get("ok") and t.get("ok") else "")
    body = "{}\n\n{}\n{}\n\n{}\n{}\n\n运行时间：{}".format(
        head, ct, cb.replace("**", ""), tt, tb.replace("**", ""), result.get("run_at", "-"))
    return {"msg_type": "text", "content": {"text": body}}


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------
def gen_sign(timestamp: str, secret: str) -> str:
    string_to_sign = "{}\n{}".format(timestamp, secret)
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send(payload: dict, webhook: str, secret: str = "") -> dict:
    if secret:
        ts = str(int(time.time()))
        payload = dict(payload)
        payload["timestamp"] = ts
        payload["sign"] = gen_sign(ts, secret)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="推送 Buddy 任务结果到飞书")
    ap.add_argument("result", help="cloud_runner.py 产出的 result.json")
    ap.add_argument("--text", action="store_true", help="发纯文本而不是卡片")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发送的 JSON，不真的发")
    ap.add_argument("--quiet-noop", action="store_true",
                    help="这次是「纯确认」（签到幂等跳过 + 旅行今日已派完）就不推送。"
                         "给 cron 兜底用，避免和准点触发那次重复发卡。失败永不静默。")
    args = ap.parse_args(argv[1:])

    with open(args.result, encoding="utf-8") as f:
        result = json.load(f)

    payload = build_text(result) if args.text else build_card(result)

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.quiet_noop and is_noop(result):
        print("本次没有新动作（签到幂等跳过、旅行今日已派完），按 --quiet-noop 跳过推送")
        return 0

    webhook = (os.environ.get(ENV_WEBHOOK) or "").strip()
    if not webhook:
        print("未设置 {}，跳过飞书推送".format(ENV_WEBHOOK), file=sys.stderr)
        return 0

    try:
        resp = send(payload, webhook, (os.environ.get(ENV_SECRET) or "").strip())
    except Exception as e:  # noqa: BLE001
        print("飞书推送失败：{}".format(e), file=sys.stderr)
        return 1

    # 注意：只打印 code/msg，不打印 webhook 地址
    print("飞书推送结果：code={} msg={}".format(resp.get("code"), resp.get("msg")))
    return 0 if resp.get("code") in (0, None) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
