"""救活入口的短命令版本（文件名不含 qq，避免 QQ 自动把 qq 转成表情）。

用法：
    python weather_watch/go.py 检查
    python weather_watch/go.py 救活
    python weather_watch/go.py 停
    python weather_watch/go.py 恢复

等同于 qqbot_ctl.py 的同名命令。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import qqbot_ctl  # noqa: E402


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "检查"
    table = {
        "检查": qqbot_ctl.cmd_status, "状态": qqbot_ctl.cmd_status, "体检": qqbot_ctl.cmd_status,
        "救活": qqbot_ctl.cmd_restart, "重启": qqbot_ctl.cmd_restart, "复活": qqbot_ctl.cmd_restart,
        "停": qqbot_ctl.cmd_stop, "停止": qqbot_ctl.cmd_stop,
        "恢复": qqbot_ctl.cmd_start, "启动": qqbot_ctl.cmd_start,
        "status": qqbot_ctl.cmd_status, "restart": qqbot_ctl.cmd_restart,
        "stop": qqbot_ctl.cmd_stop, "start": qqbot_ctl.cmd_start,
    }
    fn = table.get(cmd) or table.get(str(cmd).lower())
    if not fn:
        print("可用：检查 / 救活 / 停 / 恢复")
        return 2
    print(fn())
    return 0


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv))
