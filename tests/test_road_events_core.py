"""Offline checks for TDX road-event rules (normalising, identity, state, schedule, budget).

Run: python -I tests/test_road_events_core.py   (from the repository root)
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "apps" / "fleet_bridge" / "fleet_bridge"))
import road_events_core as core  # noqa: E402

ITEM = {
    "EventID": "NFB-123", "EventTitle": "國道1號北向12K施工", "Description": "內側車道封閉",
    "EventType": 2, "EventSubType": 207, "EventStep": 1,
    "EffectiveTime": "2026-11-26T08:00:00+08:00", "ExpireTime": "2026-11-26T17:00:00+08:00",
    "LastUpdateTime": "2026-11-26T07:55:00+08:00", "LocationType": 0,
    "Location": {"FreeExpressHighway": [{"Road": "國道1號", "Direction": "北向", "StartKM": "12.0", "EndKM": "13.5",
                                          "SectionStart": "汐止", "SectionEnd": "五股"}]},
    "Impact": {"Description": "內側車道封閉", "Severity": 1, "Regulations": [2], "BlockWay": 0, "BlockedLanes": "內側"},
    "Source": "高公局",
}


def item(**changes):
    data = {**ITEM, **changes}
    if isinstance(changes.get("Impact"), dict):
        data["Impact"] = {**ITEM["Impact"], **changes["Impact"]}
    return data


class NormalizeTests(unittest.TestCase):
    def test_fields(self):
        r = core.normalize("freeway_live", ITEM)
        self.assertEqual(r["event_key"], "freeway_live:NFB-123")
        self.assertEqual(r["road"], "國道1號")
        self.assertIn("12.0K–13.5K", r["location_text"])
        self.assertEqual(r["effective_time"], "2026-11-26 08:00:00")
        self.assertEqual(r["regulation_labels"], "部分封閉")
        self.assertEqual((r["is_major"], r["special_vehicle_control"]), (0, 0))

    def test_utc_converted_to_taipei(self):
        self.assertEqual(core.to_taipei("2026-11-26T00:00:00Z"), "2026-11-26 08:00:00")
        self.assertIsNone(core.to_taipei("not a date"))
        self.assertIsNone(core.to_taipei(None))

    def test_major_rules(self):
        self.assertEqual(core.normalize("freeway_live", item(Impact={"Regulations": [1]}))["is_major"], 1)
        special = core.normalize("freeway_live", item(Impact={"Regulations": [9]}))
        self.assertEqual((special["is_major"], special["special_vehicle_control"]), (1, 1))
        self.assertEqual(core.normalize("freeway_live", item(Impact={"Severity": 2}))["is_major"], 1)
        self.assertEqual(core.normalize("freeway_live", item(EventType=6))["is_major"], 1)  # 災害
        # An ordinary lane closure (as the live freeway feed reports it) is not major.
        lane = core.normalize("freeway_live", item(Impact={"Severity": 1, "Regulations": [2], "BlockWay": 1}))
        self.assertEqual(lane["is_major"], 0)

    def test_fallback_identity_ignores_time_format(self):
        a = core.normalize("highway_live", item(EventID=None, EffectiveTime="2026-11-26T00:00:00Z"))
        b = core.normalize("highway_live", item(EventID=None, EffectiveTime="2026-11-26T08:00:00+08:00"))
        self.assertEqual(a["event_key"], b["event_key"])

    def test_missing_event_id_uses_stable_hash(self):
        a = core.normalize("highway_live", item(EventID=None))
        b = core.normalize("highway_live", item(EventID="", Description="更新後描述", LastUpdateTime="2026-11-26T09:00:00+08:00"))
        self.assertTrue(a["event_key"].startswith("highway_live:h:"))
        self.assertEqual(a["event_key"], b["event_key"])

    def test_city_and_odd_shapes(self):
        r = core.normalize("city_live:Taoyuan", item(Location={"CityRoad": {"Roadways": [{"City": "桃園市", "Town": "蘆竹區",
            "Road": "南崁路", "Direction": "雙向"}]}}, Impact=None))
        self.assertEqual(r["road"], "南崁路")
        self.assertEqual(r["feed_label"], "桃園市即時事件")
        with self.assertRaises(ValueError):
            core.normalize("freeway_live", ["not", "a", "dict"])
        with self.assertRaises(ValueError):
            core.feed_spec("city_live:Atlantis")

    def test_no_invented_limits(self):
        r = core.normalize("freeway_live", ITEM)
        self.assertNotIn("限高", r["regulation_labels"] + r["impact_description"])


class StateTests(unittest.TestCase):
    NOW = "2026-11-26 12:00:00"

    def prev(self, record, status):
        return {"status": status, "fingerprint": record["fingerprint"]}

    def test_new_unchanged_changed(self):
        r = core.normalize("freeway_live", ITEM)
        self.assertEqual(core.decide(None, r, self.NOW), ("新增", True))
        self.assertEqual(core.decide(self.prev(r, "新增"), r, self.NOW), ("持續", False))
        changed = core.normalize("freeway_live", item(Description="改為外側車道"))
        self.assertEqual(core.decide(self.prev(r, "持續"), changed, self.NOW), ("變更", True))

    def test_last_update_time_alone_is_not_a_change(self):
        r = core.normalize("freeway_live", ITEM)
        touched = core.normalize("freeway_live", item(LastUpdateTime="2026-11-26T11:59:00+08:00"))
        self.assertEqual(core.decide(self.prev(r, "持續"), touched, self.NOW)[0], "持續")

    def test_major_notice_follows_notified_state(self):
        closed = core.normalize("freeway_live", item(Impact={"Regulations": [1], "Severity": 2}))
        fp = closed["major_fingerprint"]
        self.assertTrue(core.needs_major_notice("新增", 1, fp, None))
        self.assertFalse(core.needs_major_notice("持續", 1, fp, fp))           # already notified
        self.assertTrue(core.needs_major_notice("持續", 1, fp, "older-state"))  # crash before marking → retried
        self.assertFalse(core.needs_major_notice("狀態不明", 1, fp, None))
        self.assertFalse(core.needs_major_notice("持續", 0, fp, None))
        lifted = core.normalize("freeway_live", item(Impact={"Regulations": [2]}))
        self.assertNotEqual(lifted["major_fingerprint"], fp)

    def test_official_expiry(self):
        ended = core.normalize("freeway_live", item(ExpireTime="2026-11-26T10:00:00+08:00", Impact={"Regulations": [1]}))
        self.assertEqual(core.decide(None, ended, self.NOW), ("已結束", True))
        self.assertEqual(core.decide(self.prev(ended, "已結束"), ended, self.NOW), ("已結束", False))

    def test_reappearance(self):
        r = core.normalize("freeway_live", ITEM)
        self.assertEqual(core.decide(self.prev(r, "狀態不明"), r, self.NOW)[0], "變更")

    def test_absence_never_means_cleared(self):
        self.assertEqual(core.absent_status("持續", None, self.NOW, True), "狀態不明")
        self.assertEqual(core.absent_status("持續", "2026-11-26 10:00:00", self.NOW, True), "已結束")
        self.assertIsNone(core.absent_status("持續", None, self.NOW, False))  # incomplete response: keep
        self.assertIsNone(core.absent_status("狀態不明", None, self.NOW, True))

    def test_completeness(self):
        # (count, fetched, unique, invalid, pages_exhausted, pages_consistent)
        self.assertTrue(core.response_complete(30, 30, 30, 0, False, True))
        self.assertFalse(core.response_complete(800, 500, 500, 0, False, True))   # short
        self.assertFalse(core.response_complete(3000, 3000, 3000, 0, True, True))  # page cap hit
        self.assertFalse(core.response_complete(None, 12, 12, 0, False, True))    # no official Count
        self.assertFalse(core.response_complete(4, 4, 3, 0, False, True))          # duplicate across pages
        self.assertFalse(core.response_complete(4, 4, 3, 1, False, True))          # unreadable item
        self.assertFalse(core.response_complete(4, 4, 4, 0, False, False))         # list moved between pages


class ScheduleTests(unittest.TestCase):
    TIMES = core.parse_schedule(core.DEFAULT_SCHEDULE)

    def test_parse(self):
        self.assertEqual(self.TIMES, [(5, 30), (11, 30), (17, 30), (22, 30)])
        self.assertEqual(core.parse_schedule("6:00， 18:00、6:00"), [(6, 0), (18, 0)])
        for bad in ("", "25:00", "abc", "12:60"):
            with self.assertRaises(ValueError):
                core.parse_schedule(bad)

    def test_due_and_catch_up(self):
        self.assertEqual(core.due_slot(datetime(2026, 11, 26, 11, 30), self.TIMES), datetime(2026, 11, 26, 11, 30))
        self.assertEqual(core.due_slot(datetime(2026, 11, 26, 13, 0), self.TIMES), datetime(2026, 11, 26, 11, 30))
        self.assertIsNone(core.due_slot(datetime(2026, 11, 26, 15, 0), self.TIMES))   # missed beyond 3 h: wait
        self.assertEqual(core.due_slot(datetime(2026, 11, 27, 0, 30), self.TIMES), datetime(2026, 11, 26, 22, 30))
        self.assertIsNone(core.due_slot(datetime(2026, 11, 26, 5, 29), self.TIMES))

    def test_next_and_previous(self):
        self.assertEqual(core.next_slot(datetime(2026, 11, 26, 23, 0), self.TIMES), datetime(2026, 11, 27, 5, 30))
        self.assertEqual(core.previous_slot(datetime(2026, 11, 26, 3, 0), self.TIMES), datetime(2026, 11, 25, 22, 30))

    def test_points(self):
        self.assertAlmostEqual(core.estimated_points(1500, 0), 1.0)
        self.assertAlmostEqual(core.estimated_points(0, 150_000_000), 1.0)
        self.assertAlmostEqual(core.estimated_points(750, 75_000_000), 1.0)


VDS = [  # main-line detectors (M/N) and ramp detectors (I/O) as in the live freeway VD list
    {"VDID": "VD-N3-S-368.900-M-RS", "RoadName": "國道3號", "LocationMile": "368K+900",
     "DetectionLinks": [{"ActualLaneNum": 3, "RoadDirection": "S"}]},
    {"VDID": "VD-N3-S-370.000-N-RS", "RoadName": "國道3號", "LocationMile": "370K+000",
     "DetectionLinks": [{"ActualLaneNum": 4, "RoadDirection": "S"}]},
    {"VDID": "VD-N3-S-369.300-I-ES-1", "RoadName": "國道3號", "LocationMile": "369K+300",
     "DetectionLinks": [{"ActualLaneNum": 1, "RoadDirection": "S"}]},
    {"VDID": "VD-N6-W-21.000-M-RS", "RoadName": "國道6號", "LocationMile": "21K+000",
     "DetectionLinks": [{"ActualLaneNum": 2, "RoadDirection": "W"}]},
]


def lane_event(blocked, road="國道三號", direction="南向", km="369K+300", location_type=0, **impact):
    return item(Location={"FreeExpressHighway": [{"Road": road, "Direction": direction, "StartKM": km, "EndKM": km}]},
                LocationType=location_type, Impact={"Severity": 1, "Regulations": [2], "BlockWay": 1,
                                                    "BlockedLanes": blocked, **impact})


class TruckLaneTests(unittest.TestCase):
    INDEX = core.lane_index_from_vds(VDS)

    def judge(self, blocked, **kw):
        r = core.apply_truck_impact(core.normalize("freeway_live", lane_event(blocked, **kw)), self.INDEX)
        return r["truck_impact"], r["lane_count"], r

    def test_names_and_mileage(self):
        self.assertEqual(core.road_key("國道三號"), "國道3號")
        self.assertEqual(core.road_key("國道十號"), "國道10號")
        self.assertEqual(core.road_key("國道三甲"), "國道3甲")
        self.assertEqual(core.road_key("臺十五"), "台15")
        self.assertEqual(core.parse_km("243K+800"), 243.8)
        self.assertEqual(core.parse_km("12.0K"), 12.0)
        self.assertIsNone(core.parse_km("abc"))

    def test_index_uses_main_line_only_and_minimum(self):
        self.assertEqual(sorted(self.INDEX), ["國道3號|S", "國道6號|W"])
        self.assertEqual(core.lanes_near(self.INDEX, "國道3號", "S", 369.3, 369.3), 3)  # 3 and 4 nearby -> 3
        self.assertIsNone(core.lanes_near(self.INDEX, "國道3號", "S", 100.0, 100.0))
        self.assertIsNone(core.lanes_near({}, "國道3號", "S", 369.3, 369.3))

    def test_owner_rule(self):
        self.assertEqual(self.judge("2")[0], "等同封閉")          # outer-most lane of 3
        self.assertEqual(self.judge("1,RS", road="國道六號", direction="西向", km="21K+000")[0], "等同封閉")
        self.assertEqual(self.judge("999")[0], "等同封閉")
        self.assertEqual(self.judge("RS")[0], "通過時注意")       # outer shoulder only
        self.assertEqual(self.judge("0,RS")[0], "通過時注意")     # inner lane + outer shoulder, outer lane open
        self.assertEqual(self.judge("0,LS")[0], "無影響")
        self.assertEqual(self.judge("LS")[0], "無影響")
        self.assertEqual(self.judge("1")[0], "無影響")            # middle lane of 3
        self.assertEqual(self.judge("-1")[0], "無阻斷")
        self.assertEqual(self.judge("255")[0], "狀態不明")
        self.assertEqual(self.judge("2,3")[0], "等同封閉")        # lane number beyond the detector count

    def test_never_guess(self):
        self.assertEqual(self.judge("1", km="100K+000")[0], "狀態不明")        # no detector nearby
        self.assertEqual(self.judge("0", location_type=2)[0], "狀態不明")      # ramp junction lane numbering
        self.assertEqual(self.judge("x,y")[0], "狀態不明")
        empty = core.apply_truck_impact(core.normalize("highway_live", item(Impact=None)), self.INDEX)
        self.assertEqual(empty["truck_impact"], "未提供車道資訊")
        self.assertEqual((empty["is_major"], empty["is_caution"]), (0, 0))

    def test_alert_flags_and_fingerprints(self):
        closed, _, r = self.judge("2")
        self.assertEqual((r["is_major"], r["is_caution"]), (1, 0))
        _, _, caution = self.judge("RS")
        self.assertEqual((caution["is_major"], caution["is_caution"]), (0, 1))
        self.assertTrue(caution["caution_fingerprint"])
        self.assertTrue(core.needs_caution_notice("新增", 1, caution["caution_fingerprint"], None))
        self.assertFalse(core.needs_caution_notice("持續", 1, caution["caution_fingerprint"], caution["caution_fingerprint"]))
        structural = core.apply_truck_impact(core.normalize("freeway_live", lane_event("0", Regulations=[1])), self.INDEX)
        self.assertEqual((structural["truck_impact"], structural["is_major"]), ("等同封閉", 1))

    def test_labels(self):
        self.assertEqual(core.blocked_lanes_label("2", 3), "第3車道（最外側）｜本路段 3 車道")
        self.assertEqual(core.blocked_lanes_label("0,LS", 3), "內側路肩、第1車道（最內側）｜本路段 3 車道")
        self.assertEqual(core.blocked_lanes_label("RS"), "外側路肩")
        self.assertEqual(core.blocked_lanes_label("-1"), "無阻斷車道")
        self.assertIn("不一致", core.blocked_lanes_label("2,3", 3))


class LaneFixAndPermitTests(unittest.TestCase):
    def test_single_lane_main_line_detectors_ignored_and_nearest_used(self):
        vds = [{"VDID": "VD-N1-S-7.500-M-RS", "RoadName": "國道1號", "LocationMile": "7K+500",
                "DetectionLinks": [{"ActualLaneNum": 1, "RoadDirection": "S"}]},
               {"VDID": "VD-N1-S-8.200-M-RS", "RoadName": "國道1號", "LocationMile": "8K+200",
                "DetectionLinks": [{"ActualLaneNum": 3, "RoadDirection": "S"}]},
               {"VDID": "VD-N1-S-9.400-M-RS", "RoadName": "國道1號", "LocationMile": "9K+400",
                "DetectionLinks": [{"ActualLaneNum": 2, "RoadDirection": "S"}]}]
        index = core.lane_index_from_vds(vds)
        self.assertEqual(index["國道1號|S"], [[8.2, 3], [9.4, 2]])          # the 1-lane detector is gone
        self.assertEqual(core.lanes_near(index, "國道1號", "S", 7.6, 7.6), 3)  # nearest, not the window minimum
        self.assertEqual(core.lanes_near(index, "國道1號", "S", 8.0, 9.5), 2)  # inside the range: the fewest

    def test_lane_table_stored_before_the_fix_skips_single_lane_points(self):
        # tables built by the old code still hold 1-lane points and stay in use for LANE_REFRESH_DAYS
        old_table = {"國道1號|S": [[7.5, 1], [8.2, 3], [9.4, 2]]}
        self.assertEqual(core.lanes_near(old_table, "國道1號", "S", 7.6, 7.6), 3)
        self.assertEqual(core.lanes_near(old_table, "國道1號", "S", 7.0, 8.5), 3)   # the 1-lane point inside the range is ignored
        self.assertIsNone(core.lanes_near({"國道1號|S": [[7.5, 1]]}, "國道1號", "S", 7.6, 7.6))

    def test_permit_routes(self):
        routes = core.parse_permit_routes(core.DEFAULT_B1_ROUTES)
        self.assertEqual(len(routes), 29)
        self.assertIn("台74甲", routes["台1"])
        appendix = "\n".join(["七、台3線：土城－大溪－【台3】－竹東", "三、台1線叉路口－【台1丙】－台19線叉路口",
                              "四、台1丁線：莿桐－斗南"])
        self.assertEqual(sorted(core.parse_permit_routes(appendix)), ["台1丁", "台1丙", "台3"])
        self.assertEqual(core.permit_match("台3", "highway_live", routes)[0], "台3")
        self.assertEqual(core.permit_match("臺1甲線", "highway_live", routes)[0], "台1甲")
        self.assertEqual(core.permit_match("台61", "highway_live", routes), ("", ""))
        self.assertEqual(core.permit_match("國道一號", "freeway_live", routes), ("", ""))
        self.assertEqual(core.permit_match("國道一號", "freeway_live", routes, True)[0], "國道")
        self.assertEqual((core.road_class("freeway_live"), core.road_class("highway_preview"), core.road_class("city_live:Taipei")),
                         ("國道", "省道", "縣市道路"))


class CityScheduleTests(unittest.TestCase):
    def test_parse_weekdays(self):
        self.assertEqual(core.parse_weekdays(core.DEFAULT_CITY_DAYS), [0, 2, 4])
        self.assertEqual(core.parse_weekdays("一三五"), [0, 2, 4])
        self.assertEqual(core.parse_weekdays("週一、週三，星期五"), [0, 2, 4])
        self.assertEqual(core.parse_weekdays("1,3,5"), [0, 2, 4])
        self.assertEqual(core.parse_weekdays("六,日"), [5, 6])
        self.assertEqual(core.weekdays_text([0, 2, 4]), "一,三,五")
        for bad in ("", "  ", "Mon", "八", "0"):
            with self.assertRaises(ValueError):
                core.parse_weekdays(bad)

    def test_city_feeds_only_mon_wed_fri_morning(self):
        days = core.parse_weekdays(core.DEFAULT_CITY_DAYS)
        times = core.parse_schedule(core.DEFAULT_CITY_TIMES)
        fri, thu, sat = datetime(2026, 11, 27, 5, 30), datetime(2026, 11, 26, 5, 30), datetime(2026, 11, 28, 5, 30)
        self.assertTrue(core.city_feeds_due(fri, days, times, scheduled=True))
        self.assertFalse(core.city_feeds_due(fri.replace(hour=11), days, times, scheduled=True))   # other slots: no
        self.assertFalse(core.city_feeds_due(thu, days, times, scheduled=True))
        self.assertFalse(core.city_feeds_due(sat, days, times, scheduled=True))                     # weekend: never
        self.assertTrue(core.city_feeds_due(datetime(2026, 11, 27, 15, 2), days, times, scheduled=False))  # manual, Friday
        self.assertFalse(core.city_feeds_due(datetime(2026, 11, 29, 15, 2), days, times, scheduled=False))  # manual, Sunday


class MapPointTests(unittest.TestCase):
    def test_parse_point(self):
        self.assertEqual(core.parse_point("POINT(120.457355 23.669432)"), (23.669432, 120.457355))
        self.assertEqual(core.parse_point("POINT (121.5 25.05)"), (25.05, 121.5))
        self.assertEqual(core.parse_point("LINESTRING(121.0 24.0, 121.1 24.1)"), (24.0, 121.0))
        self.assertEqual(core.parse_point("POINT(118.32 24.43)"), (24.43, 118.32))   # Kinmen
        self.assertIsNone(core.parse_point("POINT(0 0)"))
        self.assertIsNone(core.parse_point(""))
        self.assertIsNone(core.parse_point(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
