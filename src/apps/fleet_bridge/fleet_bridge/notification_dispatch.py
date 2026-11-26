"""Deliver each configured reminder time once as an ERP in-app alert."""
import hashlib

import frappe
from frappe.utils import get_datetime, now_datetime


def install():
    if frappe.db.exists("DocType", "Fleet Notification Dispatch"):
        return
    frappe.get_doc({
        "doctype": "DocType", "name": "Fleet Notification Dispatch", "module": "Fleet Bridge",
        "custom": 1, "autoname": "field:dispatch_key", "track_changes": 1,
        "fields": [
            {"fieldname": "dispatch_key", "label": "發送識別碼", "fieldtype": "Data", "unique": 1, "reqd": 1},
            {"fieldname": "reminder", "label": "提醒", "fieldtype": "Link", "options": "Fleet Legacy Record", "reqd": 1},
            {"fieldname": "scheduled_at", "label": "預定通知時間", "fieldtype": "Datetime", "reqd": 1},
            {"fieldname": "recipient", "label": "接收帳號", "fieldtype": "Link", "options": "User", "reqd": 1},
            {"fieldname": "notification_log", "label": "系統通知", "fieldtype": "Link", "options": "Notification Log", "reqd": 1},
        ],
        "permissions": [{"role": "System Manager", "read": 1}],
    }).insert()


def recipients():
    managers = set(frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent"))
    managers.add("Administrator")
    return sorted(frappe.get_all("User", filters={"name": ["in", list(managers)], "enabled": 1}, pluck="name"))


def dispatch_key(reminder_id, scheduled_at, recipient):
    value = "\0".join((reminder_id, str(scheduled_at), recipient))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run():
    """Frappe calls this every minute; record lock and unique key prevent duplicate alerts."""
    if not frappe.db.exists("DocType", "Fleet Notification Dispatch"):
        return
    now = now_datetime()
    users = recipients()
    if not users:
        return
    due_ids = frappe.get_all("Fleet Legacy Record",
        filters={"record_kind": "reminders", "custom_notification_at": ["<=", now]},
        pluck="name", order_by="custom_notification_at asc, name asc", limit_page_length=0)
    for reminder_id in due_ids:
        frappe.db.sql("SELECT name FROM `tabFleet Legacy Record` WHERE name=%s FOR UPDATE", (reminder_id,))
        row = frappe.get_doc("Fleet Legacy Record", reminder_id)
        scheduled = row.get("custom_notification_at")
        if row.record_kind != "reminders" or not scheduled or get_datetime(scheduled) > now:
            continue
        plate = frappe.db.get_value("Fleet Legacy Asset", row.asset, "display_name") or row.asset
        deliver(row, plate, scheduled, users, "原記錄日期與證件效力請另行核對。", {"reminder": row.name})
    if not frappe.db.exists("DocType", "Fleet Manual Reminder"):
        return
    manual_ids = frappe.get_all("Fleet Manual Reminder", filters={"notification_at": ["<=", now]},
        pluck="name", order_by="notification_at asc, name asc", limit_page_length=0)
    for reminder_id in manual_ids:
        frappe.db.sql("SELECT name FROM `tabFleet Manual Reminder` WHERE name=%s FOR UPDATE", (reminder_id,))
        row = frappe.get_doc("Fleet Manual Reminder", reminder_id)
        scheduled = row.get("notification_at")
        if not scheduled or get_datetime(scheduled) > now:
            continue
        deliver(row, row.plate or row.vehicle_key, scheduled, users, f"提醒日期：{row.due_date}。",
                {"manual_reminder": row.name})


def deliver(row, plate, scheduled, users, detail, link):
    for user in users:
        key = dispatch_key(row.name, scheduled, user)
        if frappe.db.exists("Fleet Notification Dispatch", key):
            continue
        alert = frappe.get_doc({
            "doctype": "Notification Log", "type": "Alert", "for_user": user,
            "from_user": "Administrator", "document_type": row.doctype,
            "document_name": row.name,
            "title": f"車隊提醒：{plate} · {row.description}",
            "description": f"預定通知時間（台北）：{scheduled}。{detail}",
        }).insert(ignore_permissions=True)
        previous_flag = frappe.flags.get("fleet_log_insert")
        frappe.flags.fleet_log_insert = True
        try:
            frappe.get_doc({
                "doctype": "Fleet Notification Dispatch", "dispatch_key": key,
                **link, "scheduled_at": scheduled,
                "recipient": user, "notification_log": alert.name,
            }).insert(ignore_permissions=True)
        finally:
            frappe.flags.fleet_log_insert = previous_flag
