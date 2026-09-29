#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_share_kit.py — 生成「发给朋友的一键部署包」。

分享包必须是**单个自包含文件**（朋友的机器上什么都没有，不能依赖 clone），
但代码的唯一来源应该是本仓库。所以这里把仓库里真正要在云端跑的文件
打成一个 tar.gz、base64 后注入 share_kit/deploy_template.py，
在 dist/ 下生成两个成品：

    dist/buddy-reward-deploy.py   真正的部署脚本（单文件，可直接发给别人）
    dist/一键部署.cmd             Windows 双击入口（自动找 Python）

仓库改了以后重跑一次本脚本即可。

用法：
    python3 build_share_kit.py
    python3 build_share_kit.py --out /some/other/dir
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "share_kit", "deploy_template.py")

# 这些文件不该被打进分享包：构建工具自己，以及构建产物
EXCLUDE_PREFIXES = ("share_kit/", "dist/")
EXCLUDE_FILES = {"build_share_kit.py"}

GIT_FALLBACKS = [
    r"C:\Program Files\Git\cmd\git.exe",
    os.path.join(os.environ.get("USERPROFILE", ""),
                 r".workbuddy\binaries\PortableGit\versions\1.2.0\mingw64\bin\git.exe"),
]


def find_git() -> str:
    found = shutil.which("git")
    if found:
        return found
    for c in GIT_FALLBACKS:
        if c and os.path.isfile(c):
            return c
    return "git"


def runtime_files(git: str) -> list[str]:
    """仓库里真正要在云端跑的文件（受版本控制，排除构建工具）。"""
    p = subprocess.run([git, "ls-files"], cwd=HERE, capture_output=True, text=True)
    files = []
    for line in p.stdout.splitlines():
        f = line.strip()
        if not f or f in EXCLUDE_FILES:
            continue
        if any(f.startswith(pre) for pre in EXCLUDE_PREFIXES):
            continue
        files.append(f)
    if not files:
        raise SystemExit("没找到任何文件，确认本脚本放在仓库根目录、且 git 可用")
    return sorted(files)


def blob_bytes(git: str, path: str) -> bytes:
    """取 HEAD 里这个文件的规范内容，**而不是工作区的原始字节**。

    为什么不直接 open() 读文件：工作区可能含有仓库里并不存在的差异。
    本仓库真的踩过 —— `scripts/cloud_runner.py` 工作区是 CRLF、`.gitattributes`
    写的是 `* text=auto eol=lf`，两边不一致而且 `git status` 还看着是干净的
    （git 只在规范化之后比较，所以显示不出来）。那时打出来的分享包，
    发出去的字节和仓库里的并不相同。

    读 HEAD 的 blob 就没有这个问题：分享包永远等于某一次提交的内容。
    """
    p = subprocess.run([git, "cat-file", "blob", f"HEAD:{path}"], cwd=HERE,
                       capture_output=True)
    if p.returncode != 0:
        raise SystemExit(
            "读不到 HEAD:{} —— 文件可能还没提交。\n{}".format(
                path, p.stderr.decode("utf-8", "replace").strip()))
    return p.stdout


def build_bundle(git: str, files: list[str]) -> str:
    """打成 tar.gz 再 base64。

    固定 mtime / uid / gid / uname，保证同样内容每次产出的字节一致，
    这样「分享包指纹」才有意义。
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tf:
        for path in files:
            data = blob_bytes(git, path)
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


# .cmd 内容全部是 ASCII，绕开中文 Windows 上批处理的编码坑。
# 所有中文提示都交给 Python 脚本打印（那边 UTF-8 处理是可靠的）。
CMD_TEMPLATE = r"""@echo off
setlocal
title Buddy Reward - One Click Deploy

set "HERE=%~dp0"
set "SCRIPT=%HERE%buddy-reward-deploy.py"
set "PY="

if not exist "%SCRIPT%" (
  echo.
  echo   ERROR: buddy-reward-deploy.py not found next to this file.
  echo   Keep both files in the same folder.
  echo.
  pause
  exit /b 1
)

REM --- 1. explicit override ------------------------------------------------
if defined BUDDY_PYTHON if exist "%BUDDY_PYTHON%" set "PY=%BUDDY_PYTHON%"

REM --- 2. Python bundled with the WorkBuddy desktop app --------------------
if not defined PY call :scan_workbuddy

REM --- 3. py launcher / python on PATH -------------------------------------
REM     resolved to a full path so the invocation never needs odd quoting
if not defined PY call :resolve "py -3"
if not defined PY call :resolve "python"
if not defined PY call :resolve "python3"

REM --- 4. usual install locations -----------------------------------------
if not defined PY call :scan_common

if not defined PY (
  echo.
  echo   ERROR: no Python found on this machine.
  echo.
  echo   Install Python 3 first ^(python.org, tick "Add Python to PATH"^),
  echo   or run this on a machine that has the WorkBuddy desktop app installed.
  echo.
  pause
  exit /b 1
)

echo.
echo   Using Python: %PY%
echo.

"%PY%" "%SCRIPT%"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   Script exited with code %RC%.
)
echo.
pause
exit /b %RC%


:resolve
REM Turn a command like "py -3" into the real interpreter path.
for /f "delims=" %%P in ('%~1 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
exit /b 0

:scan_workbuddy
REM WorkBuddy desktop ships its own Python; newest version first.
for /f "delims=" %%V in ('dir /b /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions" 2^>nul') do (
  if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%V\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%V\python.exe"
  )
)
exit /b 0

:scan_common
for %%D in (
  "%LOCALAPPDATA%\Programs\Python"
  "C:\Python313" "C:\Python312" "C:\Python311" "C:\Python310"
  "C:\Program Files\Python313" "C:\Program Files\Python312" "C:\Program Files\Python311"
  "D:\Python" "D:\PYTHON"
) do (
  if not defined PY if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
  if not defined PY for /f "delims=" %%V in ('dir /b /ad /o-n "%%~D" 2^>nul') do (
    if not defined PY if exist "%%~D\%%V\python.exe" set "PY=%%~D\%%V\python.exe"
  )
)
exit /b 0
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="生成一键部署分享包")
    ap.add_argument("--out", default=os.path.join(HERE, "dist"), help="输出目录")
    args = ap.parse_args(argv[1:])

    git = find_git()

    files = runtime_files(git)
    print("打包 {} 个文件（内容取自 HEAD，不是工作区原始字节）：".format(len(files)))
    for f in files:
        print("   -", f)

    bundle = build_bundle(git, files)
    print("\nbundle base64 长度：{}".format(len(bundle)))

    tmpl = open(TEMPLATE, encoding="utf-8").read()
    if "@@BUNDLE_B64@@" not in tmpl:
        raise SystemExit("模板 {} 里找不到 @@BUNDLE_B64@@ 占位符".format(TEMPLATE))
    out_py = tmpl.replace("@@BUNDLE_B64@@", bundle)

    # 生成物先做一次语法检查，别把坏文件发给别人
    compile(out_py, "buddy-reward-deploy.py", "exec")

    os.makedirs(args.out, exist_ok=True)
    py_path = os.path.join(args.out, "buddy-reward-deploy.py")
    with open(py_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(out_py)

    cmd_path = os.path.join(args.out, "一键部署.cmd")
    with open(cmd_path, "w", encoding="ascii", newline="\r\n") as f:
        f.write(CMD_TEMPLATE)

    print("\n生成：")
    for p in (py_path, cmd_path):
        print("   {}  ({:,} 字节)".format(p, os.path.getsize(p)))

    digest = hashlib.sha256(out_py.encode("utf-8")).hexdigest()[:12]
    print("\n分享包指纹（仓库内容变了这个值就会变）：{}".format(digest))
    print("把这【两个文件放在同一个文件夹】里发给朋友，让他双击「一键部署.cmd」即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
