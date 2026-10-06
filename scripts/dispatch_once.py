#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dispatch_once.py — 立刻触发一次 GitHub Actions 工作流（workflow_dispatch）。

为什么需要这个脚本：

    GitHub 的 `schedule` 事件是尽力而为，**不能当定时器用**（原因见 README
    「准点触发」一节）。想要准点，就得有另一个定时器来打 workflow_dispatch
    接口 —— 它走的是实时事件通道，不经过 cron 队列，实测几秒内就开始跑。

    本脚本就是「打这个接口」这个动作本身，三种用法：

      1. 手动验一次：直接跑，几秒后 workflow 就在跑了
      2. 配在 cron-job.org / Cloudflare Workers 之类的服务上：
         用 --show-config 打印该填的 URL、请求头和请求体
      3. 配在本机任务计划程序上：把本脚本当成定时要执行的命令，
         令牌不出本机（推荐，见 README）

令牌从哪来：

    优先 `--token`，其次环境变量 `GH_DISPATCH_TOKEN`（再退回 `GITHUB_TOKEN`）。
    **给第三方定时服务用的令牌要单独建**：fine-grained PAT，
    只勾这一个仓库的 `Actions: write`，不要用 classic PAT 的 `repo` 全权限。

用法：
    export GH_DISPATCH_TOKEN=github_pat_xxx
    python3 dispatch_once.py --repo owner/name
    python3 dispatch_once.py --repo owner/name --show-config
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
DEFAULT_WORKFLOW = ".github/workflows/buddy-reward.yml"


def get_token(cli_token: str | None) -> str:
    for candidate in (cli_token,
                      os.environ.get("GH_DISPATCH_TOKEN"),
                      os.environ.get("GITHUB_TOKEN")):
        if candidate and candidate.strip():
            return candidate.strip()
    raise SystemExit(
        "没有令牌。设置 GH_DISPATCH_TOKEN 环境变量，或用 --token 传入。\n"
        "（需要一个有 `Actions: write` 权限、且只授权目标仓库的 fine-grained PAT）")


def call(token: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "buddy-reward-dispatcher",
    }
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        return e.code, {"detail": detail}


def show_config(repo: str, workflow: str) -> None:
    print("在 cron-job.org（或任何能发 HTTP 请求的定时服务）里这样填：")
    print()
    print("  URL        : https://api.github.com/repos/{}/actions/workflows/{}/dispatches"
          .format(repo, os.path.basename(workflow)))
    print("  Method     : POST")
    print("  Request body (JSON):")
    print('               {"ref": "main"}')
    print("  Headers:")
    print("    Accept        : application/vnd.github+json")
    print("    Authorization : Bearer <你的 fine-grained PAT>")
    print("    Content-Type  : application/json")
    print("    User-Agent    : buddy-reward-dispatcher   (GitHub 要求必须带)")
    print()
    print("  时间       : 每天 07:50")
    print("  ⚠️ 时区     : 服务自己的时区设置要选 Asia/Shanghai（或 UTC+8）。")
    print("               很多服务默认 UTC —— 那就是 15:50，会晚 8 小时。")
    print()
    print("  成功标志   : HTTP 204（无内容）。接口只负责「叫醒」，")
    print("               真正的执行结果去 Actions 页面看。")
    print()
    print("  ℹ️ 令牌是凭据：放在第三方服务上等于把「触发这个仓库工作流」的权限")
    print("     交给它。只授 Actions: write、只限这一个仓库。不想给就别用第三方，")
    print("     改用本机任务计划程序调这个脚本（令牌不出本机）。")


def latest_run_url(token: str, repo: str, workflow: str) -> str | None:
    name = os.path.basename(workflow)
    code, data = call(token, "GET",
                      "/repos/{}/actions/workflows/{}/runs?per_page=1".format(repo, name))
    if code != 200:
        return None
    runs = data.get("workflow_runs") or []
    return runs[0]["html_url"] if runs else None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="触发一次 GitHub Actions 工作流")
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="workflow 文件路径")
    ap.add_argument("--ref", default="main", help="分支（默认 main）")
    ap.add_argument("--token", default=None, help="GitHub 令牌（默认读环境变量）")
    ap.add_argument("--show-config", action="store_true",
                    help="只打印定时服务该填的配置，不真的触发")
    args = ap.parse_args(argv[1:])

    if args.show_config:
        show_config(args.repo, args.workflow)
        return 0

    token = get_token(args.token)
    name = os.path.basename(args.workflow)

    path = "/repos/{}/actions/workflows/{}/dispatches".format(args.repo, name)
    code, data = call(token, "POST", path, {"ref": args.ref})

    if code == 204:
        print("已触发：{}/{} @ {}".format(args.repo, name, args.ref))
        url = latest_run_url(token, args.repo, args.workflow)
        if url:
            print("运行记录：", url)
        else:
            print("（没读到运行记录，去 Actions 页面确认一下）")
        return 0

    if code == 401:
        print("HTTP 401：令牌无效，或没有这个仓库的权限。", file=sys.stderr)
    elif code == 403:
        print("HTTP 403：令牌有效但权限不够 —— 需要 Actions: write。", file=sys.stderr)
    elif code == 404:
        print("HTTP 404：仓库不存在，或令牌看不到它（私有仓库要单独授权）。",
              file=sys.stderr)
    else:
        print("触发失败：HTTP {}".format(code), file=sys.stderr)
    if data.get("detail"):
        print(data["detail"], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
