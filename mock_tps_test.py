import socket, threading, struct, json, base64, hashlib, time, sys

# 模拟 TPS 本地 WS 服务端，用于验证 trackman_live.py 的客户端实现
SHOTS = [
    {"Timestamp": "2026-10-23T18:40:01", "Club": "7 Iron", "BallSpeed": 118.4,
     "ClubSpeed": 84.2, "SmashFactor": 1.41, "LaunchAngle": 18.2, "SpinRate": 6320,
     "SpinAxis": -3.1, "ClubPath": 1.2, "FaceAngle": -0.8, "AttackAngle": -3.4,
     "Carry": 152.3, "Total": 161.8, "MaxHeight": 27.5, "HangTime": 6.2,
     "BallTrajectory": [{"X": i, "Y": i * 1.3, "Z": i * 0.4} for i in range(300)]},
    {"Timestamp": "2026-10-23T18:41:12", "Club": "Driver", "BallSpeed": 167.9,
     "ClubSpeed": 113.6, "SmashFactor": 1.48, "LaunchAngle": 11.4, "SpinRate": 2410,
     "SpinAxis": 6.7, "ClubPath": -2.4, "FaceAngle": 1.9, "AttackAngle": 3.8,
     "Carry": 258.4, "Total": 279.1, "MaxHeight": 31.2, "HangTime": 6.9,
     "Player": {"Name": "Test Player"}},
    {"Timestamp": "2026-10-23T18:42:30", "Club": "PW", "BallSpeed": 96.1,
     "ClubSpeed": 78.3, "LaunchAngle": 26.8, "SpinRate": 9120, "Carry": 118.2,
     "Total": 121.4, "HangTime": 5.4},
    {"Timestamp": "2026-10-23T18:43:02", "Club": "5 Iron", "BallSpeed": 132.2,
     "ClubSpeed": 92.1, "LaunchAngle": 14.9, "SpinRate": 5180, "Carry": 178.6,
     "Total": 191.3, "HangTime": 6.5},
    {"Timestamp": "2026-10-23T18:44:20", "Club": "SW", "BallSpeed": 78.4,
     "ClubSpeed": 62.7, "LaunchAngle": 32.1, "SpinRate": 10450, "Carry": 88.7,
     "Total": 90.2, "HangTime": 4.9},
]


def frame(opcode, data, fin=True):
    b1 = (0x80 if fin else 0) | opcode
    header = bytearray([b1])
    n = len(data)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126); header += struct.pack(">H", n)
    else:
        header.append(127); header += struct.pack(">Q", n)
    return bytes(header) + data


def handle(conn):
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = conn.recv(4096)
        if not c:
            return
        buf += c
    head, _, rest = buf.partition(b"\r\n\r\n")
    key = ""
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip().decode()
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    conn.sendall(
        ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
         f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())

    # 读客户端订阅（要解掩码）
    def read_client():
        d = rest
        while len(d) < 2:
            d += conn.recv(4096)
        b1, b2 = d[0], d[1]
        masked, ln = b2 >> 7, b2 & 0x7F
        off = 2
        if ln == 126:
            ln = struct.unpack(">H", d[2:4])[0]; off = 4
        elif ln == 127:
            ln = struct.unpack(">Q", d[2:10])[0]; off = 10
        mask = d[off:off + 4]; off += 4
        while len(d) < off + ln:
            d += conn.recv(4096)
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(d[off:off + ln]))
        return payload.decode()

    sub = read_client()
    print("SERVER 收到订阅:", sub, flush=True)
    conn.sendall(frame(0x1, json.dumps({"Type": "Acknowledge", "SubType": "Subscribe"}).encode()))
    conn.sendall(frame(0x1, json.dumps({"Type": "DeviceStatus", "Payload": {"IsConnected": True, "IsMeasuring": False}}).encode()))
    conn.sendall(frame(0x1, json.dumps({"Type": "Ping"}).encode()))

    for i, shot in enumerate(SHOTS):
        time.sleep(0.25)
        body = json.dumps({"Type": "Measurement", "Payload": {"Kind": "LaunchData", "Data": shot}}).encode()
        if i == 0:
            # 第一条故意分片，验证客户端拼接
            half = len(body) // 2
            conn.sendall(frame(0x1, body[:half], fin=False))
            conn.sendall(frame(0x0, body[half:], fin=True))
            print("SERVER 已发第1杆(分片)", flush=True)
        else:
            conn.sendall(frame(0x1, body))
            print(f"SERVER 已发第{i+1}杆", flush=True)

    time.sleep(3)
    conn.close()


srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 21599))
srv.listen(1)
print("SERVER 监听", srv.getsockname(), flush=True)
c, _ = srv.accept()
handle(c)
