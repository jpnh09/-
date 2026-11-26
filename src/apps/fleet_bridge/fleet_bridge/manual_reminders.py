"""Reminders added by hand in the dashboard; kept apart from migrated legacy records."""
import json
import re

import frappe
from frappe.utils import get_datetime
from fleet_bridge.reminders import validate_changes
from fleet_bridge.vehicle_notes import _dashboard_doc

DOCTYPE = "Fleet Manual Reminder"
CATEGORIES = ("保險", "通行證", "驗車", "其他提醒")
FIELDS = ("category", "description", "due_date", "notification_at", "notes")


def normalize(values):
    if isinstance(values, str):
        values = json.loads(values)
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        frappe.throw("提醒欄位不完整。")
    values = dict(values)
    if values["category"] not in CATEGORIES:
        frappe.throw("提醒類別無效。")
    for key in ("description", "notes"):
        values[key] = (values[key] or "").replace("\r\n", "\n").replace("\r", "\n")
    values["description"] = values["description"].strip()
    values["notification_at"] = values["notification_at"] or None
    if not values["due_date"]:
        frappe.throw("請填寫提醒日期。")
    validate_changes({k: values[k] for k in ("description", "due_date", "notes", "notification_at")})
    return values


def _request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", value):
        frappe.throw("INVALID_REQUEST_ID")
    return value


def _plate(asset_id):
    if asset_id.startswith(("Truck:", "Trailers:")):
        doctype, name = asset_id.split(":", 1)
        field = "license_plate" if doctype == "Truck" else "plate_number"
        return frappe.db.get_value(doctype, name, field) or name
    return frappe.db.get_value("Fleet Legacy Asset", asset_id, "display_name") or asset_id


def _authorized_row(reminder_id, expected_version):
    row = frappe.get_doc(DOCTYPE, reminder_id)
    _dashboard_doc(row.vehicle_key)
    row.check_permission("write")
    if get_datetime(expected_version) != get_datetime(row.modified):
        frappe.throw("VERSION_CONFLICT", frappe.TimestampMismatchError)
    return row


def _notify():
    for user in set(frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent")) | {"Administrator"}:
        frappe.publish_realtime("fleet_reminders_changed", {}, user=user, after_commit=True)


def _values(row):
    return {"category": row.category, "description": row.description,
            "due_date": str(row.due_date) if row.due_date else None,
            "notification_at": str(row.notification_at) if row.notification_at else None,
            "notes": row.notes or ""}


@frappe.whitelist(methods=["POST"])
def dashboard_manual_create(asset_id, values, request_id):
    _dashboard_doc(asset_id)
    values, request_id = normalize(values), _request_id(request_id)
    # A retried request returns the reminder it already created instead of adding a duplicate.
    existing = frappe.db.get_value(DOCTYPE, {"request_id": request_id}, "name")
    if existing:
        row = frappe.get_doc(DOCTYPE, existing)
        if row.vehicle_key != asset_id or _values(row) != values:
            frappe.throw("IDEMPOTENCY_CONFLICT")
        return {"reminder_id": row.name, "version": str(row.modified), "replayed": True}
    row = frappe.get_doc({"doctype": DOCTYPE, "vehicle_key": asset_id, "plate": _plate(asset_id),
                          "request_id": request_id, **values})
    row.insert()
    _notify()
    return {"reminder_id": row.name, "version": str(row.modified), "replayed": False}


@frappe.whitelist(methods=["POST"])
def dashboard_manual_update(reminder_id, values, expected_version):
    row = _authorized_row(reminder_id, expected_version)
    row.update(normalize(values))
    row.save()
    _notify()
    return {"reminder_id": row.name, "version": str(row.modified)}


@frappe.whitelist(methods=["POST"])
def dashboard_manual_delete(reminder_id, expected_version):
    row = _authorized_row(reminder_id, expected_version)
    frappe.delete_doc(DOCTYPE, row.name)
    _notify()
    return {"reminder_id": reminder_id, "deleted": True}


def reminders_by_vehicle():
    """Dashboard projection; caller has already checked the System Manager role."""
    groups = {}
    if not frappe.db.exists("DocType", DOCTYPE):
        return groups
    for row in frappe.get_list(DOCTYPE, fields=["name", "vehicle_key", *FIELDS, "modified"],
                               order_by="due_date asc, name asc", limit_page_length=0):
        recorded = str(row.due_date) if row.due_date else None
        notification_at = str(row.notification_at) if row.notification_at else None
        groups.setdefault(row.vehicle_key, []).append({
            "record_id": row.name, "manual": True, "category": row.category,
            "description": row.description, "date": None, "notes": row.notes or "", "legacy_tags": [],
            "recorded_due_date": recorded, "notification_at": notification_at,
            "effective_date": notification_at[:10] if notification_at else recorded,
            "notification_timezone": "Asia/Taipei", "version": str(row.modified), "date_type": "提醒日期"})
    return groups


def install():
    if not frappe.db.exists("DocType", DOCTYPE):
        fields = [dict(fieldname=n, label=label, fieldtype=kind, **extra) for n, label, kind, extra in [
            ("vehicle_key", "車輛", "Data", {"reqd": 1, "in_list_view": 1, "read_only": 1,
                "description": "車隊首頁的車輛識別碼；請由車隊首頁新增。"}),
            ("plate", "車牌", "Data", {"in_list_view": 1, "read_only": 1}),
            ("category", "提醒類別", "Select", {"options": "\n".join(CATEGORIES), "reqd": 1, "in_list_view": 1}),
            ("description", "提醒名稱", "Data", {"reqd": 1, "length": 140, "in_list_view": 1}),
            ("due_date", "提醒日期", "Date", {"reqd": 1, "in_list_view": 1}),
            ("notification_at", "預定通知時間（台北）", "Datetime", {"description": "未設定則不排程通知。"}),
            ("notes", "備註", "Long Text", {}),
            ("request_id", "請求編號", "Data", {"unique": 1, "read_only": 1, "hidden": 1})]]
        frappe.get_doc(dict(doctype="DocType", name=DOCTYPE, module="Fleet Bridge", custom=1,
            autoname="MR-.#####", track_changes=1, title_field="description", fields=fields,
            permissions=[dict(role="System Manager", read=1, write=1, create=1, delete=1)])).insert()
    # Let the per-minute alert job record manual reminders beside legacy ones.
    dispatch = frappe.get_doc("DocType", "Fleet Notification Dispatch")
    changed = False
    for field in dispatch.fields:
        if field.fieldname == "reminder" and field.reqd:
            field.reqd, changed = 0, True
    if not any(f.fieldname == "manual_reminder" for f in dispatch.fields):
        dispatch.append("fields", {"fieldname": "manual_reminder", "label": "手動提醒",
                                   "fieldtype": "Data", "insert_after": "reminder"})
        changed = True
    if changed:
        dispatch.save()
