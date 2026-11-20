"""QQ 通道远程运维 CLI —— 给 QQ 官方机器人那边的 agent 调用的救活工具。

用法（在 default-workspace 目录下）：
    python weather_watch/qqbot_ctl.py status     # 体检：QQ / SnowCat / 守护 / 今日播报
    python weather_watch/qqbot_ctl.py restart    # 重启第三方 QQ（SnowCat）
    python weather_watch/qqbot_ctl.py kill       # 只结束 QQ，不重启
    python weather_watch/qqbot_ctl.py stop       # 停掉天气守护（置 STOP）
    python weather_watch/qqbot_ctl.py start      # 恢复天气守护（删 STOP + 拉起看门狗）

输出是给人看的纯文本，可以直接转发到 QQ。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
SNOWCAT = r"D:\SnowCat.Portable\tools\build\snowcat-run.ps1"
SHELL_DIR = r"D:\SnowCat.Portable\SnowCat.9.9.37-54395.Shell"
UIN = "3884617214"
PYW = r"C:\Users\li180\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"

PORTS = {
    3001: "OneBot（QQ 是否在线）",
    6099: "SnowCat WebUI",
    47820: "看门狗",
    47821: "雨天守护",
    47822: "指令监听",
}


def port_up(port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def qq_count() -> int:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=0x08000000).stdout
        return out.lower().count("qq.exe")
    except Exception:
        return -1


def login_info() -> dict | None:
    try:
        sys.path.insert(0, str(BASE))
        import ws_send
        d = ws_send.call("get_login_info", {}, timeout=6)
        return d.get("data") if str(d.get("status")) == "ok" else None
    except Exception:
        return None


def state_summary() -> dict:
    try:
        st = json.loads((BASE / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    today = datetime.now().strftime("%Y-%m-%d")
    briefs = [b for b in (st.get("sent_briefs") or []) if b.endswith(today)]
    return {"last_check": st.get("last_check"),
            "today_briefs": len(briefs),
            "outbox": len(st.get("outbox") or [])}



PORT_SCRIPT = {47820: "watchdog.pyw", 47821: "rain_watch.py",
               47822: "cmd_listener.py", 47825: "qqbot_service.py"}


def stale_code_report() -> str:
    """比对代码修改时间 vs 进程启动时间，提示哪些改动还没生效。"""
    import subprocess as _sp
    lines = []
    try:
        for port, script in PORT_SCRIPT.items():
            f = BASE / script
            if not f.exists():
                continue
            code_mtime = f.stat().st_mtime
            ps = (f"$c = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
                  f"Where-Object {{ $_.LocalPort -eq {port} }} | "
                  f"Select-Object -ExpandProperty OwningProcess -Unique; "
                  f"foreach ($p in $c) {{ (Get-Process -Id $p).StartTime.ToUniversalTime().ToString('o') }}")
            try:
                out = _sp.run(["powershell", "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, timeout=15,
                              creationflags=0x08000000).stdout.strip()
            except Exception:
                out = ""
            if not out:
                continue
            from datetime import timezone
            try:
                started = datetime.strptime(out.split(".")[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if started.timestamp() < code_mtime - 2:
                gap = int((code_mtime - started.timestamp()) / 60)
                lines.append(f"  ⚠️ {script} 代码比进程新 {gap} 分钟 → 需要重启")
    except Exception:
        pass
    return "\n".join(lines)


def cmd_status() -> str:
    lines = [f"【QQ 通道体检】{datetime.now():%Y-%m-%d %H:%M:%S}"]
    qq = qq_count()
    lines.append(f"QQ 进程：{qq} 个")
    for p, name in PORTS.items():
        lines.append(f"  {p} {name}：{'✅ 通' if port_up(p) else '❌ 不通'}")
    info = login_info()
    if info:
        lines.append(f"登录账号：{info.get('nickname')}（{info.get('user_id')}）")
    else:
        lines.append("登录账号：❌ 读不到（未登录或通道断了）")
    s = state_summary()
    if s:
        lines.append(f"守护最后心跳：{s.get('last_check')}")
        lines.append(f"今日已发播报：{s.get('today_briefs')} 条，待补发：{s.get('outbox')} 条")

    # 一句话结论
    if info and port_up(47821):
        verdict = "✅ 一切正常"
    elif qq == 0:
        verdict = "❌ QQ 没在跑 → 需要 restart"
    elif not port_up(6099):
        verdict = "❌ SnowCat 内核没起来 → 需要 restart"
    elif not port_up(3001):
        verdict = "❌ QQ 在但没登录 → 需要扫码（打开 http://127.0.0.1:6099/webui/）"
    else:
        verdict = "⚠️ 通道在但守护有异常"
    lines.append(f"结论：{verdict}")
    stale = stale_code_report()
    if stale:
        lines.append("代码新旧自检：")
        lines.append(stale)
    return "\n".join(lines)


def kill_qq() -> str:
    try:
        subprocess.run(["taskkill", "/F", "/IM", "QQ.exe"], capture_output=True,
                       timeout=20, creationflags=0x08000000)
    except Exception:
        pass
    time.sleep(2)
    if qq_count() > 0:
        # 提权再试一次
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-WindowStyle','Hidden','-Command','Stop-Process -Name QQ -Force -ErrorAction SilentlyContinue'"],
                           capture_output=True, timeout=20, creationflags=0x08000000)
            time.sleep(4)
        except Exception:
            pass
    return f"结束 QQ 后剩余：{qq_count()} 个进程"


def cmd_restart() -> str:
    out = [f"【重启第三方 QQ】{datetime.now():%H:%M:%S}"]
    out.append(kill_qq())
    if not Path(SNOWCAT).exists():
        out.append(f"❌ 找不到启动器：{SNOWCAT}")
        return "\n".join(out)
    launch_log = BASE / "logs" / "snowcat-launch.log"
    launch_log.parent.mkdir(exist_ok=True)
    try:
        with open(launch_log, "wb") as lf:
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-File", SNOWCAT, "-QqUin", UIN, "-Shell", SHELL_DIR],
                             stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                             creationflags=0x08000000)   # CREATE_NO_WINDOW
        out.append("已提交启动命令，等待内核起来…")
    except Exception as e:
        out.append(f"❌ 启动失败：{e}")
        return "\n".join(out)

    for i in range(18):          # 最多等 90 秒
        time.sleep(5)
        if port_up(6099):
            out.append(f"✅ SnowCat 已就绪（{5 * (i + 1)} 秒）")
            break
    else:
        out.append("❌ 90 秒内 6099 没起来，注入可能失败")
        try:
            tail = launch_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-12:]
            if tail:
                out.append("启动器日志（logs/snowcat-launch.log）：")
                out.extend("  " + ln for ln in tail)
        except Exception:
            pass

    if port_up(6099):
        for i in range(12):      # 再等登录（自动登录时 3001 会出现）
            if port_up(3001):
                info = login_info()
                out.append(f"✅ 已自动登录：{(info or {}).get('nickname')}（免扫码）")
                return "\n".join(out)
            time.sleep(5)
        out.append("⚠️ 还没登录 → 需要扫码")
        out.append("在电脑上打开：http://127.0.0.1:6099/webui/ （token 见 config/webui.json）")

    out.append(ensure_daemons())
    return "\n".join(out)


def ensure_daemons() -> str:
    """清掉 STOP/GIVEUP，把看门狗拉起来（它会带起两个守护）。"""
    msgs = []
    for name in ("STOP", "GIVEUP"):
        f = BASE / name
        if f.exists():
            f.unlink()
            msgs.append(f"已清理 {name}")
    already = port_up(47820)
    if not already:
        try:
            subprocess.Popen([PYW, str(BASE / "watchdog.pyw")], cwd=str(BASE),
                             creationflags=0x08000000)
            msgs.append("已拉起看门狗")
        except Exception as e:
            msgs.append(f"拉起看门狗失败：{e}")

    for _ in range(10):
        if port_up(47821) and port_up(47822):
            break
        time.sleep(3)
    states = []
    for port, name in ((47820, "看门狗"), (47821, "雨天守护"), (47822, "指令监听")):
        states.append(f"{name}={'✅' if port_up(port) else '❌'}")
    msgs.append("守护：" + "  ".join(states))
    return "\n".join(msgs)


def cmd_stop() -> str:
    (BASE / "STOP").write_text(datetime.now().isoformat(), encoding="utf-8")
    return "已置 STOP，天气守护和看门狗会在 1 分钟内自己退出"


def cmd_start() -> str:
    f = BASE / "STOP"
    if f.exists():
        f.unlink()
    try:
        subprocess.Popen([PYW, str(BASE / "watchdog.pyw")], cwd=str(BASE),
                         creationflags=0x08000000)
    except Exception as e:
        return f"拉起看门狗失败：{e}"
    time.sleep(6)
    return f"已删除 STOP 并拉起看门狗；47821={'通' if port_up(47821) else '未通'}"


def main(argv: list[str]) -> int:
    raw = (argv[1] if len(argv) > 1 else "检查").strip()
    table = {
        # 中文（用户习惯）
        "检查": cmd_status, "状态": cmd_status, "体检": cmd_status, "在吗": cmd_status,
        "救活": cmd_restart, "重启": cmd_restart, "复活": cmd_restart, "拉起": cmd_restart,
        "停": cmd_stop, "停止": cmd_stop,
        "恢复": cmd_start, "启动": cmd_start,
        # 英文
        "status": cmd_status, "restart": cmd_restart,
        "kill": lambda: kill_qq(), "stop": cmd_stop, "start": cmd_start,
    }
    fn = table.get(raw) or table.get(raw.lower())
    if not fn:
        print("可用命令：检查 / 救活 / 停 / 恢复（也接受 status/restart/stop/start）")
        return 2
    print(fn())
    return 0


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv))
