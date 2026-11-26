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
MAJOR_RULE = ("曳引車等同封閉：官方全封閉、特定車種管制需求、完全阻斷、災害，或國道封到最外側車道／全部車道；"
              "只封外側路肩為「通過時注意」；只封內側車道或內側路肩不影響")
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
        "blocked_lanes_label": blocked_lanes_label(_str(impact.get("BlockedLanes"))),
        "road_key": road_key(road), "direction_code": DIRECTION_CODES.get(direction, ""),
        "km_start": _first_km(item.get("Location"), "StartKM"), "km_end": _first_km(item.get("Location"), "EndKM"),
        "impact_description": _str(impact.get("Description")),
        "detour_description": _str(detour.get("Description")),
        "special_vehicle_control": 1 if 9 in regulations else 0,
        "is_major": 1 if is_major(severity, regulations, event_type) else 0,
        "structural_major": 1 if is_major(severity, regulations, event_type) else 0,
        "truck_impact": "", "truck_impact_reason": "", "lane_count": None, "is_caution": 0,
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
    record["caution_fingerprint"] = ""
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


# ---------------------------------------------------------------- 曳引車車道影響
# Lane coding (MOTC road-event data, checked against live freeway data 2026-11-26): lanes are numbered from the
# inner (left) side 0,1,2,...; LS = inner shoulder, RS = outer shoulder; "-1" = no lane blocked (comes with
# Severity 0); "255" = unknown (comes with Severity 255); "999" = all lanes. Several values are comma-separated.
# Tractor-trailers may not use the inner freeway lanes, so (owner's rule):
#   outer-most lane or all lanes blocked, or a structural major event  -> 等同封閉 (alert each)
#   outer shoulder (RS) only, outer lane open                        -> 通過時注意 (alert, combined per run)
#   inner lanes / inner shoulder only                                -> 無影響
#   no lane count for the place, ramps, unknown codes                -> 狀態不明 (never guessed)
TRUCK_IMPACTS = ("等同封閉", "通過時注意", "無影響", "無阻斷", "狀態不明", "未提供車道資訊")
DIRECTION_CODES = {"北向": "N", "南向": "S", "東向": "E", "西向": "W"}
MAINLINE_VD_KINDS = ("M", "N")   # VDID 5th part; I/O/R/A/B/C/L/PS are ramps or other places (mostly 1–2 lanes)
LANE_WINDOW_KM = 2.0             # VD detectors considered around the event's mileage range
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(text):
    """'三' -> 3, '十' -> 10, '十八' -> 18, '二十' -> 20; None if not a Chinese number."""
    if not text or any(ch not in _CN_DIGITS and ch != "十" for ch in text):
        return None
    if "十" not in text:
        return int("".join(str(_CN_DIGITS[ch]) for ch in text))
    tens, _, ones = text.partition("十")
    return (_CN_DIGITS[tens] if tens else 1) * 10 + (_CN_DIGITS[ones] if ones else 0)


def road_key(name):
    """Comparable road name: '國道三號' and '國道3號' -> '國道3號'; '國道三甲' -> '國道3甲'."""
    name = _str(name).replace(" ", "")
    match = re.match(r"^(國道|台|臺)([零一二三四五六七八九十]+)(.*)$", name)
    if match:
        number = _cn_number(match.group(2))
        if number is not None:
            name = f"{match.group(1)}{number}{match.group(3)}"
    return name.replace("臺", "台")


def parse_km(value):
    """'243K+800' -> 243.8, '12.0K' -> 12.0, '96' -> 96.0; None if unreadable."""
    text = _str(value).upper().replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)K?(?:\+(\d+(?:\.\d+)?))?", text)
    if not match:
        return None
    km = float(match.group(1))
    if match.group(2):
        km += float(match.group(2)) / 1000
    return round(km, 3)


def _first_km(location, field):
    location = location if isinstance(location, dict) else {}
    for seg in _as_list(location.get("FreeExpressHighway")):
        if isinstance(seg, dict) and seg.get(field) not in (None, ""):
            return parse_km(seg.get(field))
    return None


def parse_blocked_lanes(text):
    """-> ('none'|'unknown'|'all'|'lanes', set_of_lane_numbers, has_LS, has_RS)."""
    raw = _str(text)
    if raw == "":
        return "empty", set(), False, False
    tokens = [t.strip().upper() for t in raw.replace("，", ",").split(",") if t.strip()]
    if tokens == ["-1"]:
        return "none", set(), False, False
    if "999" in tokens:
        return "all", set(), "LS" in tokens, "RS" in tokens
    lanes, ls, rs = set(), False, False
    for token in tokens:
        if token == "LS":
            ls = True
        elif token == "RS":
            rs = True
        elif token.isdigit() and int(token) < 255:
            lanes.add(int(token))
        else:
            return "unknown", set(), False, False   # 255, -1 mixed with others, or anything unexpected
    return "lanes", lanes, ls, rs


def blocked_lanes_label(text, lane_count=None):
    kind, lanes, ls, rs = parse_blocked_lanes(text)
    if kind == "empty":
        return ""
    if kind == "none":
        return "無阻斷車道"
    if kind == "unknown":
        return "阻斷車道不明"
    parts = ["內側路肩"] if ls else []
    if kind == "all":
        parts.append("全部車道")
    consistent = bool(lane_count) and (not lanes or max(lanes) < lane_count)
    for lane in sorted(lanes):
        outer = consistent and lane == lane_count - 1
        parts.append(f"第{lane + 1}車道" + ("（最外側）" if outer else "（最內側）" if lane == 0 else ""))
    if rs:
        parts.append("外側路肩")
    if lane_count and not consistent:
        return "、".join(parts) + f"｜偵測器車道數 {lane_count} 與事件不一致"
    return "、".join(parts) + (f"｜本路段 {lane_count} 車道" if lane_count else "")


def lane_index_from_vds(vds):
    """VD records (TDX v2 JSON) -> {"國道3號|N": [[km, lanes], ...]} using main-line detectors only."""
    index = {}
    for vd in vds or []:
        if not isinstance(vd, dict):
            continue
        parts = _str(vd.get("VDID")).split("-")
        if len(parts) < 5 or parts[4] not in MAINLINE_VD_KINDS:
            continue
        km = parse_km(vd.get("LocationMile"))
        key_road = road_key(vd.get("RoadName"))
        if km is None or not key_road:
            continue
        for link in _as_list(vd.get("DetectionLinks")):
            if not isinstance(link, dict):
                continue
            lanes, direction = _int(link.get("ActualLaneNum")), _str(link.get("RoadDirection")).upper()
            if lanes and lanes > 0 and direction in ("N", "S", "E", "W"):
                index.setdefault(f"{key_road}|{direction}", []).append([km, lanes])
    for points in index.values():
        points.sort()
    return index


def lanes_near(index, road, direction, km_start, km_end, window=LANE_WINDOW_KM):
    """Fewest main-line lanes measured within the event's mileage range (± window). None when nothing is near.
    Using the minimum errs towards 「封到最外側」, never towards 「沒影響」."""
    if not index or not road or not direction or km_start is None:
        return None
    lo, hi = sorted((km_start, km_end if km_end is not None else km_start))
    near = [lanes for km, lanes in index.get(f"{road}|{direction}", []) if lo - window <= km <= hi + window]
    return min(near) if near else None


def truck_impact(record, lane_count):
    """(impact, reason) for a normalised record and the lane count at its place (None if unknown)."""
    if record.get("structural_major"):
        return "等同封閉", "官方標示全封閉、特定車種管制、完全阻斷或災害"
    kind, lanes, _ls, rs = parse_blocked_lanes(record.get("blocked_lanes"))
    if kind == "empty":
        return "未提供車道資訊", "來源未提供阻斷車道"
    if kind == "none":
        return "無阻斷", "官方標示無阻斷車道"
    if kind == "unknown":
        return "狀態不明", "官方阻斷車道為未知或無法辨識"
    if kind == "all":
        return "等同封閉", "全部車道受阻"
    if lanes:
        if record.get("location_type") not in ("道路主線", ""):
            return "狀態不明", f"{record.get('location_type')}的車道數無法判斷"
        if not lane_count:
            return "狀態不明", "查無此路段車道數"
        if max(lanes) >= lane_count:
            return "等同封閉", f"事件車道編號超過偵測器車道數 {lane_count}，保守視為封到外側"
        if max(lanes) == lane_count - 1:
            return "等同封閉", f"封到最外側車道（本路段 {lane_count} 車道）"
        if rs:
            return "通過時注意", f"外側路肩施工，內側車道封閉（本路段 {lane_count} 車道）"
        return "無影響", f"只封內側車道（本路段 {lane_count} 車道）"
    if rs:
        return "通過時注意", "外側路肩施工，車道未封"
    return "無影響", "只封內側路肩"


def apply_truck_impact(record, lane_index):
    """Adds truck impact, lane count and label to a normalised record and refreshes the alert fingerprints."""
    lane_count = lanes_near(lane_index, record.get("road_key"), record.get("direction_code"),
                            record.get("km_start"), record.get("km_end"))
    impact, reason = truck_impact(record, lane_count)
    record.update(truck_impact=impact, truck_impact_reason=reason, lane_count=lane_count,
                  blocked_lanes_label=blocked_lanes_label(record.get("blocked_lanes"), lane_count),
                  is_major=1 if record.get("structural_major") or impact == "等同封閉" else 0,
                  is_caution=1 if impact == "通過時注意" else 0)
    record["major_fingerprint"] = _digest([record["is_major"], impact, record.get("severity"), record.get("regulations"),
                                           record.get("block_way"), record.get("blocked_lanes")], 16)
    record["caution_fingerprint"] = _digest([impact, record.get("blocked_lanes"), record.get("km_start")], 16) \
        if record["is_caution"] else ""
    return record


def needs_caution_notice(status, is_caution, caution_fingerprint, notified_fingerprint):
    return status in ACTIVE and bool(is_caution) and caution_fingerprint != notified_fingerprint
