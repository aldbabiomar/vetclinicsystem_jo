"""
Database backups for VetClinicSystem JO.

Runs `pg_dump` against the running Postgres database and writes a
timestamped, restorable dump file into whatever folder is configured on
the Settings page. Tries a local `pg_dump` binary first (native Postgres
installs); if that isn't available, falls back to running it inside the
Docker container via `docker exec` (works with the docker-compose.yml
setup with nothing extra to install).

To restore a backup later:
    pg_restore --clean --if-exists -d <DATABASE_URL> <path-to-.dump-file>
"""
import json
import os
import shutil
import subprocess
from urllib.parse import unquote, urlsplit
import threading
from datetime import datetime

import logic

FILENAME_PREFIX = "vetclinicsystemjo_backup_"
FILENAME_SUFFIX = ".dump"

# Same VETCLINICSYSTEMJO_DATA_DIR convention attachments.py uses — this
# file must survive an in-app update (which replaces the release folder
# this module lives in), so it's anchored in the persistent data dir, not
# next to this file. See ORPHANED_RECORDS_AUDIT.md F-20.
_data_dir = os.environ.get("VETCLINICSYSTEMJO_DATA_DIR")
_RESTORE_MARKER_PATH = os.path.join(_data_dir, "last_restore.json") if _data_dir \
    else os.path.join(os.path.dirname(__file__), "last_restore.json")


def _write_restore_marker(status, dump_path=None, started=None):
    """reconcile_attachments.py's entire safety argument rests on knowing
    *when* the most recently restored backup was taken — but pg_restore
    --clean drops and recreates every table, including restore_log itself,
    and the row recording a successful restore is written only after the
    wipe (see _try_log_restore, best-effort). If the process dies in that
    window, the restore happened but left no DB record. This marker file
    lives outside the database entirely so it survives that exact failure
    mode — written 'in_progress' immediately before pg_restore runs, then
    updated to 'success' or 'failed' once it's known. See F-20."""
    try:
        tmp = _RESTORE_MARKER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "status": status,
                "source_file": dump_path,
                "started_at": started.isoformat(timespec="seconds") if started else None,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _RESTORE_MARKER_PATH)  # atomic
    except OSError:
        pass


def read_restore_marker():
    """Returns the marker dict written by _write_restore_marker()/
    ensure_no_restore_marker(), or None if it doesn't exist or is
    unreadable. Public reader for reconcile_attachments.py — see F-20."""
    try:
        with open(_RESTORE_MARKER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ensure_no_restore_marker():
    """Called once at app boot (app.py). Writes a 'no_restore_since_boot'
    marker unless the existing marker already records a real restore
    ('in_progress'/'success') — never overwrites actual restore evidence.
    This is what makes 'no restore has happened' a provable fact an
    operator can check, rather than an absence of evidence. See F-20."""
    try:
        with open(_RESTORE_MARKER_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if existing.get("status") in ("in_progress", "success"):
            return
    except (OSError, ValueError):
        pass
    _write_restore_marker("no_restore_since_boot")

# Held for the duration of a backup, restore, or in-app update (see
# updater.py) so none of those three can start while another is already
# running against the same database. An RLock, not a plain Lock — an
# update's apply_update() (updater.py) holds this lock for its whole run
# and, on the very same thread, also calls into run_backup() as its own
# first step; a plain Lock would fail to re-acquire itself there.
maintenance_lock = threading.RLock()


def _pg_conn_parts():
    """Pull user/password/db/host/port out of DATABASE_URL for pg_dump and
    pg_restore.

    The password matters as much as the rest: without it libpq falls back to
    prompting, and it reads that prompt from the controlling terminal rather
    than stdin — so a backup launched from the app would sit forever behind a
    "Password:" in whatever Terminal window the launcher happens to own,
    looking to everyone like it had simply hung. Installs without the
    Postgres client tools never hit this, because the docker exec fallback
    below connects over the container's local socket instead."""
    url = os.environ.get("DATABASE_URL", "")
    try:
        parts = urlsplit(url)
        user = unquote(parts.username or "")
        password = unquote(parts.password or "")
        dbname = (parts.path or "").lstrip("/")
        host = parts.hostname or "127.0.0.1"
        port = str(parts.port or 5432)
        if not (user and dbname):
            raise ValueError("DATABASE_URL missing user or database name")
        return user, password, dbname, host, port
    except Exception:
        return "vetclinicsystemjo", "", "vetclinicsystemjo", "127.0.0.1", "5432"


def _pg_env(password):
    """A child environment carrying the password, so pg_dump/pg_restore never
    need to ask for one."""
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password
    return env


def _run_pg_dump(out_path):
    user, password, dbname, host, port = _pg_conn_parts()
    env = _pg_env(password)

    if shutil.which("pg_dump"):
        cmd = ["pg_dump", "-w", "-h", host, "-p", port, "-U", user, "-F", "c", "-f", out_path, dbname]
        subprocess.run(cmd, check=True, env=env, capture_output=True, text=True)
        # The dump contains full patient/owner PHI (names, phones,
        # addresses, medical history) and the configured backup folder is
        # explicitly documented as sometimes being a synced folder like
        # Google Drive/OneDrive — leaving this at the process's default
        # umask could make it group/world-readable on a shared machine.
        # Owner-only, same as any other secret this app writes to disk.
        os.chmod(out_path, 0o600)
        return

    container = os.environ.get("VETCLINICSYSTEMJO_PG_CONTAINER", "vetclinicsystemjo_postgres")
    if shutil.which("docker"):
        cmd = ["docker", "exec", "-e", f"PGPASSWORD={password}", container,
               "pg_dump", "-w", "-U", user, "-F", "c", dbname]
        with open(out_path, "wb") as f:
            subprocess.run(cmd, check=True, stdout=f, stderr=subprocess.PIPE)
        os.chmod(out_path, 0o600)
        return

    raise RuntimeError(
        "Could not find pg_dump locally or the 'docker' command — "
        "install Docker Desktop (recommended) or the PostgreSQL client tools."
    )


def resolve_restorable_backup(db, source_file):
    """
    Confines what settings_restore_now() (app.py) will accept as a restore
    source. Without this, any user with manage_settings could point the
    restore endpoint at *any* readable .dump file path on the machine —
    the folder browser used to pick one can browse the whole filesystem,
    and a naive check would only be "does this path exist and end in
    .dump". That's full data-loss capability from an arbitrary path.

    Two independent checks, both required:
      1. Path confinement — the file must resolve (symlinks included) to
         somewhere inside the currently-configured backup_dir. Blocks an
         absolute path elsewhere on disk and a "../.." traversal alike.
      2. Provenance — the exact path must be recorded in backup_log with
         status='success', i.e. it's a file THIS app's own Backup Now /
         nightly job actually produced, not merely a same-named file
         someone placed in that folder by other means (filesystem
         access, a copied file from another install, etc).

    Deliberately does NOT require a second admin's approval — that's a
    process control, not this function's job; this closes the "arbitrary
    path" hole while leaving restore a single-admin action.

    Returns (ok: bool, resolved_path: str|None, message: str|None).
    """
    if not source_file:
        return False, None, "Choose a backup file to restore from."
    if not source_file.endswith(FILENAME_SUFFIX):
        return False, None, f"That doesn't look like a VetClinicSystem JO backup file (expected a {FILENAME_SUFFIX} file)."

    backup_dir = logic.get_setting(db, "backup_dir")
    if not backup_dir:
        return False, None, "No backup folder is configured yet — set one on the Settings page."

    backup_dir_real = os.path.realpath(backup_dir)
    source_real = os.path.realpath(source_file)
    try:
        inside = os.path.commonpath([backup_dir_real, source_real]) == backup_dir_real
    except ValueError:
        # commonpath raises ValueError when the paths don't share a root
        # (e.g. different drive letters on Windows) — definitely outside.
        inside = False
    if not inside:
        return False, None, "That file isn't inside the configured backup folder."

    if not os.path.isfile(source_file):
        return False, None, "Choose a valid backup file to restore from."

    # Matched against the exact string as submitted (not the realpath'd
    # form above) — that's what run_backup()/_log() actually wrote into
    # backup_log.filepath, since dest_dir there is the backup_dir setting
    # as configured, not a canonicalized path.
    row = db.execute(
        "SELECT id FROM backup_log WHERE filepath=? AND status='success'",
        (source_file,),
    ).fetchone()
    if not row:
        return False, None, (
            "That file isn't in this app's own backup history — restore is only allowed for "
            "backups VetClinicSystem JO itself created (see Recent Backups on the Settings page)."
        )
    return True, source_file, None


def _pg_restore_toc_count(list_cmd):
    """
    Runs `pg_restore --list` (locally or via docker exec, whichever
    list_cmd specifies) and counts real entries in the table of contents,
    for a real X-of-Y figure during the restore rather than a guess. TOC
    lines look like "123; 1259 12345 TABLE public visits vetclinicsystemjo" —
    comment/blank lines (starting with ';', or empty) don't count.
    Returns None if the listing fails for any reason; callers fall back
    to phase-only progress when that happens.
    """
    try:
        result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    count = sum(1 for line in result.stdout.splitlines()
                if line.strip() and not line.lstrip().startswith(";"))
    return count or None


def _stream_restore_progress(proc, total, on_count):
    """
    Reads pg_restore --verbose's stderr as it runs and counts
    recognizable per-object lines ("processing item ...", "creating ...",
    "restoring data for ...") against the TOC count above, calling
    on_count(done, total) as real progress comes in. Collects stderr text
    for the error message if the run ultimately fails.
    """
    done = 0
    lines = []
    markers = (" processing ", " creating ", " restoring data ")
    for raw_line in proc.stderr:
        lines.append(raw_line)
        if total and any(m in raw_line for m in markers):
            done += 1
            if on_count:
                on_count(min(done, total), total)
    proc.wait()
    return "".join(lines)


def _run_pg_restore(dump_path, on_count=None):
    """on_count(done, total), if given, is called with real counts as
    pg_restore reports each object it processes — see
    _stream_restore_progress above. total may be None (TOC listing
    failed), in which case the caller just doesn't get sub-step detail."""
    user, password, dbname, host, port = _pg_conn_parts()
    env = _pg_env(password)
    # A safety net, not the primary fix (that's closing the calling
    # request's own connection before this runs — see settings_restore_now
    # in app.py). This just makes sure that if some OTHER connection ever
    # holds a lock during a restore — a second admin with Settings open in
    # another tab, say — pg_restore fails fast with a clear "canceling
    # statement due to lock timeout" error instead of hanging silently
    # for hours with nothing to show for it.
    env["PGOPTIONS"] = "-c lock_timeout=30000"

    if shutil.which("pg_restore"):
        total = _pg_restore_toc_count(["pg_restore", "--list", dump_path])
        cmd = ["pg_restore", "-w", "-h", host, "-p", port, "-U", user, "-d", dbname,
               "--clean", "--if-exists", "--verbose", dump_path]
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True)
        stderr_text = _stream_restore_progress(proc, total, on_count)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, output=None, stderr=stderr_text)
        return

    container = os.environ.get("VETCLINICSYSTEMJO_PG_CONTAINER", "vetclinicsystemjo_postgres")
    if shutil.which("docker"):
        # docker exec can't read a file straight off the host, so the dump
        # has to be copied into the container first.
        container_path = "/tmp/" + os.path.basename(dump_path)
        subprocess.run(["docker", "cp", dump_path, f"{container}:{container_path}"],
                        check=True, capture_output=True, text=True)
        try:
            total = _pg_restore_toc_count(["docker", "exec", container, "pg_restore", "--list", container_path])
            cmd = ["docker", "exec", "-e", "PGOPTIONS=-c lock_timeout=30000",
                   "-e", f"PGPASSWORD={password}",
                   container, "pg_restore", "-w", "-U", user, "-d", dbname,
                   "--clean", "--if-exists", "--verbose", container_path]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True)
            stderr_text = _stream_restore_progress(proc, total, on_count)
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, cmd, output=None, stderr=stderr_text)
        finally:
            subprocess.run(["docker", "exec", container, "rm", "-f", container_path],
                            capture_output=True, text=True)
        return

    raise RuntimeError(
        "Could not find pg_restore locally or the 'docker' command — "
        "install Docker Desktop (recommended) or the PostgreSQL client tools."
    )


def run_restore(get_fresh_db, dump_path, triggered_by=None, on_progress=None):
    """Acquires maintenance_lock (see its own comment) before running the
    actual restore in _run_restore_locked(). Returns (ok: bool, message:
    str) immediately, without touching anything, if another backup/
    restore/update is already in progress."""
    if not maintenance_lock.acquire(blocking=False):
        return False, "Another backup, restore, or update is already running — try again once it finishes."
    try:
        return _run_restore_locked(get_fresh_db, dump_path, triggered_by, on_progress)
    finally:
        maintenance_lock.release()


def _run_restore_locked(get_fresh_db, dump_path, triggered_by=None, on_progress=None):
    """
    Restores the database from a backup .dump file, replacing ALL current
    data with whatever that backup contained.

    Takes get_fresh_db — a zero-arg connection factory (e.g. db.connect),
    NOT an existing open connection — because pg_restore --clean drops and
    recreates every table in the database, including restore_log and
    backup_log themselves. Any connection (or log row) from before the
    restore is unsafe to keep using afterward: a row inserted before
    running pg_restore would just get wiped out along with everything else
    the old data held, which is why the completion entry is written with a
    brand new connection only once the restore has actually finished.

    on_progress(step_index, label=None), if given, is called at each real
    phase transition, plus with live sub-counts ("Restoring database
    (42/128 objects)") as pg_restore reports its own progress — the same
    shape jobs.py's update() uses, so a caller running this inside
    jobs.start() can just pass that straight through.

    Returns (ok: bool, message: str).
    """
    def step(i, label=None):
        if on_progress:
            on_progress(i, label)

    step(0)  # Checking backup file
    if not dump_path or not os.path.isfile(dump_path):
        return False, "Choose a valid backup file to restore from."
    if not dump_path.endswith(FILENAME_SUFFIX):
        return False, f"That doesn't look like a VetClinicSystem JO backup file (expected a {FILENAME_SUFFIX} file)."

    started = datetime.now()
    step(1, "Restoring database")

    def on_count(done, total):
        step(1, f"Restoring database ({done}/{total} objects)")

    _write_restore_marker("in_progress", dump_path, started)
    try:
        _run_pg_restore(dump_path, on_count=on_count)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "").strip() or str(e)
        _write_restore_marker("failed", dump_path, started)
        _try_log_restore(get_fresh_db, "failed", dump_path, err, started, triggered_by)
        return False, (f"Restore failed: {err} — the database may be in a partially restored "
                        f"state. Check it carefully before continuing to use the app.")
    except Exception as e:
        _write_restore_marker("failed", dump_path, started)
        _try_log_restore(get_fresh_db, "failed", dump_path, str(e), started, triggered_by)
        return False, f"Restore failed: {e}"
    # The data itself is restored (and IDs rewound) at this point,
    # regardless of whether the schema-reconcile step below succeeds —
    # marked here, not at the very end, since this is the actual moment
    # reconcile_attachments.py's safety concern applies from.
    _write_restore_marker("success", dump_path, started)

    step(2, "Reconciling schema")
    # A backup taken before this running app version shipped a schema
    # change (a new column/table, or a retroactive permission grant —
    # see setup.INCREMENTAL_SCHEMA_STATEMENTS) restores the database back
    # to that older shape. Nothing else re-syncs it afterward — the code
    # currently running keeps serving requests against what it expects,
    # not what actually got restored — so without this, features that
    # depend on anything added since the backup start failing with
    # "column does not exist" (or, for a permission grant, staff losing
    # access to a page they should have) until the next in-app update
    # happens to run setup.apply_schema() again. These statements are
    # additive-only and idempotent by the same contract that lets them
    # run on every normal app startup, so re-running them here is safe.
    #
    # JO's apply_schema() already runs apply_incremental_migrations()
    # internally as its last step (unlike IQ, where the two are separate
    # top-level calls) — call only apply_schema() here, since calling
    # apply_incremental_migrations() again afterward would both be
    # redundant and, worse, TypeError on JO's version of that function,
    # which requires a connection argument IQ's zero-arg version doesn't
    # take.
    try:
        import setup
        setup.apply_schema()
    except Exception as e:
        err = (f"Restore succeeded, but bringing the restored database up to this app version's "
                f"schema failed: {e}. The data is restored, but some newer features may not work "
                f"until this is resolved.")
        _try_log_restore(get_fresh_db, "failed", dump_path, err, started, triggered_by)
        return False, err

    step(3)  # Recording result
    _try_log_restore(get_fresh_db, "success", dump_path, None, started, triggered_by)
    step(4)  # Done
    return True, f"Restored from {dump_path}"


def _try_log_restore(get_fresh_db, status, dump_path, error, started, triggered_by):
    """Best-effort logging — a failed restore may have left the database in
    a state where even this can't succeed, and that shouldn't mask the
    original restore error from the caller."""
    try:
        db = get_fresh_db()
        try:
            db.execute(
                "INSERT INTO restore_log (started_at, finished_at, status, source_file, error, triggered_by) "
                "VALUES (?,?,?,?,?,?)",
                (started.isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"),
                 status, dump_path, error, triggered_by),
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def recent_restores(db, limit=10):
    return db.execute("SELECT * FROM restore_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def run_backup(db, dest_dir=None, retention=None, triggered_by=None, on_progress=None):
    """Acquires maintenance_lock (see its own comment) before running the
    actual backup in _run_backup_locked(). Returns (ok: bool, message: str)
    immediately, without touching anything, if another backup/restore/
    update is already running."""
    if not maintenance_lock.acquire(blocking=False):
        msg = "Another backup, restore, or update is already running — try again once it finishes."
        _log(db, "failed", None, None, msg)
        return False, msg
    try:
        return _run_backup_locked(db, dest_dir, retention, triggered_by, on_progress)
    finally:
        maintenance_lock.release()


def _run_backup_locked(db, dest_dir=None, retention=None, triggered_by=None, on_progress=None):
    """
    Performs one backup, applies retention, and logs the outcome to
    backup_log. Returns (ok: bool, message: str).

    on_progress(step_index, label=None), if given, is called at each real
    phase transition (checking the folder, running pg_dump, applying
    retention) — the same three-argument shape jobs.py's update() uses, so
    a caller running this inside jobs.start() can just pass that straight
    through.
    """
    def step(i, label=None):
        if on_progress:
            on_progress(i, label)

    dest_dir = dest_dir or logic.get_setting(db, "backup_dir")
    if not dest_dir:
        # Deliberately not written to backup_log: nothing was attempted, and a
        # nightly job with no folder set would otherwise fill Recent Backups
        # with failures and bury real ones. The Dashboard already reports this
        # state on its own via logic.backup_alert_message().
        msg = "No backup folder configured yet — set one on the Settings page."
        return False, msg

    retention = retention or int(logic.get_setting(db, "backup_retention", "30") or 30)

    step(0)  # Checking backup folder
    try:
        os.makedirs(dest_dir, exist_ok=True)
        probe = os.path.join(dest_dir, ".vetclinicsystemjo_write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        msg = f"Backup folder isn't writable: {e}"
        _log(db, "failed", None, None, msg)
        return False, msg

    started = datetime.now()
    filename = f"{FILENAME_PREFIX}{started.strftime('%Y%m%d_%H%M%S')}{FILENAME_SUFFIX}"
    out_path = os.path.join(dest_dir, filename)

    log_id = _log(db, "running", None, None, None, started=started)

    step(1)  # Dumping database
    try:
        _run_pg_dump(out_path)
        size = os.path.getsize(out_path)
        _finish_log(db, log_id, "success", out_path, size, None)
        step(2)  # Applying retention policy
        _apply_retention(dest_dir, retention)
        step(3)  # Done
        return True, f"Backup saved to {out_path}"
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "").strip() or str(e)
        _finish_log(db, log_id, "failed", out_path, None, err)
        return False, f"Backup failed: {err}"
    except Exception as e:
        _finish_log(db, log_id, "failed", out_path, None, str(e))
        return False, f"Backup failed: {e}"


def _apply_retention(dest_dir, retention):
    files = sorted(
        (f for f in os.listdir(dest_dir) if f.startswith(FILENAME_PREFIX) and f.endswith(FILENAME_SUFFIX)),
        reverse=True,
    )
    for old in files[retention:]:
        try:
            os.remove(os.path.join(dest_dir, old))
        except OSError:
            pass


def _log(db, status, filepath, size, error, started=None):
    ts = (started or datetime.now()).isoformat(timespec="seconds")
    row = db.execute(
        "INSERT INTO backup_log (started_at, status, filepath, filesize_bytes, error) "
        "VALUES (?,?,?,?,?) RETURNING id",
        (ts, status, filepath, size, error),
    ).fetchone()
    db.commit()
    return row["id"]


def _finish_log(db, log_id, status, filepath, size, error):
    db.execute(
        "UPDATE backup_log SET status=?, finished_at=?, filepath=?, filesize_bytes=?, error=? WHERE id=?",
        (status, datetime.now().isoformat(timespec="seconds"), filepath, size, error, log_id),
    )
    db.commit()


def reap_stale_running(db):
    """A 'running' row can only be genuine if this process created it, and
    this process was just started — so anything still 'running' at boot is
    from a run that was killed (machine shut down mid-backup is not exotic
    for a nightly 02:00 job on a single clinic desktop). Left alone, a
    stranded 'running' row actively misleads: backup_alert_message() only
    alarms on 'failed' or a 2+-day-old started_at — 'running' is neither,
    so the Dashboard reports healthy backups for as long as that row sits
    there. Called once at app boot. See ORPHANED_RECORDS_AUDIT.md F-21."""
    n = db.execute(
        "UPDATE backup_log SET status='failed', finished_at=?, "
        "error='Backup did not finish — the app was stopped or the machine shut down "
        "while it was running.' "
        "WHERE status='running' RETURNING id",
        (datetime.now().isoformat(timespec="seconds"),),
    ).rowcount
    db.commit()
    return n


def last_backup(db):
    return db.execute("SELECT * FROM backup_log ORDER BY id DESC LIMIT 1").fetchone()


def recent_backups(db, limit=10):
    return db.execute("SELECT * FROM backup_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
