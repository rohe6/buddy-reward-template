#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_runner.py — 云端一次性执行「Buddy 加油站签到 + Buddy 旅行」

在 GitHub Actions 的 runner 上运行；不使用任何第三方依赖（纯标准库）。
凭证只从环境变量 WB_COOKIE 读取，不落盘、不打印。

执行顺序（严格按需求）：
    1. 签到（幂等：今日已签到不报错）
    2. 旅行：
       a. 查状态
       b. state = arrived/returned/finished → 先 claim 领掉已到家的奖励
       c. claim 后重新查状态：idle 且 daily_limit_reached=false 才 depart
          —— 今天已经派过（daily_limit_reached=true）就不派
       d. state = traveling → 不重复派遣
    3. 汇总 JSON 输出到 stdout，并可写到 --out 指定的文件

退出码：
    0  签到成功 / 今日已签到 ——「签到成功即算成功」
    1  签到失败（或凭证失效）——旅行结果不影响该判断

用法：
    python3 cloud_runner.py --out result.json [--raw-out raw.json] [--status-only]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wb_api  # noqa: E402

CST = datetime.timezone(datetime.timedelta(hours=8))

CHECKIN_STATUS_PATH = "/billing/meter/checkin-status"
CHECKIN_PATH = "/billing/meter/daily-checkin"

TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"

CODE_OK = 0
CODE_ALREADY_CHECKED = 10001  # 服务端「今日已签到」的业务码

# state 取值 → 归一到三个内部状态
ARRIVED_STATES = ("arrived", "returned", "finished", "done")
IDLE_STATES = ("idle",)



# HTTP 401 说明（如实描述，别再只甩一句「会话过期」把人带偏）：
#
# 401 是网关层直接拒绝，可能有两种原因——
#   1) Cookie 真的过期了
#   2) Secret 里的值不完整 / 粘贴错位（截断后的值仍非空，401 长得一模一样）
#
# 工作流里的 Preflight 步骤会先打印长度与 sha256 指纹，用来区分这两种情况。
CRED_401_HINT = (
    "凭证被网关拒绝（HTTP 401）：Cookie 已过期，"
    "或 Secret 里的值不完整（写入时被截断）。"
    "先看本次运行 Preflight 打印的长度与指纹："
    "长度只有几百、或指纹与本机刷新时打印的不一致 → 是 Secret 没注入对，"
    "在本机重跑一次刷新/部署脚本即可；"
    "两者都吻合 → 才是会话真的过期，重新登录 WorkBuddy 后再注入。"
)


def _now() -> dict:
    now = datetime.datetime.now(CST)
    return {"run_at": now.isoformat(timespec="seconds"), "run_date": now.strftime("%Y-%m-%d")}


def _fail(reason: str, **extra) -> dict:
    out = {"ok": False, "reason": reason}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# 签到
# ---------------------------------------------------------------------------
def run_checkin(raw_sink: list, status_only: bool = False) -> dict:
    """返回 {ok, status, ...}；ok 表示「签到这件事今天已经完成了」。"""
    # 1) 只读探测（today_checked_in 历史上有不可靠记录，仅作参考，不作唯一依据）
    try:
        st_code, st_body, st_raw = wb_api.post(CHECKIN_STATUS_PATH, {})
    except wb_api.TransportError as e:
        return _fail("查询签到状态失败（网络异常）：" + str(e))
    raw_sink.append({"name": "checkin-status", "http": st_code, "body": st_body, "raw": st_raw})

    if st_code == 401:
        return _fail(CRED_401_HINT, credential_expired=True)
    if st_code != 200:
        return _fail("查询签到状态失败：HTTP {}".format(st_code))

    probe = st_body.get("data") or {}
    result_base = {
        "today_checked_in": probe.get("today_checked_in"),
        "streak_days": probe.get("streak_days"),
        "activity_active": probe.get("active"),
    }

    if status_only:
        return {"ok": True, "status": "status_only", **result_base}

    # 2) 执行签到（幂等，服务端用 code=10001 表示今日已领）
    try:
        c_code, c_body, c_raw = wb_api.post(CHECKIN_PATH, {})
    except wb_api.TransportError as e:
        return _fail("签到请求失败（网络异常）：" + str(e), **result_base)
    raw_sink.append({"name": "daily-checkin", "http": c_code, "body": c_body, "raw": c_raw})

    if c_code == 401:
        return _fail(CRED_401_HINT, credential_expired=True, **result_base)

    # 注意：**不要拿 HTTP 状态码判成败**。今日已签到时服务端返回的是
    # HTTP 400 + body {"code":10001,"msg":"今天已签到，请明天再来"}。
    # 业务码才是唯一依据。
    code = c_body.get("code")
    data = c_body.get("data") or {}
    msg = c_body.get("msg") or ""

    if code is None:
        return _fail("签到请求失败：HTTP {}，响应不是预期 JSON".format(c_code), **result_base)

    if code == CODE_OK:
        return {
            "ok": True,
            "status": "success",
            "credit": data.get("credit"),
            "streak_days": data.get("streak_days"),
            "message": msg or "签到成功",
            **{k: v for k, v in result_base.items() if k != "streak_days"},
        }
    if code == CODE_ALREADY_CHECKED or "已签" in msg or "already" in msg.lower():
        return {"ok": True, "status": "already_checked",
                "message": msg or "今日已签到", **result_base}
    return _fail("签到未成功：HTTP {} code={} msg={}".format(c_code, code, msg), **result_base)


# ---------------------------------------------------------------------------
# 旅行
# ---------------------------------------------------------------------------
def _travel_status():
    code, body, raw = wb_api.get(TRAVEL_STATUS_PATH)
    return code, (body.get("data") or {}), body, raw


def _seconds_left(data: dict) -> int | None:
    arrive_at = data.get("arrive_at")
    server_now = data.get("server_now")
    if isinstance(arrive_at, int) and isinstance(server_now, int):
        return max(0, arrive_at - server_now)
    return None


def run_travel(raw_sink: list, status_only: bool = False) -> dict:
    """返回 {ok, status, ...}。任何异常都返回 failed，不抛给上层。"""
    try:
        code, data, body, raw = _travel_status()
    except wb_api.TransportError as e:
        return _fail("查询旅行状态失败（网络异常）：" + str(e))
    raw_sink.append({"name": "travel/status", "http": code, "body": body, "raw": raw})

    if code == 401:
        return _fail(CRED_401_HINT, credential_expired=True)
    if body.get("code") is None:
        return _fail("查询旅行状态失败：HTTP {}（响应不是预期 JSON）".format(code))
    if body.get("code") != CODE_OK:
        return _fail("状态接口业务错误：code={} msg={}".format(body.get("code"), body.get("msg")))

    state = data.get("state")
    daily_limit_reached = bool(data.get("daily_limit_reached"))
    info = {
        "state": state,
        "daily_limit_reached": daily_limit_reached,
        "location": (data.get("location") or {}).get("name"),
        "record_id": data.get("record_id"),
        "remaining_seconds": _seconds_left(data),
    }

    if status_only:
        return {"ok": True, "status": "status_only", **info}

    claimed = None

    # ---- 第一步：先领掉已经到家的奖励 ----
    if state in ARRIVED_STATES:
        try:
            c_code, c_body, c_raw = wb_api.post(TRAVEL_CLAIM_PATH, {})
        except wb_api.TransportError as e:
            return _fail("领取旅行奖励失败（网络异常）：" + str(e), **info)
        raw_sink.append({"name": "travel/claim", "http": c_code, "body": c_body, "raw": c_raw})

        if c_code == 401:
            return _fail("领取旅行奖励失败：" + CRED_401_HINT, credential_expired=True, **info)
        if c_body.get("code") != CODE_OK:
            return _fail("领取旅行奖励失败：HTTP {} code={} msg={}".format(
                c_code, c_body.get("code"), c_body.get("msg")), **info)

        cd = c_body.get("data") or {}
        claimed = {
            "reward_credit": cd.get("reward_credit"),
            "record_id": cd.get("record_id") or info.get("record_id"),
            "location": info.get("location"),
        }

        # 领完后重新取状态，再决定要不要派新的一趟
        try:
            code2, data, body2, raw2 = _travel_status()
        except wb_api.TransportError as e:
            return {"ok": True, "status": "claimed",
                    "message": "已领取奖励，但复查状态失败：" + str(e), **claimed, **info}
        raw_sink.append({"name": "travel/status(after-claim)", "http": code2,
                         "body": body2, "raw": raw2})
        if body2.get("code") == CODE_OK:
            state = data.get("state")
            daily_limit_reached = bool(data.get("daily_limit_reached"))
            info.update({
                "state": state,
                "daily_limit_reached": daily_limit_reached,
                "remaining_seconds": _seconds_left(data),
            })

    # ---- 第二步：判断能不能派新的一趟 ----
    if state in IDLE_STATES:
        if daily_limit_reached:
            out = {"ok": True, "status": "daily_limit_reached",
                   "message": "今日旅行次数已完成，不重复派遣", **info}
            if claimed:
                out["claimed"] = claimed
                out["status"] = "claimed_then_skip"
                out["message"] = "已领取到家的奖励；今日已派过，不再派遣"
            return out
        try:
            d_code, d_body, d_raw = wb_api.post(TRAVEL_DEPART_PATH, {"location_id": 1})
        except wb_api.TransportError as e:
            return _fail("派遣失败（网络异常）：" + str(e), **info)
        raw_sink.append({"name": "travel/depart", "http": d_code, "body": d_body, "raw": d_raw})

        if d_code == 401:
            return _fail("派遣失败：" + CRED_401_HINT, credential_expired=True, **info)
        if d_body.get("code") != CODE_OK:
            return _fail("派遣失败：HTTP {} code={} msg={}".format(
                d_code, d_body.get("code"), d_body.get("msg")), **info)

        dd = d_body.get("data") or {}
        out = {
            "ok": True,
            "status": "departed",
            "record_id": dd.get("record_id"),
            "location": (dd.get("location") or {}).get("name") or info.get("location"),
            "arrive_at": dd.get("arrive_at"),
            "message": "已派出新的一趟，等它回家",
            **{k: v for k, v in info.items() if k not in ("state", "record_id", "location")},
        }
        if claimed:
            out["claimed"] = claimed
            out["status"] = "claimed_then_departed"
        return out

    if state == "traveling":
        out = {"ok": True, "status": "traveling", "message": "猫猫在路上，不重复派遣", **info}
        if claimed:
            out["claimed"] = claimed
        return out

    out = _fail("未知旅行状态：state={}".format(state), **info)
    if claimed:
        out["claimed"] = claimed
    return out


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Buddy 加油站签到 + Buddy 旅行（云端执行）")
    ap.add_argument("--out", default="", help="把结果 JSON 写到该文件")
    ap.add_argument("--raw-out", default="", help="把脱敏后的原始返回写到该文件")
    ap.add_argument("--status-only", action="store_true", help="只读查询，不做任何领取/派遣")
    args = ap.parse_args(argv[1:])

    raw_sink: list = []
    result = _now()

    try:
        wb_api.load_cookie()
    except wb_api.CredentialMissing as e:
        result["checkin"] = _fail(str(e), credential_expired=True)
        result["travel"] = _fail(str(e), credential_expired=True)
        result["credential_expired"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    # 签到在前：签到成功就算成功，旅行失败不影响这个结论
    result["checkin"] = run_checkin(raw_sink, status_only=args.status_only)
    result["travel"] = run_travel(raw_sink, status_only=args.status_only)
    result["credential_expired"] = bool(
        result["checkin"].get("credential_expired") or result["travel"].get("credential_expired")
    )
    result["checkin_ok"] = bool(result["checkin"].get("ok"))
    result["travel_ok"] = bool(result["travel"].get("ok"))

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")

    if args.raw_out:
        dump = {
            "note": "以下为接口原始返回，已截断并脱敏；凭证类字段不会出现",
            "responses": [
                {"name": r["name"], "http": r["http"],
                 "raw": wb_api.mask_text(r["raw"], 1500)}
                for r in raw_sink
            ],
        }
        with open(args.raw_out, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=2)

    return 0 if result["checkin_ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
