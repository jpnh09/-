"""TDX 道路情報：定時同步、狀態比對、通知，以及車隊首頁「道路情報」分頁的資料。

Runs entirely inside the ERP (Frappe scheduler + RQ worker); no AI service is involved.
The worker reaches TDX only through the 政府 API 專用出口代理 (see gov_gateway.py).
"""
import hashlib
import time
from collections import Counter
from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime
from frappe.utils.password import get_decrypted_password

from fleet_bridge import road_events_core as core
from fleet_bridge.gov_gateway import GatewayError, call

SETTINGS = "Fleet Road Info Settings"
EVENT = "Fleet Road Event"
RUN = "Fleet Road Sync Run"
DISPATCH = "Fleet Road Event Dispatch"
USAGE = "Fleet Gov API Usage"
SOURCE = "tdx"
TOKEN_PATH = "/auth/realms/TDXConnect/protocol/openid-connect/token"
TOKEN_CACHE = "fleet_road_tdx_token"
LAST_CALL_CACHE = "fleet_road_tdx_last_call"
LOCK_NAME = "fleet_road_sync"
PAGE_SIZE = 5000           # one call normally returns the whole snapshot; paging is only a fallback
MAX_PAGES = 3
CALL_SPACING = 15          # 免費會員：每把金鑰每分鐘 5 次（12 秒），留 3 秒餘裕
MAX_NOTIFY_PER_RUN = 15
STALE_QUEUED_MINUTES = 60  # queued longer than this → failed
STALE_RUNNING_MINUTES = 45  # job timeout is 30 min
REQUEUE_AFTER_MINUTES = 2   # a queued run whose enqueue was lost gets enqueued again
BASELINE_KEY = "fleet_road_baseline:"
LIST_KEYS = ("LiveEvents", "Events", "Items")
# Within one worker job, consecutive cache reads did not see values written moments earlier (live run
# 2026-11-26: token fetched per call, calls 2 s apart), while reads across jobs did. Keep the token and the
# last call time in this process first; Redis only shares them between jobs and workers.
_PROCESS = {"token": None, "token_until": 0.0, "last_call": 0.0}
UNKNOWN_DAYS, ENDED_DAYS = 7, 2
NOTICE = ("道路情報每日依排程更新，不是即時交通預警；請以最後成功更新時間判斷資料新舊。"
          "TDX 沒有提供「已解除」欄位：事件從最新完整回應中消失只標記為「狀態不明」，官方結束時間已過才標記「已結束」。"
          "TDX 未提供限高、限寬、限重欄位；未列出限制不代表特殊車輛可以通行，出車前仍須依通行證與現場公告確認。")


class SyncError(Exception):
    """Expected failure with a message that is safe to show and store (no secrets)."""


def _now():
    return now_datetime().replace(tzinfo=None)


def _now_str():
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _require_manager():
    if frappe.session.user == "Guest" or "System Manager" not in frappe.get_roles():
        frappe.throw("道路情報限已有管理權限的使用者。", frappe.PermissionError)


# ---------------------------------------------------------------- install

def _field(fieldname, label, fieldtype, **extra):
    return dict(fieldname=fieldname, label=label, fieldtype=fieldtype, **extra)


def install():
    """Idempotent: creates the road-information DocTypes and registers the scheduler hook."""
    def read_only():
        return [dict(role="System Manager", read=1)]

    if not frappe.db.exists("DocType", USAGE):
        frappe.get_doc(dict(doctype="DocType", name=USAGE, module="Fleet Bridge", custom=1,
            autoname="field:usage_key", track_changes=0, fields=[
                _field("usage_key", "識別碼", "Data", reqd=1, unique=1, in_list_view=1),
                _field("source", "資料來源", "Data", reqd=1, in_list_view=1),
                _field("month", "月份", "Data", reqd=1, in_list_view=1),
                _field("data_calls", "資料呼叫次數", "Int", default="0", in_list_view=1),
                _field("token_calls", "認證呼叫次數", "Int", default="0"),
                _field("wire_bytes", "傳輸位元組", "Float", default="0"),
                _field("failed_calls", "未送達次數", "Int", default="0")],
            permissions=read_only())).insert()
    if not frappe.db.exists("DocType", EVENT):
        frappe.get_doc(dict(doctype="DocType", name=EVENT, module="Fleet Bridge", custom=1, autoname="hash",
            track_changes=0, title_field="title", sort_field="modified", fields=[
                _field("event_key", "事件識別", "Data", reqd=1, unique=1, length=255, read_only=1),
                _field("status", "狀態", "Select", options="\n".join(core.STATUSES), in_list_view=1,
                       in_standard_filter=1),
                _field("is_major", "重大管制", "Check", in_standard_filter=1),
                _field("special_vehicle_control", "特定車種管制需求", "Check", in_standard_filter=1),
                _field("feed", "資料集", "Data", in_standard_filter=1),
                _field("feed_label", "資料集名稱", "Data"),
                _field("kind", "即時／預告", "Data"),
                _field("event_id", "官方事件代碼", "Data", length=140),
                _field("event_type", "事件類型代碼", "Int"),
                _field("event_type_label", "事件類型", "Data", in_list_view=1),
                _field("event_sub_type", "事件子類型代碼", "Int"),
                _field("event_step", "事件積報序號", "Int"),
                _field("title", "標題", "Small Text", in_list_view=1),
                _field("description", "描述", "Long Text"),
                _field("road", "道路", "Data", length=140, in_list_view=1, in_standard_filter=1),
                _field("direction", "方向", "Data", length=40),
                _field("location_type", "位置類型", "Data"),
                _field("location_text", "位置", "Small Text"),
                _field("effective_time", "事件起始時間（台北）", "Datetime"),
                _field("expire_time", "官方結束時間（台北）", "Datetime"),
                _field("publish_time", "發布時間（台北）", "Datetime"),
                _field("source_update_time", "來源最後更新（台北）", "Datetime"),
                _field("severity", "影響程度代碼", "Int"),
                _field("severity_label", "影響程度", "Data"),
                _field("regulations", "管制代碼", "Data"),
                _field("regulation_labels", "管制方式", "Data", length=255),
                _field("block_way", "阻斷代碼", "Int"),
                _field("block_way_label", "阻斷方向", "Data"),
                _field("blocked_lanes", "阻斷車道", "Data", length=140),
                _field("impact_description", "影響說明", "Small Text"),
                _field("detour_description", "替代道路", "Small Text"),
                _field("duration_text", "管制時段（原始）", "Small Text"),
                _field("reporting_source", "通報來源", "Data", length=140),
                _field("positions", "代表點位", "Data", length=500),
                _field("web_url", "官方網址", "Data", length=500),
                _field("fingerprint", "內容指紋", "Data", hidden=1),
                _field("major_fingerprint", "重大指紋", "Data", hidden=1),
                _field("notified_major_fingerprint", "已通知重大指紋", "Data", hidden=1),
                _field("first_seen_at", "首次取得", "Datetime"),
                _field("first_seen_run", "首次取得批次", "Data"),
                _field("last_seen_at", "最後出現", "Datetime"),
                _field("last_seen_run", "最後出現批次", "Data"),
                _field("last_change_at", "最後狀態變動", "Datetime"),
                _field("last_change_run", "最後變動批次", "Data"),
                _field("missing_since", "未再出現起", "Datetime"),
                _field("raw_json", "官方原始資料", "Code", options="JSON")],
            permissions=read_only())).insert()
    if not frappe.db.exists("DocType", RUN):
        frappe.get_doc(dict(doctype="DocType", name=RUN, module="Fleet Bridge", custom=1,
            autoname="RS-.YYYY.-.#####", track_changes=0, sort_field="creation", fields=[
                _field("slot_key", "排程識別", "Data", reqd=1, unique=1, in_list_view=1),
                _field("run_trigger", "觸發方式", "Select", options="排程\n手動", in_list_view=1),
                _field("requested_by", "要求帳號", "Link", options="User"),
                _field("status", "結果", "Select", options="排隊中\n執行中\n成功\n部分成功\n失敗\n已略過",
                       in_list_view=1, in_standard_filter=1),
                _field("started_at", "開始時間", "Datetime"),
                _field("finished_at", "完成時間", "Datetime", in_list_view=1),
                _field("new_count", "新增", "Int"),
                _field("changed_count", "變更", "Int"),
                _field("ended_count", "已結束", "Int"),
                _field("unknown_count", "狀態不明", "Int"),
                _field("major_count", "重大待通知", "Int"),
                _field("data_calls", "資料呼叫次數", "Int"),
                _field("wire_bytes", "傳輸位元組", "Float"),
                _field("error", "錯誤摘要", "Small Text"),
                _field("feeds_json", "各資料集結果", "Code", options="JSON")],
            permissions=read_only())).insert()
    if not frappe.db.exists("DocType", DISPATCH):
        frappe.get_doc(dict(doctype="DocType", name=DISPATCH, module="Fleet Bridge", custom=1,
            autoname="field:dispatch_key", track_changes=0, fields=[
                _field("dispatch_key", "發送識別碼", "Data", reqd=1, unique=1),
                _field("kind", "通知類型", "Data", in_list_view=1),
                _field("road_event", "道路事件", "Link", options=EVENT),
                _field("sync_run", "更新批次", "Link", options=RUN),
                _field("recipient", "接收帳號", "Link", options="User", reqd=1, in_list_view=1),
                _field("notification_log", "系統通知", "Link", options="Notification Log", reqd=1)],
            permissions=read_only())).insert()
    if not frappe.db.exists("DocType", SETTINGS):
        frappe.get_doc(dict(doctype="DocType", name=SETTINGS, module="Fleet Bridge", custom=1, issingle=1,
            track_changes=1, fields=[
                _field("connection_section", "TDX 連線", "Section Break"),
                _field("enabled", "啟用定時更新", "Check", default="0"),
                _field("tdx_client_id", "TDX Client ID", "Data", length=140),
                _field("tdx_client_secret", "TDX Client Secret", "Password", length=255,
                       description="只存在 ERP 後端並加密保存；不會出現在車隊首頁、日誌或通知。"),
                _field("schedule_section", "排程與範圍", "Section Break"),
                _field("schedule_times", "每日更新時間（台北）", "Data", default=core.DEFAULT_SCHEDULE,
                       description="以逗號分隔，例如 05:30,11:30,17:30,22:30。"),
                _field("feed_freeway_live", "國道即時事件", "Check", default="1"),
                _field("feed_highway_live", "省道即時事件", "Check", default="1"),
                _field("feed_highway_preview", "省道預告事件", "Check", default="1"),
                _field("city_live_codes", "縣市即時事件（官方代碼）", "Small Text",
                       description="預設不啟用。每個縣市每輪至少多 1 次呼叫。可用：" + ", ".join(core.CITY_LIVE)),
                _field("city_preview_codes", "縣市預告事件（官方代碼）", "Small Text",
                       description="可用：" + ", ".join(core.CITY_PREVIEW)),
                _field("quota_section", "額度", "Section Break"),
                _field("monthly_point_budget", "每月估計點數上限", "Float", default="2.5",
                       description="免費會員每月 3 點（1,500 次或 150 MB 為 1 點，合併計算）。達到上限即暫停更新；以 TDX 會員中心數字為準。"),
                _field("manual_cooldown_minutes", "手動更新間隔（分鐘）", "Int", default="15")],
            permissions=[dict(role="System Manager", read=1, write=1)])).insert()
    # A Single DocType does not apply field defaults until someone saves the form; write them once.
    for fieldname, value in (("enabled", 0), ("schedule_times", core.DEFAULT_SCHEDULE), ("feed_freeway_live", 1),
                             ("feed_highway_live", 1), ("feed_highway_preview", 1), ("monthly_point_budget", 2.5),
                             ("manual_cooldown_minutes", 15)):
        if not frappe.db.sql("SELECT 1 FROM `tabSingles` WHERE doctype=%s AND field=%s", (SETTINGS, fieldname)):
            frappe.db.set_single_value(SETTINGS, fieldname, value)
    from frappe.core.doctype.scheduled_job_type.scheduled_job_type import sync_jobs
    sync_jobs()
    frappe.db.commit()


def validate_settings(doc, method=None):
    try:
        core.parse_schedule(doc.schedule_times or core.DEFAULT_SCHEDULE)
    except ValueError as error:
        frappe.throw(str(error))
    for field, allowed in (("city_live_codes", core.CITY_LIVE), ("city_preview_codes", core.CITY_PREVIEW)):
        codes = _codes(doc.get(field))
        unknown = [c for c in codes if c not in allowed]
        if unknown:
            frappe.throw(f"不支援的縣市代碼：{', '.join(unknown)}")
        doc.set(field, ", ".join(codes))
    if not doc.monthly_point_budget or doc.monthly_point_budget <= 0:
        frappe.throw("每月估計點數上限必須大於 0。")
    if (doc.manual_cooldown_minutes or 0) < 5:
        doc.manual_cooldown_minutes = 5
    doc.tdx_client_id = (doc.tdx_client_id or "").strip()


def _codes(text):
    seen = []
    for part in (text or "").replace("，", ",").replace("、", ",").replace("\n", ",").split(","):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return seen


def _enabled_feeds(settings):
    feeds = [key for key, field in (("freeway_live", "feed_freeway_live"), ("highway_live", "feed_highway_live"),
                                    ("highway_preview", "feed_highway_preview")) if settings.get(field)]
    feeds += [f"city_live:{c}" for c in _codes(settings.city_live_codes) if c in core.CITY_LIVE]
    feeds += [f"city_preview:{c}" for c in _codes(settings.city_preview_codes) if c in core.CITY_PREVIEW]
    return feeds


def _secret():
    return get_decrypted_password(SETTINGS, SETTINGS, "tdx_client_secret", raise_exception=False)


# ---------------------------------------------------------------- usage and budget

def _month():
    return _now().strftime("%Y-%m")


RUN_USAGE = Counter()  # this worker's counts for the current run (reset by run_sync)


def _usage(data_calls=0, token_calls=0, wire_bytes=0, failed_calls=0):
    """Counted per upstream response and committed at once, so a crash later in the run cannot lose it."""
    RUN_USAGE.update(data_calls=data_calls, token_calls=token_calls, wire_bytes=wire_bytes, failed_calls=failed_calls)
    key = f"{SOURCE}-{_month()}"
    if not frappe.db.exists(USAGE, key):
        try:
            frappe.get_doc(dict(doctype=USAGE, usage_key=key, source=SOURCE, month=_month())).insert(
                ignore_permissions=True)
        except frappe.DuplicateEntryError:
            pass
    frappe.db.sql("""UPDATE `tabFleet Gov API Usage` SET data_calls=data_calls+%s, token_calls=token_calls+%s,
        wire_bytes=wire_bytes+%s, failed_calls=failed_calls+%s WHERE name=%s""",
        (data_calls, token_calls, wire_bytes, failed_calls, key))
    frappe.db.commit()


def usage_summary(settings=None):
    settings = settings or frappe.get_single(SETTINGS)
    row = frappe.db.get_value(USAGE, f"{SOURCE}-{_month()}", ["data_calls", "token_calls", "wire_bytes", "failed_calls"],
                              as_dict=True) or frappe._dict(data_calls=0, token_calls=0, wire_bytes=0, failed_calls=0)
    points = core.estimated_points(row.data_calls or 0, row.wire_bytes or 0)
    budget = settings.monthly_point_budget or 2.5
    return {"month": _month(), "data_calls": row.data_calls or 0, "token_calls": row.token_calls or 0,
            "wire_bytes": int(row.wire_bytes or 0),
            "wire_mb": round((row.wire_bytes or 0) / 1_000_000, 2), "failed_calls": row.failed_calls or 0,
            "estimated_points": round(points, 3), "budget": budget, "exceeded": points >= budget}


# ---------------------------------------------------------------- TDX calls

def _token(force=False):
    cache = frappe.cache()
    now = time.time()
    if force:
        _PROCESS.update(token=None, token_until=0.0)
    elif _PROCESS["token"] and now < _PROCESS["token_until"]:
        return _PROCESS["token"]
    else:
        token = cache.get_value(TOKEN_CACHE, expires=True)
        if token:
            _PROCESS.update(token=token, token_until=now + 600)  # look at Redis again after 10 minutes
            return token
    client_id = (frappe.db.get_single_value(SETTINGS, "tdx_client_id") or "").strip()
    secret = _secret()
    if not client_id or not secret:
        raise SyncError("尚未設定 TDX Client ID 或 Client Secret。")
    try:
        response = call(SOURCE, "POST", TOKEN_PATH, timeout=(10, 30),
                        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret})
    except GatewayError as error:
        _usage(failed_calls=1)
        raise SyncError(f"無法連到出口代理（{error.code}）。")
    if not response.from_upstream:
        raise SyncError(f"出口代理拒絕認證請求（{response.gateway_error}）。")
    _usage(token_calls=1)
    if response.status != 200:
        raise SyncError(f"TDX 認證失敗（HTTP {response.status}）。請確認金鑰。")
    try:
        payload = response.json()
    except ValueError:
        raise SyncError("TDX 認證回應不是 JSON。")
    token, expires = payload.get("access_token"), payload.get("expires_in")
    if not isinstance(token, str) or not token:
        raise SyncError("TDX 認證回應缺少 access_token。")
    ttl = max(60, min(int(expires) if isinstance(expires, int) else 3600, 86400) - 900)
    cache.set_value(TOKEN_CACHE, token, expires_in_sec=ttl)
    _PROCESS.update(token=token, token_until=time.time() + ttl)
    return token


def _space_calls():
    """At least CALL_SPACING seconds between data calls, across workers.

    Keys written with expires_in_sec live only in Redis; get_value(expires=True) reads Redis every time.
    A plain get_value would remember the first miss in the per-process cache and never see later writes."""
    cache = frappe.cache()
    shared = cache.get_value(LAST_CALL_CACHE, expires=True)
    last = max(_PROCESS["last_call"], float(shared) if shared else 0.0)
    wait = CALL_SPACING - (time.time() - last)
    if wait > 0:
        time.sleep(min(wait, CALL_SPACING))
    stamp = time.time()
    _PROCESS["last_call"] = stamp
    cache.set_value(LAST_CALL_CACHE, stamp, expires_in_sec=300)


def _retry_after(value, default=60, ceiling=180):
    """Retry-After as seconds or an HTTP date; bounded so one run stays inside the 30-minute job timeout."""
    value = (value or "").strip()
    if value.isdigit():
        return min(int(value), ceiling)
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        wait = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        return min(max(int(wait), 1), ceiling)
    except (TypeError, ValueError, IndexError, OverflowError):
        return default


def _get(path, params):
    """GET with token reuse, one re-auth on 401, limited retries with backoff for 429/5xx/timeouts."""
    reauthed = False
    backoff = (5, 20)
    for attempt in range(3):
        _space_calls()
        try:
            response = call(SOURCE, "GET", path, params=params, headers={"Authorization": "Bearer " + _token()})
        except GatewayError as error:
            _usage(failed_calls=1)
            if attempt < 2:
                time.sleep(backoff[attempt])
                continue
            raise SyncError(f"無法連到出口代理（{error.code}）。")
        if not response.from_upstream:
            if response.discarded_bytes is not None:
                _usage(data_calls=1, wire_bytes=response.discarded_bytes)  # provider answered; still billable
            else:
                _usage(failed_calls=1)
            if response.status in (502, 503, 504) and attempt < 2:
                time.sleep(backoff[attempt])
                continue
            raise SyncError(f"出口代理拒絕或無法送達（{response.gateway_error}）。")
        _usage(data_calls=1, wire_bytes=response.wire_bytes)
        if response.status == 200:
            return response
        if response.status == 401 and not reauthed:
            reauthed = True
            frappe.cache().delete_value(TOKEN_CACHE)
            _PROCESS.update(token=None, token_until=0.0)
            continue
        if response.status == 429 and attempt < 2:
            time.sleep(_retry_after(response.headers.get("Retry-After")))
            continue
        if response.status >= 500 and attempt < 2:
            time.sleep(backoff[attempt])
            continue
        raise SyncError(f"TDX 回應 HTTP {response.status}。")
    raise SyncError("TDX 多次重試仍未成功。")


def _fetch(feed):
    """Whole snapshot of one feed. Completeness is judged later, once items are normalised (_apply)."""
    path, label, _kind = core.feed_spec(feed)
    items, count, pages, meta, exhausted, update_times = [], None, 0, {}, False, set()
    while True:
        response = _get(path, {"$format": "JSON", "$top": str(PAGE_SIZE), "$skip": str(len(items)), "$count": "true"})
        try:
            payload = response.json()
        except ValueError:
            raise SyncError(f"{label}：回應不是 JSON。")
        if not isinstance(payload, dict):
            raise SyncError(f"{label}：回應不是 JSON 物件。")
        # Verified against the live API on 2026-11-26: LiveEvent answers carry "LiveEvents",
        # Event answers carry "Events"; the published OAS schema names it "Items".
        list_key = next((k for k in LIST_KEYS if isinstance(payload.get(k), list)), None)
        if list_key is None:
            raise SyncError(f"{label}：找不到事件清單（回應欄位：{', '.join(sorted(map(str, payload)))[:150]}）。")
        page = payload[list_key]
        pages += 1
        update_times.add(payload.get("UpdateTime"))
        if isinstance(payload.get("Count"), int) and not isinstance(payload.get("Count"), bool):
            count = payload["Count"] if count is None or count == payload["Count"] else -1  # -1: changed mid-way
        if pages == 1:
            meta = {"update_time": core.to_taipei(payload.get("UpdateTime")),
                    "src_update_time": core.to_taipei(payload.get("SrcUpdateTime")),
                    "update_interval": payload.get("UpdateInterval")}
        items.extend(page)
        if not page or (count is not None and count != -1 and len(items) >= count):
            break  # an empty page before Count is reached leaves the snapshot incomplete (checked later)
        if count is None and len(page) < PAGE_SIZE:
            break
        if pages >= MAX_PAGES:
            exhausted = True
            break
    return items, {"feed": feed, "label": label, "count": None if count == -1 else count, "fetched": len(items),
                   "pages": pages, "pages_exhausted": exhausted,
                   "pages_consistent": len(update_times) == 1 and count != -1, **meta}


# ---------------------------------------------------------------- applying a response

COPY_FIELDS = ("feed", "feed_label", "kind", "event_id", "title", "description", "event_type", "event_type_label",
               "event_sub_type", "event_step", "effective_time", "expire_time", "publish_time", "source_update_time",
               "location_type", "location_text", "road", "direction", "severity", "severity_label", "regulations",
               "regulation_labels", "block_way", "block_way_label", "blocked_lanes", "impact_description",
               "detour_description", "duration_text", "special_vehicle_control", "is_major", "reporting_source",
               "positions", "web_url", "fingerprint", "major_fingerprint", "raw_json")


def _apply(feed, items, meta, run_name, now_str, baseline):
    """Write one feed's snapshot. Missing events change state only when the snapshot is provably complete.

    On a baseline (first complete snapshot of a feed) major events are recorded as already notified,
    so switching the module on does not flood everyone with alerts for closures that already existed.
    """
    records, invalid = {}, 0
    for item in items:
        try:
            record = core.normalize(feed, item)
        except ValueError:
            invalid += 1
            continue
        records[record["event_key"]] = record
    complete = core.response_complete(meta["count"], meta["fetched"], len(records), invalid,
                                      meta["pages_exhausted"], meta["pages_consistent"])
    existing = {row.event_key: row for row in frappe.get_all(EVENT, filters={"feed": feed}, limit_page_length=0,
        fields=["name", "event_key", "status", "fingerprint", "expire_time"])}
    stats = Counter()
    for key, record in records.items():
        previous = existing.get(key)
        status, changed = core.decide(previous and {"status": previous.status, "fingerprint": previous.fingerprint},
                                      record, now_str)
        values = {f: record[f] for f in COPY_FIELDS}
        values.update(status=status, last_seen_at=now_str, last_seen_run=run_name, missing_since=None)
        if not record["is_major"]:
            values["notified_major_fingerprint"] = None  # a later return to major alerts again
        elif baseline:
            values["notified_major_fingerprint"] = record["major_fingerprint"]
        if changed:
            values.update(last_change_at=now_str, last_change_run=run_name)
        if previous is None:
            frappe.get_doc(dict(doctype=EVENT, event_key=key, first_seen_at=now_str, first_seen_run=run_name,
                                **values)).insert(ignore_permissions=True)
        elif changed or previous.status != status:
            frappe.get_doc(EVENT, previous.name).update(values).save(ignore_permissions=True)
        else:
            # Same content and state: refresh the copied fields (source, links, raw data) without a "change".
            frappe.db.set_value(EVENT, previous.name, values, update_modified=False)
        if changed:
            stats[{"新增": "new", "變更": "changed", "已結束": "ended"}.get(status, "changed")] += 1
    for key, previous in existing.items():
        if key in records:
            continue
        expire = str(previous.expire_time) if previous.expire_time else None
        status = core.absent_status(previous.status, expire, now_str, complete)
        if status and status != previous.status:
            frappe.db.set_value(EVENT, previous.name, {"status": status, "missing_since": now_str,
                "last_change_at": now_str, "last_change_run": run_name})
            stats["ended" if status == "已結束" else "unknown"] += 1
    return stats, invalid, complete


def _expire_sweep(run_name, now_str, skip_feeds):
    """Official end time passed → 已結束. Feeds that failed this round are left untouched (the source
    may have extended the end time and we could not see it)."""
    # Not frappe.get_all: its "<" filter wraps the column in IFNULL(expire_time, '0001-01-01'), so every
    # event without an official end time would count as expired.
    skip = tuple(skip_feeds) or ("",)
    rows = [r[0] for r in frappe.db.sql(f"""SELECT name FROM `tab{EVENT}`
        WHERE status IN %(active)s AND expire_time IS NOT NULL AND expire_time < %(now)s AND feed NOT IN %(skip)s""",
        {"active": core.ACTIVE, "now": now_str, "skip": skip})]
    for name in rows:
        frappe.db.set_value(EVENT, name, {"status": "已結束", "last_change_at": now_str, "last_change_run": run_name})
    return len(rows)


def repair_null_expiry():
    """road-info-3 repair: 已結束 without an official end time can only come from the old sweep bug.
    Restore 新增 (first seen in the run that closed it) or 持續. Idempotent; touches nothing else."""
    rows = frappe.db.sql(f"""SELECT name, first_seen_run, last_change_run FROM `tab{EVENT}`
        WHERE status='已結束' AND expire_time IS NULL""", as_dict=True)
    for row in rows:
        status = "新增" if row.first_seen_run == row.last_change_run else "持續"
        frappe.db.set_value(EVENT, row.name, "status", status, update_modified=False)
    frappe.db.commit()
    print(f"repaired {len(rows)} events")
    return len(rows)


def _pending_major():
    """Active major events whose current major state has not been notified (also retries after a crash)."""
    return [r[0] for r in frappe.db.sql(f"""SELECT name FROM `tab{EVENT}`
        WHERE status IN %(active)s AND is_major=1
          AND (notified_major_fingerprint IS NULL OR notified_major_fingerprint != major_fingerprint)
        ORDER BY effective_time DESC, name""", {"active": core.ACTIVE})]


# ---------------------------------------------------------------- notifications

def _dispatch(kind, key_text, user, title, description, road_event=None, sync_run=None):
    key = hashlib.sha256(f"{kind}\0{key_text}\0{user}".encode("utf-8")).hexdigest()
    if frappe.db.exists(DISPATCH, key):
        return False
    alert = frappe.get_doc({"doctype": "Notification Log", "type": "Alert", "for_user": user,
        "from_user": "Administrator", "document_type": EVENT if road_event else RUN,
        "document_name": road_event or sync_run, "title": title, "description": description}).insert(
        ignore_permissions=True)
    previous_flag = frappe.flags.get("fleet_log_insert")
    frappe.flags.fleet_log_insert = True
    try:
        frappe.get_doc({"doctype": DISPATCH, "dispatch_key": key, "kind": kind, "road_event": road_event,
                        "sync_run": sync_run, "recipient": user, "notification_log": alert.name}).insert(
            ignore_permissions=True)
    finally:
        frappe.flags.fleet_log_insert = previous_flag
    return True


def _notify_major(names, run_name):
    """Alert for each pending major event (capped), then mark each one notified for its current major state.
    The dispatch key (event + major state + user) still blocks duplicates if marking is interrupted."""
    from fleet_bridge.notification_dispatch import recipients
    users = recipients()
    if not users or not names:
        return 0
    shown, overflow = names[:MAX_NOTIFY_PER_RUN], names[MAX_NOTIFY_PER_RUN:]
    from frappe.utils import escape_html
    for name in shown:
        event = frappe.get_doc(EVENT, name)
        control = event.regulation_labels or event.severity_label or event.event_type_label
        title = escape_html(f"道路管制：{event.road or event.feed_label}｜{control}"[:120])
        description = escape_html(f"{event.event_type_label}｜{event.location_text or '位置未提供'}｜{event.title}。"
                                  f"來源最後更新（台北）：{event.source_update_time or '未提供'}。資料來源：交通部 TDX。"
                                  "未列出限制不代表特殊車輛可通行。")
        for user in users:
            _dispatch("重大管制", f"{event.event_key}\0{event.major_fingerprint}", user, title, description,
                      road_event=name)
        frappe.db.set_value(EVENT, name, "notified_major_fingerprint", event.major_fingerprint, update_modified=False)
    if overflow:
        states = frappe.get_all(EVENT, filters={"name": ["in", overflow]}, fields=["name", "event_key", "major_fingerprint"])
        digest_key = hashlib.sha256("\0".join(sorted(f"{s.event_key}\0{s.major_fingerprint}" for s in states))
                                    .encode("utf-8")).hexdigest()
        for user in users:
            _dispatch("重大管制彙整", digest_key, user, f"道路管制：另有 {len(overflow)} 筆重大事件",
                      "本輪重大事件較多，其餘請到車隊首頁「道路情報」查看。", sync_run=run_name)
        for state in states:
            frappe.db.set_value(EVENT, state.name, "notified_major_fingerprint", state.major_fingerprint,
                                update_modified=False)
    frappe.db.commit()
    return len(names)


def _notify_failure_streak(run):
    """Alert once per streak when two or more scheduled runs in a row failed (data may be stale).
    The streak is identified by its first failed run, so later failures in the same streak stay quiet."""
    if run.run_trigger != "排程":
        return
    recent = frappe.get_all(RUN, filters={"run_trigger": "排程", "status": ["in", ["成功", "部分成功", "失敗"]]},
                            fields=["name", "status"], order_by="creation desc", limit=50)
    streak = []
    for row in recent:
        if row.status != "失敗":
            break
        streak.append(row.name)
    if len(streak) < 2:
        return
    from fleet_bridge.notification_dispatch import recipients
    for user in recipients():
        from frappe.utils import escape_html
        _dispatch("更新失敗", streak[-1], user, "道路情報連續更新失敗，資料可能過期",
                  escape_html(f"已連續 {len(streak)} 次排程更新失敗：{(run.error or '')[:200]}"), sync_run=run.name)


def _notify_budget(run):
    from fleet_bridge.notification_dispatch import recipients
    for user in recipients():
        _dispatch("額度暫停", _month(), user, "道路情報已暫停：本月估計點數達上限",
                  "可到「Fleet Road Info Settings」調整上限，或待下月重新計算。", sync_run=run.name)


def _publish_changed():
    from fleet_bridge.notification_dispatch import recipients
    for user in recipients():
        frappe.publish_realtime("fleet_road_events_changed", {}, user=user, after_commit=True)


# ---------------------------------------------------------------- runs

def _new_run(trigger, slot_key, requested_by=None):
    return frappe.get_doc(dict(doctype=RUN, slot_key=slot_key, run_trigger=trigger, status="排隊中",
                               requested_by=requested_by)).insert(ignore_permissions=True)


def _enqueue(run_name, after_commit=True):
    frappe.enqueue("fleet_bridge.road_events.run_sync", queue="long", timeout=1800, enqueue_after_commit=after_commit,
                   job_id=f"fleet-road-sync-{run_name}", deduplicate=True, run_name=run_name)


def _finish(run_name, status, **values):
    frappe.db.set_value(RUN, run_name, {"status": status, "finished_at": _now_str(), **values})
    frappe.db.commit()


def _claim(run_name):
    """Atomically move a run from 排隊中 to 執行中; only one worker can win, a duplicate job gets False."""
    frappe.db.sql(f"UPDATE `tab{RUN}` SET status='執行中', started_at=%s WHERE name=%s AND status='排隊中'",
                  (_now_str(), run_name))
    claimed = frappe.db.sql("SELECT ROW_COUNT()")[0][0] == 1
    frappe.db.commit()
    return claimed


def run_sync(run_name):
    """Worker job. One run at a time (MariaDB named lock); each feed fails or succeeds on its own."""
    import json
    if not _claim(run_name):
        return  # already taken by another job, finished, or reaped; leave its record alone
    run = frappe.get_doc(RUN, run_name)
    lock = f"{frappe.local.site}:{LOCK_NAME}"
    if not frappe.db.sql("SELECT GET_LOCK(%s, 0)", (lock,))[0][0]:
        _finish(run_name, "已略過", error="另一輪更新正在進行。")
        return
    RUN_USAGE.clear()
    try:
        settings = frappe.get_single(SETTINGS)
        usage_before = usage_summary(settings)
        if usage_before["exceeded"]:
            _finish(run_name, "已略過", error=f"本月估計點數 {usage_before['estimated_points']} 已達上限 {usage_before['budget']}。")
            _notify_budget(run)
            frappe.db.commit()
            return
        feeds = _enabled_feeds(settings)
        if not feeds:
            _finish(run_name, "已略過", error="沒有啟用任何資料集。")
            return
        now_str = _now_str()
        results, totals, errors, failed = [], Counter(), [], set()
        for index, feed in enumerate(feeds):
            label = core.feed_spec(feed)[1]
            if index and usage_summary(settings)["exceeded"]:
                failed.update(feeds[index:])
                errors.append(f"本月估計點數已達上限，略過：{'、'.join(core.feed_spec(f)[1] for f in feeds[index:])}")
                results += [{"feed": f, "label": core.feed_spec(f)[1], "ok": False, "error": "額度上限"} for f in feeds[index:]]
                _notify_budget(run)
                break
            try:
                items, meta = _fetch(feed)
                baseline = not frappe.db.get_global(BASELINE_KEY + feed)
                stats, invalid, complete = _apply(feed, items, meta, run_name, now_str, baseline)
                if complete and baseline:
                    frappe.db.set_global(BASELINE_KEY + feed, now_str)
                meta.update(ok=True, complete=complete, baseline=baseline and complete, invalid_items=invalid, **stats)
                if not complete:
                    failed.add(feed)  # keep its old states: no absence or expiry changes from this round
                    errors.append(f"{label}：回應無法確認完整，本輪未判定消失或結束的事件。")
                totals.update(stats)
                frappe.db.commit()
            except SyncError as error:
                frappe.db.rollback()
                failed.add(feed)
                meta = {"feed": feed, "label": label, "ok": False, "error": str(error)}
                errors.append(f"{label}：{error}")
            results.append(meta)
        totals["ended"] += _expire_sweep(run_name, now_str, failed)
        ok = [r for r in results if r.get("ok")]
        all_good = len(ok) == len(results) and all(r.get("complete") for r in ok)
        status = "成功" if all_good else "部分成功" if ok else "失敗"
        frappe.db.commit()
        notify_error = None
        try:
            notified = _notify_major(_pending_major(), run_name)
        except Exception:
            # Import outcome stands; unsent alerts stay pending (notified fingerprint not set) for the next run.
            frappe.db.rollback()
            frappe.log_error(title="Fleet road alerts failed", reference_doctype=RUN, reference_name=run_name)
            notified, notify_error = 0, "重大管制通知未送出，下一輪會重試。"
        _finish(run_name, status, new_count=totals["new"], changed_count=totals["changed"],
                ended_count=totals["ended"], unknown_count=totals["unknown"], major_count=notified or 0,
                data_calls=RUN_USAGE["data_calls"], wire_bytes=RUN_USAGE["wire_bytes"],
                error="\n".join(errors + ([notify_error] if notify_error else []))[:1000] or None,
                feeds_json=json.dumps(results, ensure_ascii=False, indent=1))
        try:
            run.reload()
            if status == "失敗":
                _notify_failure_streak(run)
            _publish_changed()
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title="Fleet road post-run notices failed", reference_doctype=RUN, reference_name=run_name)
        return
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title="Fleet road sync failed", reference_doctype=RUN, reference_name=run_name)
        _finish(run_name, "失敗", error="內部錯誤，詳見 Error Log。")
    finally:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock,))


def _reap_stale_runs():
    """Queued runs are judged by creation, running ones by their own start time."""
    now = _now()
    queued = (now - timedelta(minutes=STALE_QUEUED_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    running = (now - timedelta(minutes=STALE_RUNNING_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    names = frappe.get_all(RUN, filters={"status": "排隊中", "creation": ["<", queued]}, pluck="name")
    names += frappe.get_all(RUN, filters={"status": "執行中", "started_at": ["<", running]}, pluck="name")
    for name in names:
        _finish(name, "失敗", error="逾時未完成（背景工作可能重新啟動）。")


def _requeue_lost():
    """Queued runs whose enqueue never reached Redis get enqueued again (job_id deduplicates)."""
    since = (_now() - timedelta(minutes=REQUEUE_AFTER_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    for name in frappe.get_all(RUN, filters={"status": "排隊中", "creation": ["<", since]}, pluck="name"):
        try:
            _enqueue(name, after_commit=False)
        except Exception:
            frappe.log_error(title="Fleet road sync enqueue failed", reference_doctype=RUN, reference_name=name)


def scheduler_tick():
    """Frappe calls this every minute. A unique slot key makes each scheduled time run at most once,
    also across restarts; a missed time is caught up within 3 hours."""
    if not frappe.db.exists("DocType", SETTINGS) or not frappe.db.exists("DocType", RUN):
        return
    _reap_stale_runs()
    _requeue_lost()
    if not frappe.db.get_single_value(SETTINGS, "enabled"):
        return
    try:
        times = core.parse_schedule(frappe.db.get_single_value(SETTINGS, "schedule_times") or core.DEFAULT_SCHEDULE)
    except ValueError:
        return
    slot = core.due_slot(_now(), times)
    if not slot:
        return
    slot_key = f"排程:{slot:%Y-%m-%d %H:%M}"
    if frappe.db.exists(RUN, {"slot_key": slot_key}):
        return
    try:
        run = _new_run("排程", slot_key)
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        return
    frappe.db.commit()
    try:
        _enqueue(run.name, after_commit=False)
    except Exception:
        # The row stays 排隊中; _requeue_lost() picks it up on a later tick.
        frappe.log_error(title="Fleet road sync enqueue failed", reference_doctype=RUN, reference_name=run.name)


# ---------------------------------------------------------------- dashboard API

EVENT_FIELDS = ["name", "status", "is_major", "special_vehicle_control", "feed", "feed_label", "kind", "event_type_label",
                "title", "description", "road", "direction", "location_text", "effective_time", "expire_time",
                "source_update_time", "severity_label", "regulation_labels", "block_way_label", "blocked_lanes",
                "impact_description", "detour_description", "reporting_source", "web_url", "first_seen_at",
                "last_seen_at", "last_change_at", "last_change_run", "missing_since"]


def _plain(rows):
    return [{k: (str(v) if v is not None and not isinstance(v, (int, float, str)) else v) for k, v in row.items()}
            for row in rows]


@frappe.whitelist(methods=["GET"])
def road_overview():
    _require_manager()
    if not frappe.db.exists("DocType", SETTINGS):
        return {"installed": False, "notice": NOTICE}
    settings = frappe.get_single(SETTINGS)
    now = _now()
    configured = bool((settings.tdx_client_id or "").strip() and _secret())
    try:
        times = core.parse_schedule(settings.schedule_times or core.DEFAULT_SCHEDULE)
    except ValueError:
        times = core.parse_schedule(core.DEFAULT_SCHEDULE)
    runs = frappe.get_all(RUN, fields=["name", "run_trigger", "status", "started_at", "finished_at", "error", "new_count",
        "changed_count", "ended_count", "unknown_count", "major_count", "feeds_json", "creation"],
        order_by="creation desc", limit=8)
    # Freshness counts only runs where every feed returned a provably complete snapshot.
    success = frappe.get_all(RUN, filters={"status": "成功"}, fields=["name", "finished_at", "status"],
                             order_by="creation desc", limit=1)
    last_success = success[0] if success else None
    usage = usage_summary(settings)
    previous = core.previous_slot(now, times)
    # Give a scheduled run 20 minutes to finish before calling the data stale.
    stale = bool(settings.enabled) and bool(previous) and now - previous > timedelta(minutes=20) and (
        not last_success or get_datetime(last_success.finished_at) < previous)
    latest = runs[0] if runs else None
    from frappe.utils import get_system_timezone
    if get_system_timezone() != "Asia/Taipei":
        api_status = f"系統時區為 {get_system_timezone()}，排程與時間判斷需要 Asia/Taipei"
    elif not configured:
        api_status = "尚未設定金鑰"
    elif usage["exceeded"]:
        api_status = "本月估計點數已達上限，更新暫停"
    elif not latest:
        api_status = "尚未更新"
    elif latest.status in ("排隊中", "執行中"):
        api_status = f"更新{latest.status}"
    elif latest.status == "成功":
        api_status = "正常"
    else:
        api_status = f"最近一次{latest.status}"
    unknown_since = (now - timedelta(days=UNKNOWN_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    ended_since = (now - timedelta(days=ENDED_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    active = frappe.get_all(EVENT, filters={"status": ["in", core.ACTIVE]}, fields=EVENT_FIELDS,
                            order_by="is_major desc, effective_time desc", limit_page_length=2000)
    unknown = frappe.get_all(EVENT, filters={"status": "狀態不明", "missing_since": [">=", unknown_since]},
                             fields=EVENT_FIELDS, order_by="missing_since desc", limit_page_length=300)
    ended = frappe.get_all(EVENT, filters={"status": "已結束", "last_change_at": [">=", ended_since]},
                           fields=EVENT_FIELDS, order_by="last_change_at desc", limit_page_length=100)
    return {"installed": True, "configured": configured, "enabled": bool(settings.enabled),
            "schedule": ", ".join(f"{h:02d}:{m:02d}" for h, m in times),
            "feeds": [core.feed_spec(f)[1] for f in _enabled_feeds(settings)],
            "last_success_at": str(last_success.finished_at) if last_success else None,
            "last_success_run": last_success.name if last_success else None,
            "next_run_at": str(core.next_slot(now, times)) if settings.enabled else None,
            "api_status": api_status, "stale": stale, "usage": usage,
            "latest_run": _plain([latest])[0] if latest else None,
            "runs": _plain(runs), "active": _plain(active), "unknown": _plain(unknown), "ended": _plain(ended),
            "major_rule": core.MAJOR_RULE, "notice": NOTICE, "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "cooldown_minutes": settings.manual_cooldown_minutes or 15}


@frappe.whitelist(methods=["POST"])
def road_refresh():
    _require_manager()
    settings = frappe.get_single(SETTINGS)
    if not (settings.tdx_client_id or "").strip() or not _secret():
        frappe.throw("尚未設定 TDX 金鑰，請先到「Fleet Road Info Settings」輸入。")
    if frappe.db.exists(RUN, {"status": ["in", ["排隊中", "執行中"]]}):
        frappe.throw("已有一輪更新在排隊或執行中，請稍候。")
    cooldown = max(settings.manual_cooldown_minutes or 15, 5)
    since = (_now() - timedelta(minutes=cooldown)).strftime("%Y-%m-%d %H:%M:%S")
    if frappe.db.exists(RUN, {"run_trigger": "手動", "creation": [">=", since]}):
        frappe.throw(f"手動更新每 {cooldown} 分鐘限一次，以免耗用免費額度。")
    usage = usage_summary(settings)
    if usage["exceeded"]:
        frappe.throw(f"本月估計點數 {usage['estimated_points']} 已達上限 {usage['budget']}，暫停更新。")
    # One manual run per cooldown window: two simultaneous clicks collide on the unique slot_key.
    window = int(time.time() // (cooldown * 60))
    try:
        run = _new_run("手動", f"手動:{window}", requested_by=frappe.session.user)
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        frappe.throw(f"手動更新每 {cooldown} 分鐘限一次，以免耗用免費額度。")
    _enqueue(run.name)
    return {"run": run.name}
