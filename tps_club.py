#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tps_club.py — 从 TPS 本地数据库取「这一杆用的是哪根杆」

为什么必须查数据库：
  TrackMan 设备的雷达/视觉消息总线一共 103 种消息类型，没有任何一种携带球杆名。
  球杆是 TPS 应用层给每一杆打的标签（用户在界面上选的 8i），只落在本地
  SQL Server LocalDB 的 Strokes 表里 —— 所以只能从这儿读。

匹配依据：
  Strokes.Data 的 XML 里 <MeasurementId> 与设备推送的 Measurement.Id 完全相同，
  用这个去重 ID 直查，不会错配。

连接方式（三层，按可靠性排）：
  1. pyodbc + LocalDB 命名管道（旧版 "SQL Server" ODBC 驱动不认 (localdb)\\ 语法，
     但认 np:\\\\.\\pipe\\LOCALDB#xxxx\\tsql\\query）。实测单次查询 20ms 左右。
  2. 本进程没装 pyodbc 时，借一个**装了 pyodbc 的解释器**跑本文件的 --json 模式
     （看板可能被任意 python.exe 拉起，见 pydeps.py）。
  3. 再不行才退 PowerShell + .NET SqlClient（慢，且依赖 powershell.exe 可用）。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PS_SCRIPT = HERE / "tps_club.ps1"
DB_NAME = "TrackMan Performance Studio"

try:
    import pydeps
except Exception:                                # noqa: BLE001
    pydeps = None

# TrackMan 的 ClubType 枚举 -> (诊断基准名, 中文显示名, 缩写)
# 基准名必须是 analysis.BENCH 里的键；混合杆借用同号铁杆的基准。
CLUB_MAP = {
    "Driver":         ("Driver",         "一号木",      "DR"),
    "2Wood":          ("3 Wood",         "2号木",       "2W"),
    "3Wood":          ("3 Wood",         "3号木",       "3W"),
    "5Wood":          ("5 Wood",         "5号木",       "5W"),
    "6Wood":          ("5 Wood",         "6号木",       "6W"),
    "7Wood":          ("5 Wood",         "7号木",       "7W"),
    "2Iron":          ("2 Iron",         "2号铁",       "2i"),
    "3Iron":          ("3 Iron",         "3号铁",       "3i"),
    "4Iron":          ("4 Iron",         "4号铁",       "4i"),
    "5Iron":          ("5 Iron",         "5号铁",       "5i"),
    "6Iron":          ("6 Iron",         "6号铁",       "6i"),
    "7Iron":          ("7 Iron",         "7号铁",       "7i"),
    "8Iron":          ("8 Iron",         "8号铁",       "8i"),
    "9Iron":          ("9 Iron",         "9号铁",       "9i"),
    "3Hybrid":        ("3 Iron",         "3号混合杆",   "3H"),
    "4Hybrid":        ("4 Iron",         "4号混合杆",   "4H"),
    "5Hybrid":        ("5 Iron",         "5号混合杆",   "5H"),
    "8Hybrid":        ("8 Iron",         "8号混合杆",   "8H"),
    "PitchingWedge":  ("Pitching Wedge", "P杆",         "PW"),
    "GapWedge":       ("Gap Wedge",      "G杆",         "GW"),
    "SandWedge":      ("Sand Wedge",     "S杆",         "SW"),
    "LobWedge":       ("Lob Wedge",      "L杆",         "LW"),
    # TrackMan 里按倾角命名的挖起杆
    "46Wedge":        ("Pitching Wedge", "46°挖起杆",   "46"),
    "48Wedge":        ("Pitching Wedge", "48°挖起杆",   "48"),
    "50Wedge":        ("Gap Wedge",      "50°挖起杆",   "50"),
    "52Wedge":        ("Gap Wedge",      "52°挖起杆",   "52"),
    "54Wedge":        ("Sand Wedge",     "54°挖起杆",   "54"),
    "56Wedge":        ("Sand Wedge",     "56°挖起杆",   "56"),
    "58Wedge":        ("Lob Wedge",      "58°挖起杆",   "58"),
    "60Wedge":        ("Lob Wedge",      "60°挖起杆",   "60"),
    "64Wedge":        ("Lob Wedge",      "64°挖起杆",   "64"),
}

_RE_MID = re.compile(r"<(?:[A-Za-z0-9]+:)?MeasurementId>([^<]+)<")
_RE_NAME = re.compile(
    r"<(?:[A-Za-z0-9]+:)?MeasuredClubData.*?<(?:[A-Za-z0-9]+:)?Name>([^<]+)<", re.S)
_RE_TIME = re.compile(r"<(?:[A-Za-z0-9]+:)?MeasurementTime>([^<]+)<")


def describe(club_type, club_name=""):
    """ClubType -> {club, clubZh, clubShort, clubRaw}

    未知枚举也给出可读结果，绝不返回"未知"——宁可显示 8Iron 也不能丢信息。
    """
    t = (club_type or "").strip()
    shown = (club_name or "").strip()
    if t in CLUB_MAP:
        bench, zh, short = CLUB_MAP[t]
        return {"club": bench, "clubZh": zh,
                "clubShort": short, "clubRaw": shown or t, "club_src": "tps"}
    label = shown or t
    if not label:
        return None
    return {"club": label, "clubZh": label, "clubShort": label, "clubRaw": label, "club_src": "tps"}


def _pipe_names():
    """枚举当前 LocalDB 的 T-SQL 命名管道。实例重启后名字会变，所以每次连不上就重枚举。"""
    base = "\\\\" + "." + "\\pipe\\"
    try:
        names = os.listdir(base)
    except OSError:
        return []
    return [n for n in names
            if "LOCALDB" in n.upper() and n.upper().endswith("TSQL\\QUERY")]


class TpsClub:
    """TPS 本地库的球杆查询器。任何环节失败都静默降级，绝不影响看板。"""

    def __init__(self, take=12):
        self.take = take
        self._cn = None
        self._pipe = None
        self._lock = threading.Lock()
        self._driver = None      # 'pyodbc' | 'python' | 'powershell' | None
        self._helper_argv = None
        self.last_error = None
        self.ok = False
        self._probe()

    # ---------------- 连接 ----------------
    def _try_pyodbc(self):
        try:
            import pyodbc  # noqa: F401
        except ImportError:
            self.last_error = "未安装 pyodbc"
            return False
        pipes = _pipe_names()
        if not pipes:
            self.last_error = "未见 LocalDB 管道"
            return False
        import pyodbc
        for p in pipes:
            cs = (f"DRIVER={{SQL Server}};Server=np:\\\\.\\pipe\\{p};"
                  f"Database={DB_NAME};Trusted_Connection=yes;")
            try:
                cn = pyodbc.connect(cs, timeout=4)
                self._cn, self._pipe, self._driver = cn, p, "pyodbc"
                self.last_error = None
                return True
            except Exception as e:
                self.last_error = f"连不上 {p}: {str(e)[:110]}"
        return False

    def _probe(self):
        best = self._best_pyodbc_argv()
        if best is None:
            self.last_error = "本机找不到带 pyodbc 的 Python"
        elif best[0].lower() == (sys.executable or "").lower():
            if self._try_pyodbc():
                self.ok = True
                return
        else:
            # 别的解释器有 pyodbc，但还没验证它连得上库：交给首次查询确认
            self._helper_argv = best
            self._driver = "python"
            self.ok = True
            return
        if PS_SCRIPT.exists():
            self._driver = "powershell"
            self.ok = True        # 还没验证，首次查询时再确认
            return
        self._driver = None
        self.ok = False

    def _best_pyodbc_argv(self):
        """找一个能 import pyodbc 的解释器启动参数；本进程有就直接用本进程。"""
        try:
            import pyodbc  # noqa: F401
            return [sys.executable]
        except ImportError:
            pass
        if pydeps is None:
            return None
        return pydeps.find_argv(("pyodbc",))

    def _ensure(self):
        """连接断了（TPS 重启、LocalDB 换管道）就重连一次。"""
        if self._driver == "pyodbc" and self._cn is not None:
            return True
        if self._driver in ("powershell", "python"):
            return True
        return self._try_pyodbc()


    # ---------------- 查询 ----------------
    def _via_pyodbc(self, retry=True):
        if not self._ensure():
            return None
        try:
            cur = self._cn.cursor()
            cur.execute(
                f"SELECT TOP ({int(self.take)}) ClubType, DateCreated, "
                f"CAST(Data AS nvarchar(max)) FROM Strokes ORDER BY DateCreated DESC")
            rows = cur.fetchall()
            out = []
            for r in rows:
                xml = r[2] or ""
                mid = _RE_MID.search(xml)
                nm = _RE_NAME.search(xml)
                created = r[1]
                # 旧版 ODBC 驱动不支持 datetime2，会原样返回字符串
                if hasattr(created, "isoformat"):
                    created = created.isoformat()
                out.append({
                    "measurementId": mid.group(1).strip() if mid else "",
                    "clubName": (nm.group(1).strip() if nm else ""),
                    "clubType": (r[0] or "").strip(),
                    "createdAt": str(created or ""),
                })
            self.ok, self.last_error = True, None
            return out
        except Exception as e:
            self.last_error = f"查询失败: {str(e)[:110]}"
            try:
                self._cn.close()
            except Exception:
                pass
            self._cn = None
            # 管道可能换了（TPS 重启），重连后只再试一次
            if retry and self._try_pyodbc():
                return self._via_pyodbc(retry=False)
            return None

    def _via_helper(self):
        """本进程没装 pyodbc：借一个装了 pyodbc 的解释器跑本文件的 --json 模式。

        比 PowerShell 兜底快且稳（不用起 .NET 宿主、不依赖执行策略），代价是一次
        进程启停（约 0.3s）——只在缺依赖的环境里发生，有 pyodbc 时永远走不到这。
        """
        if self._helper_argv is None:
            self._helper_argv = self._best_pyodbc_argv()
        if not self._helper_argv:
            self.last_error = "本机找不到带 pyodbc 的 Python"
            self.ok = False
            return None
        try:
            cmd = [*self._helper_argv, str(Path(__file__).resolve()), "--json", str(self.take)]
            p = subprocess.run(cmd, capture_output=True, timeout=25,
                               creationflags=0x08000000 if os.name == "nt" else 0)
            txt = (p.stdout or b"").decode("utf-8", "replace").strip()
            if not txt:
                self.last_error = "helper 无输出 " + \
                    (p.stderr or b"").decode("utf-8", "replace")[:120]
                self.ok = False
                return None
            data = json.loads(txt[txt.find("["):txt.rfind("]") + 1] or "[]")
            if not isinstance(data, list):
                self.last_error = "helper 返回格式异常"
                self.ok = False
                return None
            self.ok, self.last_error = True, None
            return data
        except Exception as e:                       # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"[:140]
            self.ok = False
            return None

    def _via_powershell(self):
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="tmclub_")
        os.close(fd)
        try:
            cmd = ["powershell.exe", "-NoProfile", "-NonInteractive",
                   "-ExecutionPolicy", "Bypass", "-File", str(PS_SCRIPT),
                   "-Take", str(self.take), "-Out", tmp]
            flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW
            p = subprocess.run(cmd, capture_output=True, timeout=15,
                               creationflags=flags)
            txt = Path(tmp).read_text(encoding="utf-8-sig", errors="replace").strip()
            if not txt:
                self.last_error = "PowerShell 无输出 " + \
                    (p.stderr or b"").decode("utf-8", "replace")[:120]
                self.ok = False
                return None
            data = json.loads(txt)
            if isinstance(data, dict):
                data = [data]
            if data and data[0].get("error"):
                self.last_error = str(data[0]["error"])[:140]
                self.ok = False
                return None
            self.ok, self.last_error = True, None
            return data
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:140]
            self.ok = False
            return None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def fetch(self):
        with self._lock:
            if self._driver == "pyodbc":
                rows = self._via_pyodbc()
                if rows is not None:
                    return rows
            if self._driver == "python" or self._best_pyodbc_argv() is not None:
                rows = self._via_helper()
                if rows is not None:
                    self._driver = "python"
                    return rows
            if PS_SCRIPT.exists():
                rows = self._via_powershell()
                if rows is not None:
                    self._driver = "powershell"
                    return rows
            return None


    def find(self, mid, tries=4, wait=0.6):
        """按 MeasurementId 找球杆。找不到就重试——TPS 算完到落库有几秒差。"""
        if not mid:
            return None
        mid = str(mid).strip().lower()
        for i in range(max(1, tries)):
            rows = self.fetch()
            if rows:
                for r in rows:
                    if (r.get("measurementId") or "").strip().lower() == mid:
                        return describe(r.get("clubType"), r.get("clubName"))
            elif not self.ok:
                return None      # 库都连不上，没必要白等
            if i + 1 < tries:
                time.sleep(wait)
        return None

    def find_by_time(self, iso_utc, window=8.0):
        """退路：按 MeasurementTime 匹配（XML 里没有 MeasurementId 的老数据）。"""
        if not iso_utc:
            return None
        try:
            from datetime import datetime
            t = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None
        rows = self.fetch() or []
        best, best_d = None, window
        for r in rows:
            try:
                from datetime import datetime
                c = datetime.fromisoformat(r.get("createdAt") or "")
            except Exception:
                continue
            d = abs((c - t).total_seconds())
            if d < best_d:
                best, best_d = r, d
        return describe(best.get("clubType"), best.get("clubName")) if best else None

    def latest(self):
        rows = self.fetch()
        if not rows:
            return None
        return describe(rows[0].get("clubType"), rows[0].get("clubName"))

    def status(self):
        return {"ok": self.ok, "driver": self._driver, "error": self.last_error}


_tps = None
_tps_lock = threading.Lock()


def instance():
    global _tps
    with _tps_lock:
        if _tps is None:
            _tps = TpsClub()
        return _tps


def find_club(mid, tries=4, wait=0.6):
    try:
        return instance().find(mid, tries=tries, wait=wait)
    except Exception:
        return None


if __name__ == "__main__":
    import sys

    # --json [N]：给"借解释器"模式用。只吐 JSON 数组，别混任何日志文字。
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        take = int(sys.argv[2]) if len(sys.argv) > 2 else 12
        q = TpsClub(take=take)
        rows = q._via_pyodbc() or []
        print(json.dumps(rows, ensure_ascii=False))
        sys.exit(0 if rows else 1)

    t = TpsClub()
    print("status:", t.status())
    if len(sys.argv) > 1:
        t0 = time.time()
        r = t.find(sys.argv[1], tries=1)
        print(f"{int((time.time()-t0)*1000)}ms ->", json.dumps(r, ensure_ascii=False))
    else:
        for r in (t.fetch() or [])[:6]:
            print(r["createdAt"], r["clubType"], r["clubName"],
                  describe(r["clubType"], r["clubName"]))

