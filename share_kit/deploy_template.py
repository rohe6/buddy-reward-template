#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Buddy 加油站 · 一键部署
=========================================================================

这个脚本会把「每天自动签到 + 派猫猫旅行，结果推飞书」这套东西
部署到你自己的 GitHub 账号上，之后它每天定时在 GitHub 的免费服务器上跑，
不需要你开着电脑。

你只需要做两件事（脚本会一步步引导）：
  1. 在弹出的浏览器窗口里登录一次 WorkBuddy
  2. 粘贴一个 GitHub 令牌

其余全部自动完成：建仓库、上传代码、写入凭证、跑一次验证、把结果发飞书。

以后再跑这个脚本 = 续期凭证（会话过期时用）。

-------------------------------------------------------------------------
它不会做什么
-------------------------------------------------------------------------
  - 不上传你的 Cookie 到除 GitHub Secret 之外的任何地方
  - 不把 Cookie 写进代码或提交到仓库
  - 不在日志里回显 Cookie 内容（只打印长度和哈希指纹）

-------------------------------------------------------------------------
配置文件位置
-------------------------------------------------------------------------
  ~/.buddy-reward-gha/config.json      记住了仓库名，下次不用重填
  ~/.buddy-reward-gha/browser_profile  浏览器登录态（可随时删掉）
=========================================================================
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

IS_WIN = sys.platform == "win32"
HOME_DIR = os.path.join(os.path.expanduser("~"), ".buddy-reward-gha")
CONFIG_PATH = os.path.join(HOME_DIR, "config.json")
PROFILE_DIR = os.path.join(HOME_DIR, "browser_profile")

API = "https://api.github.com"
WORKBUDDY_URL = "https://www.workbuddy.cn/profile/growth-center"
WORKBUDDY_API = "https://www.workbuddy.cn"

DEFAULT_REPO = "buddy-reward-gha"

# 仓库文件打包后的 base64（由 build_share_kit.py 注入，勿手改）
BUNDLE_B64 = """@@BUNDLE_B64@@"""


# =========================================================================
# 0. 输出小工具
# =========================================================================
def hr(char: str = "-", n: int = 66) -> None:
    print(char * n)


def title(text: str) -> None:
    print()
    hr("=")
    print("  " + text)
    hr("=")


def step(n: int, total: int, text: str) -> None:
    print()
    print("[{}/{}] {}".format(n, total, text))
    hr()


def ok(text: str) -> None:
    print("  ✓ " + text)


def warn(text: str) -> None:
    print("  ! " + text)


def die(text: str, code: int = 1):
    print()
    print("  ✗ " + text)
    print()
    if IS_WIN:
        try:
            input("按回车键关闭…")
        except EOFError:
            pass
    sys.exit(code)


def ask(prompt: str, default: str = "") -> str:
    suffix = " [{}]".format(default) if default else ""
    try:
        val = input("  {}{}: ".format(prompt, suffix)).strip()
    except EOFError:
        val = ""
    return val or default


def read_clipboard() -> str:
    """从剪贴板读文本。

    用它而不是让用户粘贴到终端：几千个字符的 Cookie 粘进控制台有可能被
    行缓冲区截断，而且会明文回显在屏幕上。剪贴板读取既完整又不回显。
    """
    if IS_WIN:
        import ctypes
        import ctypes.wintypes as wt

        CF_UNICODETEXT = 13
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        u32.OpenClipboard.argtypes = [wt.HWND]
        u32.GetClipboardData.restype = wt.HANDLE
        for _ in range(20):
            if u32.OpenClipboard(None):
                break
            time.sleep(0.1)
        else:
            return ""
        try:
            h = u32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            ptr = k32.GlobalLock(h)
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                k32.GlobalUnlock(h)
        finally:
            u32.CloseClipboard()
    if sys.platform == "darwin":
        try:
            return subprocess.run(["pbpaste"], capture_output=True, text=True,
                                  timeout=10).stdout
        except Exception:  # noqa: BLE001
            return ""
    for cmd in (["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"]):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except Exception:  # noqa: BLE001
            continue
    return ""


def ask_secret(prompt: str, allow_clipboard: bool = False) -> str:
    """读一行但不回显。

    allow_clipboard=True 时，直接回车即从剪贴板取——对几千字符的
    Cookie 来说，这比粘进控制台可靠（不会撞上输入缓冲区上限）。
    """
    if allow_clipboard:
        print("  {}（复制好之后直接回车 = 从剪贴板读取）".format(prompt))
    else:
        print("  {}（粘贴后回车，屏幕上不会显示）".format(prompt))

    line = ""
    if IS_WIN:
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # 功能键会带一个前缀字符，丢掉
                msvcrt.getwch()
                continue
            if ch in ("\r", "\n"):
                print()
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\b":
                if line:
                    line = line[:-1]
                continue
            line += ch
    else:
        import getpass
        line = getpass.getpass("  > ")

    line = line.strip()
    if not line and allow_clipboard:
        line = read_clipboard().strip()
        if line:
            print("  （已从剪贴板读到 {} 个字符）".format(len(line)))
    return line


# =========================================================================
# 1. 依赖：加密库 pynacl（GitHub Secret 必须用 libsodium sealed box 加密）
# =========================================================================
_DEP_DIR = os.path.join(HOME_DIR, "pylibs")


def ensure_pynacl() -> None:
    try:
        import nacl  # noqa: F401
        return
    except ImportError:
        pass

    if os.path.isdir(_DEP_DIR):
        sys.path.insert(0, _DEP_DIR)
        try:
            import nacl  # noqa: F401
            return
        except ImportError:
            pass

    print("  需要装一个小库 pynacl（GitHub 写 Secret 要用的加密），只装到本工具自己的目录里…")
    os.makedirs(_DEP_DIR, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
           "--target", _DEP_DIR, "pynacl"]
    p = subprocess.run(cmd)
    if p.returncode != 0 or not os.path.isdir(_DEP_DIR):
        die("装 pynacl 失败。请先确认能联网，或手动执行：\n"
            "     {} -m pip install --target \"{}\" pynacl".format(sys.executable, _DEP_DIR))
    sys.path.insert(0, _DEP_DIR)
    try:
        import nacl  # noqa: F401
    except ImportError:
        die("pynacl 装好了但导入失败，请把上面的命令手动跑一遍看看报错。")
    ok("pynacl 就绪")


# =========================================================================
# 2. 抓 Cookie：拉浏览器 + CDP（纯标准库，不需要 playwright）
# =========================================================================
BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", "") or "", r"Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def find_browser() -> str | None:
    for p in BROWSER_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


class WS:
    """最小 WebSocket 客户端，够 CDP 用即可。"""

    def __init__(self, url: str, timeout: float = 15.0):
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port_s = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port_s or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""

        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = ("GET /{} HTTP/1.1\r\nHost: {}\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: {}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n").format(path, hostport, key)
        self.sock.sendall(req.encode())

        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手时连接被关闭")
            self._buf += chunk
        head, _, self._buf = self._buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise RuntimeError("WebSocket 握手被拒绝：{}".format(status))

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = secrets.token_bytes(4)
        header += mask
        self.sock.sendall(bytes(header) +
                          bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _send_control(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        header.append(0x80 | len(payload))
        mask = secrets.token_bytes(4)
        header += mask
        header += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header))

    def recv_text(self) -> str:
        chunks = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            masked, n = b1 & 0x80, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(n) if n else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise RuntimeError("浏览器关闭了调试连接")
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_control(0x8, b"")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


class CDP:
    def __init__(self, ws: WS):
        self.ws = ws
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 25.0):
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("{} 失败：{}".format(method, msg["error"]))
                return msg.get("result", {})
        raise TimeoutError("{} 响应超时".format(method))


def _http_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _port_alive(port: int) -> bool:
    try:
        d = _http_json("http://127.0.0.1:{}/json/version".format(port), timeout=2.0)
        return bool(d.get("webSocketDebuggerUrl"))
    except Exception:  # noqa: BLE001
        return False


def _read_active_port(profile_dir: str) -> int | None:
    f = os.path.join(profile_dir, "DevToolsActivePort")
    if not os.path.isfile(f):
        return None
    try:
        with open(f, encoding="utf-8") as fh:
            line = fh.readline().strip()
        if line.isdigit() and int(line) > 0:
            return int(line)
    except Exception:  # noqa: BLE001
        pass
    return None


def wait_ws_url(port: int, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            d = _http_json("http://127.0.0.1:{}/json/version".format(port), timeout=3.0)
            if d.get("webSocketDebuggerUrl"):
                return d["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    raise TimeoutError("拿不到浏览器调试地址")


class CookieUnavailable(RuntimeError):
    """自动抓取拿不到可用会话。调用方据此退回「手工粘贴」那条路。"""


# 本机可能已经登录着 WorkBuddy 网页版的浏览器 profile。
# WorkBuddy 自己的「Buddy 旅行」技能就维护着其中一个，仓库里的
# scripts/refresh_credentials.py 也是从那里读会话的 —— 复用同一份登录态，语义一致。
KNOWN_PROFILES = (
    os.path.join(os.path.expanduser("~"), ".workbuddy", "buddy-travel-data", "browser_profile"),
)
# 登录态只需要这三个文件：Cookie 的值由 Local State 里的密钥加密，
# 同一个 Windows 用户可以解，所以复制过去浏览器就能直接读出来。
PROFILE_AUTH_FILES = ("Local State",
                      os.path.join("Default", "Network", "Cookies"),
                      os.path.join("Default", "Preferences"))


def _cookie_db_names(path: str) -> set:
    """只读 Cookie 库里的名字，不解密值 —— 用来判断这份 profile 值不值得折腾。"""
    import sqlite3
    con = sqlite3.connect("file:" + path.replace(os.sep, "/") + "?immutable=1", uri=True)
    try:
        return {r[0] for r in con.execute(
            "select name from cookies where host_key like '%workbuddy%'")}
    finally:
        con.close()


def session_from_local_profile() -> str | None:
    """试着复用本机已经登录好的浏览器会话，成功返回 Cookie 头，失败返回 None。

    为什么值得有这条快路径：默认那条路要在一个全新窗口里手动登录一次。
    但若这台机器上 WorkBuddy 自己就维护着已登录的 profile，这一步可以完全省掉
    （实测 10 秒内拿到可用会话，零交互）。

    做法：只把登录态需要的三个文件复制到临时目录，用浏览器打开那个副本读 Cookie。
    不碰原 profile（那是别的工具在用的），也不影响用户正开着的浏览器窗口。
    任何一步不顺就安静放弃，交给后面那条「弹窗登录」的路 —— 快路径绝不能把主流程带崩。
    """
    exe = find_browser()
    if not exe:
        return None

    for src in KNOWN_PROFILES:
        db = os.path.join(src, "Default", "Network", "Cookies")
        if not os.path.isfile(db):
            continue
        try:
            if "KEYCLOAK_SESSION" not in _cookie_db_names(db):
                continue
        except Exception:  # noqa: BLE001
            continue

        print("  发现本机已有的 WorkBuddy 登录态，先试着直接复用…")
        tmp = tempfile.mkdtemp(prefix="buddy-reuse-")
        proc = None
        try:
            for rel in PROFILE_AUTH_FILES:
                s = os.path.join(src, rel)
                if os.path.isfile(s):
                    d = os.path.join(tmp, rel)
                    os.makedirs(os.path.dirname(d), exist_ok=True)
                    shutil.copy2(s, d)

            # 剥掉代理：从 WorkBuddy 里拉起的 shell 可能带着一个随时失效的本地代理，
            # 浏览器继承后连不上网（读 Cookie 不受影响，但没必要给它添乱）。
            env = {k: v for k, v in os.environ.items()
                   if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
            # ⚠️ 不要加 --headless=new：实测 headless 下读不到 session_2
            # （10 条 cookie、头长 2182、校验 401），可见窗口下正常
            # （9 条、头长 6773、校验通过）。session_2 正是过网关卡的那一个。
            # 窗口挪到屏幕外，免得闪一下打扰用户。
            proc = subprocess.Popen([
                exe, "--remote-debugging-port=0", "--user-data-dir=" + tmp,
                "--no-first-run", "--no-default-browser-check",
                "--window-position=-32000,-32000", "--window-size=800,600",
                WORKBUDDY_URL,
            ], env=env)

            port = None
            deadline = time.time() + 25
            while time.time() < deadline:
                p = _read_active_port(tmp)
                if p and _port_alive(p):
                    port = p
                    break
                time.sleep(0.3)
            if not port:
                return None

            cdp = CDP(WS(wait_ws_url(port), timeout=40.0))

            # 轮询而不是睡固定时长：Cookie 什么时候从库里读出来没有保证，
            # 拿到一份就校验一份，通过了立刻走人。
            last_tried = None
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    cookies = cdp.call("Storage.getCookies").get("cookies", []) or []
                except Exception:  # noqa: BLE001
                    time.sleep(1.0)
                    continue
                ours = [c for c in cookies if "workbuddy.cn" in (c.get("domain") or "")]
                if not ours:
                    time.sleep(1.0)
                    continue
                header = "; ".join("{}={}".format(c["name"], c["value"]) for c in ours)
                if header != last_tried:
                    last_tried = header
                    good, _ = validate_cookie(header)
                    if good:
                        print("  复用成功，不用再登录了。")
                        return header
                time.sleep(1.5)
            print("  那份登录态已经失效，继续走登录流程。")
            return None
        except Exception:  # noqa: BLE001
            return None
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
    return None


def capture_cookie(required: str = "KEYCLOAK_SESSION", wait_seconds: int = 420) -> str:
    """拉起专用浏览器窗口，等一个**真的能用**的会话，返回 Cookie 头。

    ⚠️ 判据绝不能用「某个 Cookie 名字出现了」。
    实测（全新 profile、完全没有登录）打开页面两三秒内就会带上 29 条 Cookie，
    里面赫然包括 KEYCLOAK_SESSION / KEYCLOAK_IDENTITY / AUTH_SESSION_ID /
    session / session_2 —— 那只是一份**匿名会话**，调接口只会回 401。

    按名字判断的后果很严重：脚本会在两三秒内"抓取成功" → 校验失败 →
    顺手把浏览器窗口关掉 → 用户根本没机会登录，只看到窗口一闪而过。
    所以这里**只有 validate_cookie 通过才算数**。

    另外：成功后**不关闭**这个窗口。一是 profile 会保留登录态，下次跑直接复用；
    二是超时后用户还能在窗口里慢慢登录再重跑。
    """
    exe = find_browser()
    if not exe:
        die("本机没找到 Edge 或 Chrome。请先装一个浏览器再重跑本脚本。")

    os.makedirs(PROFILE_DIR, exist_ok=True)

    proc = None
    port = _read_active_port(PROFILE_DIR)
    if port and _port_alive(port):
        ok("复用上次留下的浏览器窗口")
    else:
        if port:
            # 上一次留下的端口文件可能指向一个已经退出的浏览器，
            # 不清掉的话本次会连到死端口。
            try:
                os.remove(os.path.join(PROFILE_DIR, "DevToolsActivePort"))
            except OSError:
                pass
        proc = subprocess.Popen([
            exe,
            "--remote-debugging-port=0",
            "--user-data-dir=" + PROFILE_DIR,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,msEdgeTranslate",
            "--disable-session-crashed-bubble",
            WORKBUDDY_URL,
        ])
        deadline = time.time() + 30
        while time.time() < deadline:
            p = _read_active_port(PROFILE_DIR)
            if p and _port_alive(p):
                port = p
                break
            time.sleep(0.3)
        if not port:
            die("浏览器起来了但调试端口没通。\n"
                "     常见原因：这个工具之前开的浏览器窗口还开着。\n"
                "     请把那个窗口全部关掉，然后重新运行本脚本。")

    ws = WS(wait_ws_url(port), timeout=40.0)
    cdp = CDP(ws)

    print()
    hr("*")
    print("  👉 请在刚弹出的浏览器窗口里登录 WorkBuddy")
    print("     登录完成后什么都不用做，脚本会自动继续。")
    print("     （这个窗口里可能已经显示着一个「未登录」的页面，")
    print("       那是匿名会话，直接登录即可，不用管它。）")
    hr("*")
    print()
    print("  正在等待登录…", end="", flush=True)

    deadline = time.time() + wait_seconds
    last_tried = None
    rejected_note = False
    dots = 0
    try:
        while time.time() < deadline:
            try:
                cookies = cdp.call("Storage.getCookies").get("cookies", []) or []
            except Exception:  # noqa: BLE001
                time.sleep(1.0)
                continue

            ours = [c for c in cookies if "workbuddy.cn" in (c.get("domain") or "")]
            names = {c["name"] for c in ours}
            header = None
            if required in names:
                header = "; ".join("{}={}".format(c["name"], c["value"]) for c in ours)

            # 只在 Cookie 组合**发生变化**时才去调接口，别每 1.5 秒打一次
            if header and header != last_tried:
                last_tried = header
                good, detail = validate_cookie(header)
                if good:
                    print(" 有效")
                    print("  （这个浏览器窗口先留着：profile 会记住登录，"
                          "下次运行可以直接复用。）")
                    return header
                if not rejected_note:
                    rejected_note = True
                    print()
                    warn("这个窗口当前是一份**匿名会话**（未登录也会有 KEYCLOAK_SESSION，"
                         "所以不能只看 Cookie 名字）。")
                    warn("接口返回：{}".format(detail[:80]))
                    print("     → 请在窗口里完成登录，脚本会继续等。", flush=True)

            dots += 1
            if dots % 8 == 0:
                print(".", end="", flush=True)
            time.sleep(1.5)
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
        # 注意：这里刻意**不** terminate 浏览器，理由见函数开头。

    raise CookieUnavailable(
        "等了 {} 秒还没等到一个可用的会话。\n"
        "     那个浏览器窗口还开着，你可以登录好之后重新运行本脚本（会直接复用）。\n"
        "     也可以按下面的办法手工复制一次 Cookie。".format(wait_seconds))


def validate_cookie(header: str) -> tuple[bool, str]:
    """只读校验：checkin-status 不产生任何副作用。"""
    req = urllib.request.Request(
        WORKBUDDY_API + "/billing/meter/checkin-status",
        data=b"{}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"),
            "Referer": WORKBUDDY_URL,
            "Origin": WORKBUDDY_API,
            "X-Client-Platform": "web",
            "Content-Type": "application/json",
            "Cookie": header,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "replace")
            return (r.status == 200), raw[:200]
    except urllib.error.HTTPError as e:
        return False, "HTTP {} {}".format(e.code, e.read().decode("utf-8", "replace")[:160])
    except Exception as e:  # noqa: BLE001
        return False, "{}: {}".format(type(e).__name__, e)


COOKIE_PASTE_HINT = """
  自动抓取没成功。退一步，用剪贴板复制一次也可以（只需要这一次）：

    1. 在浏览器里打开并登录  https://www.workbuddy.cn/profile/growth-center
    2. 按 F12 打开开发者工具，切到「网络 / Network」标签
    3. 刷新页面，在请求列表里点任意一条 workbuddy.cn 的请求
    4. 找到「请求标头 / Request Headers」
    5. 把 Cookie 那一行的**值**（等号右边开始到最后，很大一坨）右键复制
"""


# =========================================================================
# 3. GitHub
# =========================================================================
def _git_env() -> dict:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
                "GIT_ASKPASS": "echo", "SSH_ASKPASS": "echo"})
    return env


def token_from_env() -> str:
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return ""


def token_from_windows_store() -> str:
    if not IS_WIN:
        return ""
    import ctypes
    import ctypes.wintypes as wt

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
            ("Comment", wt.LPWSTR), ("LastWritten", ctypes.c_ulonglong),
            ("CredentialBlobSize", wt.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
            ("UserName", wt.LPWSTR),
        ]

    adv = ctypes.windll.advapi32
    adv.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredReadW.restype = wt.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]

    for target in ("git:https://github.com", "git:https://www.github.com"):
        pc = ctypes.POINTER(CREDENTIAL)()
        if not adv.CredReadW(target, 1, 0, ctypes.byref(pc)):
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
                return text.splitlines()[0].strip()
        finally:
            adv.CredFree(pc)
    return ""


class GH:
    def __init__(self, token: str):
        self.token = token
        self.hdr = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "buddy-reward-deploy",
        }

    def call(self, method: str, path: str, body: dict | None = None, allow: tuple = ()):
        data = json.dumps(body).encode() if body is not None else None
        h = dict(self.hdr)
        if data:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(API + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if e.code in allow:
                try:
                    return e.code, json.loads(raw)
                except Exception:  # noqa: BLE001
                    return e.code, {}
            msg = raw[:300]
            try:
                msg = json.loads(raw).get("message", msg)
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError("GitHub API {} {} -> {} {}".format(method, path, e.code, msg))


def get_github_token() -> tuple[GH, str]:
    """返回 (客户端, 登录名)。"""
    tok = token_from_env()
    if not tok:
        tok = token_from_windows_store()
        if tok:
            print("  在本机凭据管理器里找到了 GitHub 令牌，先用它试试…")

    if not tok:
        print("  需要一个 GitHub 令牌（Personal Access Token）。")
        print("  点开下面这个链接，范围已经帮你勾好了，点最下面的绿色按钮生成，")
        print("  然后复制那一串 ghp_ 开头的字符粘进来：")
        print()
        print("    https://github.com/settings/tokens/new"
              "?scopes=repo,workflow&description=buddy-reward-gha")
        print()
        if IS_WIN:
            try:
                os.startfile("https://github.com/settings/tokens/new"
                             "?scopes=repo,workflow&description=buddy-reward-gha")
            except Exception:  # noqa: BLE001
                pass
        tok = ask_secret("复制好令牌之后", allow_clipboard=True)

    if not tok:
        die("没有令牌就没法自动部署。")

    gh = GH(tok)
    try:
        _, me = gh.call("GET", "/user")
    except RuntimeError as e:
        die("令牌无效或没有权限：{}\n"
            "     请重新生成一个勾选 repo 和 workflow 的令牌。".format(e))
    return gh, me["login"]


def secret_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def set_secret(gh: GH, repo: str, name: str, value: str) -> None:
    from nacl import encoding, public
    _, key = gh.call("GET", "/repos/{}/actions/secrets/public-key".format(repo))
    pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(value.encode("utf-8"))
    gh.call("PUT", "/repos/{}/actions/secrets/{}".format(repo, name), {
        "encrypted_value": base64.b64encode(sealed).decode(),
        "key_id": key["key_id"],
    })


def list_secret_names(gh: GH, repo: str) -> set:
    _, d = gh.call("GET", "/repos/{}/actions/secrets".format(repo))
    return {s["name"] for s in d.get("secrets", [])}


def load_bundle() -> dict:
    raw = base64.b64decode(BUNDLE_B64)
    out = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            out[m.name] = tf.extractfile(m).read()
    return out


def upload_files(gh: GH, repo: str, files: dict) -> str:
    """用 git-data API 一次性提交全部文件。"""
    _, ref = gh.call("GET", "/repos/{}/git/ref/heads/main".format(repo), allow=(404, 409))
    parent_sha = (ref or {}).get("object", {}).get("sha")

    entries = []
    for path in sorted(files):
        content = files[path]
        _, blob = gh.call("POST", "/repos/{}/git/blobs".format(repo), {
            "content": base64.b64encode(content).decode(), "encoding": "base64",
        })
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    tree_body = {"tree": entries}
    if parent_sha:
        _, parent = gh.call("GET", "/repos/{}/git/commits/{}".format(repo, parent_sha))
        tree_body["base_tree"] = parent["tree"]["sha"]

    _, tree = gh.call("POST", "/repos/{}/git/trees".format(repo), tree_body)

    commit_body = {"message": "Buddy daily reward automation", "tree": tree["sha"]}
    if parent_sha:
        commit_body["parents"] = [parent_sha]
    _, commit = gh.call("POST", "/repos/{}/git/commits".format(repo), commit_body)

    if parent_sha:
        gh.call("PATCH", "/repos/{}/git/refs/heads/main".format(repo), {"sha": commit["sha"]})
    else:
        gh.call("POST", "/repos/{}/git/refs".format(repo),
                {"ref": "refs/heads/main", "sha": commit["sha"]})
    return commit["sha"]


def dispatch_and_wait(gh: GH, repo: str, timeout: int = 420) -> dict:
    _, runs = gh.call("GET", "/repos/{}/actions/runs?per_page=1".format(repo))
    before = runs["workflow_runs"][0]["id"] if runs.get("workflow_runs") else None

    # 刚推上去的 workflow 文件，GitHub 要几秒钟才认；这里重试几轮
    for i in range(12):
        try:
            gh.call("POST",
                    "/repos/{}/actions/workflows/buddy-reward.yml/dispatches".format(repo),
                    {"ref": "main"})
            break
        except RuntimeError:
            if i == 11:
                raise
            time.sleep(5)
    print("  已触发一次运行，等待结果…")

    run_id = None
    for _ in range(30):
        time.sleep(3)
        _, runs = gh.call("GET", "/repos/{}/actions/runs?per_page=3".format(repo))
        cand = [r for r in runs.get("workflow_runs", []) if r["id"] != before]
        if cand:
            run_id = cand[0]["id"]
            break
    if not run_id:
        raise TimeoutError("触发后没看到新的运行记录")

    deadline = time.time() + timeout
    while time.time() < deadline:
        _, r = gh.call("GET", "/repos/{}/actions/runs/{}".format(repo, run_id))
        if r["status"] == "completed":
            return r
        time.sleep(5)
    raise TimeoutError("运行超时未结束，可去 Actions 页面自己看")


def fetch_result_json(gh: GH, repo: str, run_id: int) -> dict | None:
    _, arts = gh.call("GET", "/repos/{}/actions/runs/{}/artifacts".format(repo, run_id))
    for a in arts.get("artifacts", []):
        if a["name"] != "buddy-reward-result":
            continue
        req = urllib.request.Request(
            "{}/repos/{}/actions/artifacts/{}/zip".format(API, repo, a["id"]),
            headers=gh.hdr)

        class NoAuth(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, rq, fp, code, msg, headers, newurl):
                n = super().redirect_request(rq, fp, code, msg, headers, newurl)
                if n is not None:
                    n.headers = {k: v for k, v in n.headers.items()
                                 if k.lower() != "authorization"}
                return n

        op = urllib.request.build_opener(NoAuth)
        with op.open(req, timeout=90) as r:
            blob = r.read()
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(blob))
        if "result.json" in zf.namelist():
            return json.loads(zf.read("result.json").decode())
    return None


def feishu_sign(timestamp: str, secret: str) -> str:
    """飞书的签名算法：key 是 "timestamp\\nsecret"，待签名内容是空串。"""
    import hashlib as _h
    import hmac as _hmac
    string_to_sign = "{}\n{}".format(timestamp, secret)
    digest = _hmac.new(string_to_sign.encode("utf-8"), digestmod=_h.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def try_feishu(webhook: str, sign_secret: str) -> tuple[bool, str]:
    """发一条测试消息，确认 Webhook 是通的。"""
    body = {
        "msg_type": "interactive",
        "card": {
            "header": {"template": "blue",
                       "title": {"tag": "plain_text", "content": "Buddy 自动签到 · 部署测试"}},
            "elements": [{"tag": "div", "text": {
                "tag": "lark_md",
                "content": "**部署成功**，以后每天定时自动签到 + 派猫猫，结果会发到这个群。"}}],
        },
    }
    if sign_secret:
        ts = str(int(time.time()))
        body["timestamp"] = ts
        body["sign"] = feishu_sign(ts, sign_secret)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
            return d.get("code") == 0, json.dumps(d, ensure_ascii=False)[:200]
    except Exception as e:  # noqa: BLE001
        return False, "{}: {}".format(type(e).__name__, e)


# =========================================================================
# 4. 主流程
# =========================================================================
TOTAL = 6


def main() -> int:
    title("Buddy 加油站 · 一键部署")

    print("""
  这个脚本会帮你：
    1. 在你自己的 GitHub 账号下建一个仓库（默认私有）
    2. 把每天的签到 + 派猫猫自动化代码放进去
    3. 把登录凭证存成仓库的 Secret（不会写进代码）
    4. 把飞书机器人接上
    5. 立刻跑一次，把结果发到你飞书

  不需要你开着电脑，之后 GitHub 每天定时替你跑。
""")

    config = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:  # noqa: BLE001
            config = {}
    renewing = bool(config.get("repo"))

    step(1, TOTAL, "检查本机环境")
    if sys.version_info < (3, 8):
        die("Python 版本太低（当前 {}），需要 3.8 以上。".format(sys.version.split()[0]))
    ok("Python {}".format(sys.version.split()[0]))
    if not find_browser():
        die("没找到 Edge 或 Chrome，脚本需要用它来读取登录状态。")
    ok("浏览器就绪")
    ensure_pynacl()

    step(2, TOTAL, "连接 GitHub")
    gh, login = get_github_token()
    ok("已登录 GitHub：{}".format(login))

    if renewing:
        repo_name = config["repo"].split("/")[-1]
        ok("这次是「续期凭证」，目标仓库：{}".format(config["repo"]))
    else:
        repo_name = ask("仓库名（直接用默认就行）", DEFAULT_REPO)
    full_repo = "{}/{}".format(login, repo_name)

    step(3, TOTAL, "准备仓库")
    status, _ = gh.call("GET", "/repos/{}".format(full_repo), allow=(404,))
    if status == 404:
        vis = ask("仓库要公开还是私有？public / private", "private")
        _, created = gh.call("POST", "/user/repos", {
            "name": repo_name,
            "private": vis.lower().startswith("priv"),
            "description": "Buddy 加油站每日签到 + 派猫猫，结果推送飞书",
            "auto_init": False,
        })
        ok("已创建仓库：{}（{}）".format(full_repo, "私有" if created.get("private") else "公开"))
    else:
        ok("仓库已存在，直接用：{}".format(full_repo))

    step(4, TOTAL, "读取 WorkBuddy 登录状态")
    # 两条路，优先零交互的那条：
    #   1) 复用本机 WorkBuddy 自己维护的已登录 profile（有的话，10 秒搞定）
    #   2) 弹一个专用窗口让用户登录（capture_cookie 内部已校验，拿不到有效会话
    #      就抛 CookieUnavailable，再退回手工粘贴）
    # 这里刻意不做「按 Cookie 名字判断 + 重试三次」——旧写法会在两三秒内
    # 抓到一份匿名会话（未登录也有 KEYCLOAK_SESSION），然后把关掉的窗口
    # 当成"用户没登录"，纯属自己制造故障。
    cookie = session_from_local_profile()
    if cookie:
        ok("凭证有效（长度 {}，指纹 {}）".format(len(cookie), secret_fingerprint(cookie)))
    else:
        try:
            cookie = capture_cookie()
            ok("凭证有效（长度 {}，指纹 {}）".format(len(cookie), secret_fingerprint(cookie)))
        except CookieUnavailable as e:
            warn(str(e))
            print(COOKIE_PASTE_HINT)
            cookie = ask_secret("复制好 Cookie 之后", allow_clipboard=True)
            if not cookie:
                die("没读到内容，没有凭证就没法继续。")
            good, detail = validate_cookie(cookie)
            if not good:
                die("这组 Cookie 依然不通过：{}\n"
                    "     确认一下是不是在 www.workbuddy.cn 登录成功后再复制的。".format(detail[:200]))
            ok("凭证有效（长度 {}，指纹 {}）".format(len(cookie), secret_fingerprint(cookie)))

    step(5, TOTAL, "写入仓库 Secret 与代码")
    existing = list_secret_names(gh, full_repo)

    bundle = load_bundle()
    digest = hashlib.sha256(BUNDLE_B64.encode()).hexdigest()
    if not config.get("bundle_sha"):
        sha = upload_files(gh, full_repo, bundle)
        ok("已上传 {} 个文件（commit {}）".format(len(bundle), sha[:8]))
    else:
        ok("代码已在仓库里，跳过上传")

    set_secret(gh, full_repo, "WB_COOKIE", cookie)
    ok("WB_COOKIE 已写入（长度 {}，指纹 {}）".format(
        len(cookie), secret_fingerprint(cookie)))

    if "FEISHU_WEBHOOK" not in existing:
        print()
        print("  飞书机器人（可选，但强烈建议）：")
        print("    在飞书群里 → 设置 → 群机器人 → 添加机器人 → 自定义机器人")
        print("    复制它的 Webhook 地址，形如")
        print("    https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx")
        print("    如果机器人的「签名校验」是开着的，把那个密钥也一起复制过来。")
        print()
        webhook = ask_secret("复制好 Webhook 之后（想跳过就输入 skip）",
                             allow_clipboard=True)
        if webhook.lower() == "skip":
            webhook = ""
        sign_secret = ""
        if webhook:
            sign_secret = ask_secret("复制好签名密钥之后（没开签名校验就直接回车）",
                                     allow_clipboard=True)
            good, detail = try_feishu(webhook, sign_secret)
            if good:
                ok("飞书推送测试成功，去群里看看有没有收到卡片")
            else:
                warn("飞书推送测试没成功：{}".format(detail))
                warn("先继续部署，之后可以在 GitHub 仓库的 Settings → Secrets 里修正")
            set_secret(gh, full_repo, "FEISHU_WEBHOOK", webhook)
            ok("FEISHU_WEBHOOK 已写入")
            if sign_secret:
                set_secret(gh, full_repo, "FEISHU_SIGN_SECRET", sign_secret)
                ok("FEISHU_SIGN_SECRET 已写入")
    else:
        ok("飞书已配置过，跳过")

    os.makedirs(HOME_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"repo": full_repo, "bundle_sha": digest}, f, ensure_ascii=False, indent=2)

    step(6, TOTAL, "跑一次验证")
    try:
        run = dispatch_and_wait(gh, full_repo)
    except Exception as e:  # noqa: BLE001
        warn("等运行结果时出错：{}".format(e))
        print("  可以直接去这里看：https://github.com/{}/actions".format(full_repo))
        run = None

    if run:
        res = fetch_result_json(gh, full_repo, run["id"])
        print("  运行结论：{}".format(run["conclusion"]))
        if res:
            c, t = res.get("checkin", {}), res.get("travel", {})
            print("    签到：{}（{}）".format("成功" if c.get("ok") else "失败",
                                            c.get("status") or c.get("reason", "")))
            print("    猫猫：{}（{}）".format("成功" if t.get("ok") else "失败",
                                            t.get("status") or t.get("reason", "")))
        if res and res.get("credential_expired"):
            print()
            print("  提示：凭证在云端被拒绝。如果本机校验是过的，")
            print("        多半是 Secret 没写完整，重跑一次本脚本即可。")

    title("部署完成")
    print("""
  仓库地址：https://github.com/{repo}
  运行记录：https://github.com/{repo}/actions

  之后它会每天自动跑一次（目标北京时间 07:50），不需要你管。

  ⚠️ 但 GitHub 自己的定时器不准时：2026-08 起平台侧 cron 大面积积压，
     实测每天会晚 5 小时左右才跑。晚点跑不影响领取（签到幂等），
     只是飞书卡片会来得晚。想准点见仓库 README 的「准点触发」一节。

  ⚠️ 凭证（Cookie）有有效期，大概一两周。过期后飞书会收到红色卡片，
     那时候把本脚本再双击跑一次就行 —— 不用改任何配置。

  本机留下的东西（都在 {home}）：
    config.json        记住了仓库名
    browser_profile/   浏览器登录态，可随时删掉
    pylibs/            装 pynacl 用的目录
  删除整个 {home} 目录 = 彻底清理本机痕迹（不影响已部署的 GitHub 仓库）。
""".format(repo=full_repo, home=HOME_DIR))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print("  已取消。")
        sys.exit(130)
