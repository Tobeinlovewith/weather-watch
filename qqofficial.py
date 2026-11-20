"""QQ 官方机器人（开放平台）客户端。

用途：
  1. 作为「不依赖第三方协议」的稳定通道 —— 不会因为 SnowCat 被踢而失联
  2. 低频告警：QQ 掉线、守护启动失败等
  3. 被动应答：群里 @ 机器人问天气

平台约束（截至文档核对）：
  - 主动推送能力在 2025-04-21 起有过停用公告；现行文档仍列出主动消息频控
    （单聊 4 条/月/用户；群聊受 20/qpm、1000 条/天约束，群侧还需机器人具备权限）
  - 被动回复：收到消息后群聊 5 分钟内最多 5 条、单聊 60 分钟内最多 4 条
  - 因此：**定时播报仍走 SnowCat，官方机器人只做低频告警 + 被动应答**

需要的参数（q.qq.com 开放平台 → 你的机器人 → 开发设置）：
  - app_id
  - app_secret
  - 群聊还需要该群的 group_openid（收到一次群消息事件后能看到）

配置写入 config.json：
  "qqofficial": {"enabled": true, "app_id": "...", "app_secret": "...",
                 "alarm_group_openid": "", "alarm_user_openid": ""}
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
API = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

_token_cache = {"value": None, "expire_at": 0}


def _post(url: str, payload: dict, headers: dict | None = None, timeout: float = 12):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"raw": raw[:200]}


def _get(url: str, headers: dict, timeout: float = 12):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"raw": raw[:200]}



PATCH_FILE = Path(r"C:\Users\li180\.dsh\profiles\desktop\cordis.patch.yml")


def _from_patch() -> dict:
    """config.json 没填时，从 dsh profile 的 patch 文件里兜底读取。"""
    import re
    try:
        txt = PATCH_FILE.read_text(encoding="utf-8")
        app = re.search(r'appId:\s*"?(\d+)"?', txt)
        sec = re.search(r'appSecret:\s*"?([\w\-]+)"?', txt)
        openid = re.search(r'c2cAllow:\s*\n\s*-\s*([0-9A-F]+)', txt)
        return {"app_id": app.group(1) if app else None,
                "app_secret": sec.group(1) if sec else None,
                "alarm_user_openid": openid.group(1) if openid else None}
    except Exception:
        return {}


def resolve(cfg: dict) -> dict:
    """返回可用的凭据：config 优先，patch 兜底。"""
    q = dict((cfg or {}).get("qqofficial") or {})
    if not q.get("app_secret") or not q.get("app_id"):
        fb = _from_patch()
        for k, v in fb.items():
            if v and not q.get(k):
                q[k] = v
    return q


def access_token(cfg: dict) -> str | None:
    """AppID + AppSecret 换 access_token（默认 7200 秒，提前 5 分钟刷新）。"""
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expire_at"]:
        return _token_cache["value"]
    _q = resolve(cfg)
    app_id = _q.get("app_id")
    secret = _q.get("app_secret")
    if not (app_id and secret):
        return None
    try:
        d = _post(TOKEN_URL, {"appId": str(app_id), "clientSecret": secret})
        tok = d.get("access_token")
        if not tok:
            return None
        ttl = int(d.get("expires_in") or 7200)
        _token_cache.update({"value": tok, "expire_at": now + max(ttl - 300, 60)})
        return tok
    except Exception:
        return None


def _auth_headers(cfg: dict) -> dict | None:
    tok = access_token(cfg)
    if not tok:
        return None
    return {"Authorization": f"QQBot {tok}", "X-Union-Appid": str(cfg.get("app_id"))}


def send_group(cfg: dict, group_openid: str, content: str,
               msg_id: str | None = None, msg_seq: int = 1) -> dict:
    """发群消息。带 msg_id = 被动回复；不带 = 主动消息（受配额限制）。"""
    h = _auth_headers(cfg)
    if not h:
        return {"error": "no_token"}
    body = {"content": content, "msg_type": 0}
    if msg_id:
        body["msg_id"] = msg_id
        body["msg_seq"] = msg_seq
    url = f"{API}/v2/groups/{urllib.parse.quote(group_openid)}/messages"
    try:
        return _post(url, body, h)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def send_c2c(cfg: dict, user_openid: str, content: str,
             msg_id: str | None = None, msg_seq: int = 1) -> dict:
    """发单聊消息。"""
    h = _auth_headers(cfg)
    if not h:
        return {"error": "no_token"}
    body = {"content": content, "msg_type": 0}
    if msg_id:
        body["msg_id"] = msg_id
        body["msg_seq"] = msg_seq
    url = f"{API}/v2/users/{urllib.parse.quote(user_openid)}/messages"
    try:
        return _post(url, body, h)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


GATEWAY_URL = "wss://api.sgroup.qq.com/websocket"


def gateway(cfg: dict) -> str | None:
    """官方网关地址是固定的；/gateway 接口有严格频控，默认直接用常量。"""
    if _auth_headers(cfg):
        return GATEWAY_URL
    return None


def notify(cfg: dict, subject: str, body: str, log=None) -> bool:
    """给配置里的告警目标发一条（优先群，其次单聊）。"""
    conf = resolve(cfg)
    if not conf.get("enabled"):
        return False
    text = f"{subject}\n{body}"
    ok = False
    gid = conf.get("alarm_group_openid")
    if gid:
        r = send_group(conf, gid, text)
        ok = ok or bool(r.get("id"))
        if log:
            log(f"[官方机器人-群] {r.get('id') or r}")
    uid = conf.get("alarm_user_openid")
    if uid:
        r = send_c2c(conf, uid, text)
        ok = ok or bool(r.get("id"))
        if log:
            log(f"[官方机器人-单聊] {r.get('id') or r}")
    return ok


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    q = cfg.get("qqofficial") or {}
    print("配置:", {k: (v if k != "app_secret" else "***") for k, v in q.items()})
    tok = access_token(q)
    print("access_token:", (tok[:12] + "...") if tok else "获取失败（检查 app_id/app_secret）")
    print("gateway:", gateway(q))
