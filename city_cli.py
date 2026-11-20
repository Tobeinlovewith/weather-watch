"""城市管理小工具 —— 群里的 #加城市 / #城市列表 由它落地。

    python city_cli.py list
    python city_cli.py add 上海
    python city_cli.py add 广州 --target group
    python city_cli.py remove 上海

坐标和 city_code 都通过和风 GeoAPI 查（和风的 LocationID 就是中国天气网的城市码）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import rain_watch as rw  # noqa: E402

MAX_CITIES = 10


def geo_lookup(name: str) -> dict | None:
    cfg = rw.load_config()
    qw = cfg.get("qweather") or {}
    key, host = qw.get("key"), qw.get("host")
    if not key or not host:
        raise SystemExit("没配置和风 key/host，查不了坐标")
    url = (f"https://{host}/geo/v2/city/lookup?location={quote(name)}"
           f"&key={key}&lang=zh&number=5")
    data = json.loads(rw.http_text(url))
    if str(data.get("code")) != "200":
        return None
    rows = [r for r in (data.get("location") or []) if r.get("country") == "中国"]
    if not rows:
        return None
    exact = [r for r in rows if r.get("name") == name]
    if exact:
        return exact[0]
    # 「上海市」→「上海」这种，允许名字互相包含，但必须是真沾边
    part = [r for r in rows
            if r.get("name") and (r["name"] in name or name in r["name"])]
    if len(part) == 1:
        return part[0]
    return None


def slugify(loc_id: str, name: str) -> str:
    ascii_part = re.sub(r"[^a-z0-9]", "", name.lower())
    return ascii_part or f"c{loc_id}"


def cmd_list() -> int:
    cfg = rw.load_config()
    locs = rw.locations(cfg)
    subs = {t["kind"] + ":" + str(t["id"]): t["locations"] for t in rw.targets(cfg)}
    print(f"共 {len(locs)} 个城市：")
    for loc in locs:
        who = [k for k, v in subs.items() if not v or loc["key"] in v]
        print(f"  - {loc['name']}  ({loc['key']}, {loc['city_code']})  -> {', '.join(who)}")
    return 0


def cmd_add(name: str, target: str) -> int:
    cfg = rw.load_config()
    locs = rw.locations(cfg)
    if any(l["name"] == name for l in locs):
        print(f"已经有了：{name}")
        return 0
    if len(locs) >= MAX_CITIES:
        print(f"城市太多了（上限 {MAX_CITIES}），先删几个吧")
        return 1
    row = geo_lookup(name)
    if not row:
        print(f"没查到这个城市：{name}（只发城市名试试，比如「上海」）")
        return 1
    loc = {
        "key": slugify(row.get("id", ""), name),
        "name": row.get("name") or name,
        "lat": round(float(row["lat"]), 4),
        "lon": round(float(row["lon"]), 4),
        "city_code": str(row.get("id") or ""),
    }
    if not loc["city_code"]:
        print("没拿到城市码，放弃")
        return 1
    cfg.setdefault("locations", []).append(loc)
    for t in cfg["qq"]["targets"]:
        if target == "all" or t["kind"] == target:
            if loc["key"] not in t["locations"]:
                t["locations"].append(loc["key"])
    with open(BASE / "config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"加上了：{loc['name']}（{loc['city_code']}，{loc['lat']},{loc['lon']}）-> {target}")
    return 0


def cmd_remove(name: str) -> int:
    cfg = rw.load_config()
    locs = rw.locations(cfg)
    hit = [l for l in locs if l["name"] == name or l["key"] == name]
    if not hit:
        print(f"没这个城市：{name}")
        return 1
    for l in hit:
        cfg["locations"].remove(l)
        for t in cfg["qq"]["targets"]:
            if l["key"] in t["locations"]:
                t["locations"].remove(l["key"])
    with open(BASE / "config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"删掉了：{name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_add = sub.add_parser("add")
    p_add.add_argument("name")
    p_add.add_argument("--target", default="group", choices=["group", "private", "all"])
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("name")
    args = ap.parse_args()

    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "add":
        return cmd_add(args.name, args.target)
    return cmd_remove(args.name)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
