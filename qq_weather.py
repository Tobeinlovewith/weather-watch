"""命令行查天气：给 QQ 官方机器人那边的 agent 调用。

用法：
  python qq_weather.py                # 所有订阅城市，今天
  python qq_weather.py 成都            # 指定城市
  python qq_weather.py 明天            # 所有城市，明天
  python qq_weather.py 成都 明天        # 指定城市 + 明天

输出就是可以直接发出去的播报文本。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def main(argv: list[str]) -> int:
    import rain_watch as rw

    args = [a for a in argv[1:] if a.strip()]
    mode = "morning"
    city = None
    for a in args:
        if a in ("明天", "明日", "tomorrow"):
            mode = "evening"
        else:
            city = a

    cfg = rw.load_config()
    log = rw.setup_logging()
    now = datetime.now(rw.TZ)

    locs = rw.locations(cfg)
    if city:
        key = city.split("·")[-1].rstrip("市县区旗")
        hits = [l for l in locs if key in l["name"] or l["name"] in city]
        if not hits:
            names = "、".join(l["name"] for l in locs)
            print(f"没有订阅这个城市。当前订阅：{names}")
            return 1
        locs = hits[:1]

    out = []
    for loc in locs:
        msg = rw.build_briefing(cfg, loc, mode, now, log)
        out.append(msg or f"【{loc['name']}】暂时取不到数据")
    print("\n\n".join(out))
    return 0


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv))
