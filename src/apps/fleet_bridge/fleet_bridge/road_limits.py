"""Provincial height / weight limit signs: CSV upload (owner downloads it from the Highway Bureau in a browser
every month), storage, dashboard data and a permit-route check. No outbound calls."""
import os

import frappe
from frappe.utils import get_datetime, now_datetime

from fleet_bridge import limits_core as core
from fleet_bridge.road_events import SETTINGS, _field, _now_str, _require_manager

SIGN = "Fleet Road Limit Sign"
IMPORT = "Fleet Road Limit Import"
STALE_DAYS = 35            # monthly data: remind after about five weeks
MAX_UPLOAD = 5 * 1024 * 1024
SIGN_FIELDS = ["kind", "sign_id", "road", "road_key", "county", "town", "km", "lat", "lon", "limit_value",
               "limit_known", "limit_text", "direction", "position", "remark", "status", "survey_date", "side_facility"]


def install():
    """Idempotent: the sign table and the import log (read-only in the desk; written only by import_limits)."""
    read_only = [dict(role="System Manager", read=1)]
    if not frappe.db.exists("DocType", SIGN):
        frappe.get_doc(dict(doctype="DocType", name=SIGN, module="Fleet Bridge", custom=1, autoname="hash",
            track_changes=0, fields=[
                _field("kind", "類別", "Select", options="height\nweight", in_list_view=1, in_standard_filter=1),
                _field("road", "公路編號", "Data", in_list_view=1, in_standard_filter=1),
                _field("road_key", "道路比對鍵", "Data", search_index=1),
                _field("km", "里程（公里）", "Float", in_list_view=1),
                _field("limit_value", "限制值", "Float", in_list_view=1),
                _field("limit_known", "限制值可判讀", "Check"),
                _field("limit_text", "牌面內容", "Data"),
                _field("county", "縣市", "Data"),
                _field("town", "鄉鎮", "Data"),
                _field("lat", "緯度", "Float", precision="6"),
                _field("lon", "經度", "Float", precision="6"),
                _field("direction", "牌面方向", "Data"),
                _field("position", "設置位置", "Data"),
                _field("remark", "備註", "Data"),
                _field("side_facility", "可能是側向設施", "Check"),
                _field("status", "現況", "Data"),
                _field("survey_date", "調查日期", "Data"),
                _field("sign_id", "標誌編號", "Data"),
                _field("import_name", "匯入批次", "Data")],
            permissions=read_only)).insert()
    if not frappe.db.exists("DocType", IMPORT):
        frappe.get_doc(dict(doctype="DocType", name=IMPORT, module="Fleet Bridge", custom=1, autoname="hash",
            track_changes=0, fields=[
                _field("kind", "類別", "Select", options="height\nweight", in_list_view=1),
                _field("imported_at", "匯入時間（台北）", "Datetime", in_list_view=1),
                _field("imported_by", "匯入者", "Data", in_list_view=1),
                _field("file_name", "檔名", "Data"),
                _field("file_url", "檔案", "Data"),
                _field("record_count", "標誌數", "Int", in_list_view=1),
                _field("invalid_count", "無法讀取列數", "Int"),
                _field("unknown_count", "限制值不明", "Int"),
                _field("side_count", "可能是側向設施", "Int")],
            permissions=read_only)).insert()
    frappe.db.commit()


def _file_bytes(file_url):
    """Bytes of an uploaded private/public File, looked up by its URL (never an arbitrary path)."""
    name = frappe.db.get_value("File", {"file_url": file_url}, "name")
    if not name:
        frappe.throw("找不到上傳的檔案，請重新上傳。")
    path = frappe.get_doc("File", name).get_full_path()
    if not os.path.isfile(path) or os.path.getsize(path) > MAX_UPLOAD:
        frappe.throw("檔案不存在或超過 5 MB。")
    with open(path, "rb") as handle:
        return handle.read(), os.path.basename(path)


@frappe.whitelist(methods=["POST"])
def import_limits(kind, file_url):
    """Replace all stored signs of one kind with an uploaded Highway Bureau CSV, in one transaction.
    A file that looks wrong (missing columns, too few rows, too many bad rows) is rejected and nothing changes."""
    _require_manager()
    if kind not in core.KIND_LABELS:
        frappe.throw("類別只能是限高或限重。")
    raw, file_name = _file_bytes(file_url)
    records, invalid, columns = core.parse_limit_signs(raw, kind)
    error = core.validate_import(records, invalid, columns, kind)
    if error:
        frappe.throw(error)
    log = frappe.get_doc(dict(doctype=IMPORT, kind=kind, imported_at=_now_str(), imported_by=frappe.session.user,
                              file_name=file_name, file_url=file_url, record_count=len(records), invalid_count=invalid,
                              unknown_count=sum(r["limit_value"] is None for r in records),
                              side_count=sum(r["side_facility"] for r in records))).insert(ignore_permissions=True)
    frappe.db.delete(SIGN, {"kind": kind})
    now, user = now_datetime(), frappe.session.user
    columns_db = ["name", "creation", "modified", "owner", "modified_by", "import_name", *SIGN_FIELDS]
    values = []
    for r in records:
        row = {**r, "limit_known": 1 if r["limit_value"] is not None else 0, "side_facility": 1 if r["side_facility"] else 0}
        # NOT NULL numeric columns: unknown values are stored as 0 with limit_known / coordinates checked on read
        row.update({k: row[k] if row[k] is not None else 0 for k in ("km", "lat", "lon", "limit_value")})
        values.append([frappe.generate_hash(length=12), now, now, user, user, log.name, *[row[f] for f in SIGN_FIELDS]])
    frappe.db.bulk_insert(SIGN, columns_db, values)
    frappe.db.commit()
    return {"kind": kind, "records": len(records), "invalid": invalid, "unknown": log.unknown_count, "side": log.side_count}


def _signs():
    rows = frappe.get_all(SIGN, fields=SIGN_FIELDS, limit_page_length=0)
    for r in rows:
        r["limit_value"] = r.limit_value if r.limit_known else None
        r["side_facility"] = bool(r.side_facility)
        if not (r.lat and r.lon):
            r["lat"] = r["lon"] = None
    return rows


@frappe.whitelist(methods=["GET"])
def limits_overview(with_signs=0):
    """Last import per kind (and staleness); the signs themselves only when the map asks for them."""
    _require_manager()
    if not frappe.db.exists("DocType", SIGN):
        return {"installed": False}
    now = now_datetime()
    latest = {}
    for kind in core.KIND_LABELS:
        rows = frappe.get_all(IMPORT, filters={"kind": kind}, order_by="imported_at desc", limit=1,
                              fields=["imported_at", "file_name", "record_count", "unknown_count", "side_count", "imported_by"])
        if rows:
            row = rows[0]
            age = (now - get_datetime(row.imported_at)).days
            latest[kind] = {**row, "imported_at": str(row.imported_at)[:16], "age_days": age, "stale": age > STALE_DAYS}
        else:
            latest[kind] = None
    out = {"installed": True, "latest": latest, "stale_days": STALE_DAYS,
           "sources": {"height": "https://data.gov.tw/dataset/146233", "weight": "https://data.gov.tw/dataset/169930"}}
    if str(with_signs) in ("1", "true", "True"):
        out["signs"] = _signs()
    return out


@frappe.whitelist(methods=["GET"])
def route_check(height=None, weight=None, margin=0.1, routes=""):
    """Signs on the given routes (default: B1 routes from 道路情報設定) that a load of this height/weight does not clear."""
    _require_manager()
    def number(value, low, high, label):
        if value in (None, ""):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            frappe.throw(f"{label}要是數字。")
        if not low <= value <= high:
            frappe.throw(f"{label}要在 {low}～{high} 之間。")
        return value
    height = number(height, 0.5, 10, "車高（公尺）")
    weight = number(weight, 1, 300, "總重（公噸）")
    margin = number(margin, 0, 1, "安全裕度（公尺）") or 0.0
    if height is None and weight is None:
        frappe.throw("請輸入裝載後車高或總重。")
    text = (routes or "").strip() or (frappe.db.get_single_value(SETTINGS, "b1_routes") or "")
    keys = core.parse_routes(text)
    if not keys:
        frappe.throw("沒有可比對的路線。")
    found = core.route_conflicts(_signs(), keys, height, weight, margin)
    return {"routes": sorted(keys), "conflicts": found, "red": sum(c["level"] == "red" for c in found),
            "yellow": sum(c["level"] == "yellow" for c in found), "used_b1": not (routes or "").strip()}
