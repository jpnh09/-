import hashlib
import re

STATUSES = ("可派車", "執行任務中", "維修中", "停用")


def validate_request(status, request_id):
    if status not in STATUSES:
        raise ValueError("INVALID_STATUS")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id):
        raise ValueError("INVALID_REQUEST_ID")


def operation_name(actor, request_id):
    return hashlib.sha256((actor + ":" + request_id).encode()).hexdigest()
