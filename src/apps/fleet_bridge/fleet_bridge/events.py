import uuid
import frappe
from fleet_bridge.domain import STATUSES, operation_name


def validate_truck(doc, method=None):
    from fleet_bridge.priorities import validate_priority
    validate_priority(doc)
    legacy_unknown = not doc.custom_fleet_status and bool(doc.get("custom_legacy_asset"))
    if doc.custom_fleet_status not in STATUSES and not legacy_unknown:
        frappe.throw("INVALID_STATUS")
    if frappe.session.user == frappe.conf.get("fleet_ai_user"):
        guard_ai_write(doc, frappe.flags.get("fleet_status_write"))


def validate_vehicle_master(doc, method=None):
    """Trailers and archive-only assets: same AI field guard as trucks, without the truck-only status field."""
    from fleet_bridge.priorities import validate_priority
    validate_priority(doc)
    if frappe.session.user == frappe.conf.get("fleet_ai_user"):
        guard_ai_write(doc, None)


def guard_ai_write(doc, status_write):
    note_write = frappe.flags.get("fleet_note_write")
    priority_write = frappe.flags.get("fleet_priority_write")
    if (not status_write and not note_write and not priority_write) or doc.is_new():
        frappe.throw("Only the fleet status endpoint may write", frappe.PermissionError)
    old = doc.get_doc_before_save()
    if old:
        for field in doc.meta.fields:
            allowed = ({"custom_fleet_status"} if status_write else {"custom_general_note"} if note_write
                       else {"custom_reminder_priority", "custom_low_priority"})
            if field.fieldtype in frappe.model.no_value_fields or field.fieldname in allowed:
                continue
            if old.get(field.fieldname) != doc.get(field.fieldname):
                frappe.throw("Only the authorized fleet field may change", frappe.PermissionError)


def audit_truck(doc, method=None):
    old = doc.get_doc_before_save()
    if old and old.custom_fleet_status != doc.custom_fleet_status:
        context = frappe.flags.get("fleet_status_write") or {}
        append_log(doc, old.custom_fleet_status, context.get("request_id") or str(uuid.uuid4()),
                   context.get("expected_version") or str(old.modified),
                   "Fleet MCP" if context else "ERP UI/API")
    from fleet_bridge.priorities import audit_priority
    audit_priority(doc)


def append_log(doc, before, request_id, expected_version, source):
    frappe.flags.fleet_log_insert = True
    try:
        return frappe.get_doc({"doctype": "Fleet Operation Log",
            "operation_id": operation_name(frappe.session.user, request_id),
            "request_id": request_id, "vehicle": doc.name, "actor": frappe.session.user,
            "before_status": before, "after_status": doc.custom_fleet_status,
            "expected_version": expected_version, "result_version": str(doc.modified),
            "source": source}).insert(ignore_permissions=True)
    finally:
        frappe.flags.fleet_log_insert = False


def guard_log_insert(doc, method=None):
    if not frappe.flags.get("fleet_log_insert"):
        frappe.throw("Audit records are created by the server", frappe.PermissionError)


def guard_log_update(doc, method=None):
    if not doc.is_new():
        frappe.throw("Audit records are immutable", frappe.PermissionError)


def deny_log_delete(doc, method=None):
    frappe.throw("Audit records cannot be deleted", frappe.PermissionError)
