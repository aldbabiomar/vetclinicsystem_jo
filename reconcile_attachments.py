"""
Reconciles attachment files on disk with the `attachments` table after a
database restore.

backup.py's backup/restore only covers the Postgres database — the actual
files under uploads/ are never included (see backup.py, attachments.py).
Restoring an older backup rewinds `attachments` (and `id_counters` / the
inpatient_cases identity sequence) back to that backup's state, so any
attachment uploaded after the backup was taken loses its DB row even
though its file is still sitting on disk. attachments.py's own
serve_attachment() / list_attachments() only ever look through that table
(never scan disk), so an orphaned file is invisible to the app until its
row is put back.

Because IDs are rewound too, a NEW visit/inpatient case created after the
restore can be handed the exact same ID an orphaned file's folder name
already encodes — readopting a file in that situation would attach it to
the wrong (unrelated) record. This script tells the two cases apart by
comparing each file's embedded upload timestamp against the snapshot time
of the backup that was actually restored (parsed from restore_log, not
just "now"): before that time, the file's original record survived the
restore intact and readopting is safe; after it, the file was in the gap
that got wiped and its IDs may have been reissued, so it's flagged for a
human to check instead.

Usage:
    python3 reconcile_attachments.py                 # dry run — report only, changes nothing
    python3 reconcile_attachments.py --apply         # actually insert the missing rows
    python3 reconcile_attachments.py --check-missing # the OTHER direction: attachments
                                                      # rows whose file is gone from disk
                                                      # (report only — never deletes a row)

Safe to re-run: already-linked files are skipped every time, and a dry
run never writes anything (to the database or to disk — this script never
deletes or moves a file, only ever adds a missing DB row). Recommended to
run once against a copy of the database (or right after a restore, before
much new data has been created on top of it) rather than as a matter of
routine.
"""
import os
import re
import sys
from datetime import datetime

os.environ.setdefault("_RECONCILE_BASE_DIR", os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
_data_dir = os.environ.get("VETCLINICSYSTEMJO_DATA_DIR")
if _data_dir:
    load_dotenv(os.path.join(_data_dir, ".env"))
else:
    load_dotenv()

import db as dbmod
import attachments as attach_mod
import backup as backup_mod

# Matches attachments.py's _safe_name(): "<14-digit timestamp>_<6 hex>_<original name>".
FILENAME_RE = re.compile(r"^(\d{14})_[0-9a-f]{6}_(.+)$")
# Matches backup.py's FILENAME_PREFIX/FILENAME_SUFFIX naming exactly.
BACKUP_FILENAME_RE = re.compile(r"^vetclinicsystemjo_backup_(\d{8}_\d{6})\.dump$")

LOG_DIR = os.path.join(_data_dir or os.environ["_RECONCILE_BASE_DIR"], "logs")
LOG_PATH = os.path.join(LOG_DIR, "reconcile_attachments.log")


def _log(line):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {line}\n")


def parse_filename(name):
    """Returns (uploaded_at: datetime, reconstructed_name: str), or None if
    `name` doesn't match the pattern attachments.py always writes (e.g. a
    stray file dropped in by hand, or a .DS_Store). The reconstructed name
    is the SANITIZED name attachments.py wrote to disk
    (re.sub(r"[^A-Za-z0-9_.-]", "_", ...) at upload time) — the true
    original filename (spaces, unicode, etc.) was never persisted anywhere
    once sanitized, so this is a best-effort stand-in, not a guaranteed
    exact match of what the uploader originally called the file."""
    m = FILENAME_RE.match(name)
    if not m:
        return None
    try:
        uploaded_at = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return uploaded_at, m.group(2)


def resolve_record_key(key):
    """Reverses attachments.py's record_key(). Returns ("visit", visit_id)
    or ("inpatient", case_id), or None if `key` matches neither pattern.

    Unlike IQ, JO's record_key() special-cases visits to avoid a doubled
    prefix: a visit's record_id (db.next_id(db, "V")) is already "V0042"
    by the time it reaches record_key(), so JO's version returns
    str(record_id) as-is for visits — the on-disk folder is "V0042", not
    "VV0042". Inpatient case IDs are plain integers (an IDENTITY column),
    so that branch matches IQ: "IC" + 7 = "IC7"."""
    if key.startswith("IC"):
        try:
            return "inpatient", int(key[2:])
        except ValueError:
            return None
    if key.startswith("V"):
        return "visit", key
    return None


def _cutoff_from_restore_log(db):
    """Returns the datetime the most recently restored backup was itself
    CREATED — not when the restore ran, which is a materially different
    (later) moment. Parsed from restore_log.source_file, whose filename
    backup.py always writes as vetclinicsystemjo_backup_YYYYMMDD_HHMMSS.dump.

    This is the correct cutoff, not restore_log.started_at: a file
    uploaded before the backup was taken is faithfully represented in the
    restored data (its original record survived the restore under the
    same ID, so readopting it is safe). A file uploaded after the backup
    was taken was never captured in that snapshot — its DB row is gone,
    and because ID allocation (id_counters / the inpatient_cases identity
    sequence) rewound to the backup's state too, that same ID may already
    have been handed to an unrelated new record since the restore.
    Returns None if there's no successful restore on record, or its
    filename doesn't match backup.py's naming (e.g. restored from a
    manually renamed file)."""
    row = db.execute(
        "SELECT source_file FROM restore_log WHERE status='success' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row or not row["source_file"]:
        return None
    m = BACKUP_FILENAME_RE.match(os.path.basename(row["source_file"]))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _cutoff_from_marker_file():
    """The marker file (backup.py's _write_restore_marker) catches the
    case restore_log can't: pg_restore --clean drops restore_log itself,
    so a process killed between the data wipe and _try_log_restore()'s
    write leaves no DB row at all, even though the restore genuinely
    happened. 'in_progress'/'success' both count as real evidence a
    restore ran — 'started_at' is used as the (conservative, slightly
    early) cutoff in either case. 'no_restore_since_boot' is not a cutoff
    by itself — it's a diagnostic breadcrumb proving the boot-time check
    ran, not a positive restore event. See ORPHANED_RECORDS_AUDIT.md F-20."""
    marker = backup_mod.read_restore_marker()
    if not marker or marker.get("status") not in ("in_progress", "success"):
        return None
    try:
        return datetime.fromisoformat(marker["started_at"]) if marker.get("started_at") else None
    except ValueError:
        return None


def restored_backup_snapshot_time(db):
    """Combines both cutoff sources — see _cutoff_from_restore_log() and
    _cutoff_from_marker_file() above — taking whichever is later, since
    either one alone can miss a restore the other one catches."""
    candidates = [c for c in (_cutoff_from_restore_log(db), _cutoff_from_marker_file()) if c]
    return max(candidates) if candidates else None


def check_missing():
    """The reverse direction from the rest of this script: a row in
    `attachments` whose file is no longer on disk (uploads/ isn't part of
    the database backup, so a restore can leave a row pointing at nothing).
    Report only — never deletes a row, since the record itself is still
    legitimate history even if the file behind it is gone. See
    ORPHANED_RECORDS_AUDIT.md F-12."""
    db = dbmod.connect()
    try:
        rows = db.execute(
            "SELECT id, patient_id, visit_id, inpatient_case_id, relative_path, "
            "original_name, uploaded_at FROM attachments"
        ).fetchall()
    finally:
        db.close()
    missing = [r for r in rows if not os.path.isfile(os.path.join(attach_mod.UPLOAD_ROOT, r["relative_path"]))]
    print(f"Checked {len(rows)} attachment row(s) against disk.\n")
    print(f"Missing file, record intact ({len(missing)}):")
    for r in missing:
        print(f"  id={r['id']} path={r['relative_path']} original_name={r['original_name']!r} "
              f"uploaded_at={r['uploaded_at']}")
    if not missing:
        print("  (none)")


def main():
    if "--check-missing" in sys.argv:
        check_missing()
        return
    apply = "--apply" in sys.argv
    if apply:
        print("Running with --apply — missing attachments rows will be inserted.")
        print("Recommended: run this against a copy of the database first if you haven't already.\n")
    else:
        print("Dry run — nothing will be changed. Pass --apply to actually insert rows.\n")

    db = dbmod.connect()
    try:
        cutoff = restored_backup_snapshot_time(db)
        if cutoff:
            print(f"Most recently restored backup was taken at {cutoff.isoformat()} — files uploaded "
                  f"after that are flagged for manual review instead of being auto-readopted.\n")
        else:
            # Fail closed, not open — see ORPHANED_RECORDS_AUDIT.md F-20.
            # No cutoff could be determined (no restore_log row, no marker
            # file) means "unknown," not "provably never happened" — the
            # boot-time marker (backup.ensure_no_restore_marker) is what
            # makes "no restore" a checked fact rather than an assumption;
            # if even that's missing, treat every file as potentially
            # post-restore rather than assume it's safe.
            print("No restore cutoff could be determined. Every orphaned file will be flagged for\n"
                  "manual review rather than readopted.\n")
            cutoff = datetime.min

        if not os.path.isdir(attach_mod.UPLOAD_ROOT):
            print(f"No uploads folder at {attach_mod.UPLOAD_ROOT} — nothing to do.")
            return

        already_fine, no_home, needs_review, readopted = [], [], [], []

        for patient_id in sorted(os.listdir(attach_mod.UPLOAD_ROOT)):
            patient_dir = os.path.join(attach_mod.UPLOAD_ROOT, patient_id)
            if not os.path.isdir(patient_dir):
                continue
            for key in sorted(os.listdir(patient_dir)):
                key_dir = os.path.join(patient_dir, key)
                if not os.path.isdir(key_dir):
                    continue
                resolved = resolve_record_key(key)

                for name in sorted(os.listdir(key_dir)):
                    if name.startswith("."):
                        continue
                    full_path = os.path.join(key_dir, name)
                    if not os.path.isfile(full_path):
                        continue
                    relative_path = os.path.join(patient_id, key, name)

                    if not resolved:
                        no_home.append({"path": relative_path, "reason": f"unrecognized folder name {key!r}"})
                        continue
                    record_type, record_id = resolved

                    parsed = parse_filename(name)
                    if not parsed:
                        no_home.append({"path": relative_path, "reason": "filename doesn't match the expected pattern"})
                        continue
                    uploaded_at, reconstructed_name = parsed

                    existing = db.execute(
                        "SELECT id FROM attachments WHERE relative_path=?", (relative_path,)
                    ).fetchone()
                    if existing:
                        already_fine.append(relative_path)
                        continue

                    if record_type == "visit":
                        record = db.execute(
                            "SELECT id FROM visits WHERE id=? AND patient_id=?", (record_id, patient_id)
                        ).fetchone()
                    else:
                        record = db.execute(
                            "SELECT id FROM inpatient_cases WHERE id=? AND patient_id=?", (record_id, patient_id)
                        ).fetchone()

                    if not record:
                        no_home.append({
                            "path": relative_path,
                            "reason": f"no {record_type} {record_id} for patient {patient_id} in the current database",
                        })
                        continue

                    if cutoff and uploaded_at > cutoff:
                        needs_review.append({
                            "path": relative_path, "record_type": record_type, "record_id": record_id,
                            "patient_id": patient_id, "uploaded_at": uploaded_at.isoformat(),
                            "reason": "uploaded after the restored backup was taken — the matching "
                                      "record today may not be the one this file actually belongs to",
                        })
                        continue

                    entry = {
                        "path": relative_path, "record_type": record_type, "record_id": record_id,
                        "patient_id": patient_id, "original_name": reconstructed_name,
                        "uploaded_at": uploaded_at.isoformat(),
                    }
                    readopted.append(entry)
                    if apply:
                        db.execute(
                            "INSERT INTO attachments (patient_id, visit_id, inpatient_case_id, relative_path, "
                            "original_name, uploaded_at, uploaded_by) VALUES (?,?,?,?,?,?,?)",
                            (patient_id, record_id if record_type == "visit" else None,
                             record_id if record_type == "inpatient" else None,
                             relative_path, reconstructed_name, uploaded_at.isoformat(timespec="seconds"), None),
                        )
                        _log(f"readopted patient={patient_id} {record_type}={record_id} path={relative_path}")

        if apply and readopted:
            db.commit()
    finally:
        db.close()

    def section(title, items):
        print(f"\n{title} ({len(items)}):")
        for it in items:
            print(f"  {it}")

    section("Already linked — nothing to do", already_fine)
    section("No matching record found in the current database (left alone, not deleted)", no_home)
    section("Needs manual review — possible ID reuse since the restore", needs_review)
    section("Readopted" if apply else "Would readopt with --apply", readopted)

    print(f"\n{'Applied' if apply else 'Dry run'}: {len(readopted)} readopted, {len(no_home)} with no home, "
          f"{len(needs_review)} need manual review, {len(already_fine)} already fine.")
    if not apply and readopted:
        print("Re-run with --apply to actually insert the missing attachment rows.")
    if needs_review:
        print(f"\n{len(needs_review)} file(s) need a human to check whether the current record with that "
              f"ID is really the same one this file belongs to before it's readopted by hand.")


if __name__ == "__main__":
    main()
