"""
Starts the background jobs that run on a schedule:

* the nightly backup, at whatever time is configured in Settings
  (default 02:00);
* the daily self-check (selfcheck.py), 20 minutes after the backup, so it
  judges the backup that just ran rather than the previous night's;
* one self-check shortly after startup, which is what catches a machine
  that has been switched off for a week — the daily job alone cannot,
  because it never fired while the machine was off;
* the restore verification (selfverify.py), 45 minutes after the backup —
  the job runs daily, the actual test-restore roughly monthly.

Every scheduled job re-reads the configured time each day, so a change in
Settings takes effect the next night without restarting the app.

A theme worth carrying: a cron that fires while the machine is off does not
happen, and is not run late. Anything that must eventually happen is either
checked daily and gated on being due, or run at startup as well.
"""
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import logic

_scheduler = None

# How long after the backup the self-check runs, and how long after boot the
# startup self-check runs. The startup delay just keeps boot responsive —
# the check itself takes milliseconds.
SELF_CHECK_AFTER_BACKUP_MINUTES = 20
SELF_CHECK_STARTUP_DELAY_SECONDS = 30
# The restore verification. Later than the self-check so the two never
# contend. The JOB runs daily; the WORK inside it runs roughly monthly, gated
# by selfverify.is_due — see _do_verify_restore for why a monthly trigger was
# the wrong shape.
VERIFY_AFTER_BACKUP_MINUTES = 45


def _do_nightly_backup(get_db, close_db):
    db = get_db()
    try:
        import backup
        backup.run_backup(db, triggered_by="nightly")
    finally:
        close_db(db)


def _do_self_check(get_db, close_db, send_heartbeat=True):
    """Runs the self-check, records it, and sends the heartbeat carrying that
    fresh verdict. Deliberately swallows everything: this runs in a scheduler
    thread, where an escaping exception is logged somewhere nobody reads and
    kills the job silently — the exact failure mode this feature exists to
    prevent.

    The heartbeat is sent from HERE, in the same job, rather than on its own
    schedule, so the payload can never carry yesterday's verdict. It is a
    no-op unless an admin has set a heartbeat URL, and its failure is never
    escalated to the clinic — see heartbeat.py.
    """
    db = None
    try:
        db = get_db()
        import selfcheck
        result = selfcheck.run_self_check(db)
        selfcheck.record(db, result)
        if send_heartbeat:
            try:
                import heartbeat
                heartbeat.send_for(db, result)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        if db is not None:
            try:
                close_db(db)
            except Exception:
                pass


def _do_verify_restore(get_db, close_db):
    """Restores the newest backup into a throwaway database and checks what
    came back (selfverify.py), then re-runs the self-check so the
    restore_unverified finding clears in the same pass rather than waiting
    until tomorrow night. Swallows everything, for the same reason
    _do_self_check does.

    Runs DAILY but does the work only when due (selfverify.is_due — roughly
    monthly). A monthly CronTrigger was the obvious shape and the wrong one:
    "the 1st at 02:45" does not happen on a machine that is switched off that
    night, and it is never run late, so a clinic that powers down overnight
    would never verify a backup at all — and would then warn about it forever
    once 45 days passed. Checking daily survives any single night being
    missed, and lets a fresh install verify as soon as it has a backup instead
    of warning every day until the 1st.
    """
    db = None
    try:
        db = get_db()
        import selfverify
        if selfverify.run_if_due(db) is None:
            return  # not due; the daily self-check has already run
        import selfcheck
        selfcheck.record(db, selfcheck.run_self_check(db))
    except Exception:
        pass
    finally:
        if db is not None:
            try:
                close_db(db)
            except Exception:
                pass


def _self_check_time(hour, minute):
    """Backup time + 20 minutes, wrapping past midnight."""
    total = (hour * 60 + minute + SELF_CHECK_AFTER_BACKUP_MINUTES) % (24 * 60)
    return divmod(total, 60)


def _verify_time(hour, minute):
    """Backup time + 45 minutes — after the self-check, so the two never
    contend for the same connection."""
    total = (hour * 60 + minute + VERIFY_AFTER_BACKUP_MINUTES) % (24 * 60)
    return divmod(total, 60)


def _parse_hour_minute(time_str):
    """
    Parses 'HH:MM' into (hour, minute) ints, falling back to 02:00 for
    anything missing or malformed. Settings validates this format before
    saving, but this stays defensive: scheduler.start() runs unguarded at
    process startup (before the server starts serving), so letting a bad
    stored value raise here would previously have stopped the whole app
    from launching until someone fixed the row directly in the database.
    See ERROR_500_AUDIT.md E-01.
    """
    try:
        parsed = datetime.strptime((time_str or "02:00").strip(), "%H:%M")
    except (TypeError, ValueError):
        parsed = datetime.strptime("02:00", "%H:%M")
    return parsed.hour, parsed.minute


def start(get_db, close_db):
    """
    get_db()/close_db(conn) let this module open its own short-lived
    connection for the nightly job, independent of Flask's per-request g.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    db = get_db()
    time_str = logic.get_setting(db, "backup_time", "02:00") or "02:00"
    close_db(db)
    hour, minute = _parse_hour_minute(time_str)

    sched = BackgroundScheduler(daemon=True)
    sched.add_job(
        _do_nightly_backup,
        trigger=CronTrigger(hour=hour, minute=minute),
        args=[get_db, close_db],
        id="nightly_backup",
        replace_existing=True,
    )
    sc_hour, sc_minute = _self_check_time(hour, minute)
    sched.add_job(
        _do_self_check,
        trigger=CronTrigger(hour=sc_hour, minute=sc_minute),
        args=[get_db, close_db],
        id="daily_self_check",
        replace_existing=True,
    )
    v_hour, v_minute = _verify_time(hour, minute)
    sched.add_job(
        _do_verify_restore,
        trigger=CronTrigger(hour=v_hour, minute=v_minute),
        args=[get_db, close_db],
        id="verify_restore",
        replace_existing=True,
    )
    # The startup run. A machine that was off for a week never fired the
    # daily job at all, so without this the first news of a week-old backup
    # would wait for the next scheduled run.
    #
    # It heartbeats too, and that matters more than it looks: a clinic that
    # powers its machine on at 8am and off at 6pm is asleep when the 02:20 job
    # is due, so the startup ping is the ONLY ping it ever sends. Without it
    # every such clinic would look permanently dead to the receiver.
    sched.add_job(
        _do_self_check,
        trigger=DateTrigger(
            run_date=datetime.now() + timedelta(seconds=SELF_CHECK_STARTUP_DELAY_SECONDS)
        ),
        args=[get_db, close_db],
        id="startup_self_check",
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    return sched


def reschedule(time_str):
    """Called after Settings saves a new backup_time so it applies immediately.

    Moves BOTH scheduled jobs — the self-check is defined relative to the
    backup time, so moving only the backup would leave it judging a backup
    that has not run yet.
    """
    if _scheduler is None:
        return
    hour, minute = _parse_hour_minute(time_str)
    _scheduler.reschedule_job(
        "nightly_backup",
        trigger=CronTrigger(hour=hour, minute=minute),
    )
    sc_hour, sc_minute = _self_check_time(hour, minute)
    _scheduler.reschedule_job(
        "daily_self_check",
        trigger=CronTrigger(hour=sc_hour, minute=sc_minute),
    )
    v_hour, v_minute = _verify_time(hour, minute)
    _scheduler.reschedule_job(
        "verify_restore",
        trigger=CronTrigger(hour=v_hour, minute=v_minute),
    )
