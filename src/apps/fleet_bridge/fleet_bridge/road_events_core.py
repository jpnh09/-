"""TDX road-event rules without Frappe: normalising, identity, change detection, schedule, budget.

Official semantics (TDX 道路事件 v1 OAS) that these rules rely on:
* LiveEvent/Event responses wrap ``Items`` with ``Count``; ``$top`` defaults to 30, so callers page.
* There is no "cleared" status field. ``ExpireTime`` (nullable for live events) is the only official end.
* There are no structured height/width/weight limits; ``Impact.Regulations`` code 9 is
  「特定車種管制需求」 and any actual limit only appears in free text.
An event missing from a complete response is therefore 「狀態不明」, never 「已解除」.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))
API = "/api/basic/v1/Traffic/RoadEvent"

FEEDS = {
    "freeway_live": (API + "/LiveEvent/Freeway", "國道即時事件", "live"),
    "highway_live": (API + "/LiveEvent/Highway", "省道即時事件", "live"),
    "highway_preview": (API + "/Event/Highway", "省道預告事件", "preview"),
}
CITY_LIVE = ("Taipei", "NewTaipei", "Taoyuan", "Taichung", "Tainan", "Kaohsiung", "Keelung", "MiaoliCounty",
             "ChiayiCounty", "Chiayi", "PingtungCounty", "YilanCounty", "KinmenCounty")
CITY_PREVIEW = tuple(c for c in CITY_LIVE if c != "Kaohsiung")
CITY_NAMES = {"Taipei": "臺北市", "NewTaipei": "新北市", "Taoyuan": "桃園市", "Taichung": "臺中市", "Tainan": "臺南市",
              "Kaohsiung": "高雄市", "Keelung": "基隆市", "MiaoliCounty": "苗栗縣", "ChiayiCounty": "嘉義縣",
              "Chiayi": "嘉義市", "PingtungCounty": "屏東縣", "YilanCounty": "宜蘭縣", "KinmenCounty": "金門縣"}

EVENT_TYPES = {1: "交通事故", 2: "施工", 3: "壅塞", 4: "特殊管制", 5: "天氣", 6: "災害", 7: "活動", 8: "其它異常告警"}
SEVERITY = {-99: "來源未提供", 0: "無影響", 1: "部分阻斷", 2: "完全阻斷", 254: "其他", 255: "未知"}
REGULATIONS = {-99: "來源未提供", 0: "無管制", 1: "全封閉", 2: "部分封閉", 3: "高乘載管制", 4: "調撥車道",
               5: "開放路肩", 6: "匝道儀控", 7: "號誌管控", 8: "交管人員現場指揮", 9: "特定車種管制需求",
               254: "其他", 255: "未知"}
BLOCK_WAY = {-99: "來源未提供", 0: "無阻斷", 1: "單向阻斷", 2: "雙向阻斷", 254: "其他", 255: "未知"}
LOCATION_TYPES = {0: "道路主線", 1: "交流道或匝道", 2: "匝道出入口", 3: "交叉路口", 254: "其他", 255: "未知"}

ACTIVE = ("新增", "持續", "變更")
STATUSES = ACTIVE + ("已結束", "狀態不明")
# Measured on the live API (2026-11-26): every ordinary freeway lane closure carries 部分阻斷 + 部分封閉 + 單向阻斷,
# and provincial-highway events carry no Impact codes at all, so BlockWay cannot mark a major event.
MAJOR_RULE = "全封閉、特定車種管制需求、完全阻斷，或事件類型為災害"
DEFAULT_SCHEDULE = "05:30,11:30,17:30,22:30"
CALL_POINTS, MB_POINTS = 1500, 150  # 基礎服務：1,500 次 = 1 點，150 MB = 1 點，合併計算


def feed_spec(feed):
    """(path, label, kind) for a feed key, including per-city feeds."""
    if feed in FEEDS:
        return FEEDS[feed]
    kind, _, city = feed.partition(":")
    if kind == "city_live" and city in CITY_LIVE:
        return f"{API}/LiveEvent/City/{city}", f"{CITY_NAMES[city]}即時事件", "live"
    if kind == "city_preview" and city in CITY_PREVIEW:
        return f"{API}/Event/City/{city}", f"{CITY_NAMES[city]}預告事件", "preview"
    raise ValueError(f"unknown feed {feed}")


def to_taipei(value):
    """ISO-8601 from TDX → naive Asia/Taipei 'YYYY-MM-DD HH:MM:SS' (Frappe's system time zone)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(TAIPEI).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value, limit=None):
    text = "" if value is None else str(value).strip()
    return text[:limit] if limit else text


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _km(value):
    text = _str(value)
    if not text:
        return ""
    return text if re.search(r"[Kk]", text) else text + "K"


def describe_location(location):
    """Readable location plus the main road/direction, from whatever Location parts the source filled in."""
    location = location if isinstance(location, dict) else {}
    parts, road, direction = [], "", ""
    for seg in _as_list(location.get("FreeExpressHighway")):
        if not isinstance(seg, dict):
            continue
        road = road or _str(seg.get("Road"))
        direction = direction or _str(seg.get("Direction"))
        bits = [_str(seg.get("Road")), _str(seg.get("Direction"))]
        start, end = _km(seg.get("StartKM")), _km(seg.get("EndKM"))
        if start or end:
            bits.append(start if start == end or not end else f"{start}–{end}")
        section = "–".join(x for x in (_str(seg.get("SectionStart")), _str(seg.get("SectionEnd"))) if x)
        if section:
            bits.append(f"（{section}）")
        if _str(seg.get("Interchange")):
            bits.append(_str(seg.get("Interchange")))
        ramp = seg.get("Ramp") if isinstance(seg.get("Ramp"), dict) else {}
        if ramp:
            bits.append(" ".join(x for x in ("匝道", _str(ramp.get("Direction")), _str(ramp.get("EntryExit"))) if x))
        parts.append(" ".join(b for b in bits if b))
    city_road = location.get("CityRoad") if isinstance(location.get("CityRoad"), dict) else {}
    for seg in _as_list(city_road.get("Roadways")):
        if not isinstance(seg, dict):
            continue
        road = road or _str(seg.get("Road"))
        direction = direction or _str(seg.get("Direction"))
        section = "–".join(x for x in (_str(seg.get("SectionStart")), _str(seg.get("SectionEnd"))) if x)
        parts.append(" ".join(x for x in (_str(seg.get("City")), _str(seg.get("Town")), _str(seg.get("Road")),
                                          _str(seg.get("Direction")), f"（{section}）" if section else "") if x))
    for seg in _as_list(location.get("Intersection")):
        if isinstance(seg, dict):
            roads = [_str(r) for r in _as_list(seg.get("Roads")) if _str(r)]
            road = road or (roads[0] if roads else "")
            parts.append(f"{_str(seg.get('City'))} {'與'.join(roads)}路口".strip())
    for seg in _as_list(location.get("Address")):
        if isinstance(seg, dict):
            road = road or _str(seg.get("Road"))
            numbers = "–".join(x for x in (_str(seg.get("StartNo")), _str(seg.get("EndNo"))) if x)
            parts.append("".join(x for x in (_str(seg.get("City")), _str(seg.get("Town")), _str(seg.get("Road")),
                                             _str(seg.get("Lane")) and _str(seg.get("Lane")) + "巷",
                                             _str(seg.get("Alley")) and _str(seg.get("Alley")) + "弄",
                                             numbers and numbers + "號") if x))
    for seg in _as_list(location.get("Place")):
        if isinstance(seg, dict) and _str(seg.get("POI")):
            parts.append(_str(seg.get("POI")))
    if _str(location.get("Other")):
        parts.append(_str(location.get("Other")))
    return "；".join(p for p in parts if p), road, direction


def _digest(payload, size=32):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:size]


def event_key(feed, item, location_text):
    """Stable identity: official EventID when present; otherwise a hash of fields that do not change in place."""
    event_id = _str(item.get("EventID"))
    if event_id:
        return f"{feed}:{event_id}"
    # Normalised time, so the same instant written with another offset keeps the same identity.
    return f"{feed}:h:" + _digest([_str(item.get("EventTitle")), _int(item.get("EventType")),
                                   location_text, to_taipei(item.get("EffectiveTime"))], 24)


def is_major(severity, regulations, event_type):
    """Structured official codes only: 全封閉 (Regulations 1), 特定車種管制需求 (9), 完全阻斷 (Severity 2), 災害 (EventType 6).
    Free text is never parsed for closures (「內側車道封閉」 is not a road closure)."""
    return bool({1, 9} & set(regulations)) or severity == 2 or event_type == 6


def normalize(feed, item):
    """One TDX item → flat record for the database. Raises ValueError for a non-object item."""
    if not isinstance(item, dict):
        raise ValueError("item is not an object")
    _path, feed_label, kind = feed_spec(feed)
    location_text, road, direction = describe_location(item.get("Location"))
    impact = item.get("Impact") if isinstance(item.get("Impact"), dict) else {}
    regulations = sorted({r for r in (_int(x) for x in _as_list(impact.get("Regulations"))) if r is not None})
    severity, block_way = _int(impact.get("Severity")), _int(impact.get("BlockWay"))
    event_type = _int(item.get("EventType"))
    detour = impact.get("Detour") if isinstance(impact.get("Detour"), dict) else {}
    duration = impact.get("Duration") if isinstance(impact.get("Duration"), dict) else {}
    record = {
        "event_key": event_key(feed, item, location_text),
        "feed": feed, "feed_label": feed_label, "kind": kind,
        "event_id": _str(item.get("EventID"), 140),
        "title": _str(item.get("EventTitle"), 500),
        "description": _str(item.get("Description")),
        "event_type": event_type, "event_type_label": EVENT_TYPES.get(event_type, "未分類"),
        "event_sub_type": _int(item.get("EventSubType")),
        "event_step": _int(item.get("EventStep")),
        "effective_time": to_taipei(item.get("EffectiveTime")),
        "expire_time": to_taipei(item.get("ExpireTime")),
        "publish_time": to_taipei(item.get("PublishTime")),
        "source_update_time": to_taipei(item.get("LastUpdateTime")),
        "location_type": LOCATION_TYPES.get(_int(item.get("LocationType")), ""),
        "location_text": location_text, "road": road[:140], "direction": direction[:40],
        "severity": severity, "severity_label": SEVERITY.get(severity, "未提供"),
        "regulations": ",".join(str(r) for r in regulations),
        "regulation_labels": "、".join(REGULATIONS.get(r, str(r)) for r in regulations),
        "block_way": block_way, "block_way_label": BLOCK_WAY.get(block_way, "未提供"),
        "blocked_lanes": _str(impact.get("BlockedLanes"), 140),
        "impact_description": _str(impact.get("Description")),
        "detour_description": _str(detour.get("Description")),
        "special_vehicle_control": 1 if 9 in regulations else 0,
        "is_major": 1 if is_major(severity, regulations, event_type) else 0,
        "reporting_source": _str(item.get("Source"), 140),
        "positions": _str(item.get("Positions"), 500),
        "web_url": _str(item.get("WebURL"), 500),
        "duration_text": json.dumps(duration, ensure_ascii=False, sort_keys=True) if duration else "",
        "raw_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
    }
    record["fingerprint"] = _digest([record[k] for k in (
        "title", "description", "event_type", "event_sub_type", "event_step", "effective_time", "expire_time",
        "location_text", "severity", "regulations", "block_way", "blocked_lanes", "impact_description",
        "detour_description", "duration_text")])
    record["major_fingerprint"] = _digest([record["is_major"], severity, record["regulations"], block_way], 16)
    return record


def decide(previous, current, now):
    """State for an event seen in this response.

    previous: None or {"status", "fingerprint"}; now: 'YYYY-MM-DD HH:MM:SS' (Taipei).
    Returns (status, changed). Notification is decided separately by needs_major_notice().
    """
    ended = bool(current["expire_time"]) and current["expire_time"] < now
    if previous is None:
        return ("已結束" if ended else "新增"), True
    reappeared = previous["status"] in ("狀態不明", "已結束")
    content_changed = previous["fingerprint"] != current["fingerprint"]
    if ended:
        return "已結束", previous["status"] != "已結束" or content_changed
    if content_changed or reappeared:
        return "變更", True
    return "持續", False


def needs_major_notice(status, is_major, major_fingerprint, notified_fingerprint):
    """Notify when an active event is major and this major state has not been notified yet.

    Kept on the event row (notified_major_fingerprint) and set only after the alert is written,
    so a crash between saving the event and sending the alert is retried on the next run.
    """
    return status in ACTIVE and bool(is_major) and major_fingerprint != notified_fingerprint


def absent_status(previous_status, expire_time, now, response_complete):
    """State for an active event that is missing from this response; None keeps the old state."""
    if previous_status not in ACTIVE or not response_complete:
        return None
    if expire_time and expire_time < now:
        return "已結束"
    return "狀態不明"


def response_complete(count, fetched, unique, invalid, pages_exhausted, pages_consistent):
    """True only when the response provably held every current item exactly once.

    count: official Count (None when missing → not provable); unique: distinct event keys after normalising;
    invalid: items that could not be read; pages_consistent: every page carried the same UpdateTime.
    """
    if count is None or pages_exhausted or not pages_consistent or invalid:
        return False
    return fetched == count == unique


def parse_schedule(text):
    """'05:30,11:30' → sorted [(5, 30), (11, 30)]; raises ValueError on bad input."""
    times = set()
    for part in (text or "").replace("，", ",").replace("、", ",").split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", part)
        if not match:
            raise ValueError(f"時間格式錯誤：{part}")
        times.add((int(match.group(1)), int(match.group(2))))
    if not times:
        raise ValueError("至少需要一個排程時間")
    if len(times) > 24:
        raise ValueError("排程時間太多（上限 24 個）")
    return sorted(times)


def due_slot(now, times, catch_up_minutes=180):
    """Latest scheduled time ≤ now within the catch-up window, else None. now is naive Taipei time."""
    candidates = []
    for day in (now.date() - timedelta(days=1), now.date()):
        for hour, minute in times:
            slot = datetime(day.year, day.month, day.day, hour, minute)
            if slot <= now and now - slot <= timedelta(minutes=catch_up_minutes):
                candidates.append(slot)
    return max(candidates) if candidates else None


def next_slot(now, times):
    for day_offset in (0, 1):
        day = now.date() + timedelta(days=day_offset)
        for hour, minute in times:
            slot = datetime(day.year, day.month, day.day, hour, minute)
            if slot > now:
                return slot
    return None


def previous_slot(now, times):
    """Most recent scheduled time ≤ now (any distance), used to judge whether data is stale."""
    candidates = [datetime(d.year, d.month, d.day, h, m)
                  for d in (now.date() - timedelta(days=1), now.date()) for h, m in times]
    past = [c for c in candidates if c <= now]
    return max(past) if past else None


def estimated_points(calls, wire_bytes):
    """Conservative estimate (150 MB taken as 150×10^6 bytes). The TDX member console is authoritative."""
    return calls / CALL_POINTS + wire_bytes / (MB_POINTS * 1_000_000)
