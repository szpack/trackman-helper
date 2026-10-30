#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pydeps.py — 找一个装了可选依赖（pyodbc / uiautomation）的 Python 解释器。

为什么需要这层：
  看板可能被**任意**一个 python.exe 拉起来 —— 双击 .bat 时 .bat 找到哪个、
  IDE 里跑的是哪个、用户自己的 venv 是哪个，都不受控。而 pyodbc 与 uiautomation
  往往只装在其中某一个里。缺了它们，球杆和 Normalize 会静默降级到又慢又不可靠的
  兜底路径（PowerShell 拉库、读 layout 文件），界面上看起来就像"没实现"。

策略：
  1) 当前解释器能 import 就别折腾；
  2) 否则按候选清单逐个试（py 启动器 / PATH 上的 python / 常见安装目录 /
     WorkBuddy 自带 Python），谁能 import 就用谁；
  3) 结果按模块组合缓存，探测只在第一次发生。

对外只有两个函数：
  has(*mods)          -> 当前解释器有没有
  find_argv(mods)     -> 能 import 这些模块的解释器启动参数（list），找不到返回 None
"""

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

_CACHE = {}
_LOCK = threading.Lock()
_BAD = set()

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _candidates():
    """按"更可能是对的"排序的候选解释器启动参数列表。"""
    out = []

    def add(*argv):
        if argv and argv[0] and argv not in out:
            out.append(list(argv))

    # 1) 当前解释器
    if sys.executable:
        add(sys.executable)

    # 2) PATH 上的 py 启动器 / python
    for name in ("py", "python", "python3", "python3.13", "python3.12", "python3.11"):
        p = shutil.which(name)
        if not p:
            continue
        if Path(p).name.lower() == "py.exe":
            add(p, "-3")          # py 启动器必须带版本参数才会吐出真解释器
        else:
            add(p)

    # 3) 常见安装位置
    home = os.environ.get("USERPROFILE") or str(Path.home())
    globs = [
        (os.environ.get("LOCALAPPDATA") or "", "Programs/Python/Python3*/python.exe"),
        (os.environ.get("ProgramFiles") or "", "Python3*/python.exe"),
        (os.environ.get("ProgramFiles(x86)") or "", "Python3*/python.exe"),
        ("C:/", "Python3*/python.exe"),
        # WorkBuddy 自带的两个都看一眼：版本目录是完整解释器，envs 是虚拟环境
        (home, ".workbuddy/binaries/python/versions/*/python.exe"),
        (home, ".workbuddy/binaries/python/envs/*/Scripts/python.exe"),
    ]
    for root, pat in globs:
        if not root:
            continue
        try:
            for p in sorted(Path(root).glob(pat), reverse=True):
                add(str(p))
        except OSError:
            continue

    # 4) 兜底：venv 里的 python
    add(str(Path(home) / "miniconda3" / "python.exe"))
    add(str(Path(home) / "anaconda3" / "python.exe"))
    return out


def _check(argv, mods):
    """跑一次 `python -c "import a, b"` 判断依赖在不在。"""
    code = "import " + ", ".join(mods)
    try:
        p = subprocess.run([*argv, "-c", code], capture_output=True,
                           timeout=15, creationflags=_NO_WINDOW)
        return p.returncode == 0
    except Exception:                      # noqa: BLE001
        return False


def has(*mods):
    """当前解释器有没有这些模块。"""
    for m in mods:
        try:
            __import__(m)
        except Exception:                  # noqa: BLE001
            return False
    return True


def find_argv(mods):
    """找一个能 import 这些模块的解释器，返回启动参数（list），找不到返回 None。"""
    mods = tuple(mods)
    key = ",".join(mods)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    hit = None
    for argv in _candidates():
        sig = " ".join(argv)
        if sig in _BAD:
            continue
        if _check(argv, mods):
            hit = argv
            break
        _BAD.add(sig)
    with _LOCK:
        _CACHE[key] = hit
    return hit


def describe(argv):
    """给日志用的短名。"""
    if not argv:
        return "无"
    return Path(argv[0]).name + (" " + " ".join(argv[1:]) if len(argv) > 1 else "")


if __name__ == "__main__":
    import json
    print("当前:", sys.executable)
    for mods in (("pyodbc",), ("uiautomation",)):
        a = find_argv(mods)
        print(f"{mods[0]:14s} -> {describe(a)}   {a}")
    print(json.dumps({"has_pyodbc": has("pyodbc"),
                      "has_uia": has("uiautomation")}, ensure_ascii=False))
