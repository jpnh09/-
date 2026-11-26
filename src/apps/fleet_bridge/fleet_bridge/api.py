from types import SimpleNamespace

import frappe
from frappe.utils import get_datetime
from fleet_bridge.domain import STATUSES, operation_name, validate_request
from fleet_bridge.events import append_log
from fleet_bridge.profile import vehicle_profile
from fleet_bridge.reminders import reminder_get, reminder_update, reminder_get_history
from fleet_bridge.vehicle_notes import vehicle_note_update, vehicle_note_get_history
from fleet_bridge.priorities import vehicle_priority_update, vehicle_priority_get_history

FIELDS = ["name", "truck_number", "license_plate", "custom_fleet_status", "custom_general_note", "modified", "modified_by"]
TRAILER_FIELDS = ["name", "trailer_number", "plate_number", "modified", "modified_by"]
ASSET_FIELDS = ["name", "display_name", "asset_kind", "truck", "trailer", "modified", "modified_by"]
LOG_FIELDS = ["name", "vehicle", "actor", "before_status", "after_status", "creation", "result_version", "source"]
# Public vehicle_id prefixes. Trucks keep their bare name for compatibility with existing clients.
TRAILER_PREFIX, ASSET_PREFIX = "Trailers:", "Asset:"
KIND_LABELS = {"Truck": "貨車", "Trailers": "拖車", "Fleet Legacy Asset": "未建 ERP 主檔（舊系統資料）"}


def authorize(vehicle_id=None):
    """Operator check; with vehicle_id, resolve it against the explicit allowlists or fail closed."""
    if frappe.session.user != frappe.conf.get("fleet_ai_user") or "Fleet AI Operator" not in frappe.get_roles():
        frappe.throw("Fleet operator required", frappe.PermissionError)
    if vehicle_id:
        return resolve_vehicle(vehicle_id)
    return allowed_refs()


def allowed_refs():
    conf = frappe.conf
    return SimpleNamespace(trucks=list(conf.get("fleet_allowed_vehicles") or []),
                           trailers=list(conf.get("fleet_allowed_trailers") or []),
                           assets=list(conf.get("fleet_allowed_assets") or []))


def resolve_vehicle(vehicle_id):
    """Map a public vehicle_id (truck name, Trailers:<name>, Asset:<id>, or an authorized plate) to one record.

    Only vehicles named in site_config are reachable; anything else is FORBIDDEN without revealing whether it exists.
    """
    vehicle_id = str(vehicle_id or "").strip()
    allowed = allowed_refs()
    if vehicle_id.startswith(TRAILER_PREFIX):
        name = vehicle_id[len(TRAILER_PREFIX):]
        if name not in allowed.trailers:
            frappe.throw("Vehicle not allowed", frappe.PermissionError)
        return _trailer_ref(name)
    if vehicle_id.startswith(ASSET_PREFIX):
        name = vehicle_id[len(ASSET_PREFIX):]
        if name not in allowed.assets:
            frappe.throw("Vehicle not allowed", frappe.PermissionError)
        return _asset_ref(name)
    if vehicle_id in allowed.trucks:
        doc = frappe.get_doc("Truck", vehicle_id)
        doc.check_permission("read")
        return SimpleNamespace(doctype="Truck", name=doc.name, public_id=doc.name, doc=doc)
    # Bare plate for an authorized trailer or archive-only vehicle; never guess between several.
    candidates = [n for n in allowed.trailers if n == vehicle_id]
    candidates += [ASSET_PREFIX + n for n in allowed.assets
                   if frappe.db.get_value("Fleet Legacy Asset", n, "display_name") == vehicle_id]
    if len(candidates) > 1:
        frappe.throw("AMBIGUOUS_VEHICLE: use vehicle_search and pick one vehicle_id")
    if not candidates:
        frappe.throw("Vehicle not allowed", frappe.PermissionError)
    return resolve_vehicle(candidates[0] if candidates[0].startswith(ASSET_PREFIX) else TRAILER_PREFIX + candidates[0])


def _trailer_ref(name):
    doc = frappe.get_doc("Trailers", name)
    doc.check_permission("read")
    return SimpleNamespace(doctype="Trailers", name=doc.name, public_id=TRAILER_PREFIX + doc.name, doc=doc)


def _asset_ref(name):
    doc = frappe.get_doc("Fleet Legacy Asset", name)
    # An archive linked to a native master must be reached through that master and its own grant.
    if doc.truck or doc.trailer:
        frappe.throw("Vehicle not allowed", frappe.PermissionError)
    return SimpleNamespace(doctype="Fleet Legacy Asset", name=doc.name, public_id=ASSET_PREFIX + doc.name, doc=doc)


def vehicle_dict(doc):
    if doc.doctype == "Trailers":
        number, plate, vehicle_id = doc.trailer_number or doc.name, doc.plate_number or doc.name, TRAILER_PREFIX + doc.name
    elif doc.doctype == "Fleet Legacy Asset":
        number = plate = doc.display_name or doc.name
        vehicle_id = ASSET_PREFIX + doc.name
    else:
        number, plate, vehicle_id = doc.truck_number, doc.license_plate, doc.name
    status = doc.get("custom_fleet_status") if doc.doctype == "Truck" else None
    result = {"vehicle_id": vehicle_id, "vehicle_type": KIND_LABELS[doc.doctype], "vehicle_doctype": doc.doctype,
              "truck_number": number, "license_plate": plate,
              "status": status or "待確認", "status_known": bool(status),
              "version": str(doc.modified), "modified_by": doc.modified_by,
              "status_scope": "pilot_operational_status", "timezone": "Asia/Taipei",
              "allowed_statuses": STATUSES if doc.doctype == "Truck" else []}
    if doc.doctype != "Truck":
        result["status_note"] = "試跑營運狀態只適用貨車；此車不提供狀態修改。"
    return result


@frappe.whitelist(methods=["GET"])
def vehicle_search(query):
    allowed = authorize()
    query = str(query).strip()
    if not query or len(query) > 100:
        frappe.throw("Query must contain 1 to 100 characters")
    like = "%" + query + "%"
    docs = frappe.get_list("Truck", filters={"name": ["in", allowed.trucks]},
        or_filters={"truck_number": ["like", like], "license_plate": ["like", like]},
        fields=FIELDS, limit_page_length=51, order_by="name asc")
    matches = [vehicle_dict(frappe._dict(d, doctype="Truck")) for d in docs]
    if allowed.trailers:
        docs = frappe.get_list("Trailers", filters={"name": ["in", allowed.trailers]},
            or_filters={"trailer_number": ["like", like], "plate_number": ["like", like], "name": ["like", like]},
            fields=TRAILER_FIELDS, limit_page_length=51, order_by="name asc")
        matches += [vehicle_dict(frappe._dict(d, doctype="Trailers")) for d in docs]
    if allowed.assets:
        # The AI account has no general archive permission; the allowlist above is the authorization.
        docs = frappe.get_all("Fleet Legacy Asset", filters={"name": ["in", allowed.assets],
            "display_name": ["like", like], "truck": ["is", "not set"], "trailer": ["is", "not set"]},
            fields=ASSET_FIELDS, limit_page_length=51, order_by="name asc")
        matches += [vehicle_dict(frappe._dict(d, doctype="Fleet Legacy Asset")) for d in docs]
    return {"matches": matches[:50], "truncated": len(matches) > 50,
            "requires_selection": len(matches) > 1}


@frappe.whitelist(methods=["GET"])
def vehicle_get(vehicle_id):
    ref = authorize(vehicle_id)
    return {**vehicle_dict(ref.doc), "profile": vehicle_profile(ref.doc)}


def log_result(log, replayed=False):
    return {"operation_id": log.name, "vehicle_id": log.vehicle, "before_status": log.before_status,
            "status": log.after_status, "version": log.result_version, "actor": log.actor,
            "actor_type": "service_account", "at": str(log.creation), "timezone": "Asia/Taipei", "replayed": replayed}


@frappe.whitelist(methods=["POST"])
def vehicle_update_status(vehicle_id, status, expected_version, request_id):
    ref = authorize(vehicle_id)
    if ref.doctype != "Truck":
        frappe.throw("UNSUPPORTED_VEHICLE_TYPE: pilot status applies to trucks only")
    vehicle_id = ref.name
    if vehicle_id in (frappe.conf.get("fleet_readonly_vehicles") or []):
        frappe.throw("Imported vehicle is read-only during migration acceptance; use the marked test copy", frappe.PermissionError)
    try:
        validate_request(status, request_id)
    except ValueError as exc:
        frappe.throw(str(exc))
    # Lock the vehicle inside this POST transaction before checking version or replay.
    frappe.db.sql("SELECT name FROM `tabTruck` WHERE name=%s FOR UPDATE", (vehicle_id,))
    op_id = operation_name(frappe.session.user, request_id)
    if frappe.db.exists("Fleet Operation Log", op_id):
        log = frappe.get_doc("Fleet Operation Log", op_id)
        if (log.vehicle, log.after_status, log.expected_version) != (vehicle_id, status, expected_version):
            frappe.throw("IDEMPOTENCY_CONFLICT")
        return log_result(log, True)
    doc = frappe.get_doc("Truck", vehicle_id)
    if get_datetime(expected_version) != get_datetime(doc.modified):
        frappe.throw("VERSION_CONFLICT: refresh vehicle before retrying", frappe.TimestampMismatchError)
    frappe.flags.fleet_status_write = {"request_id": request_id, "expected_version": expected_version}
    try:
        if doc.custom_fleet_status == status:
            log = append_log(doc, status, request_id, expected_version, "Fleet MCP (no change)")
        else:
            doc.custom_fleet_status = status
            # No general write permission is granted to the API account.
            # All authorization and field selection occur above in this narrow method.
            doc.save(ignore_permissions=True)
            log = frappe.get_doc("Fleet Operation Log", op_id)
        return log_result(log)
    finally:
        frappe.flags.fleet_status_write = None
    # Frappe commits the successful POST, or rolls back BOTH status and audit on error.


@frappe.whitelist(methods=["GET"])
def vehicle_get_history(vehicle_id, limit=20):
    ref = authorize(vehicle_id)
    limit = max(1, min(int(limit), 100))
    result = {"vehicle_id": ref.public_id, "timezone": "Asia/Taipei", "actor_note": "AI 帳號不是已驗證的真人下令人"}
    if ref.doctype != "Truck":
        # Status logs exist only for trucks; notes, priority and reminders keep their own histories.
        return {**result, "entries": [], "note": "此車沒有試跑營運狀態紀錄；備註、優先度與提醒請查各自的歷史。"}
    return {**result, "entries": frappe.get_all("Fleet Operation Log", filters={"vehicle": ref.name},
        fields=LOG_FIELDS, order_by="creation desc, name desc", limit_page_length=limit)}
