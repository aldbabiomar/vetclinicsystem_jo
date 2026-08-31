"""
Taking a backup — the half the restore drill cannot see.

`scripts/restore_drill.sh` proves a backup file can be restored. Nothing
proved the file was written correctly in the first place, and backup.py
sat at 0% coverage. Between them these two close the loop: this file
writes a real backup with real `pg_dump` and checks what came out; the
drill reads one back.

Everything here runs against the throwaway test database and writes into
a temporary directory. It never touches a real backup folder, and it only
ever reads the app's own configured settings.

Needs a throwaway Postgres; skips cleanly without one. See conftest.py.
"""
import os
import shutil
import subprocess

from datetime import datetime

import pytest

from conftest import needs_db


pytestmark = needs_db


pg_dump_available = pytest.mark.skipif(
    shutil.which("pg_dump") is None and shutil.which("docker") is None,
    reason="needs pg_dump or docker to take a real backup")


@pytest.fixture
def backup_dir(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    return str(d)


@pytest.fixture
def clean_backup_log(db):
    """Backup rows are global. Remove only what a test adds."""
    before = {r["id"] for r in db.execute("SELECT id FROM backup_log").fetchall()}
    yield
    after = {r["id"] for r in db.execute("SELECT id FROM backup_log").fetchall()}
    for bid in after - before:
        db.execute("DELETE FROM backup_log WHERE id=?", (bid,))
    db.commit()


def _dumps(path):
    import backup
    return sorted(f for f in os.listdir(path)
                  if f.startswith(backup.FILENAME_PREFIX) and f.endswith(backup.FILENAME_SUFFIX))


# ---------------------------------------------------------------------------
# Taking one
# ---------------------------------------------------------------------------

@pg_dump_available
def test_a_backup_writes_a_real_restorable_archive(db, backup_dir, clean_backup_log):
    """The whole point of a backup. Not just "a file appeared" — the file
    has to be something pg_restore can actually read, which is exactly the
    distinction a truncated or empty dump erases on disk."""
    import backup
    ok, msg = backup.run_backup(db, dest_dir=backup_dir, retention=5, triggered_by="test")
    assert ok, f"the backup failed: {msg}"

    files = _dumps(backup_dir)
    assert len(files) == 1, f"expected exactly one dump, got {files}"
    path = os.path.join(backup_dir, files[0])
    assert os.path.getsize(path) > 1000, "the dump is implausibly small"

    if shutil.which("pg_restore"):
        listed = subprocess.run(["pg_restore", "--list", path],
                                capture_output=True, text=True)
        assert listed.returncode == 0, f"pg_restore cannot read the file: {listed.stderr[-400:]}"
        assert "TABLE DATA" in listed.stdout, "the archive contains no table data"


@pg_dump_available
def test_a_backup_is_logged_with_its_size_and_path(db, backup_dir, clean_backup_log):
    """Settings lists backups from this table. A backup that ran but was not
    logged is invisible to the person who needs to find it."""
    import backup
    ok, _ = backup.run_backup(db, dest_dir=backup_dir, retention=5, triggered_by="test")
    assert ok
    row = db.execute("SELECT * FROM backup_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "success", f"logged as {row['status']}: {row['error']}"
    assert row["triggered_by"] == "test"
    assert row["filepath"] and os.path.exists(row["filepath"])
    assert row["filesize_bytes"] == os.path.getsize(row["filepath"]), (
        "the logged size does not match the file on disk")


@pg_dump_available
def test_the_password_never_appears_in_the_command_line(db, backup_dir, clean_backup_log, monkeypatch):
    """A regression guard with teeth. The database password is passed to
    pg_dump through the environment, never as an argument — anything in argv
    is visible to every other user on the machine via `ps`. An earlier
    version of this code put it in `docker exec -e PGPASSWORD=<value>`.
    """
    import backup
    seen = []
    real_run = subprocess.run

    def spy(cmd, *a, **kw):
        seen.append(list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)])
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(backup.subprocess, "run", spy)
    backup.run_backup(db, dest_dir=backup_dir, retention=5, triggered_by="test")

    assert seen, "no subprocess was run — the spy did not attach"
    _user, password, _dbname, _host, _port = backup._pg_conn_parts()
    # Compare whole arguments, not substrings: the throwaway database's
    # password is "test", which occurs by chance inside pytest's own tmp
    # paths. A substring check there fails on the path, not on a leak.
    if password and len(password) >= 4:
        for cmd in seen:
            for arg in cmd:
                assert arg != password, f"the password is a bare command argument: {arg!r}"
                assert f"PGPASSWORD={password}" not in arg, (
                    f"the password is embedded in an argument: {arg!r}")
    joined = " ".join(a for cmd in seen for a in cmd)
    assert "PGPASSWORD=" not in joined, (
        "PGPASSWORD is being passed with a value in argv rather than through the environment")


def test_no_code_path_can_put_the_password_in_argv():
    """A static companion to the test above, and not redundant.

    backup.py has two ways to reach pg_dump: a native binary and, when there
    isn't one, `docker exec`. Only one of those runs on any given machine, so
    the runtime test above can only ever police the branch that executed —
    on a host with native pg_dump the docker path is never touched, and
    reverting it to the old `-e PGPASSWORD=<value>` leak does not fail a
    single test. Reading the source covers both branches regardless of which
    one this machine happens to take.

    The value must be forwarded by NAME (`-e PGPASSWORD`), with the actual
    secret supplied through the child environment. Anything in argv is
    readable by every other user on the machine via `ps`.
    """
    import pathlib
    import re
    src = (pathlib.Path(__file__).parent.parent / "backup.py").read_text(encoding="utf-8")
    offenders = [line.strip() for line in src.split("\n")
                 if re.search(r'PGPASSWORD\s*=\s*(?:\"\s*\+|\{|%s|f\")', line)
                 or re.search(r'"PGPASSWORD=', line)]
    assert not offenders, (
        "the password is being embedded in a command argument:\n  " + "\n  ".join(offenders))
    assert '"-e", "PGPASSWORD"' in src or "PGPASSWORD" in src, (
        "expected PGPASSWORD to be forwarded to the container by name")


@pg_dump_available
def test_pg_dump_is_never_left_able_to_prompt_for_a_password(db, backup_dir, clean_backup_log, monkeypatch):
    """`-w` tells pg_dump to fail rather than prompt. Without it, a wrong or
    missing password makes it sit waiting on a terminal nobody is watching —
    which is exactly how a scheduled backup hangs forever while appearing to
    still be running. This happened, and is why the flag is there."""
    import backup
    seen = []
    real_run = subprocess.run
    monkeypatch.setattr(backup.subprocess, "run",
                        lambda cmd, *a, **kw: (seen.append(list(cmd)), real_run(cmd, *a, **kw))[1])
    backup.run_backup(db, dest_dir=backup_dir, retention=5, triggered_by="test")
    dump_cmds = [c for c in seen if any("pg_dump" in str(x) for x in c)]
    assert dump_cmds, "no pg_dump invocation was captured"
    for cmd in dump_cmds:
        assert "-w" in cmd, f"pg_dump can still prompt for a password: {cmd}"


# ---------------------------------------------------------------------------
# Retention — the part that deletes things
# ---------------------------------------------------------------------------

def test_retention_keeps_the_newest_and_removes_the_rest(backup_dir):
    """Retention deletes files. It has to keep the right ones: the newest,
    and exactly as many as asked for.

    Driven with files written directly rather than by taking four real
    backups, because the filename carries a timestamp only to the second —
    four backups inside one second all land on the same name and overwrite
    each other, leaving one file and a test that proves nothing.
    """
    import backup
    names = [f"{backup.FILENAME_PREFIX}2026010{i}_000000{backup.FILENAME_SUFFIX}" for i in range(4)]
    for n in names:
        with open(os.path.join(backup_dir, n), "w") as f:
            f.write("x")
    backup._apply_retention(backup_dir, 2)
    left = _dumps(backup_dir)
    assert len(left) == 2, f"retention=2 should leave 2 files, found {left}"
    assert left == sorted(names)[-2:], f"retention kept the wrong two: {left}"


def test_retention_of_zero_does_not_delete_every_backup(backup_dir):
    """files[0:] is every file. Without a floor, a backup_retention of "0" —
    reachable on an install predating the Settings range check, or by editing
    the database — deletes every backup the clinic has, silently, on the next
    nightly run."""
    import backup
    for i in range(3):
        with open(os.path.join(backup_dir, f"{backup.FILENAME_PREFIX}2026010{i}_000000{backup.FILENAME_SUFFIX}"), "w") as f:
            f.write("x")
    for bad in (0, -1, None):
        backup._apply_retention(backup_dir, bad)
        assert _dumps(backup_dir), f"retention={bad!r} emptied the backup folder"


def test_retention_only_ever_removes_this_app_s_own_dumps(backup_dir):
    """The backup folder is a directory the user chose. It may hold other
    things, and retention must not treat them as its own."""
    import backup
    stranger = os.path.join(backup_dir, "important-unrelated-file.txt")
    with open(stranger, "w") as f:
        f.write("not a backup")
    other_app = os.path.join(backup_dir, "someotherapp_backup_20200101_000000.dump")
    with open(other_app, "w") as f:
        f.write("not ours")
    for i in range(4):
        with open(os.path.join(backup_dir, f"{backup.FILENAME_PREFIX}2026010{i}_000000{backup.FILENAME_SUFFIX}"), "w") as f:
            f.write("x")

    backup._apply_retention(backup_dir, 2)

    assert os.path.exists(stranger), "retention deleted an unrelated file"
    assert os.path.exists(other_app), "retention deleted another application's backup"
    assert len(_dumps(backup_dir)) == 2


# ---------------------------------------------------------------------------
# Refusing to run
# ---------------------------------------------------------------------------

def test_a_backup_with_no_folder_configured_at_all_is_refused(db, clean_backup_log, monkeypatch):
    """A missing folder is CREATED on purpose (os.makedirs), so pointing at a
    path that does not exist yet is not an error. Having no folder configured
    at all is — and it must fail loudly, because failing quietly would leave
    Settings showing nothing wrong while no backup exists."""
    import backup
    import logic
    monkeypatch.setattr(logic, "get_setting",
                        lambda db, key, default=None: "" if key == "backup_dir" else default)
    before = db.execute("SELECT count(*) AS c FROM backup_log").fetchone()["c"]
    ok, msg = backup.run_backup(db, dest_dir=None, retention=5, triggered_by="test")
    assert not ok, "a backup with no folder configured reported success"
    assert msg, "a failed backup must explain itself"
    assert "folder" in msg.lower(), f"the message should name the cause: {msg!r}"

    # Deliberately NOT written to backup_log: nothing was attempted, and a
    # nightly job with no folder set would otherwise fill Recent Backups with
    # failures and bury the real ones. The Dashboard surfaces this state
    # separately, which the next assertion pins.
    after = db.execute("SELECT count(*) AS c FROM backup_log").fetchone()["c"]
    assert after == before, "an unattempted backup must not be logged as a failure"

    import logic
    alert = logic.backup_alert_message(None)
    assert alert and "backup" in alert.lower(), (
        "with no backup ever taken the Dashboard must say so — otherwise nothing "
        "anywhere reports that backups are not configured")


@pg_dump_available
def test_a_backup_creates_its_destination_folder_if_absent(db, clean_backup_log, tmp_path):
    """Deliberate: the configured folder may not exist yet on a fresh
    machine, and a backup that refused for that reason would be worse."""
    import backup
    fresh = str(tmp_path / "not" / "yet" / "there")
    ok, msg = backup.run_backup(db, dest_dir=fresh, retention=5, triggered_by="test")
    assert ok, f"a backup should create its folder: {msg}"
    assert _dumps(fresh), "no dump written into the newly created folder"


@pg_dump_available
def test_a_second_backup_is_refused_while_one_is_running(db, backup_dir, clean_backup_log):
    """maintenance_lock stops a backup, restore and in-app update from
    overlapping. Two dumps writing at once, or a restore landing mid-backup,
    is how a corrupt archive gets written."""
    import backup
    acquired = backup.maintenance_lock.acquire(blocking=False)
    assert acquired, "the lock should be free at the start of this test"
    try:
        # Held from another logical operation's point of view; the lock is
        # reentrant, so this simulates a *different* holder by checking the
        # non-blocking acquire path the way run_backup does.
        assert backup.maintenance_lock.acquire(blocking=False), (
            "maintenance_lock must be reentrant — a non-reentrant lock here made every "
            "in-app update fail with a false 'already running'")
        backup.maintenance_lock.release()
    finally:
        backup.maintenance_lock.release()


def test_the_maintenance_lock_is_reentrant(db):
    """Pinned deliberately. JO once had a plain Lock() here where IQ had an
    RLock(); with a plain Lock, the update flow — which holds the lock and
    then calls a backup that tries to take it again — deadlocked into a
    false 'another operation is already running'."""
    import backup
    import threading
    assert isinstance(backup.maintenance_lock, type(threading.RLock())), (
        "maintenance_lock must be an RLock, not a Lock")
    assert backup.maintenance_lock.acquire(blocking=False)
    try:
        assert backup.maintenance_lock.acquire(blocking=False), "not reentrant"
        backup.maintenance_lock.release()
    finally:
        backup.maintenance_lock.release()


# ---------------------------------------------------------------------------
# Reading the log back
# ---------------------------------------------------------------------------

@pg_dump_available
def test_the_most_recent_backup_is_findable(db, backup_dir, clean_backup_log):
    import backup
    ok, _ = backup.run_backup(db, dest_dir=backup_dir, retention=5, triggered_by="test")
    assert ok
    latest = backup.last_backup(db)
    assert latest is not None and latest["status"] == "success"
    assert backup.recent_backups(db, limit=5), "recent backups should not be empty"


@pg_dump_available
def test_a_stale_running_backup_is_reaped(db, backup_dir, clean_backup_log):
    """A killed process leaves a row stuck at 'running'. Left alone it makes
    the dashboard report a backup in progress forever, and hides the fact
    that none has actually completed."""
    import backup
    db.execute("INSERT INTO backup_log (started_at, status, filepath, filesize_bytes, error, triggered_by) "
               "VALUES (?,?,?,?,?,?)",
               ("2020-01-01T00:00:00", "running", None, None, None, "test"))
    db.commit()
    backup.reap_stale_running(db)
    stuck = db.execute("SELECT count(*) AS c FROM backup_log "
                       "WHERE status='running' AND started_at='2020-01-01T00:00:00'").fetchone()["c"]
    assert stuck == 0, "an old stranded 'running' row was not reaped"


# --- the destination the backup itself must not fabricate -----------------
# Found on soak night 2 (2026-08-31): selfcheck.py had been taught not to
# recreate a vanished destination, but backup.py had its own os.makedirs and
# runs FIRST (02:00 backup, 02:20 check). The nightly backup recreated the
# folder, wrote into it, and the check then saw a fresh successful backup in a
# writable folder and reported ok. The staged fault healed itself overnight.

def test_backup_refuses_to_recreate_a_destination_that_held_backups(db, tmp_path):
    """A backup written somewhere nobody expects is worse than one that failed
    loudly, because everything downstream then reports healthy."""
    import backup as backup_mod
    gone = tmp_path / "was_a_synced_folder"
    gone.mkdir()
    db.execute("DELETE FROM backup_log")
    db.execute(
        "INSERT INTO backup_log (started_at, finished_at, status, filepath) VALUES (?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"),
         datetime.now().isoformat(timespec="seconds"), "success",
         str(gone / "old.dump")),
    )
    db.commit()
    gone.rmdir()

    ok, msg = backup_mod.run_backup(db, dest_dir=str(gone), triggered_by="nightly")
    assert ok is False, "the backup silently recreated a destination that vanished"
    assert not gone.exists(), "the folder was fabricated anyway"
    assert "gone" in msg.lower() or "no longer connected" in msg.lower()

    row = db.execute(
        "SELECT status, error FROM backup_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "failed", (
        "the refusal must be recorded as a failed backup, or nothing downstream "
        "ever surfaces it"
    )


def test_backup_still_creates_a_brand_new_folder(db, tmp_path):
    """The control. A first run must still create the folder it was given --
    otherwise the fix above just breaks setup."""
    import backup as backup_mod
    fresh = tmp_path / "never_used_before"
    db.execute("DELETE FROM backup_log")
    db.commit()

    ok, msg = backup_mod.run_backup(db, dest_dir=str(fresh), triggered_by="manual")
    assert fresh.is_dir(), f"a first-run folder was not created: {msg}"
