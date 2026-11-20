"""QQ 官方机器人遥控服务：在 QQ 里发「检查 / 救活 / 停 / 恢复」，立刻执行并回结果。

自己维护 WebSocket（不依赖 dsh-qqbot 插件），所以：
  - 第三方 QQ 挂了照样能用
  - 收到指令先立刻回「收到，正在…」，再执行，不会让人干等 60 秒
  - 执行结果分片回传（官方单条 4500 字符上限）

运行：pythonw qqbot_service.py    （常驻、无窗口）
单实例锁：端口 47825
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

PY = r"C:\Users\li180\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
LOG = BASE / "logs" / "qqbot_service.log"
LOCK_PORT = 47825
CHUNK = 4500

COMMANDS = {
    "检查": ("检查", "正在体检…"),
    "状态": ("检查", "正在体检…"),
    "体检": ("检查", "正在体检…"),
    "救活": ("救活", "正在救活第三方 QQ（约 1 分钟，别急）…"),
    "重启": ("救活", "正在重启第三方 QQ（约 1 分钟）…"),
    "复活": ("救活", "正在救活第三方 QQ（约 1 分钟）…"),
    "停": ("停", "正在停止天气守护…"),
    "停止": ("停", "正在停止天气守护…"),
    "恢复": ("恢复", "正在恢复天气守护…"),
}


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- WebSocket 底层
def ws_connect(url: str):
    u = urlparse(url)
    host, port, path = u.hostname, u.port or 443, u.path or "/"
    sock = socket.create_connection((host, port), timeout=15)
    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                  f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = sock.recv(4096)
        if not c:
            raise ConnectionError("握手失败")
        buf += c
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
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


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


# ---------------------------------------------------------------- 业务
_seq = {}          # msg_id -> 已用序号（同一 msg_id 内必须递增，否则 400）
_seq_lock = threading.Lock()


def _next_seq(msg_id: str) -> int:
    with _seq_lock:
        n = _seq.get(msg_id, 0) + 1
        _seq[msg_id] = n
        if len(_seq) > 200:                     # 防止无限增长
            for k in list(_seq)[:50]:
                _seq.pop(k, None)
        return n


def reply(cfg: dict, kind: str, target: str, content: str, msg_id: str | None):
    import qqofficial as qo
    for i in range(0, len(content), CHUNK):
        part = content[i:i + CHUNK]
        seq = _next_seq(msg_id) if msg_id else 1
        if kind == "c2c":
            r = qo.send_c2c(cfg, target, part, msg_id=msg_id, msg_seq=seq)
        else:
            r = qo.send_group(cfg, target, part, msg_id=msg_id, msg_seq=seq)
        log(f"[回复 {kind} seq={seq}] {str(r)[:80]}")
        if "error" in r and msg_id:
            # 被动回复失败（超窗/超次）就改发主动消息，尽量把结果送出去
            try:
                if kind == "c2c":
                    r2 = qo.send_c2c(cfg, target, part)
                else:
                    r2 = qo.send_group(cfg, target, part)
                log(f"[回复 {kind} 主动兜底] {str(r2)[:80]}")
            except Exception as e:
                log(f"[回复 {kind} 主动兜底失败] {e}")
        time.sleep(0.3)


def run_action(action: str) -> str:
    script = BASE / "go.py"
    try:
        p = subprocess.run([PY, "-u", str(script), action], cwd=str(BASE),
                           capture_output=True, text=True, timeout=240,
                           creationflags=0x08000000)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        return out if out else (err or f"（{action} 执行完毕，无输出）")
    except subprocess.TimeoutExpired:
        return f"{action} 执行超时（4 分钟）"
    except Exception as e:
        return f"{action} 执行出错：{e}"


def handle_event(cfg: dict, ev: dict) -> None:
    t = ev.get("t")
    d = ev.get("d") or {}
    if t == "C2C_MESSAGE_CREATE":
        kind = "c2c"
        target = (d.get("author") or {}).get("user_openid") or ""
    elif t in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
        kind = "group"
        target = d.get("group_openid") or ""
    else:
        return

    text = (d.get("content") or "").strip()
    msg_id = d.get("id")
    log(f"[收到 {kind}] {target[:12]}… : {text[:60]}")

    # 允许带前缀（比如 "/检查" 或 "@bot 检查"）
    cleaned = text.lstrip("/／#＃ ").strip()
    hit = None
    for key, (action, ack) in COMMANDS.items():
        if cleaned.startswith(key):
            hit = (key, action, ack)
            break
    if not hit:
        reply(cfg, kind, target, "可用指令：检查 / 救活 / 停 / 恢复", msg_id)
        return

    key, action, ack = hit
    # 先秒回执，避免用户干等
    reply(cfg, kind, target, f"收到：{ack}", msg_id)

    def worker():
        result = run_action(action)
        reply(cfg, kind, target, result, msg_id)

    threading.Thread(target=worker, daemon=True).start()




def touch_heartbeat(name):
    """由主循环自己调用：主循环卡住时心跳就会停，才能反映"真的没在干活"。"""
    import os as _o, time as _t
    try:
        p = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "logs", f"hb_{name}.txt")
        _o.makedirs(_o.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(str(int(_t.time())))
    except Exception:
        pass


def main() -> int:
    lock = socket.socket()
    try:
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except (AttributeError, OSError):
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(8)
    except OSError:
        return 0

    while True:
        try:
            cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
            q = cfg.get("qqofficial") or {}
            if not q.get("enabled"):
                time.sleep(60)
                continue
            import qqofficial as qo
            tok = qo.access_token(q)
            if not tok:
                log("取不到 token，60 秒后重试")
                time.sleep(60)
                continue
            sock = ws_connect(qo.GATEWAY_URL)
            op, data = read_frame(sock)
            hb = (json.loads(data).get("d") or {}).get("heartbeat_interval", 30000) / 1000
            send_frame(sock, json.dumps({
                "op": 2,
                "d": {"token": f"QQBot {tok}", "intents": (1 << 25) | (1 << 30),
                      "shard": [0, 1], "properties": {}},
            }).encode())
            log(f"已连接网关，心跳 {hb:.0f}s")
            sock.settimeout(hb)
            last = time.time()
            while True:
                touch_heartbeat("qqbot_service")
                try:
                    op, data = read_frame(sock)
                except socket.timeout:
                    send_frame(sock, json.dumps({"op": 1, "d": None}).encode())
                    continue
                if op == 0x8:
                    raise ConnectionError("服务端关闭")
                if op != 0x1:
                    continue
                ev = json.loads(data)
                if ev.get("op") == 11 or ev.get("t") == "READY":
                    continue
                handle_event(cfg, ev)
                if time.time() - last > 3600:
                    raise ConnectionError("运行 1 小时，主动重连")
        except Exception as e:
            log(f"连接异常：{type(e).__name__}: {e}，10 秒后重连")
            time.sleep(10)


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
