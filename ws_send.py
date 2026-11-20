"""Minimal OneBot-11 WebSocket client (RFC 6455) - no external deps.

Usage:
    python ws_send.py private 10000 "text"
    python ws_send.py group 000000000 "text"
"""
import base64
import json
import os
import socket
import ssl
import struct
import sys
import time

WS_HOST = "127.0.0.1"
WS_PORT = 3001
TOKEN = "dsh-cb2d047ea83f6159"
SELF_ID = 3884617214


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def _read_frame(sock):
    hdr = _recv_exact(sock, 2)
    fin = hdr[0] & 0x80
    opcode = hdr[0] & 0x0F
    masked = hdr[1] & 0x80
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


def _send_frame(sock, payload, opcode=0x1):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    header = bytearray()
    header.append(0x80 | opcode)
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def connect(timeout=10):
    sock = socket.create_connection((WS_HOST, WS_PORT), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {WS_HOST}:{WS_PORT}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {TOKEN}\r\n"
        f"X-Self-ID: {SELF_ID}\r\n"
        f"X-Client-Role: Universal\r\n"
        f"\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("handshake closed")
        resp += chunk
    head = resp.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    if "101" not in head.split("\r\n")[0]:
        raise ConnectionError("handshake failed: " + head.split("\r\n")[0])
    return sock


def call(action, params, timeout=15):
    sock = connect()
    try:
        echo = str(int(time.time() * 1000))
        _send_frame(sock, json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
        deadline = time.time() + timeout
        while time.time() < deadline:
            sock.settimeout(max(1, deadline - time.time()))
            fin, opcode, payload = _read_frame(sock)
            if opcode == 0x9:  # ping
                _send_frame(sock, payload, 0xA)
                continue
            if opcode == 0x8:
                raise ConnectionError("server closed")
            if opcode in (0x1, 0x2):
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                if str(msg.get("echo")) == echo:
                    return msg
        raise TimeoutError("no response")
    finally:
        try:
            sock.close()
        except Exception:
            pass


def send_private(user_id, text):
    return call("send_private_msg", {"user_id": int(user_id), "message": text})


def send_group(group_id, text):
    return call("send_group_msg", {"group_id": int(group_id), "message": text})


if __name__ == "__main__":
    kind = sys.argv[1]
    target = int(sys.argv[2])
    text = sys.argv[3] if len(sys.argv) > 3 else "test"
    fn = send_private if kind == "private" else send_group
    print(json.dumps(fn(target, text), ensure_ascii=False))
