import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.permissions import add_permission
from fleet_bridge.domain import STATUSES

AI_USER = "ai-fleet-operator@example.invalid"


def install():
    create_custom_fields({"Truck": [{
        "fieldname": "custom_fleet_status", "label": "車隊營運狀態",
        "fieldtype": "Select", "options": "\n" + "\n".join(STATUSES),
        "default": "", "insert_after": "status", "in_list_view": 1,
        "description": "請確認實際狀態後選擇；不連動派車、結算或維修工單。",
    }]})
    if not frappe.db.exists("Role", "Fleet AI Operator"):
        frappe.get_doc({"doctype": "Role", "role_name": "Fleet AI Operator", "desk_access": 0}).insert()
    add_permission("Truck", "Fleet AI Operator", 0, "read")
    if not frappe.db.exists("DocType", "Fleet Operation Log"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Fleet Operation Log", "module": "Fleet Bridge",
            "custom": 1, "autoname": "field:operation_id", "track_changes": 1,
            "fields": [
                {"fieldname": "operation_id", "label": "Operation ID", "fieldtype": "Data", "unique": 1, "reqd": 1},
                {"fieldname": "request_id", "label": "Request ID", "fieldtype": "Data", "reqd": 1},
                {"fieldname": "vehicle", "label": "Vehicle", "fieldtype": "Link", "options": "Truck", "reqd": 1, "in_list_view": 1},
                {"fieldname": "actor", "label": "執行帳號", "fieldtype": "Data", "reqd": 1},
                {"fieldname": "before_status", "label": "原狀態", "fieldtype": "Data"},
                {"fieldname": "after_status", "label": "新狀態", "fieldtype": "Data", "reqd": 1},
                {"fieldname": "expected_version", "label": "Expected Version", "fieldtype": "Data"},
                {"fieldname": "result_version", "label": "Result Version", "fieldtype": "Data", "reqd": 1},
                {"fieldname": "source", "label": "Source", "fieldtype": "Data", "reqd": 1},
            ],
            "permissions": [{"role": "System Manager", "read": 1}],
        }).insert()


def create_pilot():
    """Run only via bench as Administrator on the isolated pilot site."""
    if frappe.session.user != "Administrator":
        frappe.throw("Administrator required", frappe.PermissionError)
    if not frappe.db.exists("Fuel UOM", "Fleet Pilot Liter"):
        meta = frappe.get_meta("Fuel UOM")
        values = {"doctype": "Fuel UOM"}
        if meta.autoname and meta.autoname.startswith("field:"):
            values[meta.autoname[6:]] = "Fleet Pilot Liter"
        for field in meta.fields:
            if field.reqd and field.fieldtype == "Data":
                values.setdefault(field.fieldname, "Fleet Pilot Liter")
        unit = frappe.get_doc(values).insert()
    else:
        unit = frappe.get_doc("Fuel UOM", "Fleet Pilot Liter")
    if not frappe.db.exists("Truck", "526"):
        frappe.get_doc({"doctype": "Truck", "truck_number": "526", "license_plate": "TEST-526",
                        "fuel_type": "Diesel", "fuel_uom": unit.name,
                        "custom_fleet_status": STATUSES[0]}).insert()
    if not frappe.db.exists("User", AI_USER):
        frappe.get_doc({"doctype": "User", "email": AI_USER, "first_name": "AI-Fleet-Operator",
                        "send_welcome_email": 0, "enabled": 1, "user_type": "System User",
                        "roles": [{"role": "Fleet AI Operator"}]}).insert()
    if not frappe.db.exists("User Permission", {"user": AI_USER, "allow": "Truck", "for_value": "526"}):
        frappe.get_doc({"doctype": "User Permission", "user": AI_USER, "allow": "Truck",
                        "for_value": "526", "apply_to_all_doctypes": 1}).insert()
    frappe.conf.fleet_ai_user = AI_USER
    frappe.conf.fleet_allowed_vehicles = ["526"]
    from frappe.installer import update_site_config
    update_site_config("fleet_ai_user", AI_USER)
    update_site_config("fleet_allowed_vehicles", ["526"])
    frappe.db.commit()
    return {"vehicle": "526", "test_plate": "TEST-526", "actor": AI_USER}


def grant_vehicle_access(plates, dry_run=False):
    """Authorize the AI account for exactly these plates (trucks, trailers, or archive-only vehicles).

    Run via: bench --site fleet.localhost execute fleet_bridge.setup.grant_vehicle_access --kwargs '{"plates": [...]}'
    Nothing is granted unless every plate resolves to exactly one vehicle.
    """
    if frappe.session.user != "Administrator":
        frappe.throw("Administrator required", frappe.PermissionError)
    if isinstance(plates, str):
        import json
        plates = json.loads(plates)
    ai_user = frappe.conf.get("fleet_ai_user") or AI_USER
    resolved, problems = [], []
    for plate in dict.fromkeys(str(p).strip() for p in plates if str(p).strip()):
        trucks = sorted(set(frappe.get_all("Truck", filters={"name": plate}, pluck="name")
                            + frappe.get_all("Truck", filters={"license_plate": plate}, pluck="name")))
        trailers = sorted(set(frappe.get_all("Trailers", filters={"name": plate}, pluck="name")
                              + frappe.get_all("Trailers", filters={"plate_number": plate}, pluck="name")))
        # Archives linked to a native master are reached through that master, never directly.
        assets = frappe.get_all("Fleet Legacy Asset", filters={"display_name": plate,
            "truck": ["is", "not set"], "trailer": ["is", "not set"]}, pluck="name")
        found = [("Truck", n) for n in trucks] + [("Trailers", n) for n in trailers] + [("Fleet Legacy Asset", n) for n in assets]
        if len(found) != 1:
            problems.append({"plate": plate, "matches": found})
        else:
            resolved.append({"plate": plate, "doctype": found[0][0], "name": found[0][1]})
    if problems:
        frappe.throw("Every plate must match exactly one vehicle; nothing granted: " + frappe.as_json(problems))
    if dry_run:
        return {"dry_run": True, "resolved": resolved}

    def names(doctype):
        return [r["name"] for r in resolved if r["doctype"] == doctype]

    conf = {key: list(frappe.conf.get(key) or []) for key in
            ("fleet_allowed_vehicles", "fleet_allowed_trailers", "fleet_allowed_assets")}
    for key, doctype in (("fleet_allowed_vehicles", "Truck"), ("fleet_allowed_trailers", "Trailers"),
                         ("fleet_allowed_assets", "Fleet Legacy Asset")):
        conf[key] = list(dict.fromkeys(conf[key] + names(doctype)))
    for name in names("Truck"):
        if not frappe.db.exists("User Permission", {"user": ai_user, "allow": "Truck", "for_value": name}):
            frappe.get_doc({"doctype": "User Permission", "user": ai_user, "allow": "Truck",
                            "for_value": name, "apply_to_all_doctypes": 1}).insert()
    if conf["fleet_allowed_trailers"]:
        for name in conf["fleet_allowed_trailers"]:
            # Scoped to Trailers itself so trucks that link a default trailer stay readable.
            if not frappe.db.exists("User Permission", {"user": ai_user, "allow": "Trailers", "for_value": name}):
                frappe.get_doc({"doctype": "User Permission", "user": ai_user, "allow": "Trailers", "for_value": name,
                                "apply_to_all_doctypes": 0, "applicable_for": "Trailers"}).insert()
        # Role read is added only once user permissions exist, so it never exposes every trailer.
        add_permission("Trailers", "Fleet AI Operator", 0, "read")
    frappe.clear_cache(user=ai_user)
    frappe.set_user(ai_user)
    try:
        visible = set(frappe.get_list("Trailers", pluck="name", limit_page_length=0)) if conf["fleet_allowed_trailers"] else set()
    finally:
        frappe.set_user("Administrator")
    if not visible <= set(conf["fleet_allowed_trailers"]):
        frappe.db.rollback()
        frappe.clear_cache(user=ai_user)
        frappe.throw("AI account could see unauthorized trailers; rolled back: " + ", ".join(sorted(visible - set(conf["fleet_allowed_trailers"]))))
    frappe.db.commit()
    from frappe.installer import update_site_config
    for key, value in conf.items():
        update_site_config(key, value)
    frappe.clear_cache()
    return {"granted": resolved, "visible_trailers": sorted(visible),
            "allowed_counts": {key: len(value) for key, value in conf.items()}}
