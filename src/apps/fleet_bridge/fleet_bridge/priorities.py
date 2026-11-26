"""Three-level reminder priority with automatic, immutable server-side audit."""
import json
import re
import uuid

import frappe
from frappe.utils import get_datetime

from fleet_bridge.domain import operation_name


PRIORITIES = ("高優先", "一般", "低優先")
SUPPORTED_DOCTYPES = ("Truck", "Trailers", "Fleet Legacy Asset")


def reminder_priority(doc):
    value = doc.get("custom_reminder_priority")
    if value in PRIORITIES:
        return value
    return "低優先" if doc.get("custom_low_priority") else "一般"


def _request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", value):
        frappe.throw("INVALID_REQUEST_ID")
    return value


def validate_priority(doc, method=None):
    raw = doc.get("custom_reminder_priority")
    if raw not in (None, "") and raw not in PRIORITIES:
        frappe.throw("INVALID_PRIORITY")
    value = reminder_priority(doc)
    doc.custom_reminder_priority = value
    # Keep the old boolean field consistent for cached clients during migration.
    doc.custom_low_priority = int(value == "低優先")


def audit_priority(doc, method=None):
    old = doc.get_doc_before_save()
    if not old:
        return
    before, after = reminder_priority(old), reminder_priority(doc)
    if before == after:
        return
    context = frappe.flags.get("fleet_priority_write") or {}
    request_id = context.get("request_id") or str(uuid.uuid4())
    payload = context.get("request_payload") or json.dumps({
        "vehicle_doctype": doc.doctype, "vehicle_name": doc.name,
        "priority": after, "expected_version": str(old.modified),
    }, ensure_ascii=False, sort_keys=True)
    append_log(doc, before, request_id, context.get("expected_version") or str(old.modified),
               context.get("source") or "ERP UI/API", payload)


def append_log(doc, before, request_id, expected_version, source, request_payload):
    operation_id = operation_name(frappe.session.user, request_id)
    after = reminder_priority(doc)
    response = {
        "operation_id": operation_id,
        "vehicle_id": doc.name,
        "vehicle_doctype": doc.doctype,
        "before_priority": before,
        "reminder_priority": after,
        "version": str(doc.modified),
        "actor": frappe.session.user,
        "actor_type": "service_account" if source.startswith("Fleet MCP") else "user_account",
        "source": source,
        "replayed": False,
    }
    frappe.flags.fleet_log_insert = True
    try:
        log = frappe.get_doc({
            "doctype": "Fleet Priority Log", "operation_id": operation_id,
            "request_id": request_id, "vehicle_doctype": doc.doctype,
            "vehicle_name": doc.name, "actor": frappe.session.user, "source": source,
            "before_priority": before, "after_priority": after,
            "request_payload": request_payload,
            "result_json": json.dumps(response, ensure_ascii=False),
            "expected_version": expected_version, "result_version": str(doc.modified),
        }).insert(ignore_permissions=True)
    finally:
        frappe.flags.fleet_log_insert = False
    for user in set(frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent")) | {"Administrator"}:
        frappe.publish_realtime("fleet_reminders_changed", {}, user=user, after_commit=True)
    return log


def _dashboard_doc(asset_id):
    if frappe.session.user == "Guest" or "System Manager" not in frappe.get_roles():
        frappe.throw("無權修改提醒優先度。", frappe.PermissionError)
    if asset_id.startswith(("Truck:", "Trailers:")):
        doctype, name = asset_id.split(":", 1)
        doc = frappe.get_doc(doctype, name)
        if doc.get("custom_legacy_asset"):
            frappe.throw("請從來源主檔選取車輛。")
    else:
        asset = frappe.get_doc("Fleet Legacy Asset", asset_id)
        asset.check_permission("read")
        doc = frappe.get_doc("Truck", asset.truck) if asset.truck else frappe.get_doc("Trailers", asset.trailer) if asset.trailer else asset
        if doc != asset and doc.get("custom_legacy_asset") != asset.name:
            frappe.throw("車籍來源連結不一致。")
    doc.check_permission("write")
    return doc


def _mcp_doc(vehicle_id):
    from fleet_bridge.api import authorize
    return authorize(vehicle_id).doc


def _result(log, replayed=False):
    result = json.loads(log.result_json)
    result["replayed"] = replayed
    return result


def _update(doc, priority, expected_version, request_id, source, public_vehicle_id):
    if priority not in PRIORITIES:
        frappe.throw("INVALID_PRIORITY")
    request_id = _request_id(request_id)
    payload = json.dumps({"vehicle_id": public_vehicle_id, "vehicle_doctype": doc.doctype,
        "vehicle_name": doc.name, "priority": priority, "expected_version": expected_version},
        ensure_ascii=False, sort_keys=True)
    if doc.doctype not in SUPPORTED_DOCTYPES:
        frappe.throw("UNSUPPORTED_VEHICLE_TYPE")
    frappe.db.sql(f"SELECT name FROM `tab{doc.doctype}` WHERE name=%s FOR UPDATE", (doc.name,))
    op_id = operation_name(frappe.session.user, request_id)
    if frappe.db.exists("Fleet Priority Log", op_id):
        log = frappe.get_doc("Fleet Priority Log", op_id)
        if log.request_payload != payload:
            frappe.throw("IDEMPOTENCY_CONFLICT")
        return _result(log, True)
    doc = frappe.get_doc(doc.doctype, doc.name)
    if get_datetime(expected_version) != get_datetime(doc.modified):
        frappe.throw("VERSION_CONFLICT", frappe.TimestampMismatchError)
    before = reminder_priority(doc)
    context = {"request_id": request_id, "expected_version": expected_version,
               "request_payload": payload, "source": source}
    if before == priority and bool(doc.get("custom_low_priority")) == (priority == "低優先"):
        log = append_log(doc, before, request_id, expected_version, source + " (no change)", payload)
    else:
        doc.custom_reminder_priority = priority
        doc.custom_low_priority = int(priority == "低優先")
        frappe.flags.fleet_priority_write = context
        try:
            doc.save(ignore_permissions=source == "Fleet MCP")
        finally:
            frappe.flags.fleet_priority_write = None
        log = frappe.get_doc("Fleet Priority Log", op_id)
    return _result(log)


@frappe.whitelist(methods=["POST"])
def dashboard_priority_update(asset_id, priority, expected_version, request_id):
    return _update(_dashboard_doc(asset_id), priority, expected_version, request_id,
                   "Fleet GUI", asset_id)


@frappe.whitelist(methods=["POST"])
def vehicle_priority_update(vehicle_id, priority, expected_version, request_id):
    return _update(_mcp_doc(vehicle_id), priority, expected_version, request_id,
                   "Fleet MCP", vehicle_id)


def _history(doc, limit):
    rows = frappe.get_all("Fleet Priority Log", filters={"vehicle_doctype": doc.doctype, "vehicle_name": doc.name},
        fields=["name", "actor", "source", "before_priority", "after_priority", "creation", "result_version"],
        order_by="creation desc, name desc", limit_page_length=max(1, min(int(limit), 100)))
    return {"vehicle_id": doc.name, "vehicle_doctype": doc.doctype,
            "entries": [{"operation_id": row.name, "actor": row.actor, "source": row.source,
                "before_priority": row.before_priority, "reminder_priority": row.after_priority,
                "at": str(row.creation), "version": row.result_version} for row in rows]}


@frappe.whitelist(methods=["GET"])
def vehicle_priority_get_history(vehicle_id, limit=20):
    return _history(_mcp_doc(vehicle_id), limit)


@frappe.whitelist(methods=["GET"])
def dashboard_priority_history(asset_id, limit=20):
    return _history(_dashboard_doc(asset_id), limit)


def install():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({dt: [
        {"fieldname": "custom_low_priority", "label": "低優先提醒（相容欄位）",
         "fieldtype": "Check", "default": "0", "hidden": 1,
         "description": "保留舊版資料相容；請改用提醒優先度。"},
        {"fieldname": "custom_reminder_priority", "label": "提醒優先度", "fieldtype": "Select",
         "options": "一般\n高優先\n低優先", "default": "一般",
         "description": "高優先醒目顯示並排前；低優先可隱藏。所有修改由伺服器自動留下稽核紀錄。"},
    ] for dt in SUPPORTED_DOCTYPES})
    if frappe.db.exists("DocType", "Fleet Priority Log"):
        return
    fields = [dict(fieldname=n, label=label, fieldtype=kind, **extra) for n, label, kind, extra in [
        ("operation_id", "操作編號", "Data", {"unique": 1, "reqd": 1}),
        ("request_id", "請求編號", "Data", {"reqd": 1}),
        ("vehicle_doctype", "車籍類型", "Data", {"reqd": 1}),
        ("vehicle_name", "車籍名稱", "Data", {"reqd": 1, "in_list_view": 1}),
        ("actor", "執行帳號", "Data", {"reqd": 1}),
        ("source", "操作來源", "Data", {"reqd": 1}),
        ("before_priority", "修改前優先度", "Data", {}),
        ("after_priority", "修改後優先度", "Data", {"reqd": 1}),
        ("request_payload", "請求內容", "Long Text", {}),
        ("result_json", "操作結果", "Long Text", {}),
        ("expected_version", "讀取版本", "Data", {}),
        ("result_version", "結果版本", "Data", {}),
    ]]
    frappe.get_doc(dict(doctype="DocType", name="Fleet Priority Log", module="Fleet Bridge", custom=1,
        autoname="field:operation_id", track_changes=1, fields=fields,
        permissions=[dict(role="System Manager", read=1)])).insert()
