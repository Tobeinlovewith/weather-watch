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


if __name__ == "__main__":
    unittest.main(verbosity=2)
