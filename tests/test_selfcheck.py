"""
Layer 1 of operational monitoring — the local self-check.

Every check in selfcheck._CHECKS gets a test that arranges its condition and
asserts the finding appears with the right severity, PLUS a healthy-case
control asserting a good install reports status "ok" with no findings at all.

The control is not optional. Without it, a self-check that returned "fail"
unconditionally would pass every other test in this file — which is the
failure mode CLAUDE.md §7.3 exists to prevent, and which this project has
hit repeatedly.

This file matches IQ's because selfcheck.py does, and that was verified
rather than assumed (see selfcheck.py's own module docstring). Note the
contrast with test_money.py / test_money_routes.py, which assert
deliberately OPPOSITE things in the two apps — nothing in the self-check
touches money, so the reason those diverge does not apply here.
"""
import json
import os
from datetime import datetime, timedelta

import pytest

from conftest import needs_db

pytestmark = needs_db


SETTING_KEYS = (
    "backup_dir",
    "migration_failures",
    "last_verified_restore",
    "selfcheck_backup_max_age_days",
    "selfcheck_enabled",
)


def _set(db, key, value):
    if value is None:
        db.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        db.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    db.commit()


@pytest.fixture
def env(db, flask_app, tmp_path):
    """A deterministic starting point: an empty backup_log, a writable backup
    folder, and a recent passing restore verification — i.e. a healthy
    install. Every original row and setting is restored afterwards.

    backup_log is emptied rather than worked around because these checks read
    "the 3 most recent rows" and "the newest success"; a row left behind by
    another test file would make the results depend on test ordering.
    """
    saved_rows = db.execute("SELECT * FROM backup_log ORDER BY id").fetchall()
    saved_settings = {
        k: db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        for k in SETTING_KEYS
    }
    saved_settings = {k: (r["value"] if r else None) for k, r in saved_settings.items()}
    saved_checks = db.execute("SELECT * FROM self_check_log ORDER BY id").fetchall()

    db.execute("DELETE FROM backup_log")
    db.execute("DELETE FROM self_check_log")
    db.commit()

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _set(db, "backup_dir", str(backup_dir))
    _set(db, "migration_failures", None)
    _set(db, "selfcheck_backup_max_age_days", None)
    _set(db, "last_verified_restore", json.dumps({
        "at": datetime.now().isoformat(timespec="seconds"),
        "result": "pass",
        "detail": "test fixture",
    }))
    add_backup(db, "success", hours_ago=1)

    yield {"db": db, "backup_dir": backup_dir}

    db.execute("DELETE FROM backup_log")
    db.execute("DELETE FROM self_check_log")
    for row in saved_rows:
        db.execute(
            "INSERT INTO backup_log (id, started_at, finished_at, status, filepath, "
            "filesize_bytes, error, triggered_by) VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], row["started_at"], row["finished_at"], row["status"],
             row["filepath"], row["filesize_bytes"], row["error"], row["triggered_by"]),
        )
    for row in saved_checks:
        db.execute(
            "INSERT INTO self_check_log (id, ran_at, status, findings, reported_at) "
            "VALUES (?,?,?,?,?)",
            (row["id"], row["ran_at"], row["status"], row["findings"], row["reported_at"]),
        )
    for k, v in saved_settings.items():
        _set(db, k, v)
    db.commit()


def add_backup(db, status, hours_ago=1, error=None):
    started = (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO backup_log (started_at, finished_at, status, error) VALUES (?,?,?,?)",
        (started, started, status, error),
    )
    db.commit()


def codes(result):
    return {f["code"] for f in result["findings"]}


def severity_of(result, code):
    for f in result["findings"]:
        if f["code"] == code:
            return f["severity"]
    return None


# --- the control ----------------------------------------------------------

def test_healthy_install_reports_ok_with_no_findings(env):
    import selfcheck
    result = selfcheck.run_self_check(env["db"])
    assert result["status"] == "ok", (
        "a healthy install must report ok, otherwise every other test in this "
        "file would pass against a check that always fails: %r" % result["findings"]
    )
    assert result["findings"] == []


# --- one test per check ---------------------------------------------------

def test_backup_never(env):
    import selfcheck
    env["db"].execute("DELETE FROM backup_log")
    env["db"].commit()
    result = selfcheck.run_self_check(env["db"])
    assert "backup_never" in codes(result)
    assert severity_of(result, "backup_never") == "fail"
    assert result["status"] == "fail"
    # backup_stale must NOT also fire — one condition, one finding.
    assert "backup_stale" not in codes(result)


def test_backup_stale(env):
    import selfcheck
    env["db"].execute("DELETE FROM backup_log")
    env["db"].commit()
    add_backup(env["db"], "success", hours_ago=24 * 6)
    result = selfcheck.run_self_check(env["db"])
    assert "backup_stale" in codes(result)
    assert severity_of(result, "backup_stale") == "fail"


def test_backup_stale_respects_the_configured_threshold(env):
    import selfcheck
    env["db"].execute("DELETE FROM backup_log")
    env["db"].commit()
    add_backup(env["db"], "success", hours_ago=24 * 5)
    _set(env["db"], "selfcheck_backup_max_age_days", "30")
    result = selfcheck.run_self_check(env["db"])
    assert "backup_stale" not in codes(result), (
        "a 5-day-old backup must not be stale when the threshold is 30 days — "
        "if this fails the setting is being ignored"
    )


def test_backup_failing_after_three_consecutive_failures(env):
    import selfcheck
    env["db"].execute("DELETE FROM backup_log")
    env["db"].commit()
    for i in range(3):
        add_backup(env["db"], "failed", hours_ago=i + 1, error="disk on fire")
    result = selfcheck.run_self_check(env["db"])
    assert "backup_failing" in codes(result)
    assert severity_of(result, "backup_failing") == "fail"


def test_two_failures_are_not_yet_a_pattern(env):
    """The control for the test above: 'refused for the right reason' and
    'refused for any reason' are otherwise indistinguishable."""
    import selfcheck
    env["db"].execute("DELETE FROM backup_log")
    env["db"].commit()
    add_backup(env["db"], "success", hours_ago=3)
    add_backup(env["db"], "failed", hours_ago=2)
    add_backup(env["db"], "failed", hours_ago=1)
    result = selfcheck.run_self_check(env["db"])
    assert "backup_failing" not in codes(result)


def test_backup_stranded(env):
    import selfcheck
    add_backup(env["db"], "running", hours_ago=9)
    result = selfcheck.run_self_check(env["db"])
    assert "backup_stranded" in codes(result)
    assert severity_of(result, "backup_stranded") == "warn"


def test_a_recently_started_backup_is_not_stranded(env):
    import selfcheck
    add_backup(env["db"], "running", hours_ago=1)
    result = selfcheck.run_self_check(env["db"])
    assert "backup_stranded" not in codes(result)


def test_backup_dir_missing_when_unset(env):
    import selfcheck
    _set(env["db"], "backup_dir", None)
    result = selfcheck.run_self_check(env["db"])
    assert "backup_dir_missing" in codes(result)
    assert severity_of(result, "backup_dir_missing") == "fail"


def test_backup_dir_unwritable(env, tmp_path):
    import selfcheck
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)  # r-x: exists, but nothing can be written into it
    try:
        _set(env["db"], "backup_dir", str(locked))
        result = selfcheck.run_self_check(env["db"])
        assert "backup_dir_unwritable" in codes(result)
        assert severity_of(result, "backup_dir_unwritable") == "fail"
    finally:
        os.chmod(locked, 0o700)


def test_migration_failed(env):
    import selfcheck
    _set(env["db"], "migration_failures", "ALTER TABLE visits ... failed")
    result = selfcheck.run_self_check(env["db"])
    assert "migration_failed" in codes(result)
    assert severity_of(result, "migration_failed") == "fail"


def test_restore_unverified_when_never_verified(env):
    import selfcheck
    _set(env["db"], "last_verified_restore", None)
    result = selfcheck.run_self_check(env["db"])
    assert "restore_unverified" in codes(result)
    assert severity_of(result, "restore_unverified") == "warn"


def test_restore_unverified_when_stale(env):
    import selfcheck
    _set(env["db"], "last_verified_restore", json.dumps({
        "at": (datetime.now() - timedelta(days=90)).isoformat(timespec="seconds"),
        "result": "pass",
    }))
    result = selfcheck.run_self_check(env["db"])
    assert "restore_unverified" in codes(result)


def test_update_rolled_back_reads_the_updater_log(env, tmp_path, monkeypatch):
    import selfcheck
    import updater
    data_dir = tmp_path / "upd"
    (data_dir / "logs").mkdir(parents=True)
    monkeypatch.setattr(updater, "DATA_DIR", str(data_dir), raising=False)
    log = data_dir / "logs" / "updates.log"

    # No log yet: nothing has been updated, so there is nothing to report.
    assert "update_rolled_back" not in codes(selfcheck.run_self_check(env["db"]))

    # A normal, successful update is not a finding — the control, without
    # which "flags a rollback" and "flags any log line" look the same.
    log.write_text("2026-08-26 02:00:00  promoted app_v1.10.9 (was app_v1.10.8)\n")
    assert "update_rolled_back" not in codes(selfcheck.run_self_check(env["db"]))

    log.write_text(
        "2026-08-26 02:00:00  promoted app_v1.10.9 (was app_v1.10.8)\n"
        "2026-08-26 02:05:00  manual rollback: app_v1.10.9 -> app_v1.10.8\n"
    )
    result = selfcheck.run_self_check(env["db"])
    assert "update_rolled_back" in codes(result)
    assert severity_of(result, "update_rolled_back") == "warn"


def test_update_check_is_silent_when_updates_are_not_configured(env, monkeypatch):
    """Deliberate: an install that does not use the versioned-release layout
    has no update log, permanently and by design. Warning about that daily
    would be the cry-wolf noise the plan's §6.0 warns against."""
    import selfcheck
    import updater
    monkeypatch.setattr(updater, "DATA_DIR", None, raising=False)
    assert "update_rolled_back" not in codes(selfcheck.run_self_check(env["db"]))


def test_db_unreachable_is_reported_and_does_not_raise(env):
    import selfcheck

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("connection closed")

    result = selfcheck.run_self_check(Broken())
    assert result["status"] == "fail"
    assert "db_unreachable" in codes(result)


def test_run_self_check_never_raises_on_a_hostile_database(env):
    import selfcheck

    class Weird:
        def execute(self, *a, **k):
            return self

        def fetchone(self):
            return None

        def fetchall(self):
            raise RuntimeError("nope")

    result = selfcheck.run_self_check(Weird())
    assert result["status"] in ("ok", "warn", "fail")


# --- storage and escalation ----------------------------------------------

def test_record_writes_and_prunes(env):
    import selfcheck
    db = env["db"]
    result = selfcheck.run_self_check(db)
    assert selfcheck.record(db, result) is True
    row = selfcheck.latest(db)
    assert row is not None
    assert row["status"] == result["status"]
    assert json.loads(row["findings"]) == result["findings"]
    assert row["reported_at"] is None, "reported_at is Layer 2's to set, not Layer 1's"


def _stamp(days_ago, minute=0):
    """A timestamp anchored to a calendar DAY, at a fixed hour.

    Deliberately not `now - timedelta(hours=N)`: rows written that way land on
    different calendar days depending on what time of day the suite runs, so a
    test asserting "these are all the same day" passes in the afternoon and
    fails just after midnight. This is date arithmetic, so it is stable.
    """
    day = (datetime.now() - timedelta(days=days_ago)).date()
    return datetime.combine(day, datetime.min.time()).replace(
        hour=3, minute=minute).isoformat(timespec="seconds")


def _record(db, days_ago, status, minute=0):
    db.execute(
        "INSERT INTO self_check_log (ran_at, status, findings) VALUES (?,?,?)",
        (_stamp(days_ago, minute), status, "[]"),
    )
    db.commit()


def test_record_prunes_to_the_retention_limit(env, monkeypatch):
    """A retention bug is not hypothetical in this codebase: backup_retention
    of 0 meant files[0:], i.e. delete every backup (shipped, IQ v1.10.7). This
    table grows one row per run forever without the prune."""
    import selfcheck
    db = env["db"]
    db.execute("DELETE FROM self_check_log")
    db.commit()
    monkeypatch.setattr(selfcheck, "LOG_RETENTION_ROWS", 5)

    for i in range(12):
        selfcheck.record(db, {
            "ran_at": _stamp(0, minute=i),
            "status": "ok",
            "findings": [{"code": "marker", "severity": "warn", "message": str(i)}],
        })

    count = db.execute("SELECT COUNT(*) c FROM self_check_log").fetchone()["c"]
    assert count == 5, f"expected the log pruned to 5 rows, found {count}"

    # And it must keep the NEWEST, not just any five.
    newest = selfcheck.latest(db)
    assert json.loads(newest["findings"])[0]["message"] == "11"


def test_modal_escalates_only_on_the_third_consecutive_failing_day(env):
    import selfcheck
    db = env["db"]
    db.execute("DELETE FROM self_check_log")
    db.commit()

    _record(db, 2, "fail")
    assert selfcheck.consecutive_fail_days(db) == 1

    _record(db, 1, "fail")
    assert selfcheck.consecutive_fail_days(db) == 2, (
        "two failing days must not reach the modal threshold"
    )

    _record(db, 0, "fail")
    assert selfcheck.consecutive_fail_days(db) == 3


def test_several_failures_in_one_day_count_as_one_day(env):
    """A machine restarted six times in a morning must not escalate to a
    modal by lunchtime — the escalation is in days, not runs."""
    import selfcheck
    db = env["db"]
    db.execute("DELETE FROM self_check_log")
    db.commit()
    for minute in (5, 10, 15, 20, 25, 30):
        _record(db, 0, "fail", minute=minute)
    assert selfcheck.consecutive_fail_days(db) == 1


def test_a_passing_day_breaks_the_streak(env):
    import selfcheck
    db = env["db"]
    db.execute("DELETE FROM self_check_log")
    db.commit()
    _record(db, 2, "fail")
    _record(db, 1, "ok")
    _record(db, 0, "fail")
    assert selfcheck.consecutive_fail_days(db) == 1


def test_the_streak_is_broken_by_the_latest_result_of_that_day(env):
    """Two runs on the same day, the later one passing: that day passed."""
    import selfcheck
    db = env["db"]
    db.execute("DELETE FROM self_check_log")
    db.commit()
    _record(db, 1, "fail", minute=5)
    _record(db, 0, "fail", minute=5)
    _record(db, 0, "ok", minute=30)
    assert selfcheck.consecutive_fail_days(db) == 0


# --- a destination that went away must not be papered over ----------------
# Found 2026-08-30 while setting up soak Test C: renaming the backup folder
# away produced status "ok". os.makedirs recreated it one minute later and the
# write probe then passed. The README recommends a synced folder (Drive /
# OneDrive) as the off-site strategy, so the realistic case is that folder
# unlinking -- after which backups keep "succeeding" into a fabricated local
# directory while the off-site copy silently stops.

def test_a_folder_that_held_backups_is_not_silently_recreated(env, tmp_path):
    import selfcheck
    db = env["db"]
    gone = tmp_path / "was_on_a_synced_drive"
    gone.mkdir()
    _set(db, "backup_dir", str(gone))
    # a backup was written there, so this folder is an established destination
    db.execute(
        "INSERT INTO backup_log (started_at, finished_at, status, filepath) VALUES (?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"),
         datetime.now().isoformat(timespec="seconds"), "success",
         str(gone / "vetclinicsystem_backup.dump")),
    )
    db.commit()
    gone.rmdir()

    result = selfcheck.run_self_check(db)
    assert "backup_dir_missing" in codes(result), (
        "a folder that held backups vanished and was not reported"
    )
    assert not gone.exists(), (
        "the check RECREATED the destination -- backups would keep succeeding "
        "into a fabricated local folder while the real one stayed gone"
    )


def test_a_brand_new_folder_is_still_created(env, tmp_path):
    """The control. Creating the folder on a first run is the helpful
    behaviour and must survive the fix above."""
    import selfcheck
    db = env["db"]
    fresh = tmp_path / "not_made_yet"
    _set(db, "backup_dir", str(fresh))
    db.execute("DELETE FROM backup_log")
    db.commit()

    selfcheck.run_self_check(db)
    assert fresh.is_dir(), "a first-run folder should still be created"


def test_the_newest_backup_file_must_still_exist(env, tmp_path):
    """Everything else trusts backup_log, which is in the database -- so every
    .dump could be deleted and this feature would report ok until the monthly
    verification noticed."""
    import selfcheck
    db = env["db"]
    db.execute(
        "INSERT INTO backup_log (started_at, finished_at, status, filepath) VALUES (?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"),
         datetime.now().isoformat(timespec="seconds"), "success",
         str(tmp_path / "deleted_by_someone.dump")),
    )
    db.commit()
    result = selfcheck.run_self_check(db)
    assert "backup_file_missing" in codes(result)
    assert severity_of(result, "backup_file_missing") == "fail"


def test_a_backup_file_that_is_there_is_not_reported(env, tmp_path):
    """The control -- otherwise 'file missing' and 'always fires' look the same."""
    import selfcheck
    db = env["db"]
    real = tmp_path / "really_there.dump"
    real.write_text("x")
    db.execute(
        "INSERT INTO backup_log (started_at, finished_at, status, filepath) VALUES (?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"),
         datetime.now().isoformat(timespec="seconds"), "success", str(real)),
    )
    db.commit()
    result = selfcheck.run_self_check(db)
    assert "backup_file_missing" not in codes(result)


# ---------------------------------------------------------------------------
# The health banner and the older backup alert must not say the same thing
# twice (2026-08-31)
# ---------------------------------------------------------------------------
#
# logic.backup_alert_message() predates Layer 1 and reports the same four
# situations the backup_* findings do. On a real failing install both fired at
# once, and because toast.js converts a .flash into a toast, the admin was
# told the same thing twice in two different shapes on one screen.
#
# The suppression is deliberately narrow, so two controls below assert the
# older alert still appears when the banner is NOT covering backups. Without
# them, "suppressed correctly" and "deleted entirely" look identical -- and
# deleting it entirely would mean switching the self-check off silently
# removes every backup warning in the app.

# Wording unique to backup_alert_message(); nothing in selfcheck.py produces
# it, so finding it in the HTML identifies that specific alert.
OLD_ALERT_TEXT = "The last database backup failed"


def _record_check(db, *codes, status="fail"):
    db.execute(
        "INSERT INTO self_check_log (ran_at, status, findings) VALUES (?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), status,
         json.dumps([{"code": c, "severity": "fail",
                      "message": f"finding {c} needs attention"} for c in codes])),
    )
    db.commit()


def test_the_older_backup_alert_is_suppressed_when_the_banner_covers_it(env, db, client):
    db_ = env["db"]
    add_backup(db_, "failed", hours_ago=1, error="the backup folder is gone")
    _record_check(db_, "backup_failing")

    html = client.get("/").get_data(as_text=True)

    assert "finding backup_failing needs attention" in html, (
        "the health banner is not on the page at all — this test would pass "
        "for the wrong reason"
    )
    assert OLD_ALERT_TEXT not in html, (
        "the pre-Layer-1 backup alert is still rendered alongside the health "
        "banner, so the admin is told the same thing twice"
    )


def test_the_older_backup_alert_survives_a_banner_about_something_else(env, db, client):
    """Control. A banner about a non-backup problem must not silence it."""
    db_ = env["db"]
    add_backup(db_, "failed", hours_ago=1, error="the backup folder is gone")
    _record_check(db_, "disk_low")

    html = client.get("/").get_data(as_text=True)

    assert "finding disk_low needs attention" in html, "the banner should be showing"
    assert OLD_ALERT_TEXT in html, (
        "the backup alert was suppressed by a banner that says nothing about "
        "backups — the suppression is too broad"
    )


def test_the_older_backup_alert_survives_the_self_check_being_switched_off(env, db, client):
    """Control. Turning the self-check off must not remove backup warnings."""
    db_ = env["db"]
    add_backup(db_, "failed", hours_ago=1, error="the backup folder is gone")
    _record_check(db_, "backup_failing")
    _set(db_, "selfcheck_enabled", "0")

    html = client.get("/").get_data(as_text=True)

    assert "finding backup_failing needs attention" not in html, (
        "the banner rendered despite selfcheck_enabled=0"
    )
    assert OLD_ALERT_TEXT in html, (
        "with the self-check switched off, this alert is the only backup "
        "warning the app has left — it must still appear"
    )
