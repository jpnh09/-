app_name = "fleet_bridge"
app_title = "Fleet Bridge"
app_publisher = "Fleet Center contributors"
app_description = "Fleet workspace, permission-scoped profiles and audited status API"
app_email = "fleet-admin@example.invalid"
app_license = "MIT"
required_apps = ["frappe", "erpnext", "vsd_fleet_ms"]
after_install = "fleet_bridge.setup.install"
scheduler_events = {"cron": {"* * * * *": ["fleet_bridge.notification_dispatch.run",
                                             "fleet_bridge.road_events.scheduler_tick"]}}
doc_events = {
    "Fleet Road Info Settings": {"validate": "fleet_bridge.road_events.validate_settings"},
    "Fleet Permit Route": {"validate": "fleet_bridge.road_limits.validate_permit",
                           "on_update": "fleet_bridge.road_limits.permit_on_update"},
    "Fleet Road Event Dispatch": {
        "before_insert": "fleet_bridge.events.guard_log_insert",
        "validate": "fleet_bridge.events.guard_log_update",
        "on_trash": "fleet_bridge.events.deny_log_delete",
    },
    "Fleet Legacy Record": {"validate": "fleet_bridge.reminders.validate_record", "on_update": "fleet_bridge.reminders.audit_record"},
    "Fleet Reminder Log": {
        "before_insert": "fleet_bridge.events.guard_log_insert",
        "validate": "fleet_bridge.events.guard_log_update",
        "on_trash": "fleet_bridge.events.deny_log_delete",
    },
    "Fleet Notification Dispatch": {
        "before_insert": "fleet_bridge.events.guard_log_insert",
        "validate": "fleet_bridge.events.guard_log_update",
        "on_trash": "fleet_bridge.events.deny_log_delete",
    },
    "Fleet Vehicle Note Log": {
        "before_insert": "fleet_bridge.events.guard_log_insert",
        "validate": "fleet_bridge.events.guard_log_update",
        "on_trash": "fleet_bridge.events.deny_log_delete",
    },
    "Fleet Priority Log": {
        "before_insert": "fleet_bridge.events.guard_log_insert",
        "validate": "fleet_bridge.events.guard_log_update",
        "on_trash": "fleet_bridge.events.deny_log_delete",
    },
    "Truck": {"validate": "fleet_bridge.events.validate_truck", "on_update": "fleet_bridge.events.audit_truck"},
    "Trailers": {"validate": "fleet_bridge.events.validate_vehicle_master", "on_update": "fleet_bridge.priorities.audit_priority"},
    "Fleet Legacy Asset": {"validate": "fleet_bridge.events.validate_vehicle_master", "on_update": "fleet_bridge.priorities.audit_priority"},
    "Fleet Operation Log": {
        "before_insert": "fleet_bridge.events.guard_log_insert",
        "validate": "fleet_bridge.events.guard_log_update",
        "on_trash": "fleet_bridge.events.deny_log_delete",
    },
}
