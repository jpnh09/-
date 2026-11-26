"""Permission-scoped human dashboard over the same records used by Fleet MCP."""
from collections import Counter, defaultdict

import frappe
from frappe.utils import getdate, now_datetime, today
from fleet_bridge.profile import asset_profile
from fleet_bridge.priorities import reminder_priority


def reminder_category(description):
    text = description or ""
    if "保險" in text or "強制險" in text:
        return "保險"
    if "通行證" in text:
        return "通行證"
    if "驗車" in text:
        return "驗車"
    return "其他提醒"


@frappe.whitelist(methods=["GET"])
def overview():
    if frappe.session.user == "Guest" or "System Manager" not in frappe.get_roles():
        frappe.throw("車隊首頁限已有管理權限的使用者。", frappe.PermissionError)
    for doctype in ("Fleet Legacy Asset", "Fleet Legacy Record"):
        if not frappe.has_permission(doctype, "read"):
            frappe.throw("無權讀取車隊資料。", frappe.PermissionError)
    assets = frappe.get_list("Fleet Legacy Asset", fields=["*"], order_by="display_name asc", limit_page_length=0)
    groups = defaultdict(list)
    if assets:
        rows = frappe.get_list("Fleet Legacy Record", filters={"asset": ["in", [a.name for a in assets]]},
            fields=["name", "asset", "record_kind", "description", "record_date", "due_date", "notes", "custom_notification_at", "raw_json", "modified"],
            order_by="due_date asc, name asc", limit_page_length=0)
        for row in rows:
            groups[row.asset].append(row)
    vehicles, reminders, represented = [], [], set()
    reference = getdate(today())
    from fleet_bridge.manual_reminders import reminders_by_vehicle
    manual = reminders_by_vehicle()

    def add_reminders(profile, vehicle_id, plate, priority):
        # Manual items join the shown list only; record_counts keep describing source records.
        profile["reminders"] = profile["reminders"] + manual.get(vehicle_id, [])
        for item in profile["reminders"]:
            item["reminder_priority"] = priority
            item["low_priority"] = priority == "低優先"
            due = item["effective_date"]
            if not item.get("manual"):
                item["category"] = reminder_category(item["description"])
                item["date_type"] = "開始日" if "開始" in (item["description"] or "") else "原記錄提醒日"
            item["days_remaining"] = (getdate(due) - reference).days if due else None
            reminders.append({**item, "asset_id": vehicle_id, "plate": plate})
    for asset in assets:
        priority_doc = asset
        profile = asset_profile(asset, rows=groups[asset.name])
        native_type, native_id = ("Truck", asset.truck) if asset.truck else ("Trailers", asset.trailer)
        status, version, native_link = None, None, None
        if native_id:
            native = frappe.get_doc(native_type, native_id)
            if frappe.has_permission(native_type, "read", native):
                if native.get("custom_legacy_asset") != asset.name:
                    frappe.throw("車籍來源連結不一致，請先核對。", frappe.ValidationError)
                status = native.get("custom_fleet_status") if native_type == "Truck" else None
                # Native records are the maintained vehicle master; archive is provenance.
                for key in ("make", "model"):
                    if native.meta.has_field(key):
                        profile[key] = native.get(key) or None
                version = str(native.modified)
                native_link = {"doctype": native_type, "name": native.name}
                priority_doc = native
                represented.add((native_type, native.name))
        profile["general_note"] = priority_doc.get("custom_general_note") or ""
        priority = reminder_priority(priority_doc)
        profile["reminder_priority"] = priority
        add_reminders(profile, asset.name, asset.display_name, priority)
        vehicles.append({"id": asset.name, "archive_id": asset.name, "plate": asset.display_name, "kind": asset.asset_kind,
            "reminder_priority": priority, "low_priority": priority == "低優先",
            "priority_version": str(priority_doc.modified),
            "status": status or "待確認", "status_known": bool(status), "version": version,
            "native": native_link, "profile": profile})
    # Also show vehicles created directly in ERP, without requiring a fake legacy asset.
    for doctype, asset_kind, plate_field in (("Truck", "Truck", "license_plate"), ("Trailers", "Trailer", "plate_number")):
        if not frappe.has_permission(doctype, "read"):
            continue
        for native in frappe.get_list(doctype, fields=["*"], limit_page_length=0):
            if (doctype, native.name) in represented or native.get("custom_is_migration_test"):
                continue
            if doctype == "Truck" and native.name == "526" and native.get("license_plate") == "TEST-526":
                continue
            if native.get("custom_legacy_asset"):
                continue  # A hidden or inconsistent archive must not be bypassed.
            status = native.get("custom_fleet_status")
            priority = reminder_priority(native)
            vehicle_id, plate = doctype + ":" + native.name, native.get(plate_field) or native.name
            profile = {"make": native.get("make") or native.get("trailer_make"), "model": native.get("model"), "source_year": None,
                "general_note": native.get("custom_general_note") or "", "reminder_priority": priority,
                "legacy_tags": [], "reminders": [], "historical_records": [], "notes": [], "source": None,
                "review_notes": "ERP 車籍；未連結舊系統資料。", "record_counts": {"reminders": 0, "historical_records": 0, "notes": 0}}
            add_reminders(profile, vehicle_id, plate, priority)
            vehicles.append({"id": vehicle_id, "archive_id": None,
                "reminder_priority": priority, "low_priority": priority == "低優先", "priority_version": str(native.modified),
                "plate": plate, "kind": asset_kind,
                "status": status or "待確認", "status_known": bool(status), "version": str(native.modified),
                "native": {"doctype": doctype, "name": native.name}, "profile": profile})
    vehicles.sort(key=lambda a: a["plate"])
    reminders.sort(key=lambda r: (r["effective_date"] or "9999-12-31", r["plate"], r["record_id"]))
    due_days = [r["days_remaining"] for r in reminders if r["days_remaining"] is not None]
    return {"as_of": str(now_datetime()), "today": str(reference), "company": frappe.defaults.get_user_default("Company"),
        "assets": vehicles, "reminders": reminders,
        "counts": {"assets": len(vehicles), "kinds": dict(Counter(a["kind"] for a in vehicles)),
            "reminders": len(reminders), "past_dates": sum(d < 0 for d in due_days),
            "next_30_days": sum(0 <= d <= 30 for d in due_days),
            "next_90_days": sum(0 <= d <= 90 for d in due_days),
            "records": sum(sum(a["profile"]["record_counts"].values()) for a in vehicles)},
        "notice": "已設定通知時間的提醒，行事曆與日期篩選依通知日期顯示；原記錄日期仍保留供核對。未設定者沿用舊系統日期顯示，不會自動發送通知。日期不代表已確認證件效力；驗車開始日不是到期日。測試車不列入正式主檔。"}


def install_workspace():
    """Idempotent UI installation, without changing business rows or roles."""
    from fleet_bridge.reminders import install
    install()
    from fleet_bridge.notification_dispatch import install as install_notifications
    install_notifications()
    from fleet_bridge.vehicle_notes import install as install_vehicle_notes
    install_vehicle_notes()
    from fleet_bridge.priorities import install as install_priorities
    install_priorities()
    from fleet_bridge.manual_reminders import install as install_manual_reminders
    install_manual_reminders()
    # No implicit dispatchable default for newly created real vehicles.
    field = frappe.get_doc("Custom Field", "Truck-custom_fleet_status")
    desired = {"label": "車隊營運狀態", "default": "", "options": "\n可派車\n執行任務中\n維修中\n停用",
        "description": "請確認實際狀態後選擇；不連動派車、結算或維修工單。"}
    if any(field.get(k) != v for k, v in desired.items()):
        field.update(desired)
        field.save()
    name = "車隊管理"
    if not frappe.db.exists("Workspace", name):
        import json
        frappe.get_doc({"doctype": "Workspace", "label": name, "title": name, "module": "Fleet Bridge",
            "public": 1, "icon": "truck", "sequence_id": 1,
            "roles": [{"role": "System Manager"}],
            "content": json.dumps([
                {"id": "fleet-heading", "type": "header", "data": {"text": "<b>車隊管理</b>", "col": 12}},
                {"id": "fleet-home", "type": "shortcut", "data": {"shortcut_name": "車隊首頁", "col": 4}},
                {"id": "fleet-trucks", "type": "shortcut", "data": {"shortcut_name": "貨車車籍", "col": 4}},
                {"id": "fleet-trailers", "type": "shortcut", "data": {"shortcut_name": "拖車車籍", "col": 4}}
            ], ensure_ascii=False),
            "shortcuts": [
                {"type": "Page", "link_to": "fleet-center", "label": "車隊首頁"},
                {"type": "DocType", "link_to": "Truck", "doc_view": "List", "label": "貨車車籍"},
                {"type": "DocType", "link_to": "Trailers", "doc_view": "List", "label": "拖車車籍"}
            ]}).insert()
    frappe.clear_cache()


@frappe.whitelist(methods=["POST"])
def set_reminder_priority(asset_id, priority=None, expected_version=None, request_id=None, low_priority=None):
    """Compatibility route; the audited implementation lives in priorities.py."""
    import uuid
    # Compatibility for a stale browser still sending the previous boolean field.
    if priority is None and low_priority is not None:
        if str(low_priority) not in ("0", "1"):
            frappe.throw("優先度值無效。")
        priority = "低優先" if str(low_priority) == "1" else "一般"
    from fleet_bridge.priorities import dashboard_priority_update
    return dashboard_priority_update(asset_id, priority, expected_version, request_id or str(uuid.uuid4()))
