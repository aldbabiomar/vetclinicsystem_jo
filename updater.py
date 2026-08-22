"""
Handles checking for, downloading, and applying updates from GitHub
Releases. Never touches vetclinicsystemjo-data/ except to read/write
active_release.txt, write to logs/updates.log, and invoke backup.py
before an update.

Consumer side of CLAUDE_CODE_RELEASE_WORKFLOW.md / UPDATE_MECHANISM_PLAN.md
— read both before changing this file, since the release format and this
module's expectations of it are one system split across two docs.

Requires this install to be on the versioned-release layout (see
setup.py's --enable-updates) — VETCLINICSYSTEMJO_DATA_DIR and
VETCLINICSYSTEMJO_RELEASES_DIR must both be set. is_configured() is False
otherwise, and the Settings page shows an explanatory message instead of
the update UI.
"""
import os
import io
import json
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import urllib.error

import requests

DATA_DIR = os.environ.get("VETCLINICSYSTEMJO_DATA_DIR")
RELEASES_DIR = os.environ.get("VETCLINICSYSTEMJO_RELEASES_DIR")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

KEEP_RELEASES = 2  # the new one + the one it replaced


def is_configured():
    """True if this install is on the versioned-release layout AND
    GITHUB_REPO is set. Everything else in this module assumes both."""
    return bool(DATA_DIR and RELEASES_DIR and GITHUB_REPO
                and os.path.isdir(DATA_DIR) and os.path.isdir(RELEASES_DIR))


def _pointer_file():
    return os.path.join(DATA_DIR, "active_release.txt")


def _log_path():
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "updates.log")


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def active_release_name():
    if not is_configured():
        return None
    path = _pointer_file()
    if not os.path.isfile(path):
        return None
    return open(path).read().strip()


def current_version():
    """Reads VERSION from the currently active release (release layout),
    or from this codebase's own VERSION file otherwise (pre-migration /
    running directly from a git checkout)."""
    active = active_release_name()
    if active:
        path = os.path.join(RELEASES_DIR, active, "VERSION")
    else:
        path = os.path.join(BASE_DIR, "VERSION")
    return open(path).read().strip() if os.path.exists(path) else "unknown"


def list_releases():
    """Release folder names on disk, newest first by name (app_vX.Y.Z sorts
    correctly for this app's strict-semver tags)."""
    if not is_configured():
        return []
    names = [n for n in os.listdir(RELEASES_DIR) if n.startswith("app_v")
              and os.path.isdir(os.path.join(RELEASES_DIR, n))]
    return sorted(names, reverse=True)


def _api_headers():
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def check_latest_release():
    """GET /repos/{repo}/releases/latest — returns the parsed JSON (tag_name,
    body, tarball_url, published_at). Raises requests.RequestException on
    network failure or a non-2xx response; caller should show 'couldn't
    check for updates' rather than crash."""
    resp = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers=_api_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def is_update_available():
    latest = check_latest_release()
    return latest["tag_name"].lstrip("v") != current_version(), latest


def _run_backup():
    """Backs up the live database before touching anything else — always,
    no exceptions. Uses a dedicated folder under vetclinicsystemjo-data/
    rather than trusting the backup_dir setting, so this never silently
    no-ops just because an admin hasn't configured a backup folder on the
    Settings page. Returns the backup file path, or None on failure."""
    import db as dbmod
    import backup as backup_mod
    dest_dir = os.path.join(DATA_DIR, "backups", "pre_update")
    con = dbmod.connect()
    try:
        ok, message = backup_mod.run_backup(con, dest_dir=dest_dir, retention=5, triggered_by="update")
        if not ok:
            _log(f"backup failed: {message}")
            return None
        _log(f"backup ok: {message}")
        return message
    finally:
        con.close()


def _download_and_extract(tarball_url, dest_path):
    """Downloads the tagged release's source tarball and extracts it into
    dest_path (a NEW folder — the currently running folder is never
    touched). GitHub's tarball wraps everything in one top-level
    "{owner}-{repo}-{sha}/" directory; this strips that so dest_path ends
    up holding app.py etc. directly, matching how the rest of this module
    (and setup.py) expect a release folder to look."""
    resp = requests.get(tarball_url, headers=_api_headers(), timeout=60, stream=True)
    resp.raise_for_status()
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tf:
            tf.extractall(tmp)  # nosec - trusted source: our own tagged GitHub release
        entries = [e for e in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, e))]
        if len(entries) != 1:
            raise RuntimeError(f"Unexpected tarball layout ({len(entries)} top-level entries).")
        shutil.move(os.path.join(tmp, entries[0]), dest_path)


def _validate_release(path, tag_name):
    """Sanity checks before this release is ever installed into a venv or
    run: the VERSION file matches the tag, and every file the app needs
    to boot is actually present. Returns (ok, reason)."""
    version_path = os.path.join(path, "VERSION")
    if not os.path.isfile(version_path):
        return False, "Downloaded release has no VERSION file."
    version = open(version_path).read().strip()
    if f"v{version}" != tag_name:
        return False, f"VERSION file says {version}, but the release tag is {tag_name}."
    for required in ("app.py", "requirements.txt", "schema_postgres.sql"):
        if not os.path.isfile(os.path.join(path, required)):
            return False, f"Downloaded release is missing {required}."
    return True, None


def _venv_python(release_path):
    if sys.platform == "win32":
        return os.path.join(release_path, "venv", "Scripts", "python.exe")
    return os.path.join(release_path, "venv", "bin", "python3")


def _create_venv_and_install(release_path):
    """Each release folder gets its OWN venv — never shared across
    versions, so a dependency change in the new release can't break the
    old one during the window both exist on disk. Raises
    subprocess.CalledProcessError (with output captured) on failure."""
    subprocess.run([sys.executable, "-m", "venv", os.path.join(release_path, "venv")],
                    check=True, capture_output=True, text=True, cwd=release_path)
    py = _venv_python(release_path)
    subprocess.run([py, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                    check=True, capture_output=True, text=True, cwd=release_path)


def _check_imports(release_path):
    """python3 -c "import app" in the release's own venv — catches syntax
    errors and import-time crashes before this release is ever promoted.
    Raises subprocess.CalledProcessError (with stderr captured) on
    failure."""
    py = _venv_python(release_path)
    subprocess.run([py, "-c", "import app"], check=True, capture_output=True, text=True, cwd=release_path)


def _run_schema_sync(release_path):
    """Runs the NEW release's own schema-apply logic (setup.apply_schema,
    which applies both schema_postgres.sql AND
    INCREMENTAL_SCHEMA_STATEMENTS internally) against the shared, live
    database — using the new release's OWN copy of that logic, in case a
    future release changes it. Only ever additive (new column/table with
    a default) per CLAUDE_CODE_RELEASE_WORKFLOW.md §6 step 2 — never a
    drop/rename/type-narrowing, so this is safe to run before the new
    release is actually serving traffic."""
    py = _venv_python(release_path)
    subprocess.run(
        [py, "-c", "import setup; setup.load_dotenv_now(); setup.apply_schema()"],
        check=True, capture_output=True, text=True, cwd=release_path,
    )


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _probe_health(release_path, timeout=20):
    """Starts the new release as a throwaway subprocess on a random free
    port (never the real port — the currently running app keeps serving
    the whole time) and polls its /health endpoint. This is what actually
    proves the new release boots and can reach the database BEFORE the
    live process is ever touched — not just that a bare `import app`
    succeeded. Always terminates the probe process before returning,
    success or failure."""
    py = _venv_python(release_path)
    port = _free_port()
    env = dict(os.environ)
    env["VETCLINICSYSTEMJO_PORT"] = str(port)
    env["VETCLINICSYSTEMJO_HOST"] = "127.0.0.1"
    proc = subprocess.Popen([py, "app.py"], cwd=release_path, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    body = json.loads(r.read())
                    if r.status == 200 and body.get("status") == "ok":
                        return True, None
            except (urllib.error.URLError, ConnectionError, OSError, ValueError):
                pass
            if proc.poll() is not None:
                return False, "The new release's process exited before it became healthy."
            time.sleep(0.5)
        return False, f"The new release didn't pass its health check within {timeout}s."
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _write_pointer(release_name):
    # Write-then-atomic-rename, not a direct truncate-and-write: this file
    # is what tells the launcher script which release folder to run on
    # every start, so a process killed mid-write (power loss, OOM-kill)
    # must never leave it half-written. The temp file has to live in the
    # same directory as the real one — os.replace()'s atomicity guarantee
    # only holds within a single filesystem.
    pointer_path = _pointer_file()
    tmp_path = pointer_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(release_name)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, pointer_path)


def _prune_old_releases(keep):
    for name in list_releases():
        if name in keep:
            continue
        if len(list_releases()) <= KEEP_RELEASES:
            break
        shutil.rmtree(os.path.join(RELEASES_DIR, name), ignore_errors=True)
        _log(f"pruned old release {name}")


def _request_restart(delay=2.0):
    """The only cross-platform-safe way for this process to hand control
    to the newly-promoted release is to exit and let the launcher script's
    supervisor loop (see Start VetClinicSystem JO.command / .bat) restart
    it — that loop is what re-reads active_release.txt and actually starts
    serving the new version. delay gives the HTTP response for the update
    request a moment to flush before the process disappears."""
    def _exit():
        time.sleep(delay)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()


def apply_update(tag_name, tarball_url, on_progress=None):
    """
    Full update flow. Returns (success: bool, message: str).
    on_progress(step_index, label=None), if given, is called at each real
    phase transition — see jobs.py's update() for the shape.

    Nothing about the currently running process is touched until AFTER
    the new release has been downloaded, installed into its own venv,
    schema-synced, and health-probed on a throwaway port — so a failure
    at any point up to and including validation leaves the clinic exactly
    as it was, still on the old version, with a fresh backup taken for
    good measure.

    Holds backup.maintenance_lock for the whole flow — not just its own
    "Backing up database" step — so a manual backup/restore can't start
    partway through an update (e.g. between the schema sync and the
    process restart) any more than an update can start partway through
    one of those.
    """
    import backup as backup_mod
    if not backup_mod.maintenance_lock.acquire(blocking=False):
        return False, "A backup, restore, or another update is already running — try again once it finishes."
    try:
        return _apply_update_locked(tag_name, tarball_url, on_progress)
    finally:
        backup_mod.maintenance_lock.release()


def _apply_update_locked(tag_name, tarball_url, on_progress=None):
    def step(i, label=None):
        if on_progress:
            on_progress(i, label)

    if not is_configured():
        return False, "Updates aren't set up on this install yet — see setup.py --enable-updates."

    new_folder_name = f"app_{tag_name}"
    new_path = os.path.join(RELEASES_DIR, new_folder_name)
    if os.path.isdir(new_path):
        shutil.rmtree(new_path, ignore_errors=True)

    step(0, "Backing up database")
    backup_result = _run_backup()
    if backup_result is None:
        return False, "Backup failed — update aborted, nothing was changed."

    step(1, "Downloading release")
    try:
        _download_and_extract(tarball_url, new_path)
    except (requests.RequestException, RuntimeError, OSError) as e:
        shutil.rmtree(new_path, ignore_errors=True)
        _log(f"download failed for {tag_name}: {e}")
        return False, f"Couldn't download {tag_name} — update aborted, nothing was changed."

    step(2, "Validating release")
    ok, reason = _validate_release(new_path, tag_name)
    if not ok:
        shutil.rmtree(new_path, ignore_errors=True)
        _log(f"validation failed for {tag_name}: {reason}")
        return False, f"Downloaded release failed validation: {reason}"

    try:
        _create_venv_and_install(new_path)
        _check_imports(new_path)
    except subprocess.CalledProcessError as e:
        shutil.rmtree(new_path, ignore_errors=True)
        _log(f"install/import failed for {tag_name}: {e.stderr}")
        return False, f"{tag_name} failed to install or boot: {(e.stderr or '').strip()[-500:]}"

    step(3, "Applying database changes")
    try:
        _run_schema_sync(new_path)
    except subprocess.CalledProcessError as e:
        shutil.rmtree(new_path, ignore_errors=True)
        _log(f"schema sync failed for {tag_name}: {e.stderr}")
        return False, f"{tag_name}'s database changes failed to apply: {(e.stderr or '').strip()[-500:]}"

    step(4, "Verifying the new version")
    healthy, reason = _probe_health(new_path)
    if not healthy:
        shutil.rmtree(new_path, ignore_errors=True)
        _log(f"health probe failed for {tag_name}: {reason}")
        return False, f"{tag_name} failed its health check and was never switched to: {reason}"

    step(5, "Switching to the new version")
    old_release = active_release_name()
    _write_pointer(new_folder_name)
    _log(f"promoted {new_folder_name} (was {old_release})")
    _prune_old_releases(keep={new_folder_name, old_release})
    _request_restart()
    return True, f"Updated to {tag_name}. Restarting now — this page will reconnect in a few seconds."


def rollback_to_previous():
    """Manually points active_release.txt at the most recent OTHER release
    already on disk and restarts into it — independent of the
    check-for-updates flow, for when a new version turns out to have a
    problem the health check didn't catch (a business-logic bug, not a
    crash). Does NOT restore the database backup — data written under the
    newer version stays exactly where it is, in Postgres, untouched.

    Holds backup.maintenance_lock for the same reason apply_update() does
    — a restart mid-backup/restore would leave that job's subprocess
    orphaned against a process that's no longer there to record its
    result."""
    import backup as backup_mod
    if not backup_mod.maintenance_lock.acquire(blocking=False):
        return False, "A backup, restore, or update is already running — try again once it finishes."
    try:
        return _rollback_to_previous_locked()
    finally:
        backup_mod.maintenance_lock.release()


def _rollback_to_previous_locked():
    if not is_configured():
        return False, "Updates aren't set up on this install yet."
    current = active_release_name()
    candidates = [n for n in list_releases() if n != current]
    if not candidates:
        return False, "No previous release available to roll back to."
    target = candidates[0]
    _write_pointer(target)
    _log(f"manual rollback: {current} -> {target}")
    _request_restart()
    return True, f"Rolling back to {target.replace('app_', '')}. This page will reconnect in a few seconds."
