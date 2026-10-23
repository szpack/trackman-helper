"""版本号的唯一来源。

规则：
- `VERSION` 文件是**唯一真相**，CI 每次发版会把它 +1 并提交回仓库。
- 本地开发时，如果工作区有改动、或 HEAD 不在 tag 上，会自动加 `-dev.N+g<sha>`
  后缀，这样看板上一眼能看出跑的是不是发版代码。
- 没有 git、也没有 VERSION 时返回 `0.0.0`，绝不抛异常——版本号不该弄挂看板。
"""

import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(HERE, "VERSION")

_RE_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _git(*args, timeout=3):
    """跑一条 git 命令，失败一律返回空串。"""
    try:
        out = subprocess.run(
            ["git", *args], cwd=HERE, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def base_version():
    """`VERSION` 文件里的稳定版本号，如 `1.2.3`。"""
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            s = f.read().strip()
    except OSError:
        return "0.0.0"
    return s if _RE_SEMVER.match(s) else "0.0.0"


def git_info():
    """返回 (提交数, 短 sha, 是否脏)；拿不到就给空值。"""
    sha = _git("rev-parse", "--short", "HEAD")
    if not sha:
        return 0, "", False
    count = _git("rev-list", "--count", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    return int(count) if count.isdigit() else 0, sha, dirty


def full_version():
    """给界面和日志看的完整版本号。

    发版构建（CI 在 tag 上跑）→ `1.2.3`
    本地开发（有改动 / 或有新提交未发版）→ `1.2.3-dev.7+g3f2a1bc`
    """
    base = base_version()
    count, sha, dirty = git_info()
    if not sha:
        return base
    ahead = _git("rev-list", "--count", f"v{base}..HEAD")
    is_ahead = ahead.isdigit() and int(ahead) > 0
    if not is_ahead and not dirty:
        return base
    tag = f"dev.{ahead}" if is_ahead else "dev"
    return f"{base}-{tag}+g{sha}" + ("-dirty" if dirty else "")


def summary():
    """给 /api/status 用的一小撮信息。"""
    base = base_version()
    count, sha, dirty = git_info()
    return {
        "version": full_version(),
        "base": base,
        "commit": sha,
        "commits": count,
        "dirty": dirty,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
