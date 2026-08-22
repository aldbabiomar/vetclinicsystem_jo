"""
Handles X-ray / bloodwork / test-result uploads.

Folder layout on disk:  uploads/<patient_id>/<record_key>/<filename>
  record_key is the visit ID (e.g. V0042) or inpatient case ID (e.g. IC7) —
  NOT the date, since two records can share a calendar date for the same
  patient and that would collide. Only the relative path is stored in the
  database; the files themselves live on disk so the database stays small.
"""
import os
import re
import uuid
from datetime import datetime

UPLOAD_ROOT = os.path.join(os.path.dirname(__file__), "uploads")
ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg"}

# Magic-byte signatures so a renamed file can't slip past the extension check.
SIGNATURES = {
    "pdf": [b"%PDF"],
    "jpg": [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
}


def record_key(record_type, record_id):
    prefix = "V" if record_type == "visit" else "IC"
    return f"{prefix}{record_id}" if record_type != "visit" else str(record_id)


def _ext(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def validate_file(file_storage):
    """Checks both extension and actual file signature. Returns (ok, reason)."""
    filename = file_storage.filename or ""
    ext = _ext(filename)
    if ext not in ALLOWED_EXTENSIONS:
        return False, "Only PDF and JPG/JPEG files are allowed."
    head = file_storage.stream.read(8)
    file_storage.stream.seek(0)
    if not any(head.startswith(sig) for sig in SIGNATURES[ext]):
        return False, "This file's contents don't match a PDF or JPEG (it may have been renamed)."
    return True, None


def _safe_name(filename):
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    return f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}_{base}"


def save_attachment(db, patient_id, record_type, record_id, file_storage, uploaded_by):
    ok, reason = validate_file(file_storage)
    if not ok:
        return None, reason

    key = record_key(record_type, record_id)
    folder = os.path.join(UPLOAD_ROOT, str(patient_id), key)
    os.makedirs(folder, exist_ok=True)

    safe_name = _safe_name(file_storage.filename)
    disk_path = os.path.join(folder, safe_name)
    file_storage.save(disk_path)

    relative_path = os.path.join(str(patient_id), key, safe_name)
    visit_id = record_id if record_type == "visit" else None
    case_id = record_id if record_type == "inpatient" else None
    db.execute(
        "INSERT INTO attachments (patient_id, visit_id, inpatient_case_id, relative_path, original_name, uploaded_at, uploaded_by) "
        "VALUES (?,?,?,?,?,?,?)",
        (patient_id, visit_id, case_id, relative_path, file_storage.filename,
         datetime.now().isoformat(timespec="seconds"), uploaded_by),
    )
    db.commit()
    return relative_path, None


def list_attachments(db, record_type, record_id):
    if record_type == "visit":
        return db.execute("SELECT * FROM attachments WHERE visit_id=? ORDER BY uploaded_at DESC", (record_id,)).fetchall()
    return db.execute("SELECT * FROM attachments WHERE inpatient_case_id=? ORDER BY uploaded_at DESC", (record_id,)).fetchall()
