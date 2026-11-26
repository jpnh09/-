"""One source of truth for reminder edits from the GUI and restricted MCP."""
import json
import re
import uuid
from datetime import date, datetime

import frappe
from frappe.utils import get_datetime
from fleet_bridge.domain import operation_name

EDITABLE = ("description", "due_date", "notes", "notification_at")
FIELD_BY_KEY = {"notification_at": "custom_notification_at"}
EDITABLE_FIELDS = {FIELD_BY_KEY.get(key, key) for key in EDITABLE}
PROTECTED = ("asset", "record_kind", "source_id", "batch_id", "raw_json", "payload_sha256")


def snapshot(doc):
    return {"description": doc.description or "", "due_date": str(doc.due_date) if doc.due_date else None,
            "notes": doc.notes or "", "notification_at": str(doc.get("custom_notification_at")) if doc.get("custom_notification_at") else None}


def validate_changes(changes):
    if isinstance(changes, str):
        changes = json.loads(changes)
    if not isinstance(changes, dict) or not changes or set(changes) - set(EDITABLE):
        frappe.throw("Only description, due_date, notes and notification_at may change")
    for key in ("description", "notes"):
        if key in changes and (not isinstance(changes[key], str) or len(changes[key]) > (140 if key == "description" else 10000)):
            frappe.throw("Invalid " + key)
    if "description" in changes and not changes["description"].strip():
        frappe.throw("Reminder description cannot be empty")
    if "due_date" in changes and changes["due_date"] is not None:
        value = changes["due_date"]
        try:
            if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d{3}-\d{2}-\d{2}", value):
                raise ValueError()
            date.fromisoformat(value)
        except ValueError:
            frappe.throw("due_date must be a valid YYYY-MM-DD date or null")
    if "notification_at" in changes and changes["notification_at"] is not None:
        value = changes["notification_at"]
        try:
            if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d{3}-\d{2}-\d{2} [0-2]\d:[0-5]\d:[0-5]\d", value):
                raise ValueError()
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            if parsed.strftime("%Y-%m-%d %H:%M:%S") != value:
                raise ValueError()
        except ValueError:
            frappe.throw("notification_at must be a valid Asia/Taipei YYYY-MM-DD HH:MM:SS datetime or null")
    return changes


def authorize(vehicle_id, reminder_id):
    from fleet_bridge.api import authorize as authorize_vehicle
    ref = authorize_vehicle(vehicle_id)
    row = frappe.get_doc("Fleet Legacy Record", reminder_id)
    asset = frappe.get_doc("Fleet Legacy Asset", row.asset)
    if ref.doctype == "Fleet Legacy Asset":
        linked = asset.name == ref.name
    else:
        link_field = "truck" if ref.doctype == "Truck" else "trailer"
        linked = asset.get(link_field) == ref.name and ref.doc.get("custom_legacy_asset") == asset.name
    if row.record_kind != "reminders" or not linked:
        frappe.throw("Reminder is not linked to this authorized vehicle", frappe.PermissionError)
    return row


def result(row):
    return {"reminder_id": row.name, **snapshot(row), "version": str(row.modified),
            "modified_by": row.modified_by, "verification": "待核驗", "calendar_uses_same_record": True,
            "notification_timezone": "Asia/Taipei"}


@frappe.whitelist(methods=["GET"])
def reminder_get(vehicle_id, reminder_id):
    return result(authorize(vehicle_id, reminder_id))


@frappe.whitelist(methods=["POST"])
def reminder_update(vehicle_id, reminder_id, changes, expected_version, request_id):
    return _update(vehicle_id, reminder_id, changes, expected_version, request_id, authorize, "Fleet MCP")


def authorize_manager(asset_id, reminder_id):
    if frappe.session.user == "Guest" or "System Manager" not in frappe.get_roles():
        frappe.throw("車隊提醒修改限管理員。", frappe.PermissionError)
    row = frappe.get_doc("Fleet Legacy Record", reminder_id)
    if row.record_kind != "reminders" or row.asset != asset_id:
        frappe.throw("提醒與所選車輛不符。", frappe.PermissionError)
    row.check_permission("read")
    row.check_permission("write")
    return row


@frappe.whitelist(methods=["POST"])
def dashboard_notification_update(asset_id, reminder_id, notification_at, expected_version, request_id):
    value = notification_at or None
    return _update(asset_id, reminder_id, {"notification_at": value}, expected_version,
                   request_id, authorize_manager, "Fleet dashboard")


def _update(vehicle_id, reminder_id, changes, expected_version, request_id, authorize_fn, source):
    authorize_fn(vehicle_id, reminder_id)
    changes = validate_changes(changes)
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id):
        frappe.throw("INVALID_REQUEST_ID")
    request = json.dumps({"vehicle_id": vehicle_id, "reminder_id": reminder_id, "changes": changes,
        "expected_version": expected_version}, ensure_ascii=False, sort_keys=True)
    frappe.db.sql("SELECT name FROM `tabFleet Legacy Record` WHERE name=%s FOR UPDATE", (reminder_id,))
    op_id = operation_name(frappe.session.user, request_id)
    if frappe.db.exists("Fleet Reminder Log", op_id):
        log = frappe.get_doc("Fleet Reminder Log", op_id)
        if log.request_payload != request:
            frappe.throw("IDEMPOTENCY_CONFLICT")
        return {**json.loads(log.result_json), "replayed": True}
    row = authorize_fn(vehicle_id, reminder_id)
    if get_datetime(expected_version) != get_datetime(row.modified):
        frappe.throw("VERSION_CONFLICT", frappe.TimestampMismatchError)
    before = snapshot(row)
    frappe.flags.fleet_reminder_write = {"reminder_id": reminder_id, "request_id": request_id,
        "request_payload": request, "expected_version": expected_version, "source": source}
    try:
        row.update({FIELD_BY_KEY.get(key, key): value for key, value in changes.items()})
        if snapshot(row) == before:
            append_log(row, before, frappe.flags.fleet_reminder_write, source + " (no change)")
        else:
            row.save(ignore_permissions=True)
        return json.loads(frappe.get_doc("Fleet Reminder Log", op_id).result_json)
    finally:
        frappe.flags.fleet_reminder_write = None


@frappe.whitelist(methods=["GET"])
def reminder_get_history(vehicle_id, reminder_id, limit=20):
    authorize(vehicle_id, reminder_id)
    rows = frappe.get_all("Fleet Reminder Log", filters={"reminder": reminder_id},
        fields=["name", "actor", "creation", "source", "before_json", "after_json"],
        order_by="creation desc, name desc", limit_page_length=max(1, min(int(limit), 100)))
    return {"reminder_id": reminder_id, "entries": [{"operation_id": r.name, "actor": r.actor,
        "at": str(r.creation), "source": r.source, "before": json.loads(r.before_json),
        "after": json.loads(r.after_json)} for r in rows]}


def validate_record(doc, method=None):
    old = doc.get_doc_before_save()
    if old:
        for field in PROTECTED:
            if old.get(field) != doc.get(field):
                frappe.throw("Original migration provenance cannot be changed: " + field)
    if frappe.session.user == frappe.conf.get("fleet_ai_user"):
        context = frappe.flags.get("fleet_reminder_write") or {}
        if doc.is_new() or context.get("reminder_id") != doc.name:
            frappe.throw("Use the restricted reminder endpoint", frappe.PermissionError)
        for field in doc.meta.fields:
            if field.fieldtype not in frappe.model.no_value_fields and field.fieldname not in EDITABLE_FIELDS:
                if old.get(field.fieldname) != doc.get(field.fieldname):
                    frappe.throw("Only reminder content may change", frappe.PermissionError)
    if doc.record_kind == "reminders":
        validate_changes(snapshot(doc))


def audit_record(doc, method=None):
    old = doc.get_doc_before_save()
    if not old or doc.record_kind != "reminders" or snapshot(old) == snapshot(doc):
        return
    context = frappe.flags.get("fleet_reminder_write") or {"request_id": str(uuid.uuid4()),
        "expected_version": str(old.modified), "request_payload": ""}
    append_log(doc, snapshot(old), context,
               context.get("source", "Fleet MCP") if frappe.flags.get("fleet_reminder_write") else "ERP UI/API")


def append_log(doc, before, context, source):
    operation_id = operation_name(frappe.session.user, context["request_id"])
    response = {"operation_id": operation_id, "before": before, "after": snapshot(doc),
        "reminder": result(doc), "actor": frappe.session.user, "replayed": False,
        "message": "已更新原始提醒；車籍與行事曆共用此筆資料，不需再次修改行事曆。"}
    frappe.flags.fleet_log_insert = True
    try:
        frappe.get_doc({"doctype": "Fleet Reminder Log", "operation_id": operation_id,
            "request_id": context["request_id"], "reminder": doc.name, "actor": frappe.session.user,
            "source": source, "before_json": json.dumps(before, ensure_ascii=False),
            "after_json": json.dumps(snapshot(doc), ensure_ascii=False),
            "request_payload": context["request_payload"], "result_json": json.dumps(response, ensure_ascii=False),
            "expected_version": context["expected_version"], "result_version": str(doc.modified)}).insert(ignore_permissions=True)
    finally:
        frappe.flags.fleet_log_insert = False
    # Notify authorized dashboard users only after the data and audit commit together.
    for user in set(frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent")) | {"Administrator"}:
        frappe.publish_realtime("fleet_reminders_changed", {}, user=user, after_commit=True)


def install():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({"Fleet Legacy Record": [{"fieldname": "custom_notification_at",
        "label": "預定通知時間（台北）", "fieldtype": "Datetime", "insert_after": "due_date",
        "description": "此時間獨立於原記錄日期；未設定則不排程通知。"}]})
    if frappe.db.exists("DocType", "Fleet Reminder Log"):
        return
    fields = [dict(fieldname=n, label=label, fieldtype=kind, **extra) for n, label, kind, extra in [
        ("operation_id", "操作編號", "Data", {"unique": 1, "reqd": 1}),
        ("request_id", "請求編號", "Data", {"reqd": 1}),
        ("reminder", "提醒", "Link", {"options": "Fleet Legacy Record", "reqd": 1, "in_list_view": 1}),
        ("actor", "執行帳號", "Data", {"reqd": 1}), ("source", "操作來源", "Data", {"reqd": 1}),
        ("before_json", "修改前", "Long Text", {}), ("after_json", "修改後", "Long Text", {}),
        ("request_payload", "請求內容", "Long Text", {}), ("result_json", "操作結果", "Long Text", {}),
        ("expected_version", "讀取版本", "Data", {}), ("result_version", "結果版本", "Data", {})]]
    frappe.get_doc(dict(doctype="DocType", name="Fleet Reminder Log", module="Fleet Bridge", custom=1,
        autoname="field:operation_id", track_changes=1, fields=fields,
        permissions=[dict(role="System Manager", read=1)])).insert()
