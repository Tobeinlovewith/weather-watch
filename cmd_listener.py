"""群里的城市管理指令监听器。

连着 NapCat 的 OneBot WebSocket（和 DSH 那条并行，互不影响），只听群消息：
    #加城市 上海      → 加到群订阅
    #删城市 上海      → 从群里去掉
    #城市列表         → 看群现在订了哪些城市

配置在 config.json 的 commands 段：
    "commands": { "group_id": 000000000, "prefix": "#", "allow": [] }
allow 为空 = 群里谁都能发；填 QQ 号 = 只认这些人。
"""
from __future__ import annotations

import sys as _sys, os as _os
if _sys.stdout is None: _sys.stdout = open(_os.devnull, 'w')
if _sys.stderr is None: _sys.stderr = open(_os.devnull, 'w')

import contextlib
import io
import json
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import city_cli  # noqa: E402
import rain_watch as rw  # noqa: E402
import ws_send  # noqa: E402

LOCK_PORT = 47822
CMD_RE = re.compile(r"^[#＃]\s*(加城市|添加城市|删城市|删除城市|城市列表|城市)\s*(.*)$")
MAX_TEXT = 400


def singleton():
    import socket
    s = socket.socket()
    # Windows 下必须用 SO_EXCLUSIVEADDRUSE，否则第二个实例也能绑上同名端口（单实例锁失效）
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except (AttributeError, OSError):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(64)
        import threading as _th
        def _accept_loop(_s=s):
            while True:
                try:
                    _c, _ = _s.accept()
                    _c.close()
                except Exception:
                    break
        _th.Thread(target=_accept_loop, daemon=True).start()
        return s
    except OSError:
        return None


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def run_cli(args: list[str]) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            if args[0] == "add":
                code = city_cli.cmd_add(args[1], "group")
            elif args[0] == "remove":
                code = city_cli.cmd_remove(args[1])
            else:
                code = city_cli.cmd_list()
        except SystemExit as exc:
            code = int(exc.code or 1)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=buf)
            code = 1
    out = buf.getvalue().strip()
    return ("✅ " if code == 0 else "❌ ") + (out or ("成功" if code == 0 else "失败"))


_WEATHER_FILLERS = ("今天白天", "今天", "今日", "明天", "明日", "天气",
                    "怎么样", "如何", "咋样", "怎样", "好不好", "情况", "呢", "啊")


PRECIP_Q = ("会不会下", "有没有雨", "有没有雪", "下不下", "要下雨", "要下雪",
            "有雨吗", "有雪吗", "下雨吗", "下雪吗", "带伞", "会不会下雨", "会不会下雪")

_CITY_CACHE = {"names": None}


def _city_names() -> list[str]:
    """全国城市名（来自中央气象台索引），用来从句子里认出城市。"""
    if _CITY_CACHE["names"] is None:
        try:
            import nmc
            idx = nmc.build_index().get("cities") or {}
            names = [n for n in idx if 2 <= len(n) <= 4]
            names.sort(key=len, reverse=True)
            _CITY_CACHE["names"] = names
        except Exception:
            _CITY_CACHE["names"] = []
    return _CITY_CACHE["names"]


def _cities_in(text: str) -> list[str]:
    """返回句子里出现的城市名（长名优先，避免「大兴」被「兴」之类干扰）。"""
    hits, rest = [], text
    for name in _city_names():
        if name and name in rest:
            hits.append(name)
            rest = rest.replace(name, " ")
    return hits


def _resolve_locs(cfg: dict, text: str, loc_keys: list[str]):
    """返回（要回答的城市列表, 未订阅的城市名或 None）。"""
    locs_all = rw.locations(cfg)
    names = _cities_in(text)
    if not names:
        picked = [l for l in locs_all if l["key"] in (loc_keys or [])]
        return (picked or locs_all), None
    hit, unknown = [], []
    for n in names:
        m = [l for l in locs_all if n in l["name"] or l["name"] in n]
        if m:
            hit.extend(m)
        else:
            unknown.append(n)
    if hit:
        return hit[:2], None
    return [], unknown[0] if unknown else None


def _not_subscribed(name: str, cfg: dict) -> str:
    names = "、".join(l["name"] for l in rw.locations(cfg))
    return f"「{name}」还没订阅哦，现在是：{names}\n要加的话发「#加城市 {name}」"



_FILLERS = ("今天白天", "今天", "今日", "明天", "明日", "明早", "明晚", "早上", "上午",
            "中午", "下午", "晚上", "夜里", "白天", "天气", "怎么样", "如何", "咋样",
            "怎样", "好不好", "情况", "请问", "帮我", "看看", "查一下", "会不会",
            "有没有", "下不下", "现在", "外面", "呢", "啊", "呀", "吗", "吧", "的",
            "了", "会", "要", "下", "雨", "雪", "带伞", "出门", "?", "？", "。", "，",
            ",", "!", "！", "~", " ")


def _leftover(text: str) -> str:
    """去掉所有已知成分后剩下的内容；非空通常说明是闲聊。"""
    rest = text
    for w in _FILLERS:
        rest = rest.replace(w, "")
    for name in _cities_in(text):
        rest = rest.replace(name, "")
    return rest.strip(" ，,。.？?！!~")


def _is_query(text: str) -> bool:
    return not _leftover(text)


def _precip_answer(cfg: dict, text: str, loc_keys: list[str]) -> str | None:
    """回答「明早会不会下雨」「今晚有雨吗」这类是非问句。"""
    if not any(k in text for k in PRECIP_Q):
        return None
    if not _is_query(text):
        return None
    mode = "evening" if any(k in text for k in ("明天", "明早", "明日", "明晚")) else "morning"
    locs, unknown = _resolve_locs(cfg, text, loc_keys)
    if not locs:
        return _not_subscribed(unknown or "这个城市", cfg)

    log_ = rw.setup_logging()
    now = datetime.now(rw.TZ)
    out = []
    for loc in locs:
        try:
            msg = rw.build_briefing(cfg, loc, mode, now, log_)
        except Exception:
            msg = None
        if not msg:
            out.append(f"【{loc['name']}】暂时取不到数据")
            continue
        wet = ("⚠️" in msg) and ("没有雨雪" not in msg)
        verdict = "要下" if wet else "不下"
        out.append(f"【{loc['name']}】{'今天' if mode == 'morning' else '明天'}{verdict}。\n{msg}")
    return "\n\n".join(out)


def weather_reply(cfg: dict, text: str, loc_keys: list[str]) -> str | None:
    """识别「今天天气怎么样 / 明天天气 / 成都天气」这类问法。"""
    t = text.strip()
    if "天气" not in t:
        return _precip_answer(cfg, t, loc_keys)

    if not _is_query(t):
        return None
    mode = "evening" if any(k in t for k in ("明天", "明早", "明日", "明晚")) else "morning"
    locs, unknown = _resolve_locs(cfg, t, loc_keys)
    if not locs:
        return _not_subscribed(unknown or "这个城市", cfg)

    log_ = rw.setup_logging()
    now = datetime.now(rw.TZ)
    out = []
    for loc in locs:
        try:
            msg = rw.build_briefing(cfg, loc, mode, now, log_)
        except Exception:
            msg = None
        out.append(msg or f"【{loc['name']}】暂时取不到天气数据")
    return "\n\n".join(out)


def handle_group_text(text: str, loc_keys: list[str] | None = None) -> str | None:
    text = text.strip()
    if len(text) > MAX_TEXT:
        return None
    # 先试天气问法
    try:
        cfg = rw.load_config()
        reply = weather_reply(cfg, text, loc_keys or [])
        if reply:
            return reply
    except Exception:
        pass
    m = CMD_RE.match(text)
    if not m:
        return None
    verb, arg = m.group(1), m.group(2).strip()
    if verb in ("加城市", "添加城市"):
        if not arg:
            return "要加哪个城市？发「#加城市 上海」这样"
        return run_cli(["add", arg])
    if verb in ("删城市", "删除城市"):
        if not arg:
            return "要删哪个城市？发「#删城市 上海」这样"
        return run_cli(["remove", arg])
    return run_cli(["list"])


def strip_cq(text: str) -> str:
    return re.sub(r"\[CQ:[^\]]*\]", "", text or "").strip()




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
    lock = singleton()
    if lock is None:
        print("指令监听已在跑，退出")
        return 1
    cfg = rw.load_config()
    conf = cfg.get("commands") or {}
    group_id = int(conf.get("group_id") or 0)
    allow = {int(x) for x in (conf.get("allow") or [])}
    owners = {int(x) for x in (conf.get("owner") or [10000])}
    if not group_id:
        print("config.json 里没有 commands.group_id，退出")
        return 1
    log(f"指令监听启动：群 {group_id}，{'仅限白名单' if allow else '群成员均可'}")

    stop_flag = BASE / "STOP"
    backoff = 5
    while True:
        touch_heartbeat("cmd_listener")
        if stop_flag.exists():
            log("检测到 STOP 文件，退出")
            return 0
        sock = None
        try:
            sock = ws_send.connect()
            sock.settimeout(60)
            log("已连上 NapCat")
            backoff = 5
            idle = 0.0
            while True:
                try:
                    fin, opcode, payload = ws_send._read_frame(sock)
                except TimeoutError:
                    idle += 60
                    if idle >= 180:
                        log("空闲太久，重连")
                        break
                    ws_send._send_frame(sock, b"", 0x9)  # ping
                    continue
                if opcode == 0x9:
                    ws_send._send_frame(sock, payload, 0xA)
                    continue
                if opcode == 0x8:
                    raise ConnectionError("server closed")
                if opcode not in (0x1, 0x2):
                    continue
                try:
                    ev = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                post = ev.get("post_type")
                if post != "message":
                    continue
                sender = int(ev.get("user_id") or 0)
                if sender == ws_send.SELF_ID:
                    continue
                mtype = ev.get("message_type")
                text = strip_cq(ev.get("raw_message") or ev.get("message") or "")
                if mtype == "group" and int(ev.get("group_id") or 0) == group_id:
                    if allow and sender not in allow:
                        continue
                    keys = [t["locations"] for t in rw.targets(cfg) if t["kind"] == "group" and t["id"] == group_id]
                    reply = handle_group_text(text, keys[0] if keys else [])
                    if reply:
                        log(f"[群 {sender}] {text!r} -> {reply[:80]}")
                        ws_send.send_group(group_id, reply[:1500])
                elif mtype == "private" and sender in owners:
                    keys = [t["locations"] for t in rw.targets(cfg) if t["kind"] == "private" and t["id"] == sender]
                    reply = handle_group_text(text, keys[0] if keys else [])
                    if reply:
                        log(f"[私聊 {sender}] {text!r} -> {reply[:80]}")
                        ws_send.send_private(sender, reply[:1500])
        except KeyboardInterrupt:
            log("手动停止")
            return 0
        except Exception as exc:  # noqa: BLE001
            log(f"连接出错：{type(exc).__name__} {exc}，{backoff}s 后重连")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
