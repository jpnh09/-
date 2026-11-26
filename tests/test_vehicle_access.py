"""Offline checks for trailer / archive-only authorization with an in-memory frappe stub.

Run: python -I tests/test_vehicle_access.py   (from the repository root)
The real end-to-end check still runs on the server after deployment.
"""
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "apps" / "fleet_bridge"


class Throw(Exception):
    pass


class _dict(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


class Doc(_dict):
    def check_permission(self, ptype):
        if (self.doctype, self.name) not in PERMITTED:
            raise frappe.PermissionError("no " + ptype)


def make_frappe():
    f = types.ModuleType("frappe")
    f.PermissionError = type("PermissionError", (Throw,), {})
    f.TimestampMismatchError = type("TimestampMismatchError", (Throw,), {})
    f.ValidationError = type("ValidationError", (Throw,), {})
    f._dict = _dict
    f.session = _dict(user="ai@x")
    f.conf = _dict()
    f.flags = _dict()
    f.get_roles = lambda: ROLES

    def throw(msg, exc=Throw):
        raise exc(msg)
    f.throw = throw
    f.whitelist = lambda **kw: (lambda fn: fn)

    def get_doc(doctype, name):
        try:
            return DOCS[(doctype, name)]
        except KeyError:
            raise f.ValidationError("DoesNotExist " + doctype + " " + name)
    f.get_doc = get_doc

    def match(row, filters):
        for key, cond in (filters or {}).items():
            value = row.get(key)
            if isinstance(cond, list):
                op, arg = cond
                if op == "in" and value not in arg:
                    return False
                if op == "is" and bool(value) != (arg == "set"):
                    return False
                if op == "like" and arg.strip("%") not in (value or ""):
                    return False
            elif value != cond:
                return False
        return True

    def get_all(doctype, filters=None, or_filters=None, fields=None, pluck=None, **kw):
        rows = [d for (dt, _), d in DOCS.items() if dt == doctype and match(d, filters)]
        if or_filters:
            rows = [d for d in rows if any(match(d, {k: v}) for k, v in or_filters.items())]
        if doctype in ("Fleet Legacy Record",) and filters and "asset" in filters:
            rows.sort(key=lambda r: r.name)
        return [r[pluck] for r in rows] if pluck else [_dict(r) for r in rows]
    f.get_all = get_all
    f.get_list = get_all
    f.db = _dict(get_value=lambda dt, name, field: DOCS.get((dt, name), {}).get(field),
                 exists=lambda *a, **k: False, sql=lambda *a, **k: None)
    f.model = _dict(no_value_fields=())
    f.publish_realtime = lambda *a, **k: None
    utils = types.ModuleType("frappe.utils")
    utils.get_datetime = lambda v: v
    f.utils = utils
    sys.modules["frappe"], sys.modules["frappe.utils"] = f, utils
    return f


frappe = make_frappe()
sys.path.insert(0, str(ROOT))
from fleet_bridge import api, reminders  # noqa: E402

ROLES = ["Fleet AI Operator"]
DOCS, PERMITTED = {}, set()


def add(doctype, name, **fields):
    DOCS[(doctype, name)] = Doc(doctype=doctype, name=name, modified="2026-10-19 10:00:00", modified_by="x", **fields)


def raw(source_id):
    return json.dumps({"vehicleId": source_id, "make": "M", "model": "X", "year": 2016, "tags": ["半拖車"]})


add("Truck", "TRK-0001", truck_number="TRK-0001", license_plate="TRK-0001", custom_legacy_asset="LL-0001")
add("Trailers", "TRL-0002", trailer_number="TRL-0002", plate_number="TRL-0002", trailer_make="拖車", custom_legacy_asset="LL-0036")
add("Trailers", "TRL-0003", trailer_number="TRL-0003", plate_number="TRL-0003", custom_legacy_asset="LL-0022")
add("Fleet Legacy Asset", "LL-0001", display_name="TRK-0001", truck="TRK-0001", trailer=None, asset_kind="Truck", raw_json=raw(1), source_id="1", batch_id="b")
add("Fleet Legacy Asset", "LL-0036", display_name="TRL-0002", truck=None, trailer="TRL-0002", asset_kind="Trailer", raw_json=raw(36), source_id="36", batch_id="b")
add("Fleet Legacy Asset", "LL-0007", display_name="CAR-0004", truck=None, trailer=None, asset_kind="Passenger", raw_json=raw(7), source_id="7", batch_id="b")
add("Fleet Legacy Record", "R-36", asset="LL-0036", record_kind="reminders", description="驗車開始", due_date="2030-08-08",
    record_date=None, notes="", custom_notification_at=None, raw_json=json.dumps({"vehicleId": 36}))
add("Fleet Legacy Record", "R-7", asset="LL-0007", record_kind="reminders", description="驗車開始", due_date="2030-05-26",
    record_date=None, notes="", custom_notification_at=None, raw_json=json.dumps({"vehicleId": 7}))
add("Fleet Legacy Record", "R-1", asset="LL-0001", record_kind="reminders", description="驗車開始", due_date="2030-03-31",
    record_date=None, notes="", custom_notification_at=None, raw_json=json.dumps({"vehicleId": 1}))
PERMITTED |= {("Truck", "TRK-0001"), ("Trailers", "TRL-0002")}
frappe.conf.update(fleet_ai_user="ai@x", fleet_allowed_vehicles=["TRK-0001"],
                   fleet_allowed_trailers=["TRL-0002"], fleet_allowed_assets=["LL-0007", "LL-0001"])


def denied(fn, *args, exc=frappe.PermissionError):
    try:
        fn(*args)
    except exc:
        return True
    return False


checks = {
    "truck keeps bare id": api.resolve_vehicle("TRK-0001").public_id == "TRK-0001",
    "trailer by prefixed id": api.resolve_vehicle("Trailers:TRL-0002").doctype == "Trailers",
    "trailer by bare plate": api.resolve_vehicle("TRL-0002").public_id == "Trailers:TRL-0002",
    "archive-only by plate": api.resolve_vehicle("CAR-0004").public_id == "Asset:LL-0007",
    "ungranted trailer denied": denied(api.resolve_vehicle, "Trailers:TRL-0003") and denied(api.resolve_vehicle, "TRL-0003"),
    "archive of a truck cannot be used as a side door": denied(api.resolve_vehicle, "Asset:LL-0001"),
    "unknown plate denied": denied(api.resolve_vehicle, "NOPE-1"),
    "trailer reminder linked": reminders.authorize("TRL-0002", "R-36").name == "R-36",
    "archive reminder linked": reminders.authorize("Asset:LL-0007", "R-7").name == "R-7",
    "cross-vehicle reminder denied": denied(reminders.authorize, "TRL-0002", "R-1") and denied(reminders.authorize, "Asset:LL-0007", "R-36"),
    "status update rejected for trailer": denied(api.vehicle_update_status, "TRL-0002", "可派車", "v", "r" * 20, exc=Throw),
}
got = api.vehicle_get("TRL-0002")
checks["trailer vehicle_get"] = (got["vehicle_id"] == "Trailers:TRL-0002" and got["vehicle_type"] == "拖車"
                                 and [r["record_id"] for r in got["profile"]["reminders"]] == ["R-36"])
got = api.vehicle_get("Asset:LL-0007")
checks["archive vehicle_get"] = got["profile"]["reminders"][0]["recorded_due_date"] == "2030-05-26" and got["profile"]["has_erp_master"] is False
checks["truck vehicle_get unchanged"] = api.vehicle_get("TRK-0001")["profile"]["reminders"][0]["record_id"] == "R-1"
found = {m["vehicle_id"] for m in api.vehicle_search("TRL")["matches"]}  # TRL-0003 exists but is not granted
checks["search lists only granted"] = found == {"Trailers:TRL-0002"}
ROLES[:] = []
checks["non-operator denied"] = denied(api.vehicle_get, "TRK-0001")

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
sys.exit(1 if failed else 0)
