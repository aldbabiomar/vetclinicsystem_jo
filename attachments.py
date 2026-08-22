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

import auth

# On the versioned-release layout (VETCLINICSYSTEMJO_DATA_DIR set by the
# launcher script — see updater.py / setup.py --enable-updates), uploads
# must live in the persistent data dir, not next to this file — a release
# folder is replaced wholesale on every update, and anything stored
# relative to it would be orphaned (or deleted outright, once old releases
# get pruned) the moment that happens.
_data_dir = os.environ.get("VETCLINICSYSTEMJO_DATA_DIR")
UPLOAD_ROOT = (os.path.join(_data_dir, "attachments", "uploads") if _data_dir
               else os.path.join(os.path.dirname(__file__), "uploads"))
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
    """
    Saves an uploaded attachment: DB row first, then the file on disk,
    committing only once both have actually succeeded.

    Ordering matters here. The old order — write the file, then insert +
    commit the DB row — meant a DB failure after a successful disk write
    left an orphan clinical file with no record pointing at it: invisible
    to the app, never cleaned up, never shown to staff. Inserting the row
    first means a failure to write the file just rolls back that
    still-open insert (nothing on disk to clean up, nothing to un-commit).
    The one remaining edge — the file write succeeds but the *commit*
    itself then fails (e.g. connection dropped between insert and
    commit) — is handled explicitly below by removing the just-written
    file, so we don't trade "orphan file, no row" for "orphan file,
    uncommitted row that never existed as far as the app is concerned".
    """
    ok, reason = validate_file(file_storage)
    if not ok:
        return None, reason

    key = record_key(record_type, record_id)
    folder = os.path.join(UPLOAD_ROOT, str(patient_id), key)
    safe_name = _safe_name(file_storage.filename)
    disk_path = os.path.join(folder, safe_name)
    relative_path = os.path.join(str(patient_id), key, safe_name)
    visit_id = record_id if record_type == "visit" else None
    case_id = record_id if record_type == "inpatient" else None

    cur = db.execute(
        "INSERT INTO attachments (patient_id, visit_id, inpatient_case_id, relative_path, original_name, uploaded_at, uploaded_by) "
        "VALUES (?,?,?,?,?,?,?) RETURNING id",
        (patient_id, visit_id, case_id, relative_path, file_storage.filename,
         datetime.now().isoformat(timespec="seconds"), uploaded_by),
    )
    attachment_id = cur.fetchone()["id"]
    auth.log_change(db, "attachments", str(attachment_id), "create")

    try:
        os.makedirs(folder, exist_ok=True)
        file_storage.save(disk_path)
    except OSError as e:
        db.rollback()
        return None, f"Couldn't save the file to disk: {e}"

    try:
        db.commit()
    except Exception:
        # The row didn't actually make it into the database after all —
        # remove the file we just wrote rather than leave an orphan with
        # no attachments row to ever reference it.
        try:
            if os.path.exists(disk_path):
                os.remove(disk_path)
        except OSError:
            pass
        raise

    return relative_path, None


def list_attachments(db, record_type, record_id):
    if record_type == "visit":
        return db.execute("SELECT * FROM attachments WHERE visit_id=? ORDER BY uploaded_at DESC", (record_id,)).fetchall()
    return db.execute("SELECT * FROM attachments WHERE inpatient_case_id=? ORDER BY uploaded_at DESC", (record_id,)).fetchall()


def get_attachment(db, attachment_id):
    return db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()


def delete_attachment(db, attachment_id):
    """
    Deletes one uploaded test/X-ray: removes the file on disk, then the
    DB row — in that order, and only removes the DB row if the file
    delete actually succeeded (or the file was already gone). Caller is
    responsible for the audit_log entry (auth.log_change) and
    db.commit(), same as every other delete route in this app.

    Order matters here, the same way it does in save_attachment(). The
    old order — delete the DB row, then best-effort delete the file and
    swallow any failure — meant a filesystem error (permissions, a
    locked file, a network drive hiccup) silently destroyed the app's
    only pointer to a real clinical file while leaving that file sitting
    on disk, invisible to the app and to anyone looking for it. Doing
    the file first means: if that fails, the DB row is left exactly as
    it was — the attachment is still visible, still downloadable, and
    staff get a clear error instead of a silent, un-fixable orphan.

    Returns (deleted_row: dict|None, error: str|None):
      - (row, None) on success
      - (None, None) if no attachment with that id existed
      - (None, error) if the row exists but the file couldn't be removed
        (nothing was changed in the database in this case)
    """
    row = get_attachment(db, attachment_id)
    if not row:
        return None, None
    disk_path = os.path.join(UPLOAD_ROOT, row["relative_path"])
    try:
        if os.path.exists(disk_path):
            os.remove(disk_path)
    except OSError as e:
        return None, f"Couldn't remove the file from disk ({e}) — the attachment was not deleted."
    db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    return dict(row), None
