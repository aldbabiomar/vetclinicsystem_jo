"""
The four log tables that were never pruned.

audit_log, login_log, backup_log and restore_log grew forever. audit_log grows
fastest — auth.log_change() writes one row per CHANGED FIELD on every update,
plus one per create and delete — and all four sit inside every pg_dump, so they
inflate backup duration, backup size and restore time indefinitely. That
interacts with the shutdown backup that "may not finish" on a large database
(COMPARISON.md §18) and with the restore drill's runtime.

The floor on the retention window is the part worth testing hardest:
auth.login_lock_status() reads login_log to decide whether an account is
currently locked out, so a retention window shorter than its lookback would
silently disarm the lockout. That relationship is asserted here rather than
left to a comment.
"""
import uuid
from datetime import datetime, timedelta

import pytest

import auth
import logic
from conftest import needs_db

pytestmark = needs_db


@pytest.fixture
def aged_rows(db):
    """One clearly-old and one clearly-recent row in each of the four tables."""
    tag = uuid.uuid4().hex[:8].upper()
    old = (datetime.now() - timedelta(days=5000)).isoformat(timespec="seconds")
    new = datetime.now().isoformat(timespec="seconds")
    for ts in (old, new):
        db.execute("INSERT INTO audit_log (user_id,username,timestamp,action,table_name,record_id) "
                   "VALUES (?,?,?,?,?,?)", (None, f"probe{tag}", ts, "update", "owners", tag))
        db.execute("INSERT INTO login_log (user_id,username,success,timestamp,ip,user_agent) "
                   "VALUES (?,?,?,?,?,?)", (None, f"probe{tag}", 0, ts, "127.0.0.1", "test"))
        db.execute("INSERT INTO backup_log (started_at,status,filepath,error,triggered_by) "
                   "VALUES (?,?,?,?,?)", (ts, "success", f"/tmp/{tag}.dump", None, "test"))
        db.execute("INSERT INTO restore_log (started_at,finished_at,status,source_file,error,triggered_by) "
                   "VALUES (?,?,?,?,?,?)", (ts, ts, "success", f"/tmp/{tag}.dump", None, "test"))
    db.commit()
    yield {"tag": tag, "old": old, "new": new}
    for table, col in logic.RETENTION_TABLES:
        who = "username" if table in ("audit_log", "login_log") else "source_file"
        if table == "backup_log":
            db.execute(f"DELETE FROM {table} WHERE filepath LIKE ?", (f"%{tag}%",))
        elif table == "restore_log":
            db.execute(f"DELETE FROM {table} WHERE source_file LIKE ?", (f"%{tag}%",))
        else:
            db.execute(f"DELETE FROM {table} WHERE username=?", (f"probe{tag}",))
    db.commit()


def _count(db, table, col, value):
    return db.execute(f"SELECT COUNT(*) c FROM {table} WHERE {col} LIKE ?", (f"%{value}%",)).fetchone()["c"]


# ---------------------------------------------------------------------------
# GUARDS
# ---------------------------------------------------------------------------

def test_rows_older_than_the_window_are_removed(db, aged_rows):
    """GUARD. Before this, none of these four tables was ever pruned."""
    deleted = logic.prune_old_logs(db)
    assert sum(deleted.values()) >= 4, deleted
    for table, _ in logic.RETENTION_TABLES:
        assert table in deleted


def test_rows_inside_the_window_survive(db, aged_rows):
    """CONTROL. A prune that deletes everything satisfies the guard above and
    destroys the audit trail."""
    logic.prune_old_logs(db)
    tag = aged_rows["tag"]
    assert _count(db, "audit_log", "username", tag) == 1
    assert _count(db, "login_log", "username", tag) == 1
    assert _count(db, "backup_log", "filepath", tag) == 1
    assert _count(db, "restore_log", "source_file", tag) == 1


def test_the_retention_floor_cannot_disarm_the_lockout():
    """GUARD on the relationship, not on either number alone.

    auth.login_lock_status() only considers failures within
    LOCKOUT_LOOKBACK_HOURS. If retention could be set below that, a prune would
    delete the failures the lockout is counting and unlock the account. Either
    constant moving without the other is what this catches.
    """
    lookback_days = auth.LOCKOUT_LOOKBACK_HOURS / 24.0
    assert logic.LOG_RETENTION_MIN_DAYS > lookback_days * 2, (
        f"retention can be set to {logic.LOG_RETENTION_MIN_DAYS} days while the "
        f"lockout looks back {lookback_days:.1f} days — a prune would unlock "
        f"accounts that should still be locked")


def test_a_setting_below_the_floor_is_clamped(db):
    """GUARD. The Settings form clamps too, but this is the second line of
    defence at the point of deletion — same shape as backup_retention, where a
    stored 0 would have deleted every backup."""
    previous = db.execute("SELECT value FROM settings WHERE key='log_retention_days'").fetchone()
    try:
        for bad in ("1", "0", "-5", "notanumber", ""):
            db.execute("INSERT INTO settings (key,value) VALUES ('log_retention_days',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (bad,))
            db.commit()
            recent = datetime.now() - timedelta(days=30)
            # Nothing 30 days old may be deleted at any of these settings.
            tag = uuid.uuid4().hex[:8].upper()
            db.execute("INSERT INTO audit_log (user_id,username,timestamp,action,table_name,record_id) "
                       "VALUES (?,?,?,?,?,?)",
                       (None, f"clamp{tag}", recent.isoformat(timespec="seconds"),
                        "update", "owners", tag))
            db.commit()
            logic.prune_old_logs(db)
            survived = db.execute("SELECT COUNT(*) c FROM audit_log WHERE username=?",
                                  (f"clamp{tag}",)).fetchone()["c"]
            db.execute("DELETE FROM audit_log WHERE username=?", (f"clamp{tag}",))
            db.commit()
            assert survived == 1, f"retention setting {bad!r} deleted a 30-day-old row"
    finally:
        db.execute("INSERT INTO settings (key,value) VALUES ('log_retention_days',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (previous["value"] if previous else str(logic.LOG_RETENTION_DEFAULT_DAYS),))
        db.commit()


def test_the_settings_form_rejects_a_window_below_the_floor(client):
    """GUARD at the route: the clamp in prune_old_logs() is defence in depth,
    not the only thing standing between an admin and a 1-day window."""
    resp = client.post("/settings", data={"log_retention_days": "5"}, follow_redirects=True)
    assert b"must be between" in resp.data


def test_the_settings_form_accepts_a_sane_window(client, db):
    """CONTROL."""
    previous = db.execute("SELECT value FROM settings WHERE key='log_retention_days'").fetchone()
    try:
        resp = client.post("/settings", data={"log_retention_days": "365"}, follow_redirects=True)
        assert b"must be between" not in resp.data
        stored = db.execute("SELECT value FROM settings WHERE key='log_retention_days'").fetchone()
        assert stored["value"] == "365"
    finally:
        db.execute("INSERT INTO settings (key,value) VALUES ('log_retention_days',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (previous["value"] if previous else str(logic.LOG_RETENTION_DEFAULT_DAYS),))
        db.commit()


def test_pruning_is_idempotent(db, aged_rows):
    """CONTROL. The second run has nothing left to do and must not error or
    delete anything further."""
    first = logic.prune_old_logs(db)
    second = logic.prune_old_logs(db)
    assert sum(first.values()) >= 4
    assert sum(second.values()) == 0, second
