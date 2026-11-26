"""Audited whole-vehicle notes shared by the dashboard and Fleet MCP."""
import json
import re

import frappe
from frappe.utils import get_datetime
from fleet_bridge.domain import operation_name


def normalize_note(note):
    if not isinstance(note, str):
        frappe.throw("通用備註必須是文字。")
    note = note.replace("\r\n", "\n").replace("\r", "\n")
    if len(note) > 10000:
        frappe.throw("通用備註不可超過 10000 字。")
    return note


def _request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", value):
        frappe.throw("INVALID_REQUEST_ID")
    return value


def _dashboard_doc(asset_id):
    if frappe.session.user == "Guest" or "System Manager" not in frappe.get_roles():
        frappe.throw("無權修改車輛通用備註。", frappe.PermissionError)
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


def _result(doc):
    return {"vehicle_id": doc.name, "vehicle_doctype": doc.doctype,
            "general_note": doc.get("custom_general_note") or "", "version": str(doc.modified),
            "modified_by": doc.modified_by}


def _update(doc, note, expected_version, request_id, source, vehicle_id):
    note, request_id = normalize_note(note), _request_id(request_id)
    payload = json.dumps({"vehicle_id": vehicle_id, "note": note, "expected_version": expected_version},
                         ensure_ascii=False, sort_keys=True)
    frappe.db.sql(f"SELECT name FROM `tab{doc.doctype}` WHERE name=%s FOR UPDATE", (doc.name,))
    op_id = operation_name(frappe.session.user, request_id)
    if frappe.db.exists("Fleet Vehicle Note Log", op_id):
        log = frappe.get_doc("Fleet Vehicle Note Log", op_id)
        if log.request_payload != payload:
            frappe.throw("IDEMPOTENCY_CONFLICT")
        return {**json.loads(log.result_json), "replayed": True}
    doc = frappe.get_doc(doc.doctype, doc.name)
    if get_datetime(expected_version) != get_datetime(doc.modified):
        frappe.throw("VERSION_CONFLICT", frappe.TimestampMismatchError)
    before = doc.get("custom_general_note") or ""
    doc.custom_general_note = note
    if source == "Fleet MCP":
        frappe.flags.fleet_note_write = {"vehicle_id": vehicle_id}
    try:
        doc.save(ignore_permissions=True)
    finally:
        if source == "Fleet MCP":
            frappe.flags.fleet_note_write = None
    response = {"operation_id": op_id, "vehicle_id": vehicle_id, "before_note": before,
                "general_note": note, "version": str(doc.modified), "actor": frappe.session.user,
                "actor_type": "service_account" if source == "Fleet MCP" else "user_account",
                "source": source, "replayed": False}
    frappe.flags.fleet_log_insert = True
    try:
        frappe.get_doc({"doctype": "Fleet Vehicle Note Log", "operation_id": op_id,
            "request_id": request_id, "vehicle_doctype": doc.doctype, "vehicle_name": doc.name,
            "actor": frappe.session.user, "source": source, "before_note": before, "after_note": note,
            "request_payload": payload, "result_json": json.dumps(response, ensure_ascii=False),
            "expected_version": expected_version, "result_version": str(doc.modified)}).insert(ignore_permissions=True)
    finally:
        frappe.flags.fleet_log_insert = False
    for user in set(frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent")) | {"Administrator"}:
        frappe.publish_realtime("fleet_reminders_changed", {}, user=user, after_commit=True)
    return response


@frappe.whitelist(methods=["POST"])
def dashboard_note_update(asset_id, note, expected_version, request_id):
    return _update(_dashboard_doc(asset_id), note, expected_version, request_id, "Fleet GUI", asset_id)


@frappe.whitelist(methods=["POST"])
def vehicle_note_update(vehicle_id, note, expected_version, request_id):
    return _update(_mcp_doc(vehicle_id), note, expected_version, request_id, "Fleet MCP", vehicle_id)


@frappe.whitelist(methods=["GET"])
def vehicle_note_get_history(vehicle_id, limit=20):
    doc = _mcp_doc(vehicle_id)
    rows = frappe.get_all("Fleet Vehicle Note Log",
        filters={"vehicle_doctype": doc.doctype, "vehicle_name": doc.name},
        fields=["name", "actor", "source", "before_note", "after_note", "creation", "result_version"],
        order_by="creation desc, name desc", limit_page_length=max(1, min(int(limit), 100)))
    return {"vehicle_id": vehicle_id, "entries": [{"operation_id": r.name, "actor": r.actor,
        "source": r.source, "before_note": r.before_note or "", "general_note": r.after_note or "",
        "at": str(r.creation), "version": r.result_version} for r in rows]}


def install():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({dt: [{"fieldname": "custom_general_note", "label": "車輛通用備註",
        "fieldtype": "Long Text", "read_only": 1,
        "description": "整台車共用；請由車隊首頁或受限 MCP 修改。"}]
        for dt in ("Truck", "Trailers", "Fleet Legacy Asset")})
    if frappe.db.exists("DocType", "Fleet Vehicle Note Log"):
        return
    fields = [dict(fieldname=n, label=label, fieldtype=kind, **extra) for n, label, kind, extra in [
        ("operation_id", "操作編號", "Data", {"unique": 1, "reqd": 1}),
        ("request_id", "請求編號", "Data", {"reqd": 1}),
        ("vehicle_doctype", "車籍類型", "Data", {"reqd": 1}),
        ("vehicle_name", "車籍名稱", "Data", {"reqd": 1, "in_list_view": 1}),
        ("actor", "執行帳號", "Data", {"reqd": 1}), ("source", "操作來源", "Data", {"reqd": 1}),
        ("before_note", "修改前備註", "Long Text", {}), ("after_note", "修改後備註", "Long Text", {}),
        ("request_payload", "請求內容", "Long Text", {}), ("result_json", "操作結果", "Long Text", {}),
        ("expected_version", "讀取版本", "Data", {}), ("result_version", "結果版本", "Data", {})]]
    frappe.get_doc(dict(doctype="DocType", name="Fleet Vehicle Note Log", module="Fleet Bridge", custom=1,
        autoname="field:operation_id", track_changes=1, fields=fields,
        permissions=[dict(role="System Manager", read=1)])).insert()
