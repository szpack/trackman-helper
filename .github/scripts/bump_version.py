#!/usr/bin/env python3
"""按提交信息自动决定下一个版本号（语义化版本）。

规则（Conventional Commits）：
  - 提交信息带 `!`（如 `feat!: 换掉推送协议`）或正文含 `BREAKING CHANGE` → major
  - `feat:` / `feat(scope):`                                     → minor
  - 其它（fix / docs / chore / refactor / perf ...）              → patch
  - 仓库还没有任何 tag（首次发版）                                → 直接用 VERSION，不递增

用法：
  python .github/scripts/bump_version.py            # 打印将要发布的版本
  python .github/scripts/bump_version.py --write     # 同时改写 VERSION 文件
  python .github/scripts/bump_version.py --write --github-output   # 供 Actions 用
"""

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSION_FILE = os.path.join(ROOT, "VERSION")

_RE_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_RE_BREAK = re.compile(r"^\w+(\([^)]*\))?!:", re.M)
_RE_FEAT = re.compile(r"^feat(\([^)]*\))?:", re.M)
_SKIP = ("chore(release)",)


def git(*args):
    out = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        sys.exit(f"git {' '.join(args)} 失败：{out.stderr.strip()}")
    return out.stdout.strip()


def read_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            s = f.read().strip()
    except OSError:
        sys.exit("找不到 VERSION 文件")
    m = _RE_SEMVER.match(s)
    if not m:
        sys.exit(f"VERSION 内容不是 x.y.z 格式：{s!r}")
    return tuple(int(x) for x in m.groups())


def last_tag():
    """最近一个 vX.Y.Z tag，没有就返回 None。"""
    tags = git("tag", "--list", "v*", "--sort=-v:refname").splitlines()
    for t in tags:
        if _RE_SEMVER.match(t.lstrip("v")):
            return t
    return None


def messages_since(tag):
    """tag 之后的所有提交标题 + 正文；没有 tag 就看全历史。"""
    rng = f"{tag}..HEAD" if tag else "HEAD"
    raw = git("log", rng, "--pretty=format:%B%x00")
    out = []
    for chunk in raw.split("\x00"):
        s = chunk.strip()
        if not s or s.startswith(_SKIP):
            continue
        out.append(s)
    return out


def decide(msgs):
    """返回 (bump 类型, 触发原因)。"""
    if any("BREAKING CHANGE" in m or _RE_BREAK.search(m.splitlines()[0]) for m in msgs):
        return "major", "有破坏性变更"
    if any(_RE_FEAT.search(m.splitlines()[0]) for m in msgs):
        return "minor", "有新功能"
    return "patch", "只有修复/维护"


def bump(cur, kind):
    major, minor, patch = cur
    if kind == "major":
        return major + 1, 0, 0
    if kind == "minor":
        return major, minor + 1, 0
    return major, minor, patch + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="把新版本写回 VERSION")
    ap.add_argument("--github-output", action="store_true",
                    help="把结果写进 $GITHUB_OUTPUT")
    ap.add_argument("--force", choices=["major", "minor", "patch"],
                    help="忽略提交信息，强制某种递增")
    args = ap.parse_args()

    cur = read_version()
    tag = last_tag()

    if tag is None:
        # 首次发版：VERSION 就是发布版本，不递增
        new, kind, why = cur, "initial", "仓库还没有 tag，首次发版"
    else:
        msgs = messages_since(tag)
        if not msgs:
            print("自上个 tag 以来没有新提交，不发布", file=sys.stderr)
            if args.github_output:
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                    f.write("skip=true\n")
            return
        kind, why = args.force, "手动指定" if args.force else ""
        if not args.force:
            kind, why = decide(msgs)
        new = bump(cur, kind)

    vs = ".".join(str(x) for x in new)
    print(f"上一版：{tag or '（无）'}")
    print(f"当前 VERSION：{'.'.join(str(x) for x in cur)}")
    print(f"递增类型：{kind}（{why}）")
    print(f"发布版本：{vs}")

    if args.write:
        with open(VERSION_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(vs + "\n")
        print(f"已写入 {VERSION_FILE}")

    if args.github_output:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"version={vs}\n")
            f.write(f"tag=v{vs}\n")
            f.write(f"bump={kind}\n")
            f.write("skip=false\n")


if __name__ == "__main__":
    main()
