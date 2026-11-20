"""天气守护看门狗（常驻、无窗口）。

职责：确认 rain_watch / cmd_listener 在跑，缺了才拉起；同时盯着第三方 QQ（3001）是否掉线。

安全约束：
  1. STOP 文件 → 立即退出，不再拉起
  2. GIVEUP 文件 → 之前放弃过管理，直接退出（人工删除可恢复）
  3. 单实例锁（47820，Windows 独占绑定）
  4. 每个守护最多连续拉起 3 次；3 次不通 → 写日志 → 强制结束 → 结束不了就放弃
  5. pythonw 超过 4 个就停手
  6. 全程 pythonw + CREATE_NO_WINDOW

告警分流（按用户要求）：
  - 第三方 QQ 在线 → 走第三方 QQ 发（说明链路活着）
  - 第三方不可用（掉线/失败）→ 走 QQ 官方机器人
  - 官方也失败 → 邮件 / 微信推送
"""
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

PY = r'C:\Users\li180\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe'
LOG = os.path.join(BASE, 'logs', 'watchdog.log')
STOP = os.path.join(BASE, 'STOP')
GIVEUP = os.path.join(BASE, 'GIVEUP')
LOCK_PORT = 47820
QQ_PORT = 3001

JOBS = [
    ('rain_watch', ['rain_watch.py', '--loop'], 47821),
    ('cmd_listener', ['cmd_listener.py'], 47822),
    ('qqbot_service', ['qqbot_service.py'], 47825),
]

MAX_ATTEMPTS = 3
START_WAIT = 20
START_WAIT_BY_JOB = {'qqbot_service': 50}   # 连 QQ 网关较慢，多给点时间
MAX_PYTHONW = 4
HANG_MINUTES = 10          # 心跳超过这么多分钟没更新 = 假死

attempts = {name: 0 for name, _, _ in JOBS}
_qq_down_since = None
_qq_alerted = False


def log(msg: str) -> None:
    try:
        os.makedirs(os.path.join(BASE, 'logs'), exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def port_up(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def count_pythonw() -> int:
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq pythonw.exe', '/NH'],
                             capture_output=True, text=True, timeout=10,
                             creationflags=0x08000000).stdout
        return out.lower().count('pythonw.exe')
    except Exception:
        return 0




def _lower_priority(port: int, cls: str = "Idle") -> None:
    """把刚拉起的进程降到最低优先级，避免和游戏抢 CPU。"""
    ps = (f"$c = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
          f"Where-Object {{ $_.LocalPort -eq {port} }} | "
          f"Select-Object -ExpandProperty OwningProcess -Unique; "
          f"foreach ($p in $c) {{ try {{ (Get-Process -Id $p).PriorityClass = '{cls}' }} catch {{}} }}")
    try:
        subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps],
                       capture_output=True, timeout=15, creationflags=0x08000000)
    except Exception:
        pass


def heartbeat_age(name: str) -> float:
    """返回心跳文件距今多少分钟；文件不存在返回 -1（不做判断）。"""
    path = os.path.join(BASE, 'logs', f'hb_{name}.txt')
    try:
        return (time.time() - os.path.getmtime(path)) / 60.0
    except OSError:
        return -1.0


def kill_port_owner(port: int) -> bool:
    """结束正在监听该端口的进程。"""
    ps = (f"$c = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
          f"Where-Object {{ $_.LocalPort -eq {port} }} | "
          f"Select-Object -ExpandProperty OwningProcess -Unique; "
          f"foreach ($p in $c) {{ try {{ Stop-Process -Id $p -Force -ErrorAction Stop }} catch {{}} }}")
    try:
        subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps],
                       capture_output=True, timeout=25, creationflags=0x08000000)
        time.sleep(2)
        return not port_up(port)
    except Exception:
        return False


def single_instance():
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except (AttributeError, OSError):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('127.0.0.1', LOCK_PORT))
        s.listen(8)
        return s
    except OSError:
        return None


def load_cfg() -> dict:
    try:
        with open(os.path.join(BASE, 'config.json'), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _owner_ids(cfg: dict) -> list:
    ids = []
    try:
        for t in ((cfg.get("qq") or {}).get("targets") or []):
            if t.get("kind") == "private" and t.get("id"):
                ids.append(int(t["id"]))
    except Exception:
        pass
    return ids or [10000]


def alert(subject: str, body: str) -> None:
    """第三方在线就走第三方；不行退官方机器人；再不行退邮件。"""
    cfg = load_cfg()
    sent = []

    if port_up(QQ_PORT):
        try:
            import ws_send
            for uid in _owner_ids(cfg):
                r = ws_send.send_private(uid, f"{subject}\n{body}")
                if str(r.get("status")) == "ok":
                    sent.append("thirdparty")
                    break
        except Exception as e:
            log(f'[告警] 第三方发送异常: {e}')

    if not sent:
        try:
            import qqofficial
            if qqofficial.notify(cfg, subject, body, log):
                sent.append("qqofficial")
        except Exception as e:
            log(f'[告警] 官方机器人失败: {e}')

    if not sent:
        try:
            import alert as al
            sent += al.notify(cfg, subject, body, log)
        except Exception as e:
            log(f'[告警] 备用通道失败: {e}')

    log(f'[告警] {subject} -> {sent or "无通道可用"}')


def kill_all() -> int:
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -match 'rain_watch|cmd_listener|watchdog|qqbot_service' } | "
        "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }"
    )
    try:
        subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps],
                       capture_output=True, timeout=30, creationflags=0x08000000)
    except Exception as e:
        log(f'强制结束命令出错: {e}')
    time.sleep(3)
    left = 0
    for _, _, port in JOBS:
        if port_up(port):
            left += 1
    return left


def escalate(name: str, port: int) -> None:
    """只放弃这一个服务，不影响其它守护。"""
    log(f'【三次拉起失败】{name} 连续 {MAX_ATTEMPTS} 次启动后端口仍未就绪')
    log(f'【动作】只结束 {name}（端口 {port}），其它服务继续运行')
    killed = kill_port_owner(port)
    log(f'  结束结果：{"成功" if killed else "端口仍被占用"}')
    marker = os.path.join(BASE, f'GIVEUP-{name}')
    try:
        with open(marker, 'w', encoding='utf-8') as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {name} 三次拉起失败，"
                    f"{'已结束' if killed else '未能结束'}，停止自动拉起\n")
    except Exception:
        pass
    alert('天气守护启动失败',
          f'{name} 连续 {MAX_ATTEMPTS} 次启动后端口仍未就绪，已停止自动拉起。\n'
          f'其它服务照常在跑。排查后可删除 GIVEUP-{name} 恢复。')
    attempts[name] = 0


def check_qq() -> None:
    """盯第三方 QQ：掉线超过阈值就告警（此时第三方本身发不出去，会走官方）。"""
    global _qq_down_since, _qq_alerted
    if port_up(QQ_PORT):
        if _qq_alerted:
            alert('QQ 已恢复', '第三方 QQ 重新上线，天气推送恢复正常。')
            _qq_alerted = False
        _qq_down_since = None
        return

    cfg = load_cfg()
    limit = int(((cfg.get('alert') or {}).get('qq_offline_minutes')) or 5)
    if _qq_down_since is None:
        _qq_down_since = datetime.now()
    elif not _qq_alerted and (datetime.now() - _qq_down_since).total_seconds() > limit * 60:
        _qq_alerted = True
        alert('QQ 机器人掉线',
              f'OneBot 端口 {QQ_PORT} 已连续 {limit} 分钟不通，可能是 QQ 掉线或被踢。\n'
              '需要重新扫码登录（WebUI: http://127.0.0.1:6099/webui/）。\n'
              '也可以直接让我执行 rescue 脚本把它拉起来。')


def main() -> int:
    if os.path.exists(STOP):
        return 0
    if os.path.exists(GIVEUP):
        log('存在 GIVEUP 文件，看门狗不启动（删除该文件可恢复）')
        return 0

    lock = single_instance()
    if lock is None:
        return 0
    log('看门狗启动（STOP/GIVEUP/三次上限/第三方优先告警）')

    while True:
        if os.path.exists(STOP):
            log('检测到 STOP 文件，看门狗退出')
            return 0

        check_qq()

        for name, args, port in JOBS:
            if port_up(port):
                age = heartbeat_age(name)
                if age > HANG_MINUTES:
                    log(f'【假死】{name} 心跳 {age:.0f} 分钟未更新 → 结束进程并重启')
                    killed = kill_port_owner(port)
                    log(f'  结束结果：{"成功" if killed else "端口仍被占用"}')
                    if not killed:
                        attempts[name] += 1
                        if attempts[name] >= MAX_ATTEMPTS:
                            escalate(name, port)
                        continue
                attempts[name] = 0
                continue

            if os.path.exists(os.path.join(BASE, f'GIVEUP-{name}')):
                if not globals().get(f"_giveup_logged_{name}"):
                    log(f'{name} 已被放弃（GIVEUP-{name}），跳过')
                    globals()[f"_giveup_logged_{name}"] = True
                continue

            if attempts[name] >= MAX_ATTEMPTS:
                escalate(name, port)
                continue

            if count_pythonw() >= MAX_PYTHONW:
                log(f'pythonw 已达 {MAX_PYTHONW} 个，暂不拉起 {name}')
                continue

            try:
                subprocess.Popen([PY] + args, cwd=BASE, close_fds=True,
                                 creationflags=0x08000000)
            except Exception as e:
                attempts[name] += 1
                log(f'拉起 {name} 异常（第 {attempts[name]} 次）: {e}')
                continue

            wait = START_WAIT_BY_JOB.get(name, START_WAIT)
            time.sleep(wait)
            if port_up(port):
                attempts[name] = 0
                _lower_priority(port)
                log(f'拉起 {name} 成功（已降为 Idle 优先级）')
            else:
                attempts[name] += 1
                log(f'拉起 {name} 后 {wait}s 端口仍未就绪（第 {attempts[name]}/{MAX_ATTEMPTS} 次）')
        time.sleep(30)


if __name__ == '__main__':
    raise SystemExit(main())
