"""Read a vehicle's linked migration records after the API authorizes its identity."""
import json

import frappe
from fleet_bridge.priorities import reminder_priority


LINK_FIELDS = {"Truck": "truck", "Trailers": "trailer"}


def vehicle_profile(doc):
    """doc is an authorized Truck, Trailers, or archive-only Fleet Legacy Asset (see api.resolve_vehicle)."""
    if doc.doctype == "Fleet Legacy Asset":
        result = base_profile(doc)
        raw = json.loads(doc.raw_json)
        result.update({"make": raw.get("make"), "model": raw.get("model"),
                       "asset_kind": doc.asset_kind, "has_erp_master": False})
        return asset_profile(doc, result)
    result = base_profile(doc)
    asset_id = doc.get("custom_legacy_asset")
    if not asset_id:
        result["source_note"] = "此車沒有連結舊系統資料。"
        return result
    asset = frappe.get_doc("Fleet Legacy Asset", asset_id)
    # A marked test copy can reference an authorized original; arbitrary cross-links cannot.
    if asset.get(LINK_FIELDS[doc.doctype]) != doc.name:
        if doc.doctype != "Truck" or not result["is_migration_test"] or asset.truck not in (frappe.conf.get("fleet_allowed_vehicles") or []):
            frappe.throw("Legacy vehicle link mismatch", frappe.PermissionError)
        original = frappe.get_doc("Truck", asset.truck)
        original.check_permission("read")
        if original.get("custom_legacy_asset") != asset_id:
            frappe.throw("Legacy source link mismatch", frappe.PermissionError)
    return asset_profile(asset, result)


def base_profile(doc):
    return {
        "make": doc.get("make") or doc.get("trailer_make") or None,
        "model": doc.get("model") or None,
        "source_year": None,
        "fuel_type": doc.get("fuel_type") or None,
        "is_migration_test": bool(doc.get("custom_is_migration_test")),
        "source": None,
        "legacy_tags": [],
        "reminders": [],
        "historical_records": [],
        "notes": [],
        "general_note": doc.get("custom_general_note") or "",
        "reminder_priority": reminder_priority(doc),
        "interpretation": "營運狀態待確認不代表其他車籍資料缺失。版本僅供更新衝突檢查，不是車籍查證日期。空白紀錄表示本次匯入未提供，不代表現實中不存在。",
    }


def asset_profile(asset, result=None, rows=None):
    """Shared projection. Caller MUST authorize the asset or linked vehicle first."""
    asset_id = asset.name
    raw = json.loads(asset.raw_json)
    if result is None:
        result = {"make": raw.get("make"), "model": raw.get("model"),
                  "reminders": [], "historical_records": [], "notes": []}
    year = raw.get("year")
    result.update({
        "source_year": year if isinstance(year, int) and year > 1900 else None,
        "legacy_tags": list(dict.fromkeys(raw.get("tags") or [])),
        "source": {"system": "LubeLogger", "asset_id": asset_id,
                   "original_license_plate": raw.get("licensePlate"), "batch_id": asset.batch_id},
        "source_note": "舊系統匯入資料；年份沿用原欄位，標籤不等於已確認司機或所有權。提醒日期與保單、通行證效力尚未重新核驗；通用名稱不可猜測證件類別。",
        "review_notes": asset.review_notes,
    })
    # No general permission on the legacy archive is granted to the AI account.
    if rows is None:
        rows = frappe.get_all("Fleet Legacy Record", filters={"asset": asset_id},
            fields=["name", "record_kind", "description", "record_date", "due_date", "notes", "custom_notification_at", "raw_json", "modified"],
            order_by="due_date asc, name asc", limit_page_length=0)
    for row in rows:
        source = json.loads(row.raw_json)
        if str(source.get("vehicleId")) != str(asset.source_id):
            frappe.throw("Legacy record vehicle mismatch", frappe.PermissionError)
        item = {"record_id": row.name, "description": row.description,
                "date": str(row.record_date) if row.record_date else None,
                "notes": row.notes or "", "legacy_tags": source.get("tags") or []}
        if row.record_kind == "reminders":
            recorded_date = str(row.due_date) if row.due_date else None
            notification_at = str(row.custom_notification_at) if row.custom_notification_at else None
            item.update({"recorded_due_date": recorded_date,
                         "notification_at": notification_at,
                         "effective_date": notification_at[:10] if notification_at else recorded_date,
                         "notification_timezone": "Asia/Taipei",
                         "version": str(row.modified),
                         "metric": source.get("metric"), "due_odometer": source.get("dueOdometer"),
                         "date_meaning": "依原名稱解讀；例如驗車開始是開始日，不是到期日。"})
            result["reminders"].append(item)
        elif row.record_kind == "notes":
            result["notes"].append(item)
        else:
            item.update({"source_kind": row.record_kind, "source_cost": source.get("cost"),
                         "currency": None, "cost_note": "沿用舊系統金額；未提供幣別，不推測。"})
            result["historical_records"].append(item)
    result["record_counts"] = {key: len(result[key]) for key in ("reminders", "historical_records", "notes")}
    return result
