"""告警通道：QQ 掉了也能通知到人。

不依赖 QQ 登录、不依赖外网工具，全部走国内可直连的接口：
  - 邮件（QQ 邮箱 / 163 的 SMTP，用授权码）
  - PushPlus（微信推送，扫码绑定）
  - Server 酱（微信推送）
  - 企业微信群机器人 webhook
  - 钉钉群机器人 webhook
  - 飞书群机器人 webhook
  - Bark（iOS）

配置在 config.json 的 "alert" 段；哪个填了就走哪个，全空则静默。
"""
from __future__ import annotations

import json
import smtplib
import ssl
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

BASE = Path(__file__).resolve().parent
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"}


def _post_json(url: str, payload: dict, timeout: float = 10) -> bool:
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            **UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _get(url: str, timeout: float = 10) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def send_email(cfg: dict, subject: str, body: str) -> bool:
    e = cfg or {}
    host, port = e.get("smtp_host"), int(e.get("port") or 465)
    user, pwd, to = e.get("user"), e.get("pass"), e.get("to") or e.get("user")
    if not (host and user and pwd and to):
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(("天气守护", user))
        msg["To"] = to
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) as s:
            s.login(user, pwd)
            s.sendmail(user, [to], msg.as_string())
        return True
    except Exception:
        return False


def send_pushplus(cfg: dict, subject: str, body: str) -> bool:
    token = (cfg or {}).get("token")
    if not token:
        return False
    return _post_json("https://www.pushplus.plus/send", {
        "token": token, "title": subject, "content": body, "template": "txt"})


def send_serverchan(cfg: dict, subject: str, body: str) -> bool:
    key = (cfg or {}).get("key")
    if not key:
        return False
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = urllib.parse.urlencode({"title": subject, "desp": body}).encode()
    try:
        req = urllib.request.Request(url, data=data, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def send_wecom(cfg: dict, subject: str, body: str) -> bool:
    hook = (cfg or {}).get("webhook")
    if not hook:
        return False
    return _post_json(hook, {"msgtype": "text",
                             "text": {"content": f"{subject}\n{body}"}})


def send_dingtalk(cfg: dict, subject: str, body: str) -> bool:
    hook = (cfg or {}).get("webhook")
    if not hook:
        return False
    return _post_json(hook, {"msgtype": "text",
                             "text": {"content": f"{subject}\n{body}"}})


def send_feishu(cfg: dict, subject: str, body: str) -> bool:
    hook = (cfg or {}).get("webhook")
    if not hook:
        return False
    return _post_json(hook, {"msg_type": "text",
                             "content": {"text": f"{subject}\n{body}"}})


def send_bark(cfg: dict, subject: str, body: str) -> bool:
    base = (cfg or {}).get("url")
    if not base:
        return False
    url = base.rstrip("/") + "/" + urllib.parse.quote(subject) + "/" + urllib.parse.quote(body)
    return _get(url)


CHANNELS = {
    "email": send_email,
    "pushplus": send_pushplus,
    "serverchan": send_serverchan,
    "wecom": send_wecom,
    "dingtalk": send_dingtalk,
    "feishu": send_feishu,
    "bark": send_bark,
}


def notify(cfg: dict, subject: str, body: str, log=None) -> list[str]:
    """按配置依次尝试所有通道，返回成功的通道名。"""
    al = (cfg or {}).get("alert") or {}
    if not al.get("enabled"):
        return []
    ok = []
    for name, fn in CHANNELS.items():
        conf = al.get(name) or {}
        if not conf:
            continue
        try:
            if fn(conf, subject, body):
                ok.append(name)
        except Exception:
            continue
    if log is not None:
        try:
            log(f"[告警] {subject} -> {ok or '无可用通道'}")
        except Exception:
            pass
    return ok


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    res = notify(cfg, "天气守护自检",
                 "这是一条测试告警：如果你在微信/邮箱收到，说明通道可用。", print)
    print("成功通道:", res or "无（请先在 config.json 的 alert 段填写）")
