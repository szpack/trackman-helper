#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trackman_live.py — 实时抓取 TrackMan 每一次击球数据

两种数据源：
  A) 默认：订阅 TrackMan Performance Studio 本地 WebSocket
     ws://localhost:21581/ws   （TPS 运行中即可，无需登录设备、不抢占 session）
  B) --device <ip>：绕过 TPS 直连设备
     ws://<ip>:<port>/ws  —— 端口需先用设备 UPnP 描述文档确认，默认 2869 兜底

用法：
  python trackman_live.py                    # 抓本地 TPS，追加写入 shots.jsonl
  python trackman_live.py --host 127.0.0.1 --port 21581
  python trackman_live.py --probe            # 只握手+订阅，打印前 5 条就退出
  python trackman_live.py --device 172.30.20.1 --port 2869

零第三方依赖，仅标准库。
"""

import argparse
import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- WebSocket

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WSClient:
    """最小 RFC6455 客户端：支持分片、掩码、控制帧。"""

    def __init__(self, host, port, path="/ws", timeout=10, secure=False):
        self.host, self.port, self.path = host, port, path
        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self.buf = b""
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())

        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手期间连接被关闭")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status:
            raise ConnectionError(f"握手失败：{status}（路径 {self.path} 可能不对）")

    # --- 读 ---
    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("连接已关闭")
            self.buf += chunk

    def _read_exact(self, n):
        self._need(n)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_frame(self):
        b1, b2 = self._read_exact(2)
        fin, opcode = b1 >> 7, b1 & 0x0F
        masked, length = b2 >> 7, b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv_text(self, timeout=None):
        """收一条完整文本消息（自动拼接分片、自动回控制帧）。"""
        if timeout is not None:
            self.sock.settimeout(timeout)
        parts = []
        while True:
            fin, opcode, payload = self.recv_frame()
            if opcode == OP_CLOSE:
                raise ConnectionError("服务端关闭了连接")
            if opcode == OP_PING:          # 协议级 ping
                self._send_raw(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    # --- 写 ---
    def _send_raw(self, opcode, data):
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send_text(self, text):
        self._send_raw(OP_TEXT, text.encode())

    def close(self):
        try:
            self._send_raw(OP_CLOSE, b"\x03\xe8")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 数据提取

# 每个字段给一组候选名，全部转小写做无大小写匹配
FIELDS = {
    "杆头速度": ["clubspeed", "clubheadspeed", "clubheadspeed"],
    "球速": ["ballspeed"],
    "效率": ["smashfactor", "efficiency"],
    "发射角": ["launchangle"],
    "侧旋": ["spinaxis"],
    "后旋": ["spinrate", "backspin", "totalspin"],
    "杆头轨迹": ["clubpath"],
    "杆面角": ["faceangle", "clubfaceangle"],
    "攻击角": ["attackangle"],
    "动态杆面角": ["dynamicloft"],
    "杆面轨迹差": ["facetopath"],
    "击球点": ["impactheight", "impacthorizontal", "impactoffset"],
    "落点": ["carry", "carrydistance"],
    "总距离": ["total", "totaldistance"],
    "最大高度": ["maxheight", "apex", "peakheight"],
    "滞空": ["hangtime", "flighttime"],
    "侧向": ["carryside", "side", "totalside"],
    "曲率": ["curve"],
    "球杆": ["club", "clubname", "clubtype", "clubloft"],
    "球员": ["player", "playername", "playerid"],
}

# 设备推送 SI 单位：速度 m/s、距离 m。这里统一换算成英制再输出。
SPD_MS_MPH = 2.2369362920544
M_YD = 1.0936132983
M_FT = 3.2808398950131

SCALE = {
    "杆头速度": (SPD_MS_MPH, "mph"),
    "球速": (SPD_MS_MPH, "mph"),
    "落点": (M_YD, "yd"),
    "总距离": (M_YD, "yd"),
    "侧向": (M_YD, "yd"),
    "曲率": (M_YD, "yd"),
    "最大高度": (M_FT, "ft"),
}


def flatten(obj, out=None, depth=0):
    """把嵌套 dict/list 拍平成 lower_key -> 首个标量值。"""
    if out is None:
        out = {}
    if depth > 12:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if isinstance(v, (dict, list)):
                if lk not in out:
                    out[lk] = None  # 占位，避免子层同名键覆盖
                flatten(v, out, depth + 1)
            else:
                out.setdefault(lk, v)
    elif isinstance(obj, list):
        for v in obj[:8]:
            flatten(v, out, depth + 1)
    return out


def summarize(payload):
    flat = flatten(payload)
    got = {}
    for label, keys in FIELDS.items():
        for k in keys:
            v = flat.get(k)
            # 0 / 0.0 是合法读数（杆头轨迹 0.0、曲率 0），不能当空值丢
            if v is None or v == "":
                continue
            if isinstance(v, str) and not v.strip():
                continue
            got[label] = v
            break

    bs = got.get("球速")
    si = bs < 100 if isinstance(bs, (int, float)) else True
    if si:
        for lab, (f, _unit) in SCALE.items():
            v = got.get(lab)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                got[lab] = round(v * f, 2)
    return got


def render(summary):
    order = ["球员", "球杆", "杆头速度", "球速", "效率", "发射角", "后旋", "侧旋",
             "杆头轨迹", "杆面角", "杆面轨迹差", "攻击角", "动态杆面角",
             "落点", "总距离", "最大高度", "滞空", "侧向"]
    bits = []
    for k in order:
        if k in summary:
            v = summary[k]
            if isinstance(v, float):
                v = f"{v:.2f}" if abs(v) < 10 else f"{v:.1f}"
            unit = SCALE.get(k, (None, ""))[1]
            bits.append(f"{k} {v}{unit}")
    return "  ".join(bits)


# ---------------------------------------------------------------- 主流程

def run(host, port, path, out_path, raw_dir, probe, timeout_s, secure):
    print(f"[连接] ws{'s' if secure else ''}://{host}:{port}{path}")
    ws = WSClient(host, port, path, timeout=timeout_s, secure=secure)
    print("[已连] 发送订阅：{\"Type\":\"Subscribe\",\"Payload\":{\"MessageList\":[\"ALL!\"]}}")
    ws.send_text(json.dumps({"Type": "Subscribe", "Payload": {"MessageList": ["ALL!"]}}))

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if raw_dir:
        Path(raw_dir).mkdir(parents=True, exist_ok=True)

    shots = 0
    frames = 0
    t0 = time.time()
    last_rx = time.time()

    while True:
        try:
            text = ws.recv_text(timeout=30)
        except socket.timeout:
            # 30 秒没数据：主动保活，避免被服务端 ping 超时踢掉
            try:
                ws.send_text('{"Type":"Pong"}')
            except Exception:
                raise ConnectionError("保活发送失败，连接可能已断")
            continue

        last_rx = time.time()
        frames += 1
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue

        mtype = msg.get("Type")
        payload = msg.get("Payload")

        if mtype == "Ping":
            ws.send_text('{"Type":"Pong"}')
            print("[心跳] Ping -> Pong")
            continue
        if mtype == "Acknowledge":
            print(f"[已订] Acknowledge SubType={msg.get('SubType')}")
            continue
        if mtype == "DeviceStatus":
            print(f"[设备] {json.dumps(payload, ensure_ascii=False)[:200]}")
            continue
        if mtype == "Measuring":
            print("[状态] 开始测量…")
            continue
        if mtype != "Measurement":
            print(f"[其他] Type={mtype} len={len(text)}")
            continue

        # ---- 一次击球 ----
        shots += 1
        ts = datetime.now()
        kind = payload.get("Kind") if isinstance(payload, dict) else None
        summary = summarize(payload) if payload is not None else {}
        line = render(summary)
        print(f"\n=== 第 {shots} 杆  {ts.strftime('%H:%M:%S')}{'  [' + str(kind) + ']' if kind else ''} ===")
        if line:
            print("  " + line)
        else:
            print("  （没匹配到常见字段——不同固件/版本的字段名可能不同，已保留原始 JSON 供比对）")

        rec = {
            "seq": shots,
            "ts": ts.isoformat(timespec="milliseconds"),
            "summary": summary,
            "payload": payload,
        }
        if out_path:
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        if raw_dir:
            kind = "launch" if _looks_like_launch(payload) else "measurement"
            fn = Path(raw_dir) / f"{ts.strftime('%Y%m%d-%H%M%S')}_{shots:04d}_{kind}.json"
            fn.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  原始 -> {fn}")

        if probe and shots >= 5:
            print(f"\n[探针结束] 收到 {shots} 杆 / {frames} 帧，耗时 {time.time()-t0:.1f}s")
            break

    ws.close()


def _looks_like_launch(payload):
    flat = flatten(payload or {})
    for k in ("ballspeed", "clubspeed", "launchangle", "spinrate"):
        if k in flat:
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description="实时抓取 TrackMan 击球数据")
    ap.add_argument("--host", default="127.0.0.1", help="TPS 本机地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=21581, help="本地 API 端口（默认 21581）")
    ap.add_argument("--path", default="/ws", help="WebSocket 路径（默认 /ws）")
    ap.add_argument("--device", metavar="IP", help="改为直连设备 IP（不走 TPS）")
    ap.add_argument("--out", default="shots.jsonl", help="JSONL 落盘路径（默认 shots.jsonl）")
    ap.add_argument("--raw-dir", default="shots", help="每杆原始 JSON 目录（默认 shots/）")
    ap.add_argument("--no-save", action="store_true", help="只看不存")
    ap.add_argument("--probe", action="store_true", help="收满 5 杆就退出（连通性自检）")
    ap.add_argument("--timeout", type=float, default=10, help="建连超时秒（默认 10）")
    ap.add_argument("--secure", action="store_true", help="用 wss://")
    args = ap.parse_args()

    host = args.device or args.host
    out = None if args.no_save else args.out
    raw = None if args.no_save else args.raw_dir

    try:
        run(host, args.port, args.path, out, raw, args.probe, args.timeout, args.secure)
    except ConnectionRefusedError:
        print("\n[失败] 连接被拒绝。")
        if args.device:
            print(f"  设备 {host}:{args.port} 不在线，或端口不是 {args.port}。")
            print("  先取设备 UPnP 描述文档，里面 <webSocket> 字段才是真实端口。")
        else:
            print("  TPS 没在运行，或本地 API 未启用。")
            print("  先启动 TrackMan Performance Studio，再执行本脚本。")
        print(f"  也可用 --port 指定别的端口，或用 --device <IP> 直连设备。")
        return 2
    except (ConnectionError, OSError) as e:
        print(f"\n[失败] {e}")
        return 2
    except KeyboardInterrupt:
        print("\n[退出] 手动中断。")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
