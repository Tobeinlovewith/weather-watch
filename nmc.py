"""中央气象台（NMC）公开接口客户端。

提供三样中国天气网/和风没有覆盖到的能力：
  1. 官方预警列表  /rest/findAlarm  —— 全国在册预警，含发布单位、时间、详情页
  2. 站点实况      /rest/weather    —— 气温/湿度/降水/体感/风，含该市预警信号
  3. 城市→站点码映射 /rest/province/*  —— 首次构建后落盘缓存 7 天

不依赖第三方库，纯标准库。
"""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "http://www.nmc.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "http://www.nmc.cn/",
    "Accept": "application/json, text/plain, */*",
}
INDEX_FILE = Path(__file__).resolve().parent / "nmc_index.json"
INDEX_TTL_DAYS = 7

_MEM: dict[str, tuple[float, object]] = {}


def _mem_get(key: str, ttl: float):
    it = _MEM.get(key)
    if it and (time.time() - it[0]) < ttl:
        return it[1]
    return None


def _mem_put(key: str, val) -> None:
    _MEM[key] = (time.time(), val)


def _get_json(path: str, timeout: float = 15.0):
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------- 城市 → 站点码
def _load_index() -> dict | None:
    try:
        d = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        built = datetime.fromisoformat(d["built"])
        if (datetime.now() - built).days < INDEX_TTL_DAYS and d.get("cities"):
            return d
    except Exception:
        pass
    return None


def build_index(force: bool = False) -> dict:
    if not force:
        cached = _load_index()
        if cached:
            return cached
    cities: dict[str, list] = {}
    try:
        provinces = _get_json("/rest/province/all")
    except Exception:
        provinces = []
    for p in provinces or []:
        pcode = p.get("code")
        pname = p.get("name") or ""
        if not pcode:
            continue
        try:
            items = _get_json(f"/rest/province/{pcode}")
        except Exception:
            continue
        for it in items or []:
            city = (it.get("city") or "").strip()
            code = it.get("code")
            if not city or not code:
                continue
            # 去掉“市/县/区”后缀做别名，方便「大兴」命中「大兴区」
            aliases = {city, city.rstrip("市县区旗")}
            for name in aliases:
                if not name:
                    continue
                cities.setdefault(name, [])
                if not any(c[0] == code for c in cities[name]):
                    cities[name].append([code, pname])
    data = {"built": datetime.now().isoformat(timespec="seconds"), "cities": cities}
    try:
        INDEX_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return data


def station_for(name: str, hint: str | None = None) -> str | None:
    """按城市名找站点码；hint 可以是省份名用于消歧。"""
    idx = build_index()
    cities = idx.get("cities") or {}
    for cand in (name, name.rstrip("市县区旗"), name.split("·")[-1]):
        lst = cities.get(cand)
        if not lst:
            continue
        if hint:
            for code, prov in lst:
                if hint in prov:
                    return code
        return lst[0][0]
    return None


# ---------------------------------------------------------------- 实况
def real(station: str, ttl: float = 300) -> dict | None:
    cached = _mem_get(f"real:{station}", ttl)
    if cached is not None:
        return cached
    try:
        d = _get_json(f"/rest/weather?stationid={station}")
        data = d.get("data")
        if not isinstance(data, dict):
            return None
        r = data.get("real") or {}
        w = r.get("weather") or {}
        wind = r.get("wind") or {}
        out = {
            "ok": True,
            "station": station,
            "city": (r.get("station") or {}).get("city"),
            "obs_time": r.get("publish_time"),
            "temp": _num(w.get("temperature")),
            "feels": _num(w.get("feelst")),
            "humidity": _num(w.get("humidity")),
            "rain": _num(w.get("rain")),
            "wind": (wind.get("direct") or "") + (wind.get("power") or ""),
            "warn": r.get("warn") or {},
            "air": data.get("air") or {},
        }
        _mem_put(f"real:{station}", out)
        return out
    except Exception:
        return None


def _num(v):
    try:
        f = float(v)
        if f >= 9998:      # NMC 用 9999/999999 表示缺测
            return None
        return f
    except (TypeError, ValueError):
        return None


def rain_now(station: str) -> float | None:
    """站点最近一小时的降水量（mm）。"""
    r = real(station)
    return None if not r else r.get("rain")


# ---------------------------------------------------------------- 预警
def alarms(ttl: float = 300) -> list[dict]:
    cached = _mem_get("alarms", ttl)
    if cached is not None:
        return cached
    out: list[dict] = []
    try:
        d = _get_json("/rest/findAlarm?pageNo=1&pageSize=500")
        lst = (((d.get("data") or {}).get("page") or {}).get("list")) or []
        for it in lst:
            out.append({
                "id": it.get("alertid"),
                "time": it.get("issuetime"),
                "title": (it.get("title") or "").strip(),
                "url": (it.get("url") or "").strip(),
                "pic": it.get("pic"),
            })
    except Exception:
        pass
    _mem_put("alarms", out)
    return out


def alarms_for(city: str, limit: int = 3) -> list[dict]:
    """按城市名过滤全国预警列表。

    只认「发布单位最后一级就是本地」的预警：
      「北京市大兴区气象台」→ 命中 大兴
      「河南省洛阳市洛宁县气象台」→ 洛阳不命中（那是洛宁县的预警）
    """
    key = city.split("·")[-1].rstrip("市县区旗")
    if not key:
        return []
    hits = []
    for a in alarms():
        title = a.get("title") or ""
        if key not in title:
            continue
        m = re.search(r"([\u4e00-\u9fa5]{2,18}?)气象台", title)
        if m:
            agency = m.group(1)
            if agency.endswith(("市", "县", "区", "旗", "盟", "州")):
                agency = agency[:-1]
            last = [x for x in re.split(r"[省市自治区]", agency) if x]
            if last and key not in last[-1]:
                continue
        hits.append(a)
        if len(hits) >= limit:
            break
    return hits


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for name in ("大兴", "成都", "洛阳"):
        code = station_for(name)
        r = real(code) if code else None
        print(f"{name} -> {code}: {r}")
    al = alarms()
    print(f"\n全国在册预警 {len(al)} 条，前 3 条：")
    for a in al[:3]:
        print("  ", a["time"], a["title"])
