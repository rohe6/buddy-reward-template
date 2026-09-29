#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh_credentials.py — 本机运行：把「本机登录态」导出为云端可用的凭证，注入 GitHub 仓库 Secret

为什么需要它：
    GitHub Actions 的 runner 上没有任何本机登录态，所以签到/旅行所需的网页会话
    必须由这台常开过或偶尔开机的电脑导出，转成仓库 Secret 给 Actions 用。

它做什么：
    1. 读取本机的 WorkBuddy 网页版登录会话（浏览器 Cookie）
    2. 只读校验一次（调 checkin-status，不产生任何领取动作）
    3. 写入本机凭据文件（默认 ~/.workbuddy/buddy-reward-gha/credentials.env，权限收紧）
    4. 注入 GitHub 仓库 Secret：
       - 有 gh CLI 且已登录 → gh secret set（值走 stdin，不进命令行、不进日志）
       - 否则有 GH_TOKEN + pynacl → GitHub REST API（libsodium sealed box 加密）
       - 都没有 → 打印手动配置步骤，不自动上传

安全约定（务必保持）：
    - 明文 Cookie / Token 绝不打印到终端、绝不写入仓库目录、绝不提交
    - 只在内存与本机凭据文件之间流转；上传 GitHub 时走加密通道
    - 输出里只有「长度」「指纹前 8 位」「校验结果」

用法：
    python3 refresh_credentials.py --repo <owner>/<repo>
    python3 refresh_credentials.py --repo <owner>/<repo> --secret FEISHU_WEBHOOK
    python3 refresh_credentials.py --cookie-file cookie.txt --repo <owner>/<repo>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wb_api  # noqa: E402

SECRET_NAME = "WB_COOKIE"
DEFAULT_STATE_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "buddy-reward-gha")

DOMAIN_HINTS = ("workbuddy.cn", "codebuddy.cn")

# 默认候选：本机可能存有 WorkBuddy 网页版会话的 Chromium profile
PROFILE_CANDIDATES = [
    # WorkBuddy「Buddy 旅行」技能使用的持久化 profile
    os.path.join(os.path.expanduser("~"), ".workbuddy", "buddy-travel-data", "browser_profile"),
]

BROWSER_CANDIDATES = {
    "win32": [
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "darwin": [
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ],
    "linux": ["/usr/bin/microsoft-edge", "/usr/bin/google-chrome", "/usr/bin/chromium"],
}


def fingerprint(value: str) -> str:
    """只暴露不可逆指纹，用于确认「换没换」。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def find_browser() -> str | None:
    for p in BROWSER_CANDIDATES.get(sys.platform, []):
        if os.path.isfile(p):
            return p
    return shutil.which("msedge") or shutil.which("google-chrome") or shutil.which("chromium")


# ---------------------------------------------------------------------------
# 1. 取本机网页会话 Cookie
# ---------------------------------------------------------------------------
def cookies_from_profile(profile: str, browser: str | None, wait_ms: int = 800) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "缺少 playwright。请执行：\n"
            "  pip install -r requirements-refresh.txt\n"
            "然后在技能目录执行一次 buddy_travel.py --login 完成登录。"
        )
    if not os.path.isdir(profile):
        raise SystemExit("找不到浏览器 profile 目录：{}".format(profile))

    with sync_playwright() as p:
        kwargs = dict(
            user_data_dir=profile,
            headless=False,  # 该站点在 headless 下会清掉会话，必须保持有头
            args=["--no-first-run", "--no-default-browser-check"],
        )
        if browser:
            kwargs["executable_path"] = browser
        ctx = p.chromium.launch_persistent_context(**kwargs)
        try:
            ctx.pages and ctx.pages[0].wait_for_timeout(wait_ms)
            return ctx.cookies()
        finally:
            ctx.close()


def cookies_from_file(path: str) -> list[dict]:
    """支持三种格式：Netscape cookie jar / JSON 数组 / 原始 Cookie 头字符串。"""
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("cookies") or []
        return data
    if text.startswith("# Netscape") or "\t" in text:
        out = []
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out.append({"domain": parts[0], "path": parts[2],
                            "secure": parts[3].upper() == "TRUE",
                            "expires": float(parts[4]), "name": parts[5], "value": parts[6]})
        return out
    # 原始 "a=1; b=2" 形式
    out = []
    for kv in text.split(";"):
        kv = kv.strip()
        if "=" in kv:
            k, v = kv.split("=", 1)
            out.append({"domain": "www.workbuddy.cn", "path": "/", "secure": True,
                        "expires": 0, "name": k, "value": v})
    return out


def cookie_header(cookies: list[dict]) -> str:
    keep = [c for c in cookies if any(h in (c.get("domain") or "") for h in DOMAIN_HINTS)]
    keep.sort(key=lambda c: -len(c.get("path") or "/"))
    return "; ".join("{}={}".format(c["name"], c["value"]) for c in keep)


# ---------------------------------------------------------------------------
# 2. 校验凭证（只读）
# ---------------------------------------------------------------------------
def verify(header: str) -> tuple[bool, str]:
    try:
        code, body, _ = wb_api.post("/billing/meter/checkin-status", {}, cookie=header)
    except wb_api.TransportError as e:
        return False, "网络异常：{}".format(e)
    if code == 401:
        return False, "HTTP 401 —— 本机会话已失效，请先在浏览器/技能里重新登录一次"
    if code != 200:
        return False, "HTTP {}".format(code)
    if body.get("code") != 0:
        return False, "业务码 {}：{}".format(body.get("code"), body.get("msg"))
    data = body.get("data") or {}
    return True, "OK（今日已签到={}，连续 {} 天）".format(
        data.get("today_checked_in"), data.get("streak_days"))


# ---------------------------------------------------------------------------
# 3. 注入 GitHub Secret
# ---------------------------------------------------------------------------
def gh_available() -> bool:
    if not shutil.which("gh"):
        return False
    try:
        return subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def find_git() -> str:
    """定位 git 可执行文件。

    本机（Windows + WorkBuddy 自带 PortableGit）git 通常不在 PATH 里，
    所以除了 which 之外还要探几个已知位置。
    """
    found = shutil.which("git")
    if found:
        return found
    candidates = [
        os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "PortableGit",
                     "versions", "1.2.0", "mingw64", "bin", "git.exe"),
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "git"


def _noninteractive_env() -> dict:
    """禁止 git / 凭据管理器弹窗，否则无人值守时会挂死。"""
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_ASKPASS": "echo",
        "SSH_ASKPASS": "echo",
    })
    return env


def token_from_windows_credential_manager() -> str:
    """直接读 Windows 凭据管理器里的 GitHub 令牌（不走 git 子进程）。

    Git Credential Manager 把令牌以 UTF-16LE 存在 target
    `git:https://github.com` 下面。直接 CredRead 比开 git 子进程稳得多：
    实测 `git credential fill` 在无人值守场景会弹凭据管理器窗口并一直
    挂住，直到超时。

    注意：令牌只在本进程内使用，不打印、不落盘。
    """
    if sys.platform != "win32":
        return ""
    import ctypes
    import ctypes.wintypes as wt

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wt.DWORD),
            ("Type", wt.DWORD),
            ("TargetName", wt.LPWSTR),
            ("Comment", wt.LPWSTR),
            ("LastWritten", ctypes.c_ulonglong),
            ("CredentialBlobSize", wt.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wt.DWORD),
            ("AttributeCount", wt.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wt.LPWSTR),
            ("UserName", wt.LPWSTR),
        ]

    adv = ctypes.windll.advapi32
    adv.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredReadW.restype = wt.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]

    CRED_TYPE_GENERIC = 1
    for target in ("git:https://github.com", "git:https://www.github.com"):
        pc = ctypes.POINTER(CREDENTIAL)()
        if not adv.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pc)):
            continue
        try:
            c = pc.contents
            blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
            for enc in ("utf-16-le", "utf-8"):
                try:
                    text = blob.decode(enc).strip()
                except Exception:  # noqa: BLE001
                    continue
                if not text:
                    continue
                if text.startswith("{"):
                    try:
                        d = json.loads(text)
                    except Exception:  # noqa: BLE001
                        d = {}
                    for k in ("accessToken", "access_token", "password", "token"):
                        v = d.get(k)
                        if isinstance(v, str) and v.strip():
                            return v.strip()
                if "\n" in text:
                    return text.splitlines()[0].strip()
                return text
        finally:
            adv.CredFree(pc)
    return ""


def token_from_git_credential(host: str = "github.com") -> str:
    """兜底：走 `git credential fill`。

    本机没有 gh CLI、也没设 GH_TOKEN 时，这条路仍在：
    只要曾经 git push 过一次并登录过，Git Credential Manager 里就存着
    一个 scope 含 repo/workflow 的 OAuth 令牌，足够写 Actions Secret。

    注意：令牌只在本进程内使用，不打印、不落盘。
    """
    git = find_git()
    try:
        proc = subprocess.run(
            [git, "credential", "fill"],
            input="protocol=https\nhost={}\n\n".format(host),
            capture_output=True, text=True, timeout=45,
            env=_noninteractive_env(),
        )
    except Exception:  # noqa: BLE001
        return ""
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


def set_secret_gh(repo: str, name: str, value: str) -> bool:
    """值通过 stdin 传入，不进入命令行参数，也不会被 shell 记录。"""
    proc = subprocess.run(
        ["gh", "secret", "set", name, "-R", repo],
        input=value.encode("utf-8"), capture_output=True,
    )
    if proc.returncode != 0:
        print("  gh secret set 失败：{}".format(proc.stderr.decode("utf-8", "replace").strip()))
        return False
    return True


def set_secret_api(repo: str, name: str, value: str, token: str) -> bool:
    """GitHub REST API：需要 pynacl 做 libsodium sealed box 加密。"""
    try:
        import base64

        from nacl import encoding, public
    except ImportError:
        print("  未安装 pynacl，无法走 REST 通道。可执行：pip install pynacl")
        return False

    import urllib.request

    api = "https://api.github.com/repos/{}/actions/secrets/".format(repo)
    hdrs = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "buddy-reward-refresh",
    }

    def req(url: str, method: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        with urllib.request.urlopen(r, timeout=25) as resp:
            return json.loads(resp.read().decode() or "{}")

    try:
        pk = req(api + "public-key", "GET")
        pub = public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
        sealed = public.SealedBox(pub).encrypt(value.encode("utf-8"))
        req(api + name, "PUT", {
            "encrypted_value": base64.b64encode(sealed).decode(),
            "key_id": pk["key_id"],
        })
        return True
    except Exception as e:  # noqa: BLE001
        print("  REST 写入失败：{}".format(e))
        return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="导出本机 WorkBuddy 网页会话并注入 GitHub Secret")
    ap.add_argument("--repo", default="", help="owner/repo；缺省时从当前 git remote 推断")
    ap.add_argument("--profile", default="", help="Chromium profile 目录")
    ap.add_argument("--browser", default="", help="浏览器可执行文件路径")
    ap.add_argument("--cookie-file", default="", help="改为从文件读取 Cookie（Netscape/JSON/原始头）")
    ap.add_argument("--secret", action="append", default=[],
                    help="额外要注入的 Secret 名（值取自同名环境变量），可重复")
    ap.add_argument("--env-file", default="", help="本机凭据文件路径")
    ap.add_argument("--no-upload", action="store_true", help="只导出并校验，不上传")
    args = ap.parse_args(argv[1:])

    state_dir = DEFAULT_STATE_DIR
    os.makedirs(state_dir, exist_ok=True)
    env_file = args.env_file or os.path.join(state_dir, "credentials.env")

    # --- 取 Cookie ---
    if args.cookie_file:
        print("[1/4] 从文件读取 Cookie：{}".format(args.cookie_file))
        cookies = cookies_from_file(args.cookie_file)
    else:
        profile = args.profile
        if not profile:
            for cand in PROFILE_CANDIDATES:
                if os.path.isdir(cand):
                    profile = cand
                    break
        if not profile:
            print("找不到默认 profile，请用 --profile 指定，或用 --cookie-file 直接给 Cookie。")
            return 2
        print("[1/4] 打开本机浏览器 profile 读取会话：{}".format(profile))
        cookies = cookies_from_profile(profile, args.browser or find_browser())
    header = cookie_header(cookies)
    if not header:
        print("没有取到任何 workbuddy/codebuddy 域下的 Cookie。请先登录一次网页版。")
        return 2
    print("      取到 {} 条相关 Cookie，Cookie 头长度 {}，指纹 {}".format(
        len([c for c in cookies if any(h in (c.get("domain") or "") for h in DOMAIN_HINTS)]),
        len(header), fingerprint(header)))

    # --- 校验 ---
    print("[2/4] 只读校验（checkin-status，无副作用）…")
    ok, msg = verify(header)
    print("      {}".format(msg))
    if not ok:
        print("凭证不可用，已停止，不会写入任何地方。")
        return 2

    # --- 落本机 ---
    # 注意：Cookie 头里含 ";" 与空格，直接写成 KEY=value 会被 shell 当成命令分隔符，
    # 既把值截断、又会把明文打到终端。因此这里统一用单引号包裹。
    def shell_quote(value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"

    print("[3/4] 写入本机凭据文件：{}".format(env_file))
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("# 由 refresh_credentials.py 生成，请勿提交到仓库\n")
        f.write("# 本机自测用法：set -a; . \"{}\"; set +a\n".format(env_file))
        f.write("{}={}\n".format(SECRET_NAME, shell_quote(header)))
        for name in args.secret:
            val = os.environ.get(name, "")
            if val:
                f.write("{}={}\n".format(name, shell_quote(val)))
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass

    if args.no_upload:
        print("[4/4] 已按 --no-upload 跳过上传。")
        return 0

    # --- 上传 ---
    repo = args.repo
    if not repo:
        try:
            url = subprocess.run([find_git(), "remote", "get-url", "origin"],
                                 capture_output=True, text=True).stdout.strip()
            if "github.com" in url:
                repo = url.split("github.com")[-1].lstrip(":/").removesuffix(".git")
        except Exception:  # noqa: BLE001
            pass

    targets = [(SECRET_NAME, header)]
    for name in args.secret:
        val = os.environ.get(name, "")
        if val:
            targets.append((name, val))
        else:
            print("  跳过 {}：环境变量里没有值".format(name))

    print("[4/4] 注入 GitHub Secret（仓库：{}）…".format(repo or "未指定"))
    if not repo:
        print("  未指定仓库，无法自动上传。可加 --repo owner/repo 重跑。")
        return 0

    if gh_available():
        print("  使用 gh CLI 通道")
        for name, value in targets:
            if set_secret_gh(repo, name, value):
                print("  ✓ {} 已写入（长度 {}，指纹 {}）".format(name, len(value), fingerprint(value)))
    else:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        via = "GH_TOKEN 环境变量"
        if not token:
            token = token_from_windows_credential_manager()
            via = "Windows 凭据管理器"
        if not token:
            token = token_from_git_credential()
            via = "git credential fill"
        if token:
            print("  使用 GitHub REST API 通道（令牌来源：{}）".format(via))
            for name, value in targets:
                if set_secret_api(repo, name, value, token):
                    print("  ✓ {} 已写入（长度 {}，指纹 {}）".format(
                        name, len(value), fingerprint(value)))
        else:
            print("  三个通道都没走通：没有 gh CLI、没有 GH_TOKEN、本机也没存过 GitHub 令牌。")
            print("  两条路任选：")
            print("    A) 先在该仓库执行一次 git push 并登录，让凭据管理器存下令牌，再重跑本脚本")
            print("    B) 打开 https://github.com/{}/settings/secrets/actions".format(repo))
            print("       新建 Repository secret，名称 {}，值从下面这个文件里复制：".format(SECRET_NAME))
            print("       {}".format(env_file))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
