"""
The scheduler's two defences against a machine that was not available when a
job was due. Both were written after a real install silently skipped its
nightly backup (2026-08-27).

There are two distinct ways to be unavailable, and each needs its own fix:

* ASLEEP — the process is suspended and resumes later. APScheduler drops the
  missed run unless `misfire_grace_time` permits it, and the DEFAULT IS ONE
  SECOND. That default is what silently broke the backup: the Mac slept
  01:01 to 03:10, straight through the 02:00 backup and the 02:20 self-check,
  and on wake both were discarded rather than run late.
* OFF — the process is gone and comes back with no memory of what it missed.
  No grace time can help; only a catch-up at startup can.

The tests below are why the fix is not just "set a flag": a job configured
with the wrong misfire setting looks identical to a correct one until a
machine sleeps, which is exactly the condition no test suite naturally
reproduces.
"""
from datetime import datetime, timedelta

import pytest

from conftest import needs_db

pytestmark = needs_db


# --- ASLEEP: the jobs must be allowed to run late -------------------------

def test_every_cron_job_is_allowed_to_run_late():
    """The regression guard for the real incident.

    APScheduler's default misfire_grace_time of 1 second means a job whose
    time passed while the machine slept is DISCARDED. Every recurring job here
    must opt out of that, or it silently does not happen.
    """
    import scheduler
    assert scheduler.MISFIRE_GRACE_SECONDS is None, (
        "MISFIRE_GRACE_SECONDS must be None ('run however late'). Any finite "
        "value re-introduces a window in which a sleeping machine silently "
        "skips its backup."
    )


def test_the_scheduler_actually_applies_it_to_every_recurring_job(monkeypatch):
    """Asserting the constant alone would pass even if no job used it."""
    import scheduler
    captured = []

    class FakeSched:
        def add_job(self, func, **kw):
            captured.append(kw)

        def start(self):
            pass

    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda **kw: FakeSched())

    class FakeDB:
        def execute(self, *a, **k):
            return self

        def fetchone(self):
            return {"value": "02:00"}

        def close(self):
            pass

    scheduler.start(lambda: FakeDB(), lambda c: None)
    monkeypatch.setattr(scheduler, "_scheduler", None)

    recurring = [k for k in captured if k.get("id") != "startup_catchup"]
    assert len(recurring) == 3, f"expected 3 recurring jobs, saw {[k.get('id') for k in captured]}"
    for kw in recurring:
        assert kw.get("misfire_grace_time", "MISSING") is None, (
            f"job {kw.get('id')!r} would be discarded when it runs late"
        )
        assert kw.get("coalesce") is True, (
            f"job {kw.get('id')!r} could run several times after a long sleep"
        )


def test_the_startup_job_is_the_catchup(monkeypatch):
    import scheduler
    captured = []

    class FakeSched:
        def add_job(self, func, **kw):
            captured.append((func, kw))

        def start(self):
            pass

    class FakeDB:
        def execute(self, *a, **k):
            return self

        def fetchone(self):
            return {"value": "02:00"}

        def close(self):
            pass

    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda **kw: FakeSched())
    scheduler.start(lambda: FakeDB(), lambda c: None)
    monkeypatch.setattr(scheduler, "_scheduler", None)

    startup = [(f, k) for f, k in captured if k.get("id") == "startup_catchup"]
    assert len(startup) == 1, "there must be exactly one startup job"
    assert startup[0][0] is scheduler._do_startup_catchup


# --- OFF: the startup catch-up -------------------------------------------

def _clear(db):
    db.execute("DELETE FROM backup_log")
    db.commit()


def _log_backup(db, when, status="success"):
    db.execute(
        "INSERT INTO backup_log (started_at, finished_at, status) VALUES (?,?,?)",
        (when.isoformat(timespec="seconds"), when.isoformat(timespec="seconds"), status),
    )
    db.commit()


@pytest.fixture
def blog(db):
    saved = db.execute("SELECT * FROM backup_log ORDER BY id").fetchall()
    _clear(db)
    yield db
    _clear(db)
    for r in saved:
        db.execute(
            "INSERT INTO backup_log (id, started_at, finished_at, status, filepath, "
            "filesize_bytes, error, triggered_by) VALUES (?,?,?,?,?,?,?,?)",
            (r["id"], r["started_at"], r["finished_at"], r["status"], r["filepath"],
             r["filesize_bytes"], r["error"], r["triggered_by"]),
        )
    db.commit()


def test_catchup_is_due_when_todays_backup_was_missed(blog):
    """The machine was off over 02:00 and booted afterwards."""
    import scheduler
    now = datetime.now()
    if now.hour < 1:
        pytest.skip("run before 01:00; today's 00:30 slot has not passed yet")
    _log_backup(blog, now - timedelta(days=2))
    assert scheduler._backup_catchup_due(blog, 0, 30) is True


def test_catchup_is_not_due_when_todays_backup_already_ran(blog):
    """The control. Without it, 'due when missed' and 'always due' are the
    same result, and every boot would take a redundant backup."""
    import scheduler
    now = datetime.now()
    if now.hour < 1:
        pytest.skip("run before 01:00; today's 00:30 slot has not passed yet")
    _log_backup(blog, now - timedelta(minutes=1))
    assert scheduler._backup_catchup_due(blog, 0, 30) is False


def test_catchup_is_not_due_before_todays_scheduled_time(blog):
    """Booting at 08:00 with a 23:00 backup time must not trigger a catch-up:
    tonight's run has not been missed, it simply has not happened yet."""
    import scheduler
    _log_backup(blog, datetime.now() - timedelta(days=1))
    assert scheduler._backup_catchup_due(blog, 23, 59) is False


def test_catchup_is_due_when_no_backup_has_ever_run(blog):
    import scheduler
    now = datetime.now()
    if now.hour < 1:
        pytest.skip("run before 01:00; today's 00:30 slot has not passed yet")
    assert scheduler._backup_catchup_due(blog, 0, 30) is True


def test_a_failed_backup_does_not_count_as_todays_run(blog):
    """Only a SUCCESSFUL backup satisfies the catch-up. A failed attempt an
    hour ago means this machine still has no backup from today."""
    import scheduler
    now = datetime.now()
    if now.hour < 1:
        pytest.skip("run before 01:00; today's 00:30 slot has not passed yet")
    _log_backup(blog, now - timedelta(minutes=1), status="failed")
    assert scheduler._backup_catchup_due(blog, 0, 30) is True


def test_catchup_never_raises_on_a_broken_database():
    """It runs at boot. Raising here would take the app down on startup."""
    import scheduler

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("no database")

    assert scheduler._backup_catchup_due(Broken(), 0, 0) is False
