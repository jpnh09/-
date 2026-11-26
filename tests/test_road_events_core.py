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


if __name__ == "__main__":
    unittest.main(verbosity=2)
