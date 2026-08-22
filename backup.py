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
import os
import shutil
import subprocess
from datetime import datetime

import logic

FILENAME_PREFIX = "vetclinicsystemjo_backup_"
FILENAME_SUFFIX = ".dump"


def _pg_conn_parts():
    """Pull user/db/host/port out of DATABASE_URL for pg_dump's -U/-d flags."""
    url = os.environ.get("DATABASE_URL", "")
    # postgresql://user:pass@host:port/dbname
    try:
        rest = url.split("://", 1)[1]
        creds, hostpart = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        hostport, dbname = hostpart.split("/", 1)
        host, port = (hostport.split(":", 1) + ["5432"])[:2]
        return user, dbname, host, port
    except Exception:
        return "vetclinicsystemjo", "vetclinicsystemjo", "127.0.0.1", "5432"


def _run_pg_dump(out_path):
    user, dbname, host, port = _pg_conn_parts()
    env = dict(os.environ)

    if shutil.which("pg_dump"):
        cmd = ["pg_dump", "-h", host, "-p", port, "-U", user, "-F", "c", "-f", out_path, dbname]
        subprocess.run(cmd, check=True, env=env, capture_output=True, text=True)
        return

    container = os.environ.get("VETCLINICSYSTEMJO_PG_CONTAINER", "vetclinicsystemjo_postgres")
    if shutil.which("docker"):
        cmd = ["docker", "exec", container, "pg_dump", "-U", user, "-F", "c", dbname]
        with open(out_path, "wb") as f:
            subprocess.run(cmd, check=True, stdout=f, stderr=subprocess.PIPE)
        return

    raise RuntimeError(
        "Could not find pg_dump locally or the 'docker' command — "
        "install Docker Desktop (recommended) or the PostgreSQL client tools."
    )


def run_backup(db, dest_dir=None, retention=None, triggered_by=None):
    """
    Performs one backup, applies retention, and logs the outcome to
    backup_log. Returns (ok: bool, message: str).
    """
    dest_dir = dest_dir or logic.get_setting(db, "backup_dir")
    if not dest_dir:
        msg = "No backup folder configured yet — set one on the Settings page."
        _log(db, "failed", None, None, msg)
        return False, msg

    retention = retention or int(logic.get_setting(db, "backup_retention", "30") or 30)

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

    try:
        _run_pg_dump(out_path)
        size = os.path.getsize(out_path)
        _finish_log(db, log_id, "success", out_path, size, None)
        _apply_retention(dest_dir, retention)
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


def last_backup(db):
    return db.execute("SELECT * FROM backup_log ORDER BY id DESC LIMIT 1").fetchone()


def recent_backups(db, limit=10):
    return db.execute("SELECT * FROM backup_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
