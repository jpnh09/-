"""Offline tests for limits_core (provincial height / weight limit signs).
Run: python -I tests/test_limits_core.py   (from the repository root)
Fixtures are a few rows copied from the Highway Bureau open data (2026-11, Big5), plus synthetic edge cases."""
import codecs
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "apps" / "fleet_bridge" / "fleet_bridge"))
import limits_core as core  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HEADER = "公路編號,公路編碼,隸屬縣市,管養單位,管養工務段,調查日期,坐標-X-TWD97,坐標-Y-TWD97,坐標-E-WGS84,坐標-N-WGS84,起點樁號,隸屬鄉鎮,隸屬村里,坐標Z,設置位置,編號,種類,性質,牌面內容,現況,是否為公路局設置,備註,牌面方向"


def row(road="台1", km="10K+000", value="4.6", remark="", lon="120.5", lat="23.5"):
    return f"{road},P0010,某縣,某處,某段,2026/1/1,0,0,{lon},{lat},{km},某鄉,某村,0,右側,S0001,禁制,限3,{value},良好,是,{remark},順向"


class LimitValueTests(unittest.TestCase):
    def test_values(self):
        cases = {("4.1", "height"): 4.1, ("4.6M", "height"): 4.6, ("限高4.2公尺", "height"): 4.2, ("0", "height"): None,
                 ("", "height"): None, ("3.5/4.0", "height"): None, ("25", "height"): None,
                 ("42", "weight"): 42.0, ("20T", "weight"): 20.0, ("總重 43公噸", "weight"): 43.0, ("0", "weight"): None}
        for (text, kind), expected in cases.items():
            self.assertEqual(core.parse_limit_value(text, kind), expected, (text, kind))


class RealFileTests(unittest.TestCase):
    def test_big5_height_rows(self):
        records, invalid, columns = core.parse_limit_signs((FIXTURES / "thb_height_sample_big5.csv").read_bytes(), "height")
        self.assertEqual((len(records), invalid), (6, 0))
        self.assertTrue(set(core.REQUIRED_COLUMNS) <= set(columns))
        by_km = {(r["road_key"], r["km"]): r for r in records}
        self.assertTrue(by_km[("台1", 265.798)]["side_facility"])            # 機車道施工限高
        self.assertIsNone(by_km[("台1", 361.011)]["limit_value"])            # 牌面內容 0 → unknown
        self.assertEqual(by_km[("台1", 35.511)]["limit_value"], 4.1)
        self.assertTrue(by_km[("台15", 16.159)]["side_facility"])           # quoted remark with commas: 左側,涵洞限高,2.3
        self.assertEqual(by_km[("台15", 16.159)]["limit_value"], 2.3)
        self.assertTrue(all(r["lat"] and r["lon"] for r in records))

    def test_big5_weight_rows(self):
        records, invalid, _ = core.parse_limit_signs((FIXTURES / "thb_weight_sample_big5.csv").read_bytes(), "weight")
        self.assertEqual(invalid, 0)
        self.assertTrue(records and all(r["kind"] == "weight" and r["limit_value"] for r in records))


class ParseAndValidateTests(unittest.TestCase):
    def test_bom_bad_rows_and_coordinates(self):
        text = "\n".join([HEADER, row(), row(road="", km="1K"), row(lon="200"), row(value="0"), row() + ",extra,cells"])
        records, invalid, columns = core.parse_limit_signs(codecs.BOM_UTF8 + text.encode("utf-8"), "height")
        self.assertEqual((len(records), invalid), (3, 2))
        self.assertIsNone(records[1]["lat"])
        self.assertIsNone(records[2]["limit_value"])
        self.assertEqual(core.parse_limit_signs(b"", "height"), ([], 0, []))
        with self.assertRaises(ValueError):
            core.parse_limit_signs(b"x", "length")

    def test_validate_import(self):
        good = "\n".join([HEADER] + [row(km=f"{i}K") for i in range(250)]).encode("utf-8")
        records, invalid, columns = core.parse_limit_signs(good, "height")
        self.assertIsNone(core.validate_import(records, invalid, columns, "height"))
        self.assertIn("缺少欄位", core.validate_import(records, invalid, ["公路編號"], "height"))
        self.assertIn("少於", core.validate_import(records[:10], 0, columns, "height"))
        self.assertIn("無法讀取", core.validate_import(records, 50, columns, "height"))
        no_coords = [dict(r, lat=None) for r in records]
        self.assertIn("座標", core.validate_import(no_coords, 0, columns, "height"))

    def test_parse_routes(self):
        self.assertEqual(core.parse_routes("台1, 臺17線、台19｜溪湖－竹塘\n台26"), {"台1", "台17", "台19", "台26"})
        self.assertEqual(core.parse_routes(""), set())
        b1 = "台2｜關渡－萬里、基隆新北市界－瑞濱－蘇澳\n台3｜土城（土城交流道）－大溪、東勢－台中"
        self.assertEqual(core.parse_routes(b1), {"台2", "台3"})            # 、 inside a segment is not a route

    def test_default_b1_routes_give_29_keys(self):
        import road_events_core
        self.assertEqual(len(core.parse_routes(road_events_core.DEFAULT_B1_ROUTES)), 29)


class RouteConflictTests(unittest.TestCase):
    signs = [
        {"kind": "height", "road_key": "台1", "km": 35.5, "limit_value": 4.1, "side_facility": False},
        {"kind": "height", "road_key": "台1", "km": 70.0, "limit_value": 4.3, "side_facility": False},
        {"kind": "height", "road_key": "台1", "km": 71.0, "limit_value": 2.3, "side_facility": True},
        {"kind": "height", "road_key": "台1", "km": 361.0, "limit_value": None, "side_facility": False},
        {"kind": "height", "road_key": "台61", "km": 3.0, "limit_value": 3.0, "side_facility": False},
        {"kind": "weight", "road_key": "台1", "km": 297.7, "limit_value": 15.0, "side_facility": False},
        {"kind": "weight", "road_key": "台1", "km": 323.0, "limit_value": 42.0, "side_facility": False},
    ]

    def test_height_with_margin(self):
        found = core.route_conflicts(self.signs, {"台1"}, height_m=4.2, margin_m=0.1)
        self.assertEqual([(s["km"], s["level"]) for s in found], [(35.5, "red"), (71.0, "yellow"), (361.0, "yellow")])
        self.assertIn("含裕度 0.1 m", found[0]["reason"])
        self.assertIn("側向設施", found[1]["reason"])
        self.assertEqual(found[2]["reason"], "限高值不明")                     # unknown is never passable
        self.assertNotIn(70.0, [s["km"] for s in found])                     # 4.3 clears 4.2 + 0.1 exactly
        self.assertEqual(len(core.route_conflicts(self.signs, {"台1"}, height_m=4.2, margin_m=0.2)), 4)

    def test_weight_and_other_routes(self):
        found = core.route_conflicts(self.signs, {"台1"}, weight_t=40)
        self.assertEqual([(s["km"], s["reason"]) for s in found], [(297.7, "限重 15 t，車重 40 t")])
        self.assertEqual(core.route_conflicts(self.signs, {"台17"}, height_m=4.5), [])
        self.assertEqual(core.route_conflicts(self.signs, {"台1"}), [])


if __name__ == "__main__":
    unittest.main()
