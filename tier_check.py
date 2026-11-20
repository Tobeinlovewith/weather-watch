"""逐步降级审查：省 → 市 → 区县，多源投票（少数服从多数）。

设计目标（对应需求）：
  1. 每天审查订阅范围（先省，再市，再区县）有没有雨/雪/冰雹
  2. 每个层级都用多个数据源判断，投票，少数服从多数
  3. 有降水 → 反推时间，提前 15 分钟进入每分钟轮询
  4. 多个源给出不同开始时间时，取最早的那个（宁可早报，不许漏报）

数据源：
  - 和风 QWeather 24h 逐小时降水（最细）
  - 中国天气网 逐3小时
  - 中央气象台 站点预报（昼/夜 + 日降水量）
"""
from __future__ import annotations

import gzip
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import nmc  # noqa: E402

WET_WORDS = ("雨", "雪", "冰雹", "雷阵雨", "阵雨", "冻雨", "雨夹雪")
_MEM: dict[str, tuple[float, object]] = {}


def _cache(key: str, ttl: float):
    it = _MEM.get(key)
    if it and (time.time() - it[0]) < ttl:
        return it[1]
    return None


def _put(key: str, val) -> None:
    _MEM[key] = (time.time(), val)


def _wet(text: str) -> bool:
    return any(w in (text or "") for w in WET_WORDS)


# ---------------------------------------------------------------- 和风逐小时
def qweather_hourly(cfg: dict, loc: dict, ttl: float = 600) -> dict | None:
    key = f"qw24:{loc['key']}"
    cached = _cache(key, ttl)
    if cached is not None:
        return cached
    q = cfg.get("qweather") or {}
    host, apikey = q.get("host"), q.get("key")
    if not (host and apikey):
        return None
    url = (f"https://{host}/v7/weather/24h?location={loc['lon']},{loc['lat']}"
           f"&key={apikey}&lang=zh")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                   "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        d = json.loads(raw.decode("utf-8"))
        hourly = d.get("hourly") or []
        first = None
        for h in hourly:
            try:
                precip = float(h.get("precip") or 0)
            except (TypeError, ValueError):
                precip = 0.0
            if precip >= 0.05 or _wet(h.get("text")):
                first = h.get("fxTime")
                break
        out = {"ok": True, "first": first, "hourly": hourly}
        _put(key, out)
        return out
    except Exception:
        return {"ok": False, "first": None, "hourly": []}


# ---------------------------------------------------------------- 中央气象台
def nmc_daily(loc: dict, ttl: float = 600) -> dict | None:
    st = nmc.station_for(loc["name"])
    if not st:
        return None
    key = f"nmcd:{st}"
    cached = _cache(key, ttl)
    if cached is not None:
        return cached
    try:
        d = nmc._get_json(f"/rest/weather?stationid={st}")
        data = d.get("data") or {}
        det = ((data.get("predict") or {}).get("detail")) or []
    except Exception:
        return None
    first = None
    wet_days = 0
    today = datetime.now().date()
    horizon = today + timedelta(days=1)      # 只看未来 48 小时，别拿 7 天窗口当信号
    for row in det:
        try:
            row_date = datetime.strptime(str(row.get("date"))[:10], "%Y-%m-%d").date()
        except Exception:
            row_date = today
        if row_date > horizon:
            continue
        day = row.get("day") or {}
        night = row.get("night") or {}
        txt = f"{day.get('weather', {}).get('info', '')},{night.get('weather', {}).get('info', '')}"
        try:
            p = float(row.get("precipitation") or 0)
        except (TypeError, ValueError):
            p = 0.0
        if _wet(txt) or p > 0:
            wet_days += 1
            if first is None:
                first = row.get("date")
    out = {"ok": True, "station": st, "first": first, "detail": det, "wet_days": wet_days}
    _put(key, out)
    return out


# ---------------------------------------------------------------- 省级扫描
def province_of(loc: dict) -> tuple[str | None, list[str]]:
    """返回（省名，该省全部城市名）。"""
    idx = nmc.build_index()
    cities = idx.get("cities") or {}
    name = loc["name"].split("·")[-1].rstrip("市县区旗")
    for key in (name, loc["name"]):
        for code, prov in cities.get(key, []):
            prov_cities = sorted({c for c, lst in cities.items()
                                  for cd, pv in lst if pv == prov and len(c) >= 2})
            return prov, prov_cities
    return None, []


def province_scan(prov: str, city_names: list[str], ttl: float = 1800) -> dict:
    """省内地级市逐个数一遍：多少个未来有降水。"""
    key = f"prov:{prov}"
    cached = _cache(key, ttl)
    if cached is not None:
        return cached
    # 只取“地级”名称（2-3 字）避免区县噪声
    sample = [c for c in city_names if 2 <= len(c) <= 3][:40]
    wet, checked = [], 0
    for name in sample:
        st = nmc.station_for(name)
        if not st:
            continue
        r = nmc_daily({"name": name, "key": name})
        checked += 1
        if r and r.get("wet_days"):
            wet.append((name, r.get("first")))
    ratio = (len(wet) / checked) if checked else 0.0
    out = {"province": prov, "checked": checked, "wet": len(wet),
           "ratio": round(ratio, 2), "wet_cities": wet[:8]}
    _put(key, out)
    return out


# ---------------------------------------------------------------- 市级/区级投票
def city_votes(cfg: dict, loc: dict) -> list[dict]:
    """三个源各自表态：有没有降水、最早什么时候。"""
    votes = []

    qw = qweather_hourly(cfg, loc)
    if qw and qw.get("ok"):
        votes.append({"source": "和风24h", "wet": bool(qw.get("first")),
                      "first": qw.get("first")})

    try:
        import rain_watch as rw
        rows = rw.cn_hour3(loc["city_code"])
        first = None
        for r in rows:
            if _wet(r.get("weather")):
                first = r.get("time")
                break
        votes.append({"source": "中国天气网3h", "wet": bool(first), "first": first})
    except Exception:
        votes.append({"source": "中国天气网3h", "wet": None, "first": None})

    nd = nmc_daily(loc)
    votes.append({"source": "中央气象台", "wet": bool(nd and nd.get("first")),
                  "first": (nd or {}).get("first")})

    return votes


def vote(votes: list[dict]) -> tuple[bool, str | None]:
    """少数服从多数；只统计明确表态的源。"""
    valid = [v for v in votes if v.get("wet") is not None]
    wet = [v for v in valid if v.get("wet")]
    decided = len(wet) * 2 > len(valid)      # 严格多数
    firsts = sorted(v["first"] for v in wet if v.get("first"))
    return decided, (firsts[0] if firsts else None)


def decide(cfg: dict, loc: dict, log=None) -> dict:
    """完整降级审查：省 → 市/区，返回结论 + 各层证据。"""
    prov, prov_cities = province_of(loc)
    prov_res = province_scan(prov, prov_cities) if prov else None
    votes = city_votes(cfg, loc)
    wet, first = vote(votes)

    # 省级只是“有没有必要细查”，真正下单靠市级票
    if prov_res and prov_res.get("ratio", 0) == 0 and not wet:
        level = "province-dry"
    elif wet:
        level = "city-wet"
    else:
        level = "city-dry"

    result = {"loc": loc["key"], "name": loc["name"], "province": prov,
              "province_scan": prov_res, "votes": votes,
              "wet": wet, "first": first, "level": level}
    if log:
        srcs = " ".join(f"{v['source']}={'雨' if v['wet'] else ('?' if v['wet'] is None else '晴')}"
                        for v in votes)
        log.info("[%s] 降级审查 %s | 省=%s(%s/%s) | %s | 结论=%s 最早=%s",
                 loc["key"], loc["name"], prov or "-",
                 (prov_res or {}).get("wet", 0), (prov_res or {}).get("checked", 0),
                 srcs, "有降水" if wet else "无降水", first or "-")
    return result


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import rain_watch as rw
    cfg = rw.load_config()
    log = rw.setup_logging()
    for loc in rw.locations(cfg):
        r = decide(cfg, loc, log)
        print(json.dumps({k: r[k] for k in ("name", "province", "wet", "first", "level")},
                         ensure_ascii=False))
        print("  省级：", r["province_scan"])
        print()
