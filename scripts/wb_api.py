#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb_api.py — Buddy 加油站 / Buddy 旅行 统一 HTTP 客户端（纯标准库）

只做三件事：拼请求、发请求、解析 JSON。不含任何业务判断。

鉴权（重要，以实际抓包为准）：
    签到与旅行两组接口都由 https://www.workbuddy.cn 提供，
    使用**网页版登录会话**鉴权——即浏览器里的 Cookie（Keycloak 会话）。
    请求不需要 Authorization 头，也不需要 X-User-Id。

    凭证从环境变量读取，绝不落盘、绝不打印：
        WB_COOKIE         形如 "a=1; b=2; c=3" 的完整 Cookie 头
        WB_COOKIE_FILE    可选，改为从文件读取 Cookie 头（用于本机调试）

约定：
    call() 返回 (http_status, parsed_json, raw_text)
    - 传输层失败（DNS/连接/超时）抛 TransportError
    - HTTP 4xx/5xx 不抛异常，原样返回状态码与解析结果
    - 响应非 JSON 时 parsed_json 为 {"__non_json__": True}
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = "https://www.workbuddy.cn"

DEFAULT_TIMEOUT = 25

# 与浏览器一致的 UA：接口侧会校验客户端平台，UA 保持真实浏览器串最稳
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
)

REFERER = BASE + "/profile/growth-center"

ENV_COOKIE = "WB_COOKIE"
ENV_COOKIE_FILE = "WB_COOKIE_FILE"


class TransportError(RuntimeError):
    """未收到 HTTP 响应：DNS / 连接 / 超时 / TLS 等。"""


class CredentialMissing(RuntimeError):
    """环境里没有可用凭证。"""


def _read_cookie_file(path: str) -> str:
    """读取凭证文件。兼容两种写法：
      1) 直接放 Cookie 头本身
      2) refresh_credentials.py 生成的 KEY='value' env 文件（自动挑出 WB_COOKIE 行并去引号）
    """
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(ENV_COOKIE + "="):
            value = line[len(ENV_COOKIE) + 1:].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return text


def load_cookie() -> str:
    """从环境变量 / 文件读取 Cookie 头。找不到时抛 CredentialMissing。"""
    raw = (os.environ.get(ENV_COOKIE) or "").strip()
    if not raw:
        path = os.environ.get(ENV_COOKIE_FILE) or ""
        if path and os.path.isfile(path):
            raw = _read_cookie_file(path).strip()
    if not raw:
        raise CredentialMissing(
            "未找到凭证：请设置环境变量 {}（网页版会话 Cookie）。".format(ENV_COOKIE)
        )
    return raw


def call(
    method: str,
    path: str,
    body: dict | None = None,
    cookie: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    ua: str = DEFAULT_UA,
) -> tuple[int, dict, str]:
    """发一次请求，返回 (http_status, parsed_json, raw_text)。"""
    url = path if path.startswith("http") else BASE + path
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
        "User-Agent": ua,
        "Referer": REFERER,
        "Origin": BASE,
        "X-Client-Platform": "web",
        "Cookie": cookie if cookie is not None else load_cookie(),
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _parse(raw), raw
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        return e.code, _parse(raw), raw
    except Exception as e:  # noqa: BLE001
        raise TransportError(str(e))


def get(path: str, **kw) -> tuple[int, dict, str]:
    return call("GET", path, body=None, **kw)


def post(path: str, body: dict | None = None, **kw) -> tuple[int, dict, str]:
    return call("POST", path, body=body if body is not None else {}, **kw)


def _parse(raw: str) -> dict:
    try:
        if not raw:
            return {}
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"__non_json__": True}


# ---------------------------------------------------------------------------
# 脱敏：用于把原始返回安全地展示/落盘
# ---------------------------------------------------------------------------
_SENSITIVE_KEYS = (
    "cookie", "authorization", "access_token", "accesstoken", "refresh_token",
    "refreshtoken", "id_token", "idtoken", "session", "token", "secret",
)


def mask_text(raw: str, head: int = 1200) -> str:
    """截断 + 去掉可能夹带的凭证类字段，便于贴到聊天/日志里。"""
    if not raw:
        return ""
    text = raw
    if len(text) > head:
        text = text[:head] + "…（已截断，共 {} 字节）".format(len(raw))
    low = text.lower()
    for k in _SENSITIVE_KEYS:
        idx = low.find('"' + k + '"')
        if idx >= 0:
            text = text[:idx] + '"' + k + '":"<masked>"'
            break
    return text
