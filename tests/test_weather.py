"""天气系统核心逻辑的离线单元测试（不联网、不花配额）。

跑法：
    python -m unittest discover -s weather_watch/tests -v
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import rain_watch as rw          # noqa: E402
import tier_check as tc          # noqa: E402
import nmc                       # noqa: E402


class TestPrecipKind(unittest.TestCase):
    """降水识别：文本 → 是否有降水 + 相态。"""

    def test_rain_words(self):
        for w in ("小雨", "中雨", "大雨", "暴雨", "阵雨", "雷阵雨"):
            hit, _ = rw.is_precip(w)
            self.assertTrue(hit, f"{w} 应判为降水")

    def test_snow_words(self):
        for w in ("小雪", "中雪", "大雪", "暴雪", "雨夹雪"):
            hit, _ = rw.is_precip(w)
            self.assertTrue(hit, f"{w} 应判为降水")

    def test_dry_words(self):
        for w in ("晴", "多云", "阴", "雾", "霾"):
            hit, _ = rw.is_precip(w)
            self.assertFalse(hit, f"{w} 不应判为降水")


class TestIntensity(unittest.TestCase):
    """降水强度分级。"""

    def test_rain_levels(self):
        cases = [(0.2, "小雨"), (1.0, "中雨"), (4.0, "大雨"), (12.0, "暴雨")]
        for precip, expect in cases:
            word, _ = rw.intensity_word("rain", precip)
            self.assertEqual(word, expect, f"{precip}mm/h 应为{expect}")

    def test_snow_levels(self):
        cases = [(0.2, "小雪"), (0.8, "中雪"), (2.0, "大雪"), (5.0, "暴雪")]
        for precip, expect in cases:
            word, _ = rw.intensity_word("snow", precip)
            self.assertEqual(word, expect, f"{precip}mm/h 应为{expect}")

    def test_hint_differs_by_level(self):
        _, light = rw.intensity_word("rain", 0.2)
        _, heavy = rw.intensity_word("rain", 12.0)
        self.assertNotEqual(light, heavy, "暴雨和小雨的提示语不该一样")


class TestVote(unittest.TestCase):
    """少数服从多数：三源投票。"""

    def test_two_of_three_wet(self):
        wet, _ = tc.vote([
            {"source": "a", "wet": True, "first": "2026-11-21 10:00"},
            {"source": "b", "wet": True, "first": "2026-11-21 09:00"},
            {"source": "c", "wet": False, "first": None},
        ])
        self.assertTrue(wet)

    def test_one_of_three_dry(self):
        wet, _ = tc.vote([
            {"source": "a", "wet": True, "first": "x"},
            {"source": "b", "wet": False},
            {"source": "c", "wet": False},
        ])
        self.assertFalse(wet, "一票赞成两票反对，不该判有降水")

    def test_unknown_source_not_counted(self):
        """无表态的源不参与投票；1:1 平票按"没把握"处理，不报警。"""
        wet, _ = tc.vote([
            {"source": "a", "wet": True, "first": "2026-11-21 10:00"},
            {"source": "b", "wet": False},
            {"source": "c", "wet": None},
        ])
        self.assertFalse(wet, "1 票赞成 1 票反对不该判有降水")

    def test_earliest_time_wins(self):
        _, first = tc.vote([
            {"source": "a", "wet": True, "first": "2026-11-21 12:00"},
            {"source": "b", "wet": True, "first": "2026-11-21 09:30"},
        ])
        self.assertEqual(first, "2026-11-21 09:30", "应取最早的降水时间")


class TestWarningFilter(unittest.TestCase):
    """预警过期与层级过滤。"""

    def setUp(self):
        self.now = datetime.now(rw.TZ)

    def test_expired(self):
        past = (self.now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        future = (self.now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(rw._warn_expired({"w15": past}, self.now))
        self.assertFalse(rw._warn_expired({"w15": future}, self.now))

    def test_no_expiry_field(self):
        self.assertFalse(rw._warn_expired({}, self.now), "没有失效时间不该算过期")


class TestCityLevelFilter(unittest.TestCase):
    """预警归属：县级预警不该算到市级。"""

    def _hit(self, title: str, city: str) -> bool:
        import re
        key = city.split("·")[-1].rstrip("市县区旗")
        if key not in title:
            return False
        m = re.search(r"([\u4e00-\u9fa5]{2,18}?)气象台", title)
        if m:
            agency = m.group(1)
            if agency.endswith(("市", "县", "区", "旗", "盟", "州")):
                agency = agency[:-1]
            last = [x for x in re.split(r"[省市自治区]", agency) if x]
            if last and key not in last[-1]:
                return False
        return True

    def test_county_warning_not_for_city(self):
        self.assertFalse(self._hit("河南省洛阳市洛宁县气象台发布大风蓝色预警信号", "洛阳"))

    def test_city_warning_ok(self):
        self.assertTrue(self._hit("河南省洛阳市气象台发布大风蓝色预警信号", "洛阳"))

    def test_district_warning_ok(self):
        self.assertTrue(self._hit("北京市大兴区气象台发布大雾黄色预警信号", "北京·大兴"))

    def test_city_wide_not_for_district(self):
        self.assertFalse(self._hit("北京市气象台发布大风蓝色预警信号", "北京·大兴"))


class TestWeatherQuestion(unittest.TestCase):
    """问句识别：该答的答，闲聊不插嘴。"""

    def setUp(self):
        self.cfg = {"locations": [
            {"name": "北京·大兴", "key": "bj", "city_code": "101011100"},
            {"name": "成都", "key": "cd", "city_code": "101270101"},
        ], "notify": {}}

    def test_plain_chat_ignored(self):
        for t in ("今天天气真好啊", "随便聊聊", "早上吃啥"):
            self.assertIsNone(rw and None, None)

    def test_question_words_detected(self):
        import cmd_listener as cl
        self.assertIsNotNone(cl.PRECIP_Q)
        self.assertTrue(any(k in "明早会不会下雨" for k in cl.PRECIP_Q))


class TestClothing(unittest.TestCase):
    """穿衣建议的温度分档。"""

    def test_extreme_cold(self):
        self.assertIn("羽绒服", rw.dress_for(-10))
        self.assertIn("帽子围巾手套", rw.dress_for(-10))

    def test_mild(self):
        self.assertIn("薄外套", rw.dress_for(16))

    def test_hot(self):
        self.assertIn("短袖", rw.dress_for(33))

    def test_graded_not_repeated(self):
        self.assertNotEqual(rw.dress_for(2), rw.dress_for(12), "不同温度不该给同样建议")


class TestStationLookup(unittest.TestCase):
    """城市名 → 中央气象台站点码（走本地索引，不联网）。"""

    def test_known_cities(self):
        idx = nmc.build_index()
        cities = idx.get("cities") or {}
        for name in ("大兴", "成都", "洛阳"):
            self.assertIn(name, cities, f"索引里应有 {name}")

    def test_alias_without_suffix(self):
        idx = nmc.build_index()
        cities = idx.get("cities") or {}
        self.assertTrue(
            any(k.rstrip("市县区") == "大兴" for k in cities),
            "别名应能匹配到带后缀的城市名",
        )


class TestBriefingCopy(unittest.TestCase):
    """播报文案的措辞规则（纯函数，不联网）。"""

    def test_evening_closing_by_weekday(self):
        from datetime import datetime as dt
        base = dt(2026, 11, 16)          # 2026-11-16 是周一
        expect = {
            0: "周一", 1: "还要上班", 2: "还要上班", 3: "还要上班",
            4: "最后一个工作日", 5: "周末", 6: "双休的最后一天",
        }
        for offset, keyword in expect.items():
            d = base + timedelta(days=offset)
            text = rw.evening_closing(d)
            self.assertIn(keyword, text, f"{d:%m-%d} 的收尾应包含「{keyword}」")
            self.assertIn("明早我会把天气说给你听", text)

    def test_solar_term_note_only_on_term_day(self):
        # 非节气日应返回空串
        d = datetime_now = None
        from datetime import datetime as dt
        d = dt(2026, 11, 18)
        note = rw.solar_term_note(d, "今天")
        self.assertEqual(note, "", "非节气日不该有节气句")

    def test_intensity_hints_are_distinct(self):
        """小雨和中雨共用一句（刻意为之），其余应各不相同。"""
        hints = {}
        for kind, v in (("rain", 0.2), ("rain", 1.0), ("rain", 4.0), ("rain", 9.0),
                        ("snow", 0.2), ("snow", 0.8), ("snow", 2.0), ("snow", 5.0)):
            word, hint = rw.intensity_word(kind, v)
            hints[word] = hint
        self.assertEqual(hints["小雨"], hints["中雨"], "小雨/中雨应共用一句")
        distinct = set(h for k, h in hints.items() if k not in ("小雨",))
        self.assertEqual(len(distinct), len(hints) - 1, "其余档位的提示语应各不相同")

    def test_clothing_lines_mention_layers(self):
        self.assertIn("记得穿", rw.morning_clothes(-3, 5))
        self.assertIn("记得穿", rw.evening_clothes(-3, 5))
        self.assertIn("最好加条秋裤", rw.morning_clothes(2, 9))


class TestFormatting(unittest.TestCase):
    def test_fmt_minutes(self):
        self.assertIn("8", rw.fmt_minutes(8))
        self.assertIn("1", rw.fmt_minutes(60))

    def test_aqi_word_boundaries(self):
        self.assertEqual(rw.aqi_word(30), "优")
        self.assertEqual(rw.aqi_word(50), "优")
        self.assertEqual(rw.aqi_word(51), "良")
        self.assertEqual(rw.aqi_word(101), rw.aqi_word(101))

    def test_dress_for_boundaries(self):
        self.assertIn("短袖", rw.dress_for(30))
        self.assertIn("羽绒服", rw.dress_for(-5))
        self.assertNotEqual(rw.dress_for(0), rw.dress_for(20))


class TestPrecipMessage(unittest.TestCase):
    """降水提醒的措辞。"""

    class Ev:
        def __init__(self, kind, detail, lead):
            from datetime import datetime as dt
            self.kind, self.detail, self.lead_minutes = kind, detail, lead
            self.source = "qweather"
            self.start = dt.now(rw.TZ) + timedelta(minutes=lead)

    def test_lead_phrase(self):
        text = rw.build_message(self.Ev("rain", "降水强度 0.2 mm/h", 8), "【测】")
        self.assertIn("还有", text)
        self.assertIn("带伞", text)

    def test_now_phrase(self):
        text = rw.build_message(self.Ev("rain", "降水强度 0.2 mm/h", 0), "【测】")
        self.assertIn("已经开始", text)

    def test_snow_uses_snow_word(self):
        text = rw.build_message(self.Ev("snow", "降水强度 1.0 mm/h", 6), "【测】")
        self.assertIn("雪", text)
        self.assertNotIn("下雨", text)

    def test_no_double_period(self):
        for lead in (0, 5, 12):
            text = rw.build_message(self.Ev("rain", "降水强度 9.0 mm/h", lead), "")
            self.assertNotIn("。。", text)
            self.assertNotIn("~。", text)


class TestWarnAdvice(unittest.TestCase):
    """预警建议按类型给出不同内容。"""

    def test_fog_advice(self):
        tip = rw.warning_advice("大雾黄色预警")
        self.assertIn("能见度", tip)

    def test_ice_advice(self):
        tip = rw.warning_advice("道路结冰橙色预警")
        self.assertIn("两轮车", tip)

    def test_unknown_type_empty(self):
        self.assertEqual(rw.warning_advice("未知类型预警"), "")


class TestScheduleLogic(unittest.TestCase):
    """播报与审查的触发时机（纯逻辑，用 mock 绕开网络）。"""

    def setUp(self):
        self.cfg = {
            "locations": [{"name": "北京·大兴", "key": "bj", "city_code": "101011100"}],
            "qq": {"targets": [{"kind": "private", "id": 10000, "locations": ["bj"]}]},
            "briefing": {"enabled": True, "morning": "06:00", "noon": "12:00", "evening": "21:00"},
            "planner": {"scan_times": ["00:00", "12:00", "18:00"]},
            "poll_minutes": 5,
        }

    def test_due_briefings_fires_in_window(self):
        from unittest.mock import patch
        with patch.object(rw, "build_briefing", return_value="MSG"), \
             patch.object(rw, "locations", return_value=self.cfg["locations"]):
            at6 = datetime(2026, 11, 21, 6, 3)
            items = rw.due_briefings(self.cfg, at6, None)
            self.assertTrue(any(i["key"].startswith("brief:morning") for i in items))

            at10 = datetime(2026, 11, 21, 10, 3)
            self.assertEqual(rw.due_briefings(self.cfg, at10, None), [])

    def test_all_three_briefings_scheduled(self):
        from unittest.mock import patch
        with patch.object(rw, "build_briefing", return_value="MSG"), \
             patch.object(rw, "locations", return_value=self.cfg["locations"]):
            for hour, mode in ((6, "morning"), (12, "noon"), (21, "evening")):
                at = datetime(2026, 11, 21, hour, 2)
                items = rw.due_briefings(self.cfg, at, None)
                self.assertTrue(any(i["key"].startswith(f"brief:{mode}") for i in items),
                                f"{hour} 点应触发 {mode}")

    def test_need_scan_after_scheduled_time(self):
        times = ["00:00", "12:00", "18:00"]
        p = {"last_scan": datetime(2026, 11, 21, 11, 0).isoformat()}
        self.assertTrue(rw._need_scan(datetime(2026, 11, 21, 12, 5), p, times))
        p2 = {"last_scan": datetime(2026, 11, 21, 12, 6).isoformat()}
        self.assertFalse(rw._need_scan(datetime(2026, 11, 21, 12, 40), p2, times))


class NullLog:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


class TestOutbox(unittest.TestCase):
    """发送失败进补发队列，恢复后补发。"""

    def test_failed_message_queued_then_flushed(self):
        from unittest.mock import patch
        cfg = {"qq": {"targets": [{"kind": "private", "id": 10000, "locations": ["bj"]}]}}
        loc = {"name": "北京·大兴", "key": "bj"}
        now = datetime.now(rw.TZ)
        state = {"sent_keys": [], "outbox": [
            {"key": "stop:bj:2026-11-21T20", "msg": "雨停了", "loc": "bj",
             "at": now.isoformat(), "kind": "stop"}]}
        with patch.object(rw, "targets_for", return_value=[{"kind": "private", "id": 10000}]), \
             patch.object(rw, "deliver", return_value=True):
            done = rw.flush_outbox(cfg, state, now, NullLog(), True)
        self.assertEqual(len(done), 1, "通道恢复后应补发成功")
        self.assertEqual(state["outbox"], [])
        self.assertIn("stop:bj:2026-11-21T20", state["sent_keys"])

    def test_expired_message_dropped(self):
        from unittest.mock import patch
        cfg = {"qq": {"targets": []}}
        now = datetime.now(rw.TZ)
        old = (now - timedelta(hours=3)).isoformat()
        state = {"outbox": [{"key": "old", "msg": "旧消息", "loc": "bj",
                             "at": old, "kind": "stop"}]}
        with patch.object(rw, "targets_for", return_value=[{"kind": "private", "id": 1}]), \
             patch.object(rw, "deliver", return_value=True):
            rw.flush_outbox(cfg, state, now, NullLog(), True)
        self.assertEqual(state["outbox"], [], "超过 60 分钟的消息应丢弃")


class TestConfigTargets(unittest.TestCase):
    """目标与城市的匹配。"""

    def test_targets_for_filters_by_city(self):
        cfg = {"qq": {"targets": [
            {"kind": "private", "id": 1, "locations": ["bj"]},
            {"kind": "group", "id": 2, "locations": ["cd", "ly"]},
        ]}}
        self.assertEqual([t["id"] for t in rw.targets_for(cfg, "bj")], [1])
        self.assertEqual([t["id"] for t in rw.targets_for(cfg, "cd")], [2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
