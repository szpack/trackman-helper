#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trackman_dashboard.py — 本地实时击球看板

后端常驻订阅 TPS 本地 WebSocket（ws://localhost:21581/ws），
每来一杆就往浏览器推一次（SSE），页面自动更新，不用刷新。

启动：
    python trackman_dashboard.py                 # 默认 http://127.0.0.1:8770
    python trackman_dashboard.py --port 9000
    python trackman_dashboard.py --demo          # 没有设备时用模拟数据演示
    python trackman_dashboard.py --tps-port 21582

依赖：仅标准库 + 同目录的 trackman_live.py（复用其 WebSocket 客户端）。
"""

import argparse
import json
import queue
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from trackman_live import WSClient, flatten
from analysis import analyze, load_config, save_config, CONFIG_FILE
import tps_club
import tps_norm
import version as _ver

HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "dashboard.html"

# ---------------------------------------------------------------- 字段归一化

# 统一 key -> 候选原始字段名（全小写比对，递归查找）
MAP = {
    "clubSpeed": ["clubspeed", "clubheadspeed", "clubheadvelocity"],
    "ballSpeed": ["ballspeed", "ballvelocity"],
    "smash": ["smashfactor", "efficiency"],
    "launch": ["launchangle", "verticalangle"],
    "direction": ["launchdirection", "horizontalangle"],
    "spin": ["spinrate", "backspin", "totalspin"],
    "spinAxis": ["spinaxis"],
    "clubPath": ["clubpath"],
    "faceAngle": ["faceangle", "clubfaceangle"],
    "faceToPath": ["facetopath"],
    "attack": ["attackangle"],
    "loft": ["dynamicloft"],
    "carry": ["carry", "carrydistance"],
    "total": ["total", "totaldistance"],
    "height": ["maxheight", "apex", "peakheight"],
    "hang": ["hangtime", "flighttime"],
    "side": ["carryside", "totalside", "side", "lateral", "offline", "deviation"],
    "curve": ["curve"],
    "swingDir": ["swingdirection"],
    "landing": ["landingangle"],
    # ---- 对齐 TPS 原版面板补齐的字段 ----
    "lowPoint": ["lowpointdistance", "lowpoint"],
    "impactOffset": ["impactoffset"],
    "impactHeight": ["impactheight"],
    "spinLoft": ["spinloft"],
    "lie": ["dynamiclie"],
    "planeTilt": ["dplanetilt", "planetilt"],
    "swingPlane": ["swingplane"],
    "swingRadius": ["swingradius"],
    "club": ["club", "clubname", "clubtype"],
    "player": ["player", "playername"],
}

# TrackMan 设备推送 SI 单位：速度 m/s、距离 m。后端统一换算成英制数值，
# 前端再按 metric 开关转公制 —— 只在后端算一次，避免两处口径打架。
SPD_MS_MPH = 2.2369362920544
M_YD = 1.0936132983
M_FT = 3.2808398950131

SCALED = {
    "clubSpeed": SPD_MS_MPH,
    "ballSpeed": SPD_MS_MPH,
    "carry": M_YD,
    "total": M_YD,
    "side": M_YD,
    "curve": M_YD,
    "height": M_FT,
    # 击球点用毫米（与 TPS 界面一致），最低点/挥杆半径用英尺
    "impactOffset": 1000.0,
    "impactHeight": 1000.0,
    "lowPoint": M_FT,
    "swingRadius": M_FT,
}


def _is_si(m):
    """判断载荷是否为 SI 单位。真实球速 20~90 m/s，英制 45~200 mph。"""
    bs = m.get("ballSpeed")
    if isinstance(bs, (int, float)):
        return bs < 100
    cs = m.get("clubSpeed")
    if isinstance(cs, (int, float)):
        return cs < 80
    return True


def _norm(payload):
    flat = flatten(payload or {})
    out = {}
    for key, cands in MAP.items():
        for c in cands:
            v = flat.get(c)
            # 注意：0 / 0.0 是合法读数（ClubPath 0.0、Curve 0），不能当空值丢掉
            if v is None or v == "":
                continue
            if isinstance(v, str) and not v.strip():
                continue
            out[key] = v
            break
    # Player 可能是嵌套对象，flatten 后是 playername
    for k in ("club", "player"):
        if isinstance(out.get(k), (dict, list)):
            out.pop(k, None)
    if _is_si(out):
        for k, f in SCALED.items():
            v = out.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = round(v * f, 2)
    return out


def _shot_id(rec):
    raw = rec.get("raw") or {}
    v = raw.get("Id") or raw.get("id") or raw.get("MeasurementId")
    return str(v) if v else None


CLUB_KEYS = ("club", "clubZh", "clubShort", "clubRaw", "club_src")


def _keep_club(new_m, old_m):
    """球杆不是从设备 payload 来的，重新归一化时不能被冲掉。"""
    for k in CLUB_KEYS:
        if old_m.get(k) and not new_m.get(k):
            new_m[k] = old_m[k]
    return new_m


def _merge_raw(old, new):
    """设备是渐进补全：新值非空就覆盖，老值兜底。"""
    out = dict(old)
    for k, v in new.items():
        if v is None or v == "":
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        out[k] = v
    return out


def build_record(payload, seq):
    """把设备原始 payload 变成前端要的统一结构。"""
    m = _norm(payload)
    kind = payload.get("Kind") if isinstance(payload, dict) else None
    return {
        "seq": seq,
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "kind": kind,
        "m": m,
        "raw": payload,
    }


# ---------------------------------------------------------------- 广播中枢

# 设备算完的标志：距离类和弹道点列都齐了
COMPLETE_DEBOUNCE = 0.9    # 收齐后再等一会儿，确认没有后续帧
PARTIAL_TIMEOUT = 9.0      # 一直收不齐就强制提交，别让页面一直转圈


def _is_complete(raw):
    """设备算完的标志：距离类和弹道点列都齐了。"""
    if not isinstance(raw, dict):
        return False
    return (isinstance(raw.get("Carry"), (int, float))
            and isinstance(raw.get("Total"), (int, float))
            and bool(raw.get("BallTrajectory")))


def _analyzable(m):
    """能不能分析：球速/发射角/后旋是任何诊断的地基，缺了就没什么可说的。"""
    return all(isinstance(m.get(k), (int, float)) for k in ("ballSpeed", "launch", "spin"))


class Hub:
    def __init__(self, persist: Path | None, use_ai=True):
        self.lock = threading.Lock()
        self.shots = []
        self.clients = set()
        self.status = {
            "tps": False,
            "device": None,
            "measuring": False,
            "message": "正在连接 TPS…",
            "since": time.time(),
        }
        self.persist = persist
        self.pending = {}        # id -> rec，还没算完的杆
        self.timers = {}         # id -> Timer
        self.analyses = {}       # seq -> 分析结果
        self.use_ai = use_ai
        self.ai_cfg = load_config()
        self._cfg_mtime = self._cfg_stamp()
        self.ai_queue = queue.Queue()
        self.norm = {"on": None, "method": None, "layout": None, "detail": "尚未检测", "at": 0}
        self._load()
        threading.Thread(target=self._analysis_loop, daemon=True).start()
        threading.Thread(target=self._backfill_clubs, daemon=True).start()
        threading.Thread(target=self._norm_loop, daemon=True).start()

    def norm_status(self):
        with self.lock:
            return dict(self.norm)

    def _norm_loop(self):
        """盯着 TPS 的 Normalize 开关：状态一变就广播，前端据此标注距离口径。"""
        w = tps_norm.watcher()
        w.start()
        last = object()
        while True:
            cur = w.status()
            sig = (cur.get("on"), cur.get("method"), cur.get("stale"))
            if sig != last:
                last = sig
                with self.lock:
                    self.norm = cur
                self._broadcast("norm", dict(cur))
                if cur.get("on") is not None:
                    print(f"[归一化] TPS Normalize {'开启' if cur['on'] else '关闭'}"
                          f"（{cur.get('method')}）")
            time.sleep(2)

    def _cfg_stamp(self):
        try:
            return CONFIG_FILE.stat().st_mtime
        except OSError:
            return None

    def _refresh_cfg(self):
        """手动改 ai_config.json 后不必重启看板——下次分析自动读新配置。"""
        mt = self._cfg_stamp()
        if mt != self._cfg_mtime:
            self._cfg_mtime = mt
            self.ai_cfg = load_config()
            print(f"[AI] 配置已更新：{self.ai_cfg.get('model')} "
                  f"{'（已填 Key）' if self.ai_cfg.get('api_key') else '（Key 为空）'}")

    def _backfill_clubs(self):
        """历史杆补球杆：老文件里没存球杆，去 TPS 本地库按 MeasurementId 捞回来。"""
        pending = [(_shot_id(r), r["seq"]) for r in self.shots
                   if _shot_id(r) and not (r.get("m") or {}).get("club")]
        hit = 0
        for mid, seq in pending:
            info = tps_club.find_club(mid, tries=1)
            if not info:
                continue
            with self.lock:
                rec = next((x for x in self.shots if x.get("seq") == seq), None)
                if rec is None:
                    continue
                rec["m"].update(info)
                rec["club_src"] = "tps"
                hit += 1
        if hit:
            with self.lock:
                self._rewrite_persist()
            print(f"[杆] 历史补齐 {hit}/{len(pending)} 杆的球杆")

        # 最后一杆的分析等球杆定下来再跑，否则基准杆会选错
        if self.shots and self.use_ai:
            last = self.shots[-1]
            m = dict(last.get("m") or {})
            if _analyzable(m):
                self.ai_queue.put((last["seq"], m, last.get("club_override")))

    def _load(self):
        if not self.persist or not self.persist.exists():
            return
        try:
            raw_n = 0
            index = {}
            for line in self.persist.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                raw_n += 1
                pid = _shot_id(rec)
                if pid and pid in index:
                    # 老文件里同一杆存了多次快照，载入时合并回一条
                    old = self.shots[index[pid]]
                    rec["raw"] = _merge_raw(old.get("raw") or {}, rec.get("raw") or {})
                    rec["m"] = _keep_club(_norm(rec["raw"]), old.get("m") or {})
                    rec["seq"] = old["seq"]
                    self.shots[index[pid]] = rec
                    continue
                rec["seq"] = len(self.shots) + 1
                rec["m"] = _keep_club(_norm(rec.get("raw") or {}), rec.get("m") or {})
                self.shots.append(rec)
                if pid:
                    index[pid] = len(self.shots) - 1
            if raw_n:
                print(f"[历史] 从 {self.persist.name} 载入 {raw_n} 条推送 → {len(self.shots)} 杆")
                if raw_n != len(self.shots):
                    self._rewrite_persist()
                    print(f"[历史] 已合并重复快照，文件重写为 {len(self.shots)} 行")
        except Exception as e:
            print(f"[历史] 载入失败：{e}")

    def add_shot(self, rec, final=False):
        """设备对同一杆会分多次推送（字段逐步补齐）。

        先攒在 pending 里，等设备算完（拿到 Carry/Total/BallTrajectory）再提交，
        避免把半成品数据当成最终结果展示出来。
        """
        pid = _shot_id(rec) or f"anon-{time.time():.6f}"
        is_new = False
        with self.lock:
            cur = self.pending.get(pid)
            if cur is None:
                cur = rec
                cur["id"] = pid
                self.pending[pid] = cur
                is_new = True
            else:
                cur["raw"] = _merge_raw(cur.get("raw") or {}, rec.get("raw") or {})
                cur["m"] = _norm(cur["raw"])       # 合并后必须重算字段
                cur["kind"] = rec.get("kind") or cur.get("kind")

            t = self.timers.pop(pid, None)
            if t:
                t.cancel()
            done = final or _is_complete(cur["raw"])
            if final:
                cur["_final"] = True          # demo/外部直接投喂，不需要再等
            delay = 0.05 if final else (COMPLETE_DEBOUNCE if done else PARTIAL_TIMEOUT)
            timer = threading.Timer(delay, self._finalize, args=(pid,))
            timer.daemon = True
            self.timers[pid] = timer
            timer.start()

            if is_new:
                # 只在新杆出现时提示一次"计算中"，后续补字段不再刷屏
                self._broadcast("pending", {
                    "id": pid,
                    "seq": len(self.shots) + 1,
                    "m": {k: v for k, v in cur["m"].items()
                          if k in ("ballSpeed", "clubSpeed", "launch", "spin")},
                })

    def _finalize(self, pid):
        """设备算完了（或等超时），正式入库并广播。"""
        with self.lock:
            self.timers.pop(pid, None)
            rec = self.pending.pop(pid, None)
            if rec is None:
                return
            rec.pop("id", None)
            rec["complete"] = rec.pop("_final", False) or _is_complete(rec["raw"])
            idx = -1
            for i, old in enumerate(self.shots):
                if _shot_id(old) and _shot_id(old) == pid:
                    idx = i
                    break
            if idx >= 0:
                rec["seq"] = self.shots[idx]["seq"]
                self.shots[idx] = rec
                self._rewrite_persist()
            else:
                self.shots.append(rec)
                rec["seq"] = len(self.shots)
                self._append_persist(rec)
            self._broadcast("shot", rec)
            self._broadcast("stat", self._stat())
            seq, complete = rec["seq"], rec["complete"]

        print(f"[球] #{seq} {'✓' if complete else '⚠ 数据不全'} {_brief(rec['m'])}")

        # 球杆不在雷达数据流里，要去 TPS 本地库按 MeasurementId 反查。
        # 放在锁外做，数据先到屏幕上，球杆几十毫秒后补上。
        self._attach_club(pid, rec, seq)

    def _attach_club(self, pid, rec, seq):
        """补球杆，然后才把这一杆交给 AI —— 基准杆选错，整份分析都会歪。"""
        info = None
        if not rec["m"].get("club"):
            info = tps_club.find_club(pid)
        with self.lock:
            m = rec["m"]
            if info:
                m.update(info)
                rec["club_src"] = "tps"
            elif m.get("club"):
                rec["club_src"] = rec.get("club_src") or "payload"
            else:
                rec["club_src"] = "guess"
            self._rewrite_persist()
            m = dict(m)
            club = rec.get("club_override")

        if info:
            self._broadcast("shot", rec)      # 前端补上球杆，不必刷新
            print(f"[杆] #{seq} {info['clubZh']} ({info['clubShort']})")

        if self.use_ai and _analyzable(m):
            self.ai_queue.put((seq, m, club))
        else:
            reason = "数据不完整（缺球速/发射角/后旋），无法分析" if not _analyzable(m) else "AI 已关闭"
            self.analyses[seq] = {
                "summary": reason, "highlights": [], "issues": [], "drills": [],
                "source": "none", "notice": reason,
            }
            self._broadcast("analysis", dict(self.analyses[seq], seq=seq))

    def request_analysis(self, seq, club=None):
        """重新分析（用户改了球杆时用）。"""
        with self.lock:
            idx = next((i for i, r in enumerate(self.shots) if r.get("seq") == seq), None)
            if idx is None:
                return False
            rec = self.shots[idx]
            if club is not None:
                rec["club_override"] = club or None
                self._rewrite_persist()
            m = dict(rec["m"])
            override = rec.get("club_override")
            self.analyses.pop(seq, None)
        self.ai_queue.put((seq, m, override))
        return True

    def _analysis_loop(self):
        while True:
            job = self.ai_queue.get()
            if job is None:
                return
            seq, m, club = job
            self._refresh_cfg()
            try:
                res = analyze(m, club=club, use_ai=self.use_ai, cfg=self.ai_cfg)
            except Exception as e:
                res = {"summary": "", "highlights": [], "issues": [], "drills": [],
                       "source": "error", "notice": f"分析失败：{e}"}
            with self.lock:
                self.analyses[seq] = res
            self._broadcast("analysis", dict(res, seq=seq))
            print(f"[分析] #{seq} [{res.get('source')}] {str(res.get('summary'))[:46]}")

    def _append_persist(self, rec):
        if not self.persist:
            return
        try:
            with open(self.persist, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[落盘] 失败：{e}")

    def _rewrite_persist(self):
        """合并后要整文件重写，否则 jsonl 里会留下同一杆的旧快照。"""
        if not self.persist:
            return
        try:
            tmp = self.persist.with_suffix(self.persist.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for r in self.shots:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(self.persist)
        except Exception as e:
            print(f"[落盘] 重写失败：{e}")

    def _stat(self):
        return {"count": len(self.shots), "last": self.shots[-1]["ts"] if self.shots else None}

    def set_status(self, **kw):
        with self.lock:
            self.status.update(kw)
            self._broadcast("status", dict(self.status))

    def _broadcast(self, event, data):
        dead = []
        for q in list(self.clients):
            try:
                q.put_nowait((event, data))
            except queue.Full:
                dead.append(q)
        for q in dead:
            self.clients.discard(q)

    def subscribe(self):
        q = queue.Queue(maxsize=256)
        with self.lock:
            self.clients.add(q)
            snapshot = list(self.shots)
            status = dict(self.status)
            analyses = dict(self.analyses)
        return q, snapshot, status, analyses

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)


# ---------------------------------------------------------------- TPS 采集


def tps_worker(hub: Hub, host, port, path):
    """常驻线程：连上 TPS → 订阅 → 转发每一杆；断了自动重连。"""
    backoff = 1.0
    while True:
        try:
            hub.set_status(tps=False, message=f"连接 {host}:{port}{path} …")
            ws = WSClient(host, port, path, timeout=8)
            ws.send_text(json.dumps({"Type": "Subscribe", "Payload": {"MessageList": ["ALL!"]}}))
            hub.set_status(tps=True, message="已连接 TPS，等待击球", since=time.time())
            print(f"[TPS] 已连接 {host}:{port}{path}，已订阅")
            backoff = 1.0
            idle_since = time.time()

            while True:
                try:
                    text = ws.recv_text(timeout=20)
                except socket.timeout:
                    # 静默太久，主动保活，顺便确认链路
                    if time.time() - idle_since > 25:
                        ws.send_text('{"Type":"Pong"}')
                        idle_since = time.time()
                    continue
                idle_since = time.time()

                try:
                    msg = json.loads(text)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue

                t = msg.get("Type")
                if t == "Ping":
                    ws.send_text('{"Type":"Pong"}')
                elif t == "Acknowledge":
                    print(f"[TPS] 订阅确认 {msg.get('SubType')}")
                elif t == "DeviceStatus":
                    p = msg.get("Payload") or {}
                    hub.set_status(
                        device=p,
                        measuring=bool(p.get("IsMeasuring")),
                        message="设备已连接" if p.get("IsConnected") else "设备未连接",
                    )
                elif t == "Measuring":
                    hub.set_status(measuring=True, message="测量中…")
                elif t == "Measurement":
                    p = msg.get("Payload")
                    hub.add_shot(build_record(p, 0))
                elif t == "TrackerState":
                    hub.set_status(measuring=False)

        except KeyboardInterrupt:
            return
        except Exception as e:
            hub.set_status(tps=False, message=f"未连接（{type(e).__name__}），{int(backoff)}s 后重试")
            time.sleep(backoff)
            backoff = min(backoff * 1.6, 10)


def _brief(m):
    bits = []
    for k, unit in (("club", ""), ("ballSpeed", ""), ("carry", "")):
        if m.get(k) not in (None, ""):
            v = m[k]
            bits.append(f"{v:.1f}" if isinstance(v, float) else str(v))
    return " / ".join(bits)


def demo_worker(hub: Hub, interval=2.5):
    """没有设备时，用模拟数据驱动看板。

    注意：真实设备推的是 SI 单位（m/s、米），这里也必须按 SI 造，
    否则 _is_si() 会按量级误判——挖起杆 78 mph 会被当成 78 m/s。
    """
    import random

    hub.set_status(tps=True, device={"IsConnected": True, "IsMeasuring": False},
                   message="演示模式（模拟数据）", since=time.time())
    # 名称, 杆头mph, 球速mph, 发射角, 后旋, 落点yd, 攻击角, 动态杆面角
    clubs = [
        ("Driver", 113, 167, 11.5, 2500, 265, 2.5, 13.0),
        ("7 Iron", 84, 118, 18.0, 6300, 152, -3.5, 30.5),
        ("5 Iron", 92, 132, 15.0, 5200, 178, -2.5, 24.5),
        ("PW", 78, 96, 27.0, 9100, 118, -4.5, 41.0),
        ("SW", 62, 78, 32.0, 10400, 88, -5.0, 49.0),
    ]
    while True:
        time.sleep(interval)
        name, cs, bs, la, sr, carry, aa, loft = random.choice(clubs)
        j = lambda v, p: v * (1 + random.uniform(-p, p))
        club_mph, ball_mph = j(cs, 0.05), j(bs, 0.05)   # 先按英制算，再转回 SI
        payload = {
            "Kind": "Measurement",
            "Data": {
                "Club": name,
                "ClubSpeed": round(club_mph / SPD_MS_MPH, 4),
                "BallSpeed": round(ball_mph / SPD_MS_MPH, 4),
                "LaunchAngle": round(j(la, 0.08), 2),
                "SpinRate": int(j(sr, 0.10)),
                "SpinAxis": round(random.uniform(-9, 9), 2),
                "ClubPath": round(random.uniform(-4, 4), 2),
                "FaceAngle": round(random.uniform(-3, 3), 2),
                "AttackAngle": round(j(aa, 0.25), 2),
                "DynamicLoft": round(j(loft, 0.06), 2),
                "Carry": round(j(carry, 0.06) / M_YD, 3),
                "MaxHeight": round(j(carry * 0.18, 0.15) * 0.3048, 3),
                "HangTime": round(j(6.0, 0.1), 3),
                "ImpactHeight": round(random.uniform(-0.01, 0.012), 4),
                "ImpactOffset": round(random.uniform(-0.012, 0.012), 4),
                "Player": {"Name": "Demo Player"},
            },
        }
        d = payload["Data"]
        d["Total"] = round(d["Carry"] * 1.06, 3)
        d["FaceToPath"] = round(d["FaceAngle"] - d["ClubPath"], 2)
        d["SmashFactor"] = round(d["BallSpeed"] / d["ClubSpeed"], 3)
        d["BallTrajectory"] = [{"Kind": "Flight"}]      # 标记数据完整
        hub.add_shot(build_record(payload, 0), final=True)


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub: Hub = None

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path in ("/", "/index.html"):
            try:
                body = HTML_FILE.read_bytes()
            except FileNotFoundError:
                self._send(500, b"dashboard.html not found", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/api/shots":
            with self.hub.lock:
                body = json.dumps(self.hub.shots, ensure_ascii=False).encode()
            self._send(200, body)
        elif path == "/api/status":
            with self.hub.lock:
                body = dict(self.hub.status)
            body["clubDb"] = tps_club.instance().status()
            body["normalize"] = self.hub.norm_status()
            body["build"] = _ver.summary()
            self._send(200, json.dumps(body, ensure_ascii=False).encode())
        elif path == "/api/ai":
            from analysis import CLUB_ORDER, CLUB_ZH
            cfg = self.hub.ai_cfg
            info = {
                "configured": bool(cfg.get("api_key")),
                "use_ai": self.hub.use_ai,
                "base_url": cfg.get("base_url", ""),
                "model": cfg.get("model", ""),
                "clubs": [{"key": k, "zh": CLUB_ZH.get(k, k)} for k in CLUB_ORDER],
            }
            self._send(200, json.dumps(info, ensure_ascii=False).encode())
        elif path == "/api/analysis":
            seq = int((qs.get("seq") or ["0"])[0] or 0)
            with self.hub.lock:
                res = self.hub.analyses.get(seq)
            body = json.dumps(res or {"seq": seq, "pending": True}, ensure_ascii=False).encode()
            self._send(200, body)
        elif path == "/events":
            self._sse()
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        if path == "/api/analyze":
            try:
                seq = int(data.get("seq") or 0)
            except Exception:
                seq = 0
            club = data.get("club") or None
            ok = self.hub.request_analysis(seq, club)
            self._send(200 if ok else 404,
                       json.dumps({"ok": ok}, ensure_ascii=False).encode())

        elif path == "/api/ai-config":
            old = self.hub.ai_cfg
            key = data.get("api_key")
            if key in (None, "", "***"):
                key = old.get("api_key", "")      # 前端留空或不改，就保留原 Key
            save_config({
                "base_url": data.get("base_url") or old.get("base_url", ""),
                "api_key": key,
                "model": data.get("model") or old.get("model", ""),
            })
            with self.hub.lock:
                self.hub.ai_cfg = load_config()
            self._send(200, json.dumps({"ok": True, "configured": bool(key)},
                                       ensure_ascii=False).encode())

        elif path == "/api/ai-test":
            from analysis import llm_analyze, rule_diagnose
            with self.hub.lock:
                cfg = dict(self.hub.ai_cfg)
            sample = {"clubSpeed": 85.0, "ballSpeed": 122.0, "smash": 1.43,
                      "launch": 18.0, "spin": 6200, "spinAxis": 3.0, "clubPath": 1.0,
                      "faceAngle": 0.5, "faceToPath": -0.5, "attack": -3.0,
                      "loft": 30.0, "carry": 160.0, "total": 172.0, "height": 90.0,
                      "hang": 5.5, "side": 3.0}
            try:
                res = llm_analyze(sample, rule_diagnose(sample, "7 Iron"), cfg, timeout=45)
                self._send(200, json.dumps({"ok": True, "summary": res["summary"],
                                            "model": cfg.get("model")},
                                           ensure_ascii=False).encode())
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def _sse(self):
        q, snapshot, status, analyses = self.hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def w(event, data):
            payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()

        try:
            w("hello", {"status": status, "shots": snapshot, "analyses": analyses,
                        "normalize": self.hub.norm_status(),
                        "build": _ver.summary(),
                        "server": datetime.now().isoformat()})
            while True:
                try:
                    event, data = q.get(timeout=15)
                    w(event, data)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")   # 注释行，防中间层超时
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(q)


def main():
    ap = argparse.ArgumentParser(description="TrackMan 实时击球本地看板")
    ap.add_argument("--port", type=int, default=8770, help="看板端口（默认 8770）")
    ap.add_argument("--host", default="127.0.0.1", help="TPS 地址（默认 127.0.0.1）")
    ap.add_argument("--tps-port", type=int, default=21581, help="TPS 本地 API 端口（默认 21581）")
    ap.add_argument("--tps-path", default="/ws", help="TPS WebSocket 路径（默认 /ws）")
    ap.add_argument("--persist", default="dashboard_shots.jsonl", help="历史落盘文件（默认 dashboard_shots.jsonl），传空串关闭")
    ap.add_argument("--demo", action="store_true", help="模拟数据演示（无设备时用）")
    ap.add_argument("--no-ai", action="store_true", help="关闭 AI 分析，只出规则诊断")
    ap.add_argument("--open", action="store_true", help="启动后自动用默认浏览器打开看板")
    args = ap.parse_args()

    persist = Path(args.persist) if args.persist else None
    hub = Hub(persist, use_ai=not args.no_ai)

    if args.demo:
        threading.Thread(target=demo_worker, args=(hub,), daemon=True).start()
    else:
        threading.Thread(target=tps_worker,
                         args=(hub, args.host, args.tps_port, args.tps_path),
                         daemon=True).start()

    Handler.hub = hub
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        winerr = getattr(e, "winerror", None)
        print(f"\n[错误] 端口 {args.port} 起不来：{e}")
        if winerr == 10048:
            print(f"  端口已被占用——可能已经有一个看板在运行。")
            print(f"  先访问 http://127.0.0.1:{args.port} 看看；或换端口：--port 8771")
        sys.exit(1)
    srv.daemon_threads = True

    url = f"http://127.0.0.1:{args.port}"
    print("=" * 56)
    print(f"  看板已启动  →  {url}")
    if args.demo:
        print("  模式：演示（模拟数据）")
    else:
        print(f"  数据源：ws://{args.host}:{args.tps_port}{args.tps_path}")
    if not hub.use_ai:
        print("  AI：已关闭（--no-ai），只出规则诊断")
    elif hub.ai_cfg.get("api_key"):
        print(f"  AI：{hub.ai_cfg.get('model')} @ {hub.ai_cfg.get('base_url')}")
    else:
        print("  AI：未配置 Key，先用规则诊断（页面右上角「AI 设置」里填）")
    print("  Ctrl+C 退出")
    print("=" * 56)

    if args.open:
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[退出] 看板已关闭")
        srv.shutdown()


if __name__ == "__main__":
    main()
