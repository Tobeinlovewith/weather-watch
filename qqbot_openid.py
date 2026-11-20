"""抓取 QQ 官方机器人收到的消息，用来拿到用户/群的 openid。

用法：
    python qqbot_openid.py            # 连上后等消息，收到就打印 openid
    python qqbot_openid.py 120        # 最多等 120 秒

原理：拿 access_token → GET /gateway 拿 wss 地址 → 发送 Identify（intents=C2C+GROUP）
     → 收到 C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE 时解析 author.user_openid / group_openid

注意：官方机器人只会在「用户主动发消息」时下发事件，所以运行期间需要有人在 QQ 里给机器人发一句。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

INTENT_C2C = 1 << 25
INTENT_GROUP_AT = 1 << 25 | 0  # 群 @ 消息与单聊共用 MESSAGE 组，按需调整


def ws_connect(url: str):
    from urllib.parse import urlparse
    u = urlparse(url)
    host = u.hostname
    port = u.port or 443
    path = u.path + (("?" + u.query) if u.query else "")
    sock = socket.create_connection((host, port), timeout=15)
    ctx = ssl.create_default_context()
    sock = ctx.wrap_socket(sock, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
           f"Sec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("握手失败")
        buf += chunk
    return sock


def send_frame(sock, payload: bytes, opcode: int = 0x1):
    mask = os.urandom(4)
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


def read_frame(sock):
    def recvn(n):
        b = b""
        while len(b) < n:
            c = sock.recv(n - len(b))
            if not c:
                raise ConnectionError("closed")
            b += c
        return b
    h = recvn(2)
    opcode = h[0] & 0x0F
    length = h[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", recvn(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recvn(8))[0]
    mask = recvn(4) if h[1] & 0x80 else None
    data = recvn(length)
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


def main(argv):
    wait = int(argv[1]) if len(argv) > 1 else 180
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    q = cfg.get("qqofficial") or {}
    import qqofficial as qo
    tok = qo.access_token(q)
    if not tok:
        print("拿不到 access_token，检查 app_id/app_secret")
        return 1
    url = qo.gateway(q)
    print("gateway:", url)
    if not url:
        print("拿不到 gateway（多半是 IP 白名单没加）")
        return 1

    sock = ws_connect(url)
    print("已连接，等待 Hello…")
    op, data = read_frame(sock)
    hello = json.loads(data)
    hb = hello.get("d", {}).get("heartbeat_interval", 30000) / 1000
    print("heartbeat_interval:", hb, "秒")

    identify = {
        "op": 2,
        "d": {"token": f"QQBot {tok}", "intents": INTENT_C2C | 1 << 30,
              "shard": [0, 1], "properties": {}},
    }
    send_frame(sock, json.dumps(identify).encode())
    print("已发送 Identify，现在去 QQ 里给机器人发一句话…")

    sock.settimeout(hb)
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            op, data = read_frame(sock)
        except socket.timeout:
            send_frame(sock, json.dumps({"op": 1, "d": None}).encode())
            continue
        if op != 0x1:
            continue
        ev = json.loads(data)
        t = ev.get("t")
        if not t:
            continue
        d = ev.get("d") or {}
        author = d.get("author") or {}
        if t == "C2C_MESSAGE_CREATE":
            print("\n【单聊】user_openid =", author.get("user_openid"))
            print("内容:", d.get("content"))
            return 0
        if t in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            print("\n【群聊】group_openid =", d.get("group_openid"))
            print("user_openid =", author.get("member_openid") or author.get("user_openid"))
            print("内容:", d.get("content"))
            return 0
    print("超时，没收到消息")
    return 2


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv))
