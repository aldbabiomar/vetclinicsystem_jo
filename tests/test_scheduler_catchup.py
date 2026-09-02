"""
The scheduler's three defences against a machine that was not available when
a job was due. All three were written after real production failures, on
consecutive nights, each found after the previous fix was declared done.

Three distinct ways for a scheduled time to pass without the job happening:

* OFF — the process is gone and comes back with no memory of what it missed.
  No grace time can help; only a catch-up at startup can.
* ASLEEP, noticed late — the process is suspended and resumes. APScheduler
  drops the missed run unless `misfire_grace_time` permits it, and the
  DEFAULT IS ONE SECOND. That is what broke the backup on 2026-08-27: the Mac
  slept 01:01 to 03:10, through the 02:00 backup and the 02:20 self-check,
  and on wake both were discarded rather than run late.
* ASLEEP, never noticed — and this is the one that survived the first fix.
  On macOS `time.monotonic()` does not advance during sleep (measured
  2026-08-28: 93.64h wall clock since boot versus 48.08h monotonic).
  APScheduler waits on a MONOTONIC timeout, so the countdown to a job 22
  hours out simply freezes. Nothing is ever "missed", so misfire grace is
  irrelevant — the job just never becomes due. Only a short interval plus a
  WALL-CLOCK due check recovers.

The tests below are why none of this is "just set a flag": a job configured
with the wrong misfire setting, or trusting a frozen timer, looks identical
to a correct one until a machine sleeps — which is exactly the condition no
test suite naturally reproduces.
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
    ids = {k.get("id") for k in recurring}
    assert ids == {"nightly_backup", "daily_self_check", "verify_restore", "tick"}, (
        f"unexpected set of recurring jobs: {ids}"
    )
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
    """Only a SUCCESSFUL backup satisfies the catch-up. A failed attempt
    earlier today means this machine still has no backup from today.

    Updated 2026-09-02: the arrangement used a failure ONE MINUTE old while
    this docstring said "an hour ago", and the two had quietly drifted apart.
    A one-minute-old failure is now throttled by BACKUP_RETRY_MIN_MINUTES --
    deliberately, since retrying every tick is what buried the last success on
    a real install (COMPARISON.md §41) -- so that arrangement was asserting
    the behaviour we just removed. The property this test actually names, that
    a failure is not mistaken for a success, is unchanged and is what it now
    checks. The throttle itself is covered by
    test_a_failed_backup_is_not_retried_on_the_very_next_tick."""
    import scheduler
    now = datetime.now()
    if now.hour < 1:
        pytest.skip("run before 01:00; today's 00:30 slot has not passed yet")
    older_than_the_bound = now - timedelta(minutes=scheduler.BACKUP_RETRY_MIN_MINUTES + 5)
    if older_than_the_bound.date() != now.date():
        pytest.skip("too early in the day for a same-day failure past the retry bound")
    _log_backup(blog, older_than_the_bound, status="failed")
    assert scheduler._backup_catchup_due(blog, 0, 30) is True


# --- ASLEEP with a FROZEN TIMER: the wall-clock tick ----------------------
# The second production failure, found after the first was declared fixed.
# On macOS time.monotonic() does not advance during sleep (measured: 93.64h
# wall vs 48.08h monotonic since boot), so APScheduler's countdown to a job
# 22 hours out simply freezes. The job is never "missed" — it never becomes
# due — so misfire_grace_time is irrelevant. Only something that compares the
# WALL CLOCK against what actually happened can recover.

def test_a_tick_job_exists_and_runs_often(monkeypatch):
    """A long interval would reintroduce the bug: whatever the interval is, it
    is also the maximum time the machine can be awake with an overdue backup
    still not taken."""
    import scheduler
    assert scheduler.TICK_MINUTES <= 15, (
        "the tick must be frequent enough that waking from sleep recovers "
        "promptly; it is the only mechanism that does not trust a timer"
    )


def test_the_tick_is_registered_with_the_scheduler(monkeypatch):
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

    ticks = [(f, k) for f, k in captured if k.get("id") == "tick"]
    assert len(ticks) == 1, f"no tick job: {[k.get('id') for _, k in captured]}"
    assert ticks[0][0] is scheduler._do_tick
    trigger = ticks[0][1]["trigger"]
    assert "interval" in repr(trigger).lower(), (
        "the tick must be an interval trigger — a cron trigger has the same "
        "frozen-countdown problem it exists to work around"
    )


def test_self_check_due_uses_the_wall_clock(blog, db):
    """The tick's decision for the self-check half."""
    import scheduler
    now = datetime.now()
    if now.hour < 1:
        pytest.skip("run before 01:00; today's 00:30 slot has not passed yet")
    db.execute("DELETE FROM self_check_log")
    db.commit()
    assert scheduler._self_check_due(db, 0, 30) is True, "never run today"

    db.execute(
        "INSERT INTO self_check_log (ran_at, status, findings) VALUES (?,?,?)",
        (now.isoformat(timespec="seconds"), "ok", "[]"),
    )
    db.commit()
    assert scheduler._self_check_due(db, 0, 30) is False, "already ran today"


def test_self_check_not_due_before_its_time(blog, db):
    """Once an install has checked at least once, a later slot that has not
    come round yet is not overdue."""
    import scheduler
    db.execute("DELETE FROM self_check_log")
    db.execute(
        "INSERT INTO self_check_log (ran_at, status, findings) VALUES (?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), "ok", "[]"),
    )
    db.commit()
    assert scheduler._self_check_due(db, 23, 59) is False


def test_an_install_that_has_never_checked_is_due_whatever_the_hour(blog, db):
    """Deliberately different from the backup catch-up, which waits for its
    scheduled time. A self-check is read-only and cheap, and it carries the
    first heartbeat: a fresh install started at 01:00 with a 23:59 slot would
    otherwise send nothing for 22 hours and look dead to the receiver, while
    showing the admin none of the problems it can already see."""
    import scheduler
    db.execute("DELETE FROM self_check_log")
    db.commit()
    assert scheduler._self_check_due(db, 23, 59) is True


def test_the_tick_takes_an_overdue_backup(blog, monkeypatch):
    """End to end: the machine woke, the backup never happened, the tick
    notices by wall clock and runs it."""
    import scheduler
    now = datetime.now()
    if now.hour < 1:
        pytest.skip("run before 01:00; today's 00:30 slot has not passed yet")
    _log_backup(blog, now - timedelta(days=2))
    monkeypatch.setattr(scheduler, "_parse_hour_minute", lambda s: (0, 30))

    ran = []
    import backup as backup_mod
    monkeypatch.setattr(backup_mod, "run_backup",
                        lambda db, **kw: ran.append(kw.get("triggered_by")) or (True, "ok"))
    monkeypatch.setattr(scheduler, "_do_self_check", lambda *a, **k: None)

    scheduler._do_tick(lambda: blog, lambda c: None)
    assert ran == ["nightly"], "the tick did not take the overdue backup"


def test_the_tick_does_nothing_when_everything_is_current(blog, monkeypatch):
    """The control. A tick that always acted would take a backup every five
    minutes forever."""
    import scheduler
    now = datetime.now()
    if now.hour < 1:
        pytest.skip("run before 01:00; today's 00:30 slot has not passed yet")
    _log_backup(blog, now - timedelta(minutes=1))
    blog.execute(
        "INSERT INTO self_check_log (ran_at, status, findings) VALUES (?,?,?)",
        (now.isoformat(timespec="seconds"), "ok", "[]"),
    )
    blog.commit()
    monkeypatch.setattr(scheduler, "_parse_hour_minute", lambda s: (0, 30))

    ran = []
    import backup as backup_mod
    monkeypatch.setattr(backup_mod, "run_backup",
                        lambda db, **kw: ran.append(1) or (True, "ok"))
    checked = []
    monkeypatch.setattr(scheduler, "_do_self_check", lambda *a, **k: checked.append(1))

    scheduler._do_tick(lambda: blog, lambda c: None)
    assert ran == [], "the tick took a redundant backup"
    assert checked == [], "the tick re-ran a self-check that had already run"


def test_the_tick_never_raises_on_a_broken_database():
    """It runs every few minutes forever. Raising would spam and could kill
    the job."""
    import scheduler

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("no database")

        def close(self):
            pass

    scheduler._do_tick(lambda: Broken(), lambda c: None)


def test_catchup_never_raises_on_a_broken_database():
    """It runs at boot. Raising here would take the app down on startup."""
    import scheduler

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("no database")

    assert scheduler._backup_catchup_due(Broken(), 0, 0) is False


# --- the wake-up race: two paths, one backup ------------------------------
# Found on a real install 2026-08-29. The machine woke, the cron trigger and
# the tick both fired in the SAME SECOND, and the log recorded two backups
# (one failed with "Another backup ... is already running") and two
# self-checks, one of which pinged twice. Both paths had read "not done yet"
# before either committed, so the due-checks alone could not prevent it.

def test_two_paths_firing_together_produce_exactly_one_backup(blog, monkeypatch):
    """The regression guard for that night.

    Runs the cron entry point and the tick entry point concurrently, which is
    what a wake-up does, and requires exactly one backup to result.
    """
    import threading
    import scheduler
    now = datetime.now()
    if now.hour < 1:
        pytest.skip("run before 01:00; today's 00:30 slot has not passed yet")
    _log_backup(blog, now - timedelta(days=2))
    monkeypatch.setattr(scheduler, "_parse_hour_minute", lambda s: (0, 30))

    started = []
    import backup as backup_mod

    def fake_backup(db, **kw):
        started.append(kw.get("triggered_by"))
        # Long enough that a genuinely concurrent caller would overlap.
        import time as _t
        _t.sleep(0.25)
        _log_backup(db, datetime.now())
        return True, "ok"

    monkeypatch.setattr(backup_mod, "run_backup", fake_backup)

    threads = [threading.Thread(target=scheduler._run_backup_if_due,
                                args=(lambda: blog, lambda c: None))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(started) == 1, (
        f"{len(started)} backups started concurrently — the second should have "
        "seen the first's committed row and done nothing. A duplicate leaves a "
        "'failed: another backup is already running' row in the history, and "
        "backup_alert_message() reads the NEWEST row, so it can announce that "
        "the last backup failed in the same second one succeeded."
    )


def test_two_paths_firing_together_produce_exactly_one_self_check(blog, db, monkeypatch):
    """Same race, the self-check half — a duplicate here pings twice."""
    import threading
    import scheduler
    now = datetime.now()
    if now.hour < 1:
        pytest.skip("run before 01:00; today's 00:30 slot has not passed yet")
    db.execute("DELETE FROM self_check_log")
    db.commit()
    monkeypatch.setattr(scheduler, "_parse_hour_minute", lambda s: (0, 30))
    monkeypatch.setattr(scheduler, "_self_check_time", lambda h, m: (0, 30))

    ran = []

    def fake_self_check(get_db, close_db, send_heartbeat=True):
        ran.append(1)
        import time as _t
        _t.sleep(0.25)
        db.execute(
            "INSERT INTO self_check_log (ran_at, status, findings) VALUES (?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), "ok", "[]"),
        )
        db.commit()

    monkeypatch.setattr(scheduler, "_do_self_check", fake_self_check)

    threads = [threading.Thread(target=scheduler._run_self_check_if_due,
                                args=(lambda: db, lambda c: None))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ran) == 1, (
        f"{len(ran)} self-checks ran concurrently — each sends a heartbeat, so "
        "a duplicate pings the receiver twice for one night"
    )


# ---------------------------------------------------------------------------
# A failing backup must not retry every tick forever (2026-09-02)
# ---------------------------------------------------------------------------

def test_a_failed_backup_is_not_retried_on_the_very_next_tick(blog, db):
    """_backup_catchup_due asks whether a backup SUCCEEDED today, so a broken
    destination leaves it due forever and the 5-minute tick retried ~288 times
    a day. See COMPARISON.md §41."""
    import scheduler
    db.execute("DELETE FROM backup_log")
    db.execute(
        "INSERT INTO backup_log (started_at, finished_at, status, error) VALUES (?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"),
         datetime.now().isoformat(timespec="seconds"), "failed", "folder gone"),
    )
    db.commit()

    assert scheduler._backup_catchup_due(db, 0, 1) is False, (
        "a backup that failed moments ago is due again immediately — the tick "
        "will retry it every 5 minutes for as long as the fault lasts"
    )


def test_a_failure_older_than_the_bound_is_retried(blog, db):
    """Control. The bound must delay a retry, never cancel it — otherwise a
    destination that comes back stays un-backed-up until tomorrow."""
    import scheduler
    db.execute("DELETE FROM backup_log")
    stale = (datetime.now() - timedelta(minutes=scheduler.BACKUP_RETRY_MIN_MINUTES + 5))
    db.execute(
        "INSERT INTO backup_log (started_at, finished_at, status, error) VALUES (?,?,?,?)",
        (stale.isoformat(timespec="seconds"), stale.isoformat(timespec="seconds"),
         "failed", "folder gone"),
    )
    db.commit()

    assert scheduler._backup_catchup_due(db, 0, 1) is True, (
        "a failure older than the retry bound was not retried at all"
    )


def test_the_bound_does_not_delay_the_first_attempt_of_the_day(blog, db):
    """Control. Yesterday's SUCCESS is recent in wall-clock terms on an
    early-morning schedule; it must not throttle today's first run."""
    import scheduler
    db.execute("DELETE FROM backup_log")
    yesterday = datetime.now() - timedelta(days=1)
    db.execute(
        "INSERT INTO backup_log (started_at, finished_at, status, filepath) VALUES (?,?,?,?)",
        (yesterday.isoformat(timespec="seconds"), yesterday.isoformat(timespec="seconds"),
         "success", "/tmp/x.dump"),
    )
    db.commit()

    assert scheduler._backup_catchup_due(db, 0, 1) is True, (
        "today's backup was throttled by yesterday's successful one"
    )
