"""雨雪提前提醒 —— 每 N 分钟看一眼天，快下的时候喊一声。

支持多城市，每个 QQ 目标可以订阅不同城市：
  - 私聊：只订阅北京
  - 群聊：可以加入其他城市（群友所在的）

数据源（按优先级自动选择）：
  1. 和风天气 分钟级降水（1 公里 / 5 分钟一档），需要 key + API Host，精度≈10 分钟
  2. 中国天气网 逐三小时预报 + 实况 + 气象预警（无需 key），精度≈提前 3 小时

用法：
  python rain_watch.py --once        跑一次，只打印判断结果，不发消息
  python rain_watch.py --once --send 跑一次，该发就发
  python rain_watch.py --loop        常驻，每 poll_minutes 分钟检查一次
  python rain_watch.py --check       自检：各城市的数据源连通性
"""
from __future__ import annotations

import sys as _sys, os as _os
if _sys.stdout is None: _sys.stdout = open(_os.devnull, 'w')
if _sys.stderr is None: _sys.stderr = open(_os.devnull, 'w')

import argparse
import gzip
import json
import logging
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import ws_send  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
LOCK_PORT = 47821

RAIN_WORDS = ("雨", "阵雨", "雷阵雨", "毛毛雨", "冻雨")
SNOW_WORDS = ("雪", "雨夹雪", "米雪", "霰", "冰粒")

_CN_REFERER = {"Referer": "https://www.weather.com.cn/"}


# ---------------------------------------------------------------- 基础设施
def setup_logging(verbose: bool = False) -> logging.Logger:
    (BASE / "logs").mkdir(exist_ok=True)
    logger = logging.getLogger("rain_watch")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(BASE / "logs" / "watch.log", maxBytes=1_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def load_config() -> dict:
    with open(BASE / "config.json", encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict:
    p = BASE / "state.json"
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    tmp = BASE / "state.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BASE / "state.json")


def locations(cfg: dict) -> list[dict]:
    locs = cfg.get("locations")
    if locs:
        return locs
    legacy = cfg.get("location") or {}
    if not legacy:
        return []
    return [{"key": "home", **legacy}]


def targets(cfg: dict) -> list[dict]:
    qq = cfg.get("qq") or {}
    items = qq.get("targets")
    if items:
        out = []
        for t in items:
            if not t.get("id"):
                continue
            out.append({
                "kind": t.get("kind", "private"),
                "id": int(t["id"]),
                "locations": t.get("locations") or [],   # 空 = 全部城市
            })
        return out
    if qq.get("id"):
        return [{"kind": qq.get("kind", "private"), "id": int(qq["id"]), "locations": []}]
    return []


def http_get(url: str, headers: dict | None = None, timeout: int = 15) -> bytes:
    hdrs = {"User-Agent": UA, "Accept-Encoding": "identity"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def http_text(url: str, headers: dict | None = None, timeout: int = 15) -> str:
    raw = http_get(url, headers, timeout)
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def in_quiet_hours(cfg: dict, now: datetime) -> bool:
    quiet = cfg.get("quiet_hours") or []
    if not quiet:
        return False
    for span in quiet:
        try:
            start, end = span
            t = now.time()
            s = datetime.strptime(start, "%H:%M").time()
            e = datetime.strptime(end, "%H:%M").time()
        except Exception:
            continue
        if s <= e:
            if s <= t <= e:
                return True
        else:
            if t >= s or t <= e:
                return True
    return False


def is_precip(text: str) -> tuple[bool, str]:
    if not text:
        return False, ""
    if any(w in text for w in SNOW_WORDS):
        return True, "snow"
    if any(w in text for w in RAIN_WORDS):
        return True, "rain"
    return False, ""


# ---------------------------------------------------------------- 和风天气
@dataclass
class Event:
    loc_key: str
    loc_name: str
    kind: str                 # rain / snow
    start: datetime | None
    lead_minutes: float | None
    source: str
    detail: str = ""
    text: str = ""
    slot: str = ""


_QW_WARNED = False


def qweather_query(cfg: dict, loc: dict, now: datetime, log) -> dict:
    """一次分钟级查询。返回：
       ok        数据源可用
       raining   现在就在下
       next60    未来 60 分钟内有多少个 5 分钟档有雨
       end_soon  已知的雨会在 60 分钟内停
       event     未来（非当前）开始降水的事件
       summary   和风一句话摘要
    """
    global _QW_WARNED
    empty = {"ok": False, "raining": False, "next60": 0, "end_soon": False,
             "event": None, "summary": ""}
    qw = cfg.get("qweather") or {}
    key = qw.get("key") or os.environ.get("QWEATHER_KEY", "")
    if not key:
        return empty
    host = qw.get("host") or ""
    if not host:
        if not _QW_WARNED:
            log.info("和风 key 已配置，但缺 API Host，暂时仍用中国天气网")
            _QW_WARNED = True
        return empty

    url = (f"https://{host}/v7/minutely/5m"
           f"?location={loc['lon']},{loc['lat']}&key={key}")
    try:
        data = json.loads(http_text(url))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")) \
                .get("error", {}).get("detail", "")
        except Exception:
            pass
        if not _QW_WARNED:
            log.warning("和风请求失败 HTTP %s %s（检查 API Host / key）", exc.code, detail)
            _QW_WARNED = True
        return empty
    except Exception as exc:
        if not _QW_WARNED:
            log.warning("和风请求失败：%s", exc)
            _QW_WARNED = True
        return empty
    if str(data.get("code")) != "200":
        if not _QW_WARNED:
            log.warning("和风返回 code=%s", data.get("code"))
            _QW_WARNED = True
        return empty

    minutes = data.get("minutely") or []
    precip = []
    for item in minutes:
        try:
            precip.append(float(item.get("precip") or 0))
        except (TypeError, ValueError):
            precip.append(0.0)

    raining = bool(precip) and precip[0] >= 0.05
    next60 = sum(1 for p in precip[:12] if p >= 0.05)
    end_soon = raining and next60 <= 3     # 一小时内基本要停

    first = None
    for idx, p in enumerate(precip):
        if p >= 0.05:
            first = idx
            break

    event = None
    if first is not None and first > 0:
        item = minutes[first]
        try:
            start = datetime.fromisoformat(item["fxTime"]).astimezone(TZ)
        except Exception:
            start = now + timedelta(minutes=first * 5.0)
        lead = max((start - now).total_seconds() / 60.0, 0.0)
        # 和风的摘要里常直接写「45分钟后开始下小雨」，比时间戳更新得快，取两者较小值
        summary_txt = data.get("summary") or ""
        m = re.search(r"(\d+)\s*分钟后开始", summary_txt)
        if m:
            lead = min(lead, float(m.group(1)))
            start = now + timedelta(minutes=lead)
        kind = "snow" if str(item.get("type", "")).lower().startswith("snow") else "rain"
        event = Event(kind=kind, start=start, lead_minutes=lead, source="qweather",
                      detail=f"降水强度 {item.get('precip')} mm/h",
                      text=data.get("summary") or "",
                      slot=start.strftime("%Y-%m-%dT%H:%M"),
                      loc_key=loc["key"], loc_name=loc["name"])

    return {"ok": True, "raining": raining, "next60": next60,
            "end_soon": end_soon, "event": event,
            "summary": data.get("summary") or ""}


def qweather_minutely(cfg: dict, loc: dict, now: datetime, log) -> tuple[bool, Event | None]:
    """兼容老调用的薄封装。"""
    r = qweather_query(cfg, loc, now, log)
    return r["ok"], r["event"]


# ---------------------------------------------------------------- 中国天气网
def parse_js_var(text: str, name: str):
    m = re.search(r"var\s+" + re.escape(name) + r"\s*=", text)
    if not m:
        return None
    i = text.find("{", m.end())
    if i < 0:
        return None
    depth, j, in_str, esc = 0, i, False, False
    while j < len(text):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:j + 1])
                    except Exception:
                        return None
        j += 1
    return None


def cn_hour3(code: str) -> list[dict]:
    cached = _cache_get(("h3", code), 600)
    if cached is not None:
        return cached
    html = http_text(f"https://www.weather.com.cn/weather1d/{code}.shtml", _CN_REFERER)
    data = parse_js_var(html, "hour3data") or {}
    rows: list[str] = []
    for bucket in ("1d", "23d", "7d"):
        chunk = data.get(bucket) or []
        if not isinstance(chunk, list):
            continue
        for item in chunk:
            if isinstance(item, str):
                rows.append(item)
            elif isinstance(item, list):
                rows.extend(x for x in item if isinstance(x, str))
    out, seen = [], set()
    for raw in rows:
        parts = raw.split(",")
        if len(parts) < 3:
            continue
        m = re.match(r"(\d{1,2})日(\d{1,2})时", parts[0])
        if not m:
            continue
        k = (int(m.group(1)), int(m.group(2)))
        if k in seen:
            continue
        seen.add(k)
        temp = None
        if len(parts) > 3:
            tm = re.search(r"(-?\d+)", parts[3])
            temp = int(tm.group(1)) if tm else None
        out.append({
            "day": k[0], "hour": k[1], "weather": parts[2], "raw": raw,
            "icon": parts[1] if len(parts) > 1 else "",
            "temp": temp,
            "wind_dir": parts[4] if len(parts) > 4 else "",
            "wind_scale": parts[5] if len(parts) > 5 else "",
        })
    _cache_put(("h3", code), out)
    return out


def to_datetime(day: int, hour: int, now: datetime) -> datetime:
    year, month = now.year, now.month
    cand = now.replace(day=1, hour=hour, minute=0, second=0, microsecond=0)
    try:
        cand = cand.replace(day=day)
    except ValueError:
        cand = cand.replace(day=1)
    if cand < now - timedelta(days=10):
        month += 1
        if month > 12:
            month, year = 1, year + 1
    elif cand > now + timedelta(days=20):
        month -= 1
        if month < 1:
            month, year = 12, year - 1
    try:
        return datetime(year, month, day, hour, tzinfo=TZ)
    except ValueError:
        return now


def cn_forecast_event(cfg: dict, loc: dict, now: datetime, log) -> Event | None:
    hours = float(cfg.get("block_alert_hours", 3))
    try:
        rows = cn_hour3(loc["city_code"])
    except Exception as exc:
        log.warning("[%s] 中国天气网请求失败：%s", loc["key"], exc)
        return None
    for row in rows:
        when = to_datetime(row["day"], row["hour"], now)
        delta_min = (when - now).total_seconds() / 60
        if delta_min < -30 or delta_min > hours * 60:
            continue
        hit, kind = is_precip(row["weather"])
        if not hit:
            continue
        return Event(loc_key=loc["key"], loc_name=loc["name"], kind=kind, start=when,
                     lead_minutes=max(delta_min, 0), source="cn",
                     detail=row["weather"], slot=when.strftime("%Y-%m-%dT%H:%M"))
    return None


def cn_state(loc: dict, log) -> tuple[dict, list[dict], dict]:
    """一次请求拿到：实况、预警、逐日高低温。"""
    cached = _cache_get(("state", loc["city_code"]), 300)
    if cached is not None:
        return cached
    try:
        text = http_text(f"https://d1.weather.com.cn/weather_index/{loc['city_code']}.html",
                         _CN_REFERER)
    except Exception as exc:
        log.warning("[%s] 实况请求失败：%s", loc["key"], exc)
        return {}, [], {}
    sk = parse_js_var(text, "dataSK") or {}
    warnings = (parse_js_var(text, "alarmDZ") or {}).get("w") or []
    daily = {}
    for row in (parse_js_var(text, "fc") or {}).get("f") or []:
        try:
            m, d = [int(x) for x in str(row.get("fi", "")).split("/")]
            daily[(m, d)] = {"high": int(row.get("fc")), "low": int(row.get("fd"))}
        except (ValueError, TypeError):
            continue
    _cache_put(("state", loc["city_code"]), (sk, warnings, daily))
    return sk, warnings, daily


# ---------------------------------------------------------------- 措辞
def fmt_minutes(m: float | None) -> str:
    if m is None:
        return "一会儿"
    m = int(round(m))
    if m < 60:
        return f"{m} 分钟"
    h, mm = divmod(m, 60)
    return f"{h} 小时{(' %d 分' % mm) if mm else ''}"


_URL_CACHE: dict[str, tuple[float, object]] = {}


def _cache_get(key, ttl: float):
    it = _URL_CACHE.get(key)
    if it and (time.time() - it[0]) < ttl:
        return it[1]
    return None


def _cache_put(key, val) -> None:
    _URL_CACHE[key] = (time.time(), val)


def _warn_expired(w: dict, now: datetime) -> bool:
    """预警的失效时间（w15）已过就视为过期。"""
    raw = str(w.get("w15") or "").strip()
    if not raw:
        return False
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=TZ) < now
        except ValueError:
            continue
    return False


def _nmc():
    try:
        import nmc as _m
        return _m
    except Exception:
        return None


_STATION_CACHE: dict[str, str | None] = {}


def nmc_station(loc: dict) -> str | None:
    key = loc["key"]
    if key in _STATION_CACHE:
        return _STATION_CACHE[key]
    m = _nmc()
    code = None
    if m:
        try:
            code = m.station_for(loc["name"])
        except Exception:
            code = None
    _STATION_CACHE[key] = code
    return code


def nmc_real(loc: dict) -> dict | None:
    m = _nmc()
    st = nmc_station(loc)
    if not (m and st):
        return None
    try:
        return m.real(st)
    except Exception:
        return None


def nmc_feel(loc: dict):
    r = nmc_real(loc)
    return None if not r else r.get("feels")


def nmc_rain(loc: dict):
    r = nmc_real(loc)
    return None if not r else r.get("rain")



WARNING_ADVICE = {
    "大雾": "能见度低，骑车记得开灯、放慢，能走人行道就别上机动车道。",
    "浓雾": "能见度很低，非必要别骑两轮车，开车开雾灯。",
    "霾": "空气不好，剧烈运动挪到室内，口罩备一个。",
    "沙尘": "关好门窗，出门戴口罩和眼镜。",
    "大风": "风大，注意高空坠物，骑车容易被吹偏，尽量走内侧。",
    "道路结冰": "路面会滑，尽量别骑两轮车，走路小步慢行。",
    "暴雨": "雨势猛，避开下穿隧道和低洼路段。",
    "暴雪": "雪大路滑，能不出门就别出门。",
    "雷电": "别在空旷处停留，远离大树和广告牌。",
    "高温": "避开正午出门，多补水，别长时间暴晒。",
    "低温": "注意保暖，尤其手脚耳朵。",
    "寒潮": "降温幅度大，这两天把厚衣服全穿上。",
    "冰雹": "找个有遮挡的地方躲一下，别把车停露天。",
    "台风": "尽量待在室内，远离窗户和广告牌。",
    "干旱": "",
    "森林火险": "天干物燥，别在野外用火。",
}


def warning_advice(title: str, body: str = "") -> str:
    """按预警类型给一条具体建议。"""
    for key, tip in WARNING_ADVICE.items():
        if key in title and tip:
            return tip
    return ""


def build_warning(w: dict) -> str:
    title = (w.get("w5") or "").strip()
    level = (w.get("w7") or "").strip()
    area = (w.get("w2") or w.get("w1") or "").strip()
    body = re.sub(r"（[^）]*）", "", (w.get("w9") or "")).strip()

    # 去掉「XX气象台X日X时X分发布XX预警信号：」这种抬头，标题里已经说过了
    body = re.sub(r"^[^，。]{0,40}(?:气象台|气象局|预警中心)[^，。]{0,40}发布[^，。：:]{0,30}[，：:]\s*",
                  "", body)
    if body == (w.get("w9") or "").strip():
        head_part = body[:100]
        if ("发布" in head_part or "预警" in head_part) and re.search(r"[：:]", head_part):
            body = re.split(r"[：:]", body, maxsplit=1)[-1].strip()

    # 按句号切，尽量整句结束，不硬截断半句
    sents = [s for s in re.split(r"(?<=。)", body) if s.strip()]
    out = ""
    for s in sents:
        if out and len(out) + len(s) > 140:
            break
        out += s
    out = out.strip().rstrip("。")
    # 标题里「北京市大兴区气象台发布」这种前缀去掉，只留「大兴区」
    short_area = re.sub(r"^.*?(省|自治区|市)?", "", area) if area else ""
    short_area = area[-4:] if len(area) > 4 else area
    head = f"⚠️ {short_area}{title}{level}预警"
    advice = warning_advice(title + level, out)
    lines = [head]
    if out:
        lines.append(out.strip())
    if advice:
        lines.append(advice)
    return "\n".join(x for x in lines if x)


def intensity_word(kind: str, precip: float | None) -> tuple[str, str]:
    """按降水强度给措辞。kind: rain/snow。返回 (强度词, 提示语)。"""
    p = float(precip or 0)
    if kind == "snow":
        if p >= 4:
            return "暴雪", "雪很大，能不出门就别出门了吧"
        if p >= 1.5:
            return "大雪", "积雪结冰，骑车太容易滑了，今天能坐车就坐车吧~"
        if p >= 0.4:
            return "中雪", "路面会滑，慢点儿走小心不要摔倒咯~"
        return "小雪", "路可能有点滑，走慢一点哦~"
    if p >= 8:
        return "暴雨", "雨势很猛，非必要就别出门了吧。"
    if p >= 2.5:
        return "大雨", "雨不小，记得带伞，路上留意积水小心溅湿衣服"
    if p >= 0.5:
        return "中雨", "如果要出门的话记得带伞哦~"
    return "小雨", "如果要出门的话记得带伞哦~"


def _event_precip(ev) -> float:
    import re as _re
    m = _re.search(r"([0-9.]+)\s*mm", getattr(ev, "detail", "") or "")
    try:
        return float(m.group(1)) if m else 0.0
    except Exception:
        return 0.0


def build_message(ev: Event, prefix: str = "") -> str:
    clock = ev.start.strftime("%H:%M") if ev.start else ""
    word, hint = intensity_word(ev.kind, _event_precip(ev))
    if ev.kind == "snow":
        icon = "❄️"
    elif ev.kind == "hail":
        icon = "🧊"
    else:
        icon = "🌧"
    hint = hint.rstrip("。")

    if ev.kind == "hail":
        verb = "可能有冰雹"
    else:
        verb = f"下{word}"

    if ev.kind == "hail":
        hint = "注意遮挡，别把车停露天哦~"

    end_mark = "。" if not hint.endswith(("~", "。")) else ""
    if ev.source == "qweather":
        lead = ev.lead_minutes or 0
        if lead <= 1:
            body = f"{icon} 已经开始{verb}了，{hint}{end_mark}"
        else:
            body = f"{icon} 还有 {fmt_minutes(ev.lead_minutes)}就{verb}了，{hint}{end_mark}"
    else:
        body = f"{icon} {clock} 前后有{ev.detail}，{hint}{end_mark}"
    return f"{prefix}{body}"


# ---------------------------------------------------------------- 通知
try:
    import cnlunar  # 农历 / 节气
except Exception:  # pragma: no cover
    cnlunar = None

FESTIVALS_SOLAR = {
    (1, 1): "元旦", (2, 14): "情人节", (3, 8): "妇女节", (5, 1): "劳动节",
    (6, 1): "儿童节", (9, 10): "教师节", (10, 1): "国庆节", (12, 25): "圣诞节",
}
FESTIVALS_LUNAR = {
    (1, 1): "春节", (1, 15): "元宵节", (2, 2): "龙抬头", (5, 5): "端午节",
    (7, 7): "七夕", (7, 15): "中元节", (8, 15): "中秋节", (9, 9): "重阳节",
    (12, 8): "腊八节", (12, 23): "小年",
}


def _lunar_obj(d):
    return cnlunar.Lunar(datetime(d.year, d.month, d.day, 12, 0), godType="8char")


def next_solar_term(d) -> tuple[str, int | None]:
    if cnlunar is None:
        return "", None
    today = d.date() if isinstance(d, datetime) else d
    cands = []
    for y in (today.year, today.year + 1):
        try:
            a = cnlunar.Lunar(datetime(y, 1, 1, 12, 0), godType="8char")
            for name, (m, dd) in a.thisYearSolarTermsDic.items():
                try:
                    cands.append((datetime(y, m, dd).date(), name))
                except ValueError:
                    pass
        except Exception:
            continue
    cands.sort()
    for dt, name in cands:
        if dt >= today:
            return name, (dt - today).days
    return "", None


SOLAR_TERM_NOTES = {
    "立春": "从今天起，白天要一点点变长了",
    "雨水": "雨开始多了，出门带伞的习惯可以养起来",
    "惊蛰": "虫子醒了，风也开始带暖意",
    "春分": "昼夜等长，往后白天比夜晚长",
    "清明": "该出去走走了，风都是软的",
    "谷雨": "雨生百谷，这几天雨会多起来",
    "立夏": "夏天开场，注意别一冷一热着凉",
    "小满": "麦子开始灌浆，天气也闷起来",
    "芒种": "又忙又热，记得多喝水",
    "夏至": "一年里白天最长的一天，往后开始缩短",
    "小暑": "暑气上来了，空调别对着吹",
    "大暑": "一年最热的时候，尽量避开正午出门",
    "立秋": "名义上入秋了，但暑气还要拖一阵",
    "处暑": "暑气到此为止，早晚会凉快些",
    "白露": "露水起来了，早晚要添件外套",
    "秋分": "昼夜再次等长，天气一天比一天凉",
    "寒露": "露水变凉，脚踝和脖子要护住了",
    "霜降": "开始下霜，早晨路面可能打滑",
    "立冬": "冬天正式开始，厚衣服该翻出来了",
    "小雪": "往后的日子会一天比一天冷，注意保暖",
    "大雪": "雪会越下越大，出门注意防滑",
    "冬至": "一年里白天最短，往后慢慢变长",
    "小寒": "冷空气开始频繁，注意保暖",
    "大寒": "一年最冷的时候，能不出去就别出去",
}


def solar_term_note(d: datetime, prefix: str = "今天") -> str:
    """如果那天正好是节气，返回一句应景的话。prefix: 今天/明天。"""
    if cnlunar is None:
        return ""
    try:
        a = _lunar_obj(d)
        term = str(getattr(a, "todaySolarTerms", "") or "")
        if not term or term == "无":
            return ""
        note = SOLAR_TERM_NOTES.get(term, "")
        note = note.replace("从今天起，", "").replace("从今天起", "")
        return f"从{prefix}起，" + note + "。" if note else f"{prefix}是「{term}」节气。"
    except Exception:
        return ""


def lunar_suffix(d: datetime) -> str:
    """返回「农历十月初六 · 距小雪 8 天」这样的短句；没有库就返回空串。"""
    if cnlunar is None:
        return ""
    try:
        a = _lunar_obj(d)
    except Exception:
        return ""
    month = str(a.lunarMonthCn).rstrip("大小")
    if a.isLunarLeapMonth:
        month = "闰" + month
    base = f"农历{month}{a.lunarDayCn}"

    tags = []
    term = str(getattr(a, "todaySolarTerms", "") or "")
    if term and term != "无":
        tags.append(f"{term}节气")
    fest = FESTIVALS_LUNAR.get((a.lunarMonth, a.lunarDay))
    if not fest:
        try:
            nxt = _lunar_obj(d + timedelta(days=1))
            if nxt.lunarMonth == 1 and nxt.lunarDay == 1:
                fest = "除夕"
        except Exception:
            fest = None
    if fest:
        tags.append(fest)
    solar_fest = FESTIVALS_SOLAR.get((d.month, d.day))
    if solar_fest:
        tags.append(solar_fest)

    if tags:
        return f"{base} · {'、'.join(tags)}"
    name, days = next_solar_term(d)
    if name and days is not None:
        return f"{base} · 距离{name}还有 {days} 天"
    return base


def aqi_word(aqi: float | None) -> str:
    if aqi is None:
        return ""
    if aqi <= 50:
        return "优"
    if aqi <= 100:
        return "良"
    if aqi <= 150:
        return "轻度污染"
    if aqi <= 200:
        return "中度污染"
    if aqi <= 300:
        return "重度污染"
    return "严重污染"


def week_cn(d: datetime) -> str:
    return "一二三四五六日"[d.weekday()]


def rows_of_day(rows: list[dict], day: datetime, now: datetime) -> list[dict]:
    out = []
    for r in rows:
        try:
            dt = to_datetime(r["day"], r["hour"], now)
        except Exception:
            continue
        if dt.date() == day.date():
            out.append({**r, "dt": dt})
    return sorted(out, key=lambda x: x["dt"])


def weather_flow(rows: list[dict]) -> str:
    seq = []
    for r in rows:
        w = r.get("weather") or ""
        if w and (not seq or seq[-1] != w):
            seq.append(w)
    if not seq:
        return "—"
    if len(seq) <= 3:
        return "转".join(seq)
    return f"{seq[0]}转{seq[-1]}"


def temp_range(rows: list[dict]) -> str:
    temps = [r["temp"] for r in rows if isinstance(r.get("temp"), int)]
    if not temps:
        return ""
    return f"{min(temps)}~{max(temps)}℃"


def hour_label(h: int) -> str:
    if h < 6:
        return "凌晨"
    if h < 9:
        return "早上"
    if h < 12:
        return "上午"
    if h < 14:
        return "中午"
    if h < 18:
        return "下午"
    return "晚上"


def fmt_clock(h: int) -> str:
    h12 = h if 0 < h <= 12 else (h - 12 if h > 12 else 12)
    return f"{hour_label(h)}{h12}点"


def dress_for(t: float) -> str:
    if t >= 30:
        return "短袖短裤，注意防晒补水"
    if t >= 25:
        return "短袖或薄长袖"
    if t >= 20:
        return "长袖衬衫、薄外套"
    if t >= 15:
        return "薄外套、针织衫"
    if t >= 10:
        return "外套加毛衣"
    if t >= 5:
        return "棉服、大衣，下身加条秋裤"
    if t >= 0:
        return "羽绒服、厚棉衣"
    if t >= -5:
        return "厚羽绒服，帽子围巾手套"
    return "极寒天，羽绒服+帽子围巾手套全套"




def solar_term_name(d: datetime) -> str:
    """当天若是节气，返回节气名。"""
    if cnlunar is None:
        return ""
    try:
        term = str(getattr(_lunar_obj(d), "todaySolarTerms", "") or "")
        return "" if term in ("", "无") else term
    except Exception:
        return ""


def evening_closing(d: datetime) -> str:
    """按明天是星期几，给一句不同的收尾。"""
    wd = d.weekday()          # 0=周一
    if wd == 6:               # 明天周日
        head = "明天是双休的最后一天，不要熬夜太晚哦~"
    elif wd == 5:             # 明天周六
        head = "明天就周末了，今晚可以稍微放松点~"
    elif wd == 0:             # 明天周一
        head = "明天是周一，今晚早点睡，别让周末的作息拖累你~"
    elif wd == 4:             # 明天周五
        head = "明天是这周最后一个工作日，撑一下就周末了~"
    else:
        head = "明天还要上班，今晚早点休息哦~"
    return head + "明早我会把天气说给你听 🌙"


def morning_clothes(low: int, high: int | None) -> str:
    txt = f"早上 {low}℃，记得穿{dress_for(low)}"
    if high is not None and high - low >= 6:
        txt += f"；白天会回到 {high}℃，{dress_for(high)}，能脱能穿最省事"
    txt = (txt.replace("下身加条秋裤", "下身最好加条秋裤")
              .replace("帽子围巾手套", "帽子加围巾手套"))
    return txt


def evening_clothes(low: int, high: int | None) -> str:
    txt = f"早上 {low}℃，记得穿{dress_for(low)}"
    if high is not None and high - low >= 6:
        txt += f"；白天会到 {high}℃，{dress_for(high)}，别穿太厚闷出汗"
    txt = (txt.replace("下身加条秋裤", "下身最好加条秋裤")
              .replace("帽子围巾手套", "帽子加围巾手套"))
    return txt


def clothing_line(low: int | None, high: int | None, when: str = "早上") -> str:
    if low is None:
        return ""
    tip = f"👕 穿衣：{when} {low}℃，{dress_for(low)}"
    if high is not None and high - low >= 6:
        tip += f"；白天 {high}℃，{dress_for(high)}，方便穿脱的搭法最好"
    return tip + "。"


def precip_times(rows: list[dict]) -> str:
    hits = []
    for r in rows:
        hit, _ = is_precip(r.get("weather") or "")
        if hit:
            hits.append(r)
    if not hits:
        return ""
    kind = hits[0].get("weather", "")
    times = "、".join(fmt_clock(r["dt"].hour) for r in hits[:4])
    return f"{times}前后有{kind}"


def build_briefing(cfg: dict, loc: dict, mode: str, now: datetime, log) -> str | None:
    """mode: morning（今天）/ evening（明天）"""
    try:
        rows = cn_hour3(loc["city_code"])
    except Exception as exc:
        log.warning("[%s] 播报取数失败：%s", loc["key"], exc)
        return None
    sk, warnings, daily = cn_state(loc, log)
    target_day = now if mode == "morning" else now + timedelta(days=1)
    day_rows = rows_of_day(rows, target_day, now)
    if not day_rows:
        return None

    name = loc["name"]
    date_txt = f"{target_day.month}月{target_day.day}日 星期{week_cn(target_day)}"
    flow = weather_flow(day_rows)
    tr = temp_range(day_rows)
    rain = precip_times(day_rows)

    lines = []
    day_hl = daily.get((target_day.month, target_day.day)) or {}
    hl_txt = ""
    if day_hl.get("high") is not None and day_hl.get("low") is not None:
        hl_txt = f"最高 {day_hl['high']}℃ / 最低 {day_hl['low']}℃"
    elif tr:
        hl_txt = tr

    low, high = day_hl.get("low"), day_hl.get("high")
    if low is None or high is None:
        temps = [r["temp"] for r in day_rows if isinstance(r.get("temp"), int)]
        low = low if low is not None else (min(temps) if temps else None)
        high = high if high is not None else (max(temps) if temps else None)

    feel = nmc_feel(loc)
    aqi_txt = ""
    if sk:
        try:
            aqi_v = float(sk.get("aqi"))
        except (TypeError, ValueError):
            aqi_v = None
        if aqi_v is not None and aqi_v > 0:
            aqi_txt = f"{int(aqi_v)}（{aqi_word(aqi_v)}）"

    if mode == "morning":
        lines.append(f"【{name}】早安 ☀️")
        head = "今天是" + date_txt + (f" · {lunar_suffix(target_day)}" if lunar_suffix(target_day) else "")
        lines.append(head)
        _m_note = solar_term_note(target_day, "今天")
        if _m_note:
            lines.append(_m_note)
        lines.append("")
        lines.append(f"今天 {flow}" + (f"，{hl_txt}" if hl_txt else ""))
        now_bits = []
        if sk:
            now_txt = f"现在是 {sk.get('temp')}℃"
            if sk.get("weather"):
                now_txt += f"、{sk['weather']}"
            if sk.get("WD"):
                now_txt += f"、{sk['WD']}{sk.get('WS') or ''}".rstrip()
            now_bits.append(now_txt)
        if feel is not None:
            now_bits.append(f"体感 {feel:g}℃")
        if now_bits:
            tail = "，" + "，".join(now_bits)
            if feel is not None and high is not None and feel <= 0:
                tail += "，风一吹比温度还冷些"
            lines.append(tail.lstrip("，"))
        lines.append("")
        if low is not None:
            lines.append(morning_clothes(low, high))
        if rain:
            lines.append(f"⚠️ {rain}，伞别忘了，别淋着")
        elif flow:
            lines.append("今天没有雨雪，请放心出门")
        if aqi_txt:
            lines.append(f"空气质量 {aqi_txt}" + ("，可以开窗透透气" if "优" in aqi_txt else "，敏感的话口罩备一个"))
        lines.append("")
        lines.append("骑车注意安全，到了跟我报个平安~ 🌤")
    elif mode == "noon":
        rest = [r for r in day_rows if r["dt"] >= now.replace(minute=0, second=0, microsecond=0)]
        rest = rest or day_rows
        r_flow = weather_flow(rest)
        r_low = min((r["temp"] for r in rest if isinstance(r.get("temp"), int)), default=low)
        r_high = max((r["temp"] for r in rest if isinstance(r.get("temp"), int)), default=high)
        r_rain = precip_times(rest)

        lines.append(f"【{name}】中午好 🌤")
        head = "今天是" + date_txt + (f" · {lunar_suffix(target_day)}" if lunar_suffix(target_day) else "")
        lines.append(head)
        _n_note = solar_term_note(target_day, "今天")
        if _n_note:
            lines.append(_n_note)
        lines.append("")
        now_bits = []
        if sk:
            t = f"现在是 {sk.get('temp')}℃"
            if sk.get("weather"):
                t += f"、{sk['weather']}"
            now_bits.append(t)
        if feel is not None:
            now_bits.append(f"体感 {feel:g}℃")
        if now_bits:
            lines.append("，".join(now_bits))
        lines.append(f"接下来 {r_flow}" + (f"，{r_low}℃ 到 {r_high}℃" if r_low is not None and r_high is not None else ""))
        lines.append("")
        if r_low is not None:
            lines.append(f"下午到夜里 {r_low}℃，{dress_for(r_low)}，晚点会更凉，外套别落下了")
        if r_rain:
            lines.append(f"⚠️ {r_rain}，出门把伞带上")
        else:
            lines.append("下午到夜里没有雨雪，不用带伞")
        if aqi_txt:
            lines.append(f"空气质量 {aqi_txt}")
        lines.append("")
        lines.append("中午记得吃点东西，别忙忘了 🍚")
    else:
        lines.append(f"【{name}】晚安 🌙")
        _lun = lunar_suffix(target_day)
        _term = solar_term_name(target_day)
        if _term:
            _lun = _lun.replace(f" · {_term}节气", "") + f" · 二十四节气中的 · {_term}"
        head = "明天是" + date_txt + (f" · {_lun}" if _lun else "")
        lines.append(head)
        note = solar_term_note(target_day, "明天")
        if note:
            lines.append(note)
        lines.append("")
        wind = ""
        day_rows_w = [r for r in day_rows if 6 <= r["dt"].hour <= 20]
        if day_rows_w:
            r0 = day_rows_w[-1]
            if r0.get("wind_dir"):
                wind = f"，{r0['wind_dir']}{r0.get('wind_scale') or ''}"
        lines.append(f"{flow}" + (f"，{hl_txt}" if hl_txt else "") + wind)
        lines.append("")
        if low is not None:
            lines.append(evening_clothes(low, high))
        if rain:
            lines.append(f"⚠️ {rain}，明早出门记得带伞")
        else:
            lines.append("没有雨雪，出门不用带伞")
        pts = {r["dt"].hour: r.get("temp") for r in day_rows if r["dt"].hour in (8, 14, 20)}
        bits = []
        if pts.get(14) is not None:
            bits.append(f"下午2点最暖，{pts[14]}℃")
        if pts.get(20) is not None:
            bits.append(f"晚上8点还有{pts[20]}℃")
        if pts.get(8) is not None:
            bits.append(f"明早8点 {pts[8]}℃")
        if bits:
            lines.append("")
            lines.append("，".join(bits))
        lines.append("")
        lines.append(evening_closing(target_day))

    seen_warn = set()
    for w in warnings:
        title = w.get("w5") or ""
        level = w.get("w7") or ""
        sig = (title, level)
        if not title or sig in seen_warn:
            continue
        seen_warn.add(sig)
        lines.append(f"⚠️ {title}{level}预警仍在生效。")
        if len(seen_warn) >= 2:
            break
    return "\n".join(lines)


def due_briefings(cfg: dict, now: datetime, log) -> list[dict]:
    conf = cfg.get("briefing") or {}
    if conf.get("enabled") is False:
        return []
    window = max(15, int(cfg.get("poll_minutes", 5)) + 5)
    out = []
    defaults = {"morning": "06:00", "noon": "12:00", "evening": "21:00"}
    for mode in ("morning", "noon", "evening"):
        clock = str(conf.get(mode) or defaults[mode])
        try:
            hh, mm = [int(x) for x in clock.split(":")]
        except Exception:
            continue
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (now - start).total_seconds() / 60
        if not (0 <= delta < window):
            continue
        for loc in locations(cfg):
            msg = build_briefing(cfg, loc, mode, now, log)
            if not msg:
                continue
            out.append({"key": f"brief:{mode}:{loc['key']}:{now.date()}",
                        "msg": msg, "kind": "brief", "loc": loc["key"]})
    return out


def deliver(cfg: dict, msg: str, wanted: list[dict], log) -> bool:
    ok_any = False
    for t in wanted:
        try:
            if t["kind"] == "group":
                res = ws_send.send_group(t["id"], msg)
            else:
                res = ws_send.send_private(t["id"], msg)
        except Exception as exc:
            log.error("发送到 %s:%s 失败：%s", t["kind"], t["id"], exc)
            continue
        ok = str(res.get("status")) == "ok"
        log.info("已发送 -> %s:%s (%s): %s", t["kind"], t["id"],
                 "ok" if ok else res.get("wording") or res.get("retcode"), msg)
        ok_any = ok_any or ok
    return ok_any


# ---------------------------------------------------------------- 判断
def _scan_times(cfg) -> list[str]:
    pl = cfg.get("planner") or {}
    return pl.get("scan_times") or ["06:05", "21:05"]


def _need_scan(now, p, scan_times) -> bool:
    last = p.get("last_scan")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return True
    for t in scan_times:
        try:
            hh, mm = [int(x) for x in str(t).split(":")]
        except Exception:
            continue
        sched = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= sched and last_dt < sched:
            return True
    return False


def refresh_plan(cfg: dict, loc: dict, p: dict, now: datetime, log) -> None:
    """每天扫三次（免费三小时预报 + 多源投票）：没雨就静默，有雨就定好「几点开始盯」。

    三次的视野：
      早上（<11 点）→ 今天上午 + 全天
      中午（11–17 点）→ 今天下午到夜里
      晚上（≥18 点）→ 今晚剩余 + 明天全天
    审查本身不发消息，只决定「要不要盯分钟级」。
    """
    pl = cfg.get("planner") or {}
    arm_lead = int(pl.get("arm_lead_minutes", 30))
    if now.hour < 12:
        # 00:00 那次：0 点到 11:59 + 全天兜底
        horizon_end = now.replace(hour=23, minute=59, second=0, microsecond=0)
        scope = "今天凌晨到夜里"
    elif now.hour < 18:
        # 12:00 那次：12:00–17:59
        horizon_end = now.replace(hour=17, minute=59, second=0, microsecond=0)
        scope = "今天下午"
    else:
        # 18:00 那次：18:00–23:59，顺带把明天一整天也过一遍
        horizon_end = (now + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
        scope = "今晚到明天"
    try:
        rows = cn_hour3(loc["city_code"])
    except Exception as exc:
        log.warning("[%s] 规划扫描失败：%s", loc["key"], exc)
        return
    first_start = None
    for row in rows:
        when = to_datetime(row["day"], row["hour"], now)
        if when < now - timedelta(minutes=30):
            continue
        if when > horizon_end:
            break
        hit, _kind = is_precip(row["weather"])
        if hit:
            first_start = when
            break
    if first_start is None:
        p.update({"mode": "idle", "armed_at": None, "alerted": False,
                  "scope": scope, "horizon": horizon_end.isoformat()})
        log.info("[%s] %s无降水 → 静默，不查分钟级", loc["key"], scope)
    else:
        need = first_start - timedelta(minutes=arm_lead)
        arm = now if need < now else need
        p.update({"mode": "armed", "armed_at": arm.isoformat(),
                  "alerted": False, "rain_since": None,
                  "scope": scope, "horizon": horizon_end.isoformat()})
        log.info("[%s] %s的 %s 有降水 → %s 起盯分钟级",
                 loc["key"], scope, first_start.strftime("%m-%d %H:%M"), arm.strftime("%H:%M"))

    # 多源投票兜底：三小时预报说没雨、但过半数据源说有 → 照样盯（宁可错报，不许漏报）
    try:
        import tier_check as tc
        res = tc.decide(cfg, loc, log)
        p["tier"] = {"level": res.get("level"), "wet": res.get("wet"),
                     "first": res.get("first"),
                     "province": res.get("province"),
                     "province_wet": (res.get("province_scan") or {}).get("wet"),
                     "province_checked": (res.get("province_scan") or {}).get("checked"),
                     "votes": [{"s": v.get("source"), "w": v.get("wet")} for v in res.get("votes", [])],
                     "at": now.isoformat()}
        if res.get("wet") and p.get("mode") == "idle":
            first_raw = res.get("first")
            start_dt = None
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    start_dt = datetime.strptime(str(first_raw), fmt).replace(tzinfo=TZ)
                    break
                except (ValueError, TypeError):
                    continue
            arm_dt = (start_dt - timedelta(minutes=arm_lead)) if start_dt else (now - timedelta(minutes=1))
            if arm_dt < now:
                arm_dt = now - timedelta(minutes=1)
            p.update({"mode": "armed", "armed_at": arm_dt.isoformat(),
                      "alerted": False, "rain_since": None,
                      "scope": scope, "horizon": horizon_end.isoformat()})
            log.info("[%s] 多源投票判定有降水（最早 %s）→ 改为 %s 起盯",
                     loc["key"], first_raw or "未知", arm_dt.strftime("%m-%d %H:%M"))
    except Exception as exc:
        log.warning("[%s] 降级审查失败：%s", loc["key"], exc)


def city_tick(cfg: dict, loc: dict, now: datetime, log, p: dict) -> list[dict]:
    """单城市一次调度：扫描 → （必要时）分钟级查询 → 出提醒。"""
    out: list[dict] = []
    pl = cfg.get("planner") or {}
    poll_gap = float(pl.get("armed_poll_minutes", 2))
    max_h = float(pl.get("max_arm_hours", 8))
    prefix = f"【{loc['name']}】" if len(locations(cfg)) > 1 else ""

    if _need_scan(now, p, _scan_times(cfg)):
        refresh_plan(cfg, loc, p, now, log)
        p["last_scan"] = now.isoformat()

    if p.get("mode") != "armed" or not p.get("armed_at"):
        return out

    armed_at = datetime.fromisoformat(p["armed_at"])
    if now < armed_at:
        return out
    if (now - armed_at).total_seconds() > max_h * 3600:
        p.update({"mode": "idle", "armed_at": None, "alerted": False})
        log.info("[%s] 盯了 %d 小时没等到雨，收工", loc["key"], int(max_h))
        return out

    last_poll = p.get("last_poll")
    if last_poll:
        try:
            if (now - datetime.fromisoformat(last_poll)).total_seconds() < poll_gap * 60:
                return out
        except Exception:
            pass
    p["last_poll"] = now.isoformat()

    r = qweather_query(cfg, loc, now, log)
    if not r["ok"]:
        return out

    if not r["raining"]:
        try:
            _nr = nmc_rain(loc)
            if _nr is not None and _nr >= 0.1:
                r["raining"] = True
                log.info("[%s] 和风未见降水，但中央气象台站点实测 %.1fmm → 判定在下雨", loc["key"], _nr)
        except Exception:
            pass

    if r["raining"]:
        p["alerted"] = True
        p.setdefault("rain_since", now.isoformat())
        # 开始盯的时候就已经在下 —— 也要喊一声（每场雨只喊一次）
        if not p.get("rain_notified"):
            p["rain_notified"] = True
            _kind = "snow" if _wet_is_snow(loc) else "rain"
            _word = "下雪" if _kind == "snow" else "下雨"
            _tip = "路面滑，慢点儿走。" if _kind == "snow" else "出门记得带伞。"
            out.append({"key": f"raining:{loc['key']}:{now.strftime('%Y-%m-%dT%H')}",
                        "msg": f"{prefix}{'❄️' if _kind == 'snow' else '🌧'} 现在正在{_word}，{_tip}",
                        "kind": "rain-now", "loc": loc["key"]})
            log.info("[%s] 已在降雨，发“正在下雨”提醒", loc["key"])
        return out

    ev = r["event"]
    if ev and (ev.lead_minutes or 0) <= float(cfg.get("lead_minutes", 10)):
        p["alerted"] = True
        out.append({"key": f"precip:{loc['key']}:{ev.slot}",
                    "msg": build_message(ev, prefix), "kind": "precip", "loc": loc["key"]})
        return out

    # 多源兜底：和风没报降水，但多数源说有 → 也提醒（宁多勿漏）
    if not ev and not p.get("alerted"):
        try:
            import tier_check as tc
            votes = tc.city_votes(cfg, loc)
            wet, first = tc.vote(votes)
            stamp = now.strftime("%Y-%m-%dT%H")
            if wet and p.get("tier_alert") != stamp:
                p["tier_alert"] = stamp
                p["alerted"] = True
                detail = "、".join(v["source"] for v in votes if v.get("wet"))
                out.append({"key": f"precip:{loc['key']}:tier:{stamp}",
                            "msg": f"{prefix}🌧 多源提示有降水（{detail} 判定，最早 {first or '即将'}），记得带伞。",
                            "kind": "precip", "loc": loc["key"]})
                log.info("[%s] 和风未报，多源投票判有降水 → 兜底提醒", loc["key"])
        except Exception:
            pass

    # 雨停了：喊过雨、且未来一小时没雨 → 播报一次然后收工
    if p.get("alerted") and r["next60"] == 0:
        out.append({"key": f"stop:{loc['key']}:{now.strftime('%Y-%m-%dT%H')}",
                    "msg": f"{prefix}🌤 雨停了，接下来一小时没雨，可以出门了。",
                    "kind": "stop", "loc": loc["key"]})
        p.update({"mode": "idle", "armed_at": None, "alerted": False,
                  "rain_since": None, "rain_notified": False})
        log.info("[%s] 雨停播报，收工", loc["key"])
    return out


def warnings_items(cfg: dict, now: datetime, state: dict, log) -> list[dict]:
    """气象预警（免费数据），10 分钟查一次。"""
    if not (cfg.get("notify") or {}).get("warnings", True):
        return []
    last = state.get("last_warn_check")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < 600:
                return []
        except Exception:
            pass
    out: list[dict] = []
    multi = len(locations(cfg)) > 1
    prev_active = state.get("active_warns") or {}
    meta = state.setdefault("warn_meta", {})
    sent_keys = state.get("sent_keys") or []
    new_active: dict[str, list[str]] = {}

    for loc in locations(cfg):
        try:
            _sk, warnings, _daily = cn_state(loc, log)
        except Exception:
            new_active[loc["key"]] = prev_active.get(loc["key"], [])
            continue
        prefix = f"【{loc['name']}】" if multi else ""
        seen = set()
        current: list[str] = []
        for w in warnings:
            try:
                title = w.get("w5") or ""
                level = w.get("w7") or ""
                if not title:
                    continue
                # 已失效的预警不再推
                if _warn_expired(w, now):
                    continue
                sig = (title, level)
                local = bool(w.get("w2"))
                if sig in seen and not local:
                    continue
                seen.add(sig)
                wid = w.get("w16") or w.get("w10") or title
                current.append(wid)
                meta[wid] = {"title": title, "level": level,
                             "area": (w.get("w2") or w.get("w1") or "").strip(),
                             "loc": loc["key"], "name": loc["name"]}
                out.append({"key": f"warn:{loc['key']}:{wid}",
                            "msg": prefix + build_warning(w),
                            "kind": "warn", "loc": loc["key"]})
            except Exception:
                continue
        # 中央气象台官方预警列表（更权威、带发布单位）
        try:
            _m = _nmc()
            if _m:
                for a in _m.alarms_for(loc["name"]):
                    aid = a.get("id") or a.get("title")
                    if not aid or aid in current:
                        continue
                    current.append(aid)
                    meta[aid] = {"title": a.get("title") or "", "level": "",
                                 "area": "", "loc": loc["key"], "name": loc["name"]}
                    out.append({"key": f"warn:{loc['key']}:nmc:{aid}",
                                "msg": f"{prefix}⚠️ {a.get('title')}（{a.get('time')}）",
                                "kind": "warn", "loc": loc["key"]})
        except Exception:
            pass

        # 之前推过、现在消失的预警 → 发「已解除」
        for wid in prev_active.get(loc["key"], []):
            if wid in current:
                continue
            if f"warn:{loc['key']}:{wid}" not in sent_keys:
                continue
            m = meta.get(wid) or {}
            label = (m.get("area") or "") + (m.get("title") or "预警") + (m.get("level") or "")
            out.append({"key": f"warn-clear:{loc['key']}:{wid}",
                        "msg": f"{prefix}✅ {label}已解除。",
                        "kind": "warn-clear", "loc": loc["key"]})
        new_active[loc["key"]] = current

    state["active_warns"] = new_active
    state["warn_meta"] = {k: v for k, v in meta.items() if k in
                          {w for lst in new_active.values() for w in lst}}
    state["last_warn_check"] = now.isoformat()
    return out


def targets_for(cfg: dict, loc_key: str) -> list[dict]:
    wanted = []
    for t in targets(cfg):
        if not t["locations"] or loc_key in t["locations"]:
            wanted.append(t)
    return wanted


def flush_outbox(cfg: dict, state: dict, now: datetime, log, send: bool) -> list[dict]:
    """通道恢复后补发之前发失败的消息（只保留 60 分钟内的）。"""
    box = state.get("outbox") or []
    if not box:
        return []
    keep, done = [], []
    for it in box:
        try:
            age = (now - datetime.fromisoformat(it["at"])).total_seconds() / 60.0
        except Exception:
            age = 999
        if age > 60:
            log.info("待发消息已过期，丢弃：%s", it.get("key"))
            continue
        wanted = targets_for(cfg, it.get("loc"))
        if not wanted:
            continue
        if send and deliver(cfg, it["msg"], wanted, log):
            done.append(it)
            state.setdefault("sent_keys", []).append(it["key"])
            log.info("补发成功 [%s]（延迟 %d 分钟）", it.get("key"), int(age))
        else:
            keep.append(it)
    state["outbox"] = keep[-20:]
    return done


def run_once(cfg: dict, log, send: bool = False) -> list[dict]:
    now = datetime.now(TZ)
    state = load_state()
    sent: list[dict] = []
    locs = locations(cfg)
    if not locs:
        log.error("config.json 里没有 locations")
        return sent

    quiet = in_quiet_hours(cfg, now)
    cooldown_min = float(cfg.get("cooldown_minutes", 5))
    last = state.get("last_alert_at")
    recent = False
    if last:
        try:
            recent = (now - datetime.fromisoformat(last)) < timedelta(minutes=cooldown_min)
        except Exception:
            recent = False

    plan = state.setdefault("plan", {})
    for loc in locs:
        p = plan.setdefault(loc["key"], {})
        try:
            items = city_tick(cfg, loc, now, log, p)
        except Exception:
            log.exception("[%s] 调度出错", loc["key"])
            continue
        for item in items:
            key, msg, kind = item["key"], item["msg"], item["kind"]
            if key in (state.get("sent_keys") or []):
                continue
            if quiet:
                log.info("静音时段，跳过：%s", msg)
                state.setdefault("sent_keys", []).append(key)
                continue
            if recent and kind == "precip":
                log.info("冷却中，跳过：%s", msg)
                continue
            wanted = targets_for(cfg, item["loc"])
            if not wanted:
                continue
            log.info("触发提醒 [%s] %s", key, msg)
            if send:
                if deliver(cfg, msg, wanted, log):
                    sent.append({"key": key, "msg": msg})
                    state.setdefault("sent_keys", []).append(key)
                    if kind == "precip":
                        state["last_alert_at"] = now.isoformat()
                        recent = True
                else:
                    # 通道断了：入待发队列，恢复后补发
                    state.setdefault("outbox", []).append(
                        {"key": key, "msg": msg, "loc": item["loc"],
                         "at": now.isoformat(), "kind": kind})
            else:
                sent.append({"key": key, "msg": msg})

    for item in warnings_items(cfg, now, state, log):
        key, msg = item["key"], item["msg"]
        if key in (state.get("sent_keys") or []):
            continue
        wanted = targets_for(cfg, item["loc"])
        if not wanted:
            continue
        log.info("触发提醒 [%s] %s", key, msg)
        if send:
            if deliver(cfg, msg, wanted, log):
                sent.append({"key": key, "msg": msg})
                state.setdefault("sent_keys", []).append(key)
        else:
            sent.append({"key": key, "msg": msg})

    # 同一目标（群/私聊）的多个城市合并成一条，避免刷屏
    grouped: dict[str, dict] = {}
    for item in due_briefings(cfg, now, log):
        key, msg = item["key"], item["msg"]
        if key in (state.get("sent_briefs") or []):
            continue
        for t in targets_for(cfg, item["loc"]):
            tk = f"{t['kind']}:{t['id']}"
            g = grouped.setdefault(tk, {"target": t, "keys": [], "msgs": []})
            g["keys"].append(key)
            g["msgs"].append(msg)
    for tk, g in grouped.items():
        merged = "\n\n".join(g["msgs"])
        log.info("播报合并 [%s] %d 个城市", tk, len(g["keys"]))
        if send:
            if deliver(cfg, merged, [g["target"]], log):
                for k in g["keys"]:
                    sent.append({"key": k, "msg": merged})
                    state.setdefault("sent_briefs", []).append(k)
        else:
            for k in g["keys"]:
                sent.append({"key": k, "msg": merged})
    state["sent_briefs"] = (state.get("sent_briefs") or [])[-40:]

    # 通道恢复后补发之前发失败的消息
    try:
        sent.extend(flush_outbox(cfg, state, now, log, send))
    except Exception:
        log.exception("补发队列处理出错")

    state["sent_keys"] = (state.get("sent_keys") or [])[-60:]
    state["last_check"] = now.isoformat()
    save_state(state)
    return sent


# ---------------------------------------------------------------- 自检 / 入口
def check_sources(cfg: dict, log) -> None:
    now = datetime.now(TZ)
    for loc in locations(cfg):
        print(f"=== {loc['name']}（{loc['key']}） ===")
        sk, warnings, daily = cn_state(loc, log)
        print("  实况：", json.dumps(sk, ensure_ascii=False)[:160])
        print("  预警：", len(warnings), [w.get("w5") for w in warnings])
        print("  逐日高低温：", daily)
        try:
            rows = cn_hour3(loc["city_code"])
            print(f"  逐三小时：{len(rows)} 条")
            for r in rows[:3]:
                print("    ", r["raw"])
        except Exception as exc:
            print("  逐三小时获取失败：", exc)
        ok, ev = qweather_minutely(cfg, loc, now, log)
        if not (cfg.get("qweather") or {}).get("key"):
            print("  和风分钟级：未配置 key")
        elif not ok:
            print("  和风分钟级：请求失败（看日志 / API Host）")
        elif ev is None:
            print("  和风分钟级：未来 2 小时无降水")
        else:
            print("  和风分钟级：", json.dumps(ev.__dict__, ensure_ascii=False, default=str))
        subs = [f"{t['kind']}:{t['id']}" for t in targets_for(cfg, loc["key"])]
        print("  推送对象：", subs)


def single_instance() -> socket.socket | None:
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



def rotate_logs(max_mb: float = 5.0) -> None:
    """单个日志超过 max_mb 就切成 .1 备份，避免无限增长。"""
    try:
        logs = BASE / "logs"
        for f in logs.glob("*.log*"):
            if not f.is_file():
                continue
            if f.stat().st_size > max_mb * 1024 * 1024:
                bak = f.with_suffix(f.suffix + ".1")
                try:
                    if bak.exists():
                        bak.unlink()
                    f.rename(bak)
                except Exception:
                    pass
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--brief", choices=["morning", "evening"],
                    help="立刻生成一条播报（今天/明天）")
    ap.add_argument("--set-key", nargs="+", metavar="KEY",
                    help="写入和风 key，可选第二个参数是 API Host")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    log = setup_logging(args.verbose)
    cfg = load_config()

    if args.set_key:
        cfg.setdefault("qweather", {})["key"] = args.set_key[0].strip()
        if len(args.set_key) > 1:
            cfg["qweather"]["host"] = args.set_key[1].strip()
        with open(BASE / "config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("已写入 key（host=%s）。重启守护后生效。" % cfg["qweather"].get("host"))
        return 0

    if args.check:
        check_sources(cfg, log)
        return 0

    if args.brief:
        now = datetime.now(TZ)
        for loc in locations(cfg):
            msg = build_briefing(cfg, loc, args.brief, now, log)
            print(msg or "(今天/明天的数据没取到)")
            print("-" * 40)
            if args.send and msg:
                deliver(cfg, msg, targets_for(cfg, loc["key"]), log)
        return 0

    if args.once:
        res = run_once(cfg, log, send=args.send)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.loop:
        lock = single_instance()
        if lock is None:
            print("已有实例在跑，退出")
            return 1
        log.info("守护启动：每分钟 tick，按需查分钟级；%d 个城市", len(locations(cfg)))
        while True:
            if (BASE / "STOP").exists():
                log.info("检测到 STOP 文件，守护退出")
                return 0
            touch_heartbeat("rain_watch")
            if int(time.time()) % 3600 < 60:
                rotate_logs()
            try:
                cfg = load_config()          # 每轮重读，改配置/加城市不用重启
                run_once(cfg, log, send=True)
            except Exception:
                log.exception("循环出错")
            time.sleep(60)                   # 每分钟 tick 一次；没雨的城市这轮不花请求

    ap.print_help()
    return 0


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
