"""Provincial height / weight limit signs (Highway Bureau open data) and permit-route checks. No Frappe here.

Source CSVs (政府資料開放授權條款第1版, monthly): 省道車輛高度限制圖資 (data.gov.tw 146233) and 省道車輛重量限制標誌
(data.gov.tw 169930). The Highway Bureau download host answers programs with a bot-check page, so the owner downloads
the files in a browser and uploads them; nothing here fetches anything.

First draft by Gemini (parsers, limit-value rules); reviewed against the real 2026-11 files (1,494 height signs,
72 weight signs, Big5) and adapted: shared road_key/parse_km, sign position/remark/town, side-facility flag,
import validation and route parsing.
"""
import codecs
import csv
import io
import math
import re

try:
    from fleet_bridge.road_events_core import parse_km, road_key
except ImportError:  # offline tests put the package directory itself on sys.path
    from road_events_core import parse_km, road_key

LON_MIN, LON_MAX, LAT_MIN, LAT_MAX = 118.0, 123.0, 21.0, 27.0
_EPS = 1e-9
_FULLWIDTH = str.maketrans("０１２３４５６７８９．＋－ＫＭＴｍｔ　", "0123456789.+-KMTmt ")
REQUIRED_COLUMNS = ("公路編號", "牌面內容", "坐標-E-WGS84", "坐標-N-WGS84")
# Remarks that point to a structure beside the main carriageway (motorcycle underpass, culvert, side road, U-turn,
# level crossing...). Such signs are still listed, but as 「可能是側向設施」 instead of a red main-line conflict.
SIDE_KEYWORDS = ("機車道", "涵洞", "側向", "迴車", "連通", "人行", "平交道", "便道", "側車道")
MIN_ROWS = {"height": 200, "weight": 20}
KIND_LABELS = {"height": "限高", "weight": "限重"}


def _str(value):
    return "" if value is None else str(value).strip()


def _decode(raw):
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    for encoding in ("utf-8", "cp950", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp950", errors="replace")


def parse_limit_value(text, kind):
    """Number from 牌面內容 (height in m, weight in t). None when missing, zero, absurd or ambiguous
    (two different numbers on one sign) — the caller reports those as 「限制值不明」, never as passable."""
    cleaned = _str(text).translate(_FULLWIDTH)
    if not cleaned:
        return None
    if kind == "height":
        tagged = re.findall(r"(\d+(?:\.\d+)?)\s*(?:公尺|米|m(?![A-Za-z0-9]))", cleaned, re.IGNORECASE) \
            or re.findall(r"限高\s*(\d+(?:\.\d+)?)", cleaned)
        ceiling = 20.0
    else:
        tagged = re.findall(r"(\d+(?:\.\d+)?)\s*(?:公噸|噸|t(?![A-Za-z0-9]))", cleaned, re.IGNORECASE) \
            or re.findall(r"(?:限重|總重)\s*(\d+(?:\.\d+)?)", cleaned)
        ceiling = 200.0
    tokens = tagged or re.findall(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![A-Za-z0-9.])", cleaned)
    values = {round(float(t), 3) for t in tokens if math.isfinite(float(t)) and 0.0 < float(t) <= ceiling}
    return values.pop() if len(values) == 1 else None


def _coords(lon_text, lat_text):
    try:
        lon, lat = float(_str(lon_text).translate(_FULLWIDTH)), float(_str(lat_text).translate(_FULLWIDTH))
    except ValueError:
        return None, None
    if not (math.isfinite(lon) and math.isfinite(lat) and LON_MIN <= lon <= LON_MAX and LAT_MIN <= lat <= LAT_MAX):
        return None, None
    return round(lat, 6), round(lon, 6)


def is_side_facility(remark):
    return any(word in _str(remark) for word in SIDE_KEYWORDS)


def parse_limit_signs(raw, kind):
    """CSV bytes → (records, invalid_count, columns). kind is 'height' or 'weight'. A bad row is counted, never fatal."""
    if kind not in KIND_LABELS:
        raise ValueError(f"unknown kind {kind}")
    if not isinstance(raw, (bytes, bytearray)) or not bytes(raw).strip():
        return [], 0, []
    reader = csv.DictReader(io.StringIO(_decode(bytes(raw)), newline=""))
    if not reader.fieldnames:
        return [], 0, []
    reader.fieldnames = [_str(h).lstrip("﻿") for h in reader.fieldnames]
    records, invalid = [], 0
    for row in reader:
        if None in row:  # more cells than columns
            invalid += 1
            continue
        row = {_str(k): _str(v) for k, v in row.items()}
        road = row.get("公路編號", "")
        if not road:
            invalid += 1
            continue
        lat, lon = _coords(row.get("坐標-E-WGS84"), row.get("坐標-N-WGS84"))
        value = parse_limit_value(row.get("牌面內容"), kind)
        remark = row.get("備註", "")
        records.append({
            "kind": kind, "sign_id": row.get("編號", ""), "road": road, "road_key": road_key(road.translate(_FULLWIDTH)),
            "county": row.get("隸屬縣市", ""), "town": row.get("隸屬鄉鎮", ""),
            "km": parse_km(row.get("起點樁號", "").translate(_FULLWIDTH)), "lat": lat, "lon": lon,
            "limit_value": value, "limit_text": row.get("牌面內容", ""), "direction": row.get("牌面方向", ""),
            "position": row.get("設置位置", ""), "remark": remark, "status": row.get("現況", ""),
            "survey_date": row.get("調查日期", ""), "side_facility": is_side_facility(remark)})
    return records, invalid, list(reader.fieldnames)


def validate_import(records, invalid, columns, kind):
    """Error text for a file that should not replace the stored signs, else None."""
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        return f"檔案缺少欄位：{'、'.join(missing)}（請確認下載的是公路局「省道車輛{'高度' if kind == 'height' else '重量'}限制」CSV）"
    if len(records) < MIN_ROWS[kind]:
        return f"只讀到 {len(records)} 筆，少於 {MIN_ROWS[kind]} 筆，可能下載不完整或選錯檔案。"
    if invalid > 0.05 * (len(records) + invalid):
        return f"{invalid} 列無法讀取（超過 5%），未匯入。"
    if sum(r["lat"] is None for r in records) > 0.1 * len(records):
        return "超過一成的標誌沒有有效座標，未匯入。"
    return None


def parse_routes(text):
    """'台1, 台17' or B1 lines '台1｜區段' → set of road keys."""
    keys = set()
    for line in _str(text).splitlines():
        # B1 lines are 「路線｜區段」 and the 區段 may itself contain 、 — only the part before ｜ names routes
        for part in re.split(r"[,，、]+", line.split("｜")[0]):
            part = part.strip()
            if part:
                keys.add(road_key(part.translate(_FULLWIDTH)))
    return keys


def route_conflicts(signs, routes, height_m=None, weight_t=None, margin_m=0.0):
    """Signs on `routes` (road keys) that the load does not clear, or whose limit is unknown.
    Each result has `reason` and `level`: 'red' (main line), 'yellow' (side facility or unknown limit)."""
    out = []
    need_h = None if height_m is None else float(height_m) + float(margin_m or 0)
    for sign in signs or ():
        if sign.get("road_key") not in routes:
            continue
        kind, value = sign.get("kind"), sign.get("limit_value")
        if kind == "height" and need_h is not None:
            if value is None:
                reason = "限高值不明"
            elif value + _EPS < need_h:
                reason = f"限高 {value:g} m，車高 {float(height_m):g} m" + (f"（含裕度 {float(margin_m):g} m）" if margin_m else "")
            else:
                continue
        elif kind == "weight" and weight_t is not None:
            if value is None:
                reason = "限重值不明"
            elif value + _EPS < float(weight_t):
                reason = f"限重 {value:g} t，車重 {float(weight_t):g} t"
            else:
                continue
        else:
            continue
        side = bool(sign.get("side_facility"))
        out.append({**sign, "reason": reason + ("；備註看起來是側向設施" if side else ""),
                    "level": "yellow" if side or value is None else "red"})
    out.sort(key=lambda s: (s["level"] != "red", s.get("road_key") or "", s.get("km") if s.get("km") is not None else 1e9))
    return out


def conflict_fingerprint(conflicts):
    """Stable text for the set of red (main-line) conflicts; '' when there are none. Used so a permit is alerted
    once per new set of problems, not on every re-check."""
    import hashlib
    keys = sorted(f"{c.get('kind')}|{c.get('road_key')}|{c.get('km')}|{c.get('sign_id')}|{c.get('limit_value')}"
                  for c in conflicts or () if c.get("level") == "red")
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:32] if keys else ""


def alert_lines(conflicts, limit=5):
    """Short lines for an alert: the first red conflicts by road and mileage."""
    red = [c for c in conflicts or () if c.get("level") == "red"]
    lines = [f"{c.get('road')} {c.get('km'):g}K {c.get('county', '')}{c.get('town', '')}：{c.get('reason')}"
             if c.get("km") is not None else f"{c.get('road')} {c.get('county', '')}：{c.get('reason')}" for c in red[:limit]]
    if len(red) > limit:
        lines.append(f"另有 {len(red) - limit} 處")
    return lines
