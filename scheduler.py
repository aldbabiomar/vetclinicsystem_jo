"""
Starts the background jobs that run on a schedule:

* the nightly backup, at whatever time is configured in Settings
  (default 02:00);
* the daily self-check (selfcheck.py), 20 minutes after the backup, so it
  judges the backup that just ran rather than the previous night's;
* one startup catch-up shortly after boot: it takes the backup the machine
  missed while it was off, then runs the self-check — which is what catches a
  machine that has been switched off for a week, since the daily job never
  fired while it was off;
* the restore verification (selfverify.py), 45 minutes after the backup —
  the job runs daily, the actual test-restore roughly monthly;
* a TICK every few minutes that runs anything the wall clock says is overdue.

Every scheduled job re-reads the configured time each day, so a change in
Settings takes effect the next night without restarting the app.

THE THEME, learned the hard way four times now: **a scheduled time that
passes while the machine is unavailable does not happen by itself.** There are
three distinct ways for that to happen and they need three different fixes —
each of the first two was found in production after the previous one was
declared fixed:

* OFF — the process is gone and restarts with no memory of what it missed.
  No grace time can help. Hence _do_startup_catchup, and hence the
  verification being a daily due-check rather than a monthly cron.
* ASLEEP, and the scheduler notices late — APScheduler drops a run whose time
  passed by more than misfire_grace_time, default ONE SECOND.
  Hence MISFIRE_GRACE_SECONDS.
* ASLEEP, and the scheduler never notices at all — on macOS time.monotonic()
  does not advance during sleep, so a long timer's countdown simply freezes
  and the job never becomes due. Nothing was "missed", so misfire grace is
  irrelevant. Hence TICK_MINUTES.

The tick is the one that does not depend on believing any timer. If you
change anything in this module, keep that property: **decide what to run by
comparing the wall clock against what the database says already happened.**
"""
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

import logic

_scheduler = None

# How long after the backup the self-check runs, and how long after boot the
# startup self-check runs. The startup delay just keeps boot responsive —
# the check itself takes milliseconds.
SELF_CHECK_AFTER_BACKUP_MINUTES = 20
SELF_CHECK_STARTUP_DELAY_SECONDS = 30

# APScheduler discards a job whose scheduled time has passed by more than
# misfire_grace_time — which DEFAULTS TO ONE SECOND. That default silently
# broke the nightly backup on a real install (2026-08-27): the Mac slept from
# 01:01 to 03:10, the process was suspended straight through the 02:00 backup
# and the 02:20 self-check, and on wake both were dropped rather than run
# late. No backup, no ping, and nothing anywhere said so.
#
# None means "run it however late we are". A backup at 09:00 is worth
# enormously more than no backup, and coalesce=True (the default) means
# several missed runs still produce exactly one.
#
# **This only covers a SUSPENDED process, not a stopped one.** The scheduler
# is in-memory, so a machine that was switched off entirely comes back with no
# memory of what it missed — that case is what _do_startup_catchup handles.
MISFIRE_GRACE_SECONDS = None

# How often the wall-clock tick runs. This is the belt to the cron jobs'
# braces, and on macOS it is the ONLY thing that works.
#
# Measured on the dev Mac 2026-08-28: 93.64 h of wall clock since boot versus
# 48.08 h of time.monotonic() — the monotonic clock does not advance while the
# machine sleeps. APScheduler waits on an event with a MONOTONIC timeout, so a
# job scheduled 22 hours out has its countdown frozen every time the machine
# sleeps. It does not fire late; from the scheduler's point of view it is not
# due yet. misfire_grace_time cannot help, because nothing was ever missed.
#
# (Windows' monotonic is GetTickCount64, which does include suspend time, so
# the cron jobs probably do fire there. "Probably" is not good enough for a
# clinic's backups, hence this.)
#
# A SHORT interval bounds the damage: however long the machine sleeps, the
# tick fires within TICK_MINUTES of waking, and then decides what to run by
# WALL CLOCK rather than by any timer. Everything it calls is gated on a
# "has today's X actually happened?" check, so the tick and the cron jobs
# cannot double-run.
TICK_MINUTES = 5
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


def _backup_catchup_due(db, hour, minute):
    """True when today's scheduled backup should have run and did not.

    Covers the case misfire_grace_time cannot: the machine was switched OFF
    over its backup time. The scheduler holds no state across a restart, so
    nothing else would ever notice that the 02:00 run never happened.
    """
    now = datetime.now()
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled:
        return False  # today's run is still ahead of us; the cron will fire
    try:
        row = db.execute(
            "SELECT started_at FROM backup_log WHERE status='success' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return False
    if row is None:
        return True
    try:
        last = datetime.fromisoformat(str(row["started_at"]))
    except (TypeError, ValueError):
        return True
    return last < scheduled


def _self_check_due(db, hour, minute):
    """True when today's scheduled self-check should have run and did not.

    The self-check's own scheduled time is the backup time + 20 minutes; the
    caller passes that, already resolved.
    """
    now = datetime.now()
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled:
        return False
    try:
        row = db.execute(
            "SELECT ran_at FROM self_check_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return False
    if row is None:
        return True
    try:
        last = datetime.fromisoformat(str(row["ran_at"]))
    except (TypeError, ValueError):
        return True
    return last < scheduled


def _do_tick(get_db, close_db):
    """Every TICK_MINUTES: run anything today's wall clock says is overdue.

    This exists because the cron triggers cannot be trusted to fire on a
    machine that sleeps — see TICK_MINUTES above. It asks only "should this
    have happened by now, and did it?", so it is correct regardless of what
    any timer believes, and it is a no-op on a machine that never sleeps
    because the cron jobs will already have done the work.

    Swallows everything, like every other scheduled job here.
    """
    db = None
    backup_due = check_due = False
    try:
        db = get_db()
        time_str = logic.get_setting(db, "backup_time", "02:00") or "02:00"
        hour, minute = _parse_hour_minute(time_str)
        backup_due = _backup_catchup_due(db, hour, minute)
        if backup_due:
            import backup
            backup.run_backup(db, triggered_by="nightly")
        sc_hour, sc_minute = _self_check_time(hour, minute)
        check_due = _self_check_due(db, sc_hour, sc_minute)
    except Exception:
        pass
    finally:
        if db is not None:
            try:
                close_db(db)
            except Exception:
                pass

    # Each on its own connection, so one failure cannot leave the next
    # running inside an aborted transaction.
    if check_due:
        _do_self_check(get_db, close_db)
    try:
        db2 = get_db()
        try:
            import selfverify
            if selfverify.is_due(db2):
                _do_verify_restore_on(db2)
        finally:
            close_db(db2)
    except Exception:
        pass


def _do_verify_restore_on(db):
    """The verification body, given an open connection."""
    try:
        import selfverify
        if selfverify.run_if_due(db) is None:
            return
        import selfcheck
        selfcheck.record(db, selfcheck.run_self_check(db))
    except Exception:
        pass


def _do_startup_catchup(get_db, close_db):
    """Runs once, shortly after boot.

    Two jobs in one, in this order on purpose:

    1. Take the backup this machine missed while it was off, if it missed one.
    2. Run the self-check and heartbeat — AFTER the catch-up, so the verdict
       and the ping describe the state including that backup rather than
       reporting a stale-backup problem this job just fixed.

    Swallows everything, like every other scheduled job here.
    """
    db = None
    try:
        db = get_db()
        try:
            time_str = logic.get_setting(db, "backup_time", "02:00") or "02:00"
            hour, minute = _parse_hour_minute(time_str)
            if _backup_catchup_due(db, hour, minute):
                import backup
                backup.run_backup(db, triggered_by="nightly")
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
    # Separate connection, so a failed catch-up cannot leave the self-check
    # running inside an aborted transaction.
    _do_self_check(get_db, close_db)


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
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        coalesce=True,
    )
    sc_hour, sc_minute = _self_check_time(hour, minute)
    sched.add_job(
        _do_self_check,
        trigger=CronTrigger(hour=sc_hour, minute=sc_minute),
        args=[get_db, close_db],
        id="daily_self_check",
        replace_existing=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        coalesce=True,
    )
    v_hour, v_minute = _verify_time(hour, minute)
    sched.add_job(
        _do_verify_restore,
        trigger=CronTrigger(hour=v_hour, minute=v_minute),
        args=[get_db, close_db],
        id="verify_restore",
        replace_existing=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        coalesce=True,
    )
    sched.add_job(
        _do_tick,
        trigger=IntervalTrigger(minutes=TICK_MINUTES),
        args=[get_db, close_db],
        id="tick",
        replace_existing=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        coalesce=True,
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
        _do_startup_catchup,
        trigger=DateTrigger(
            run_date=datetime.now() + timedelta(seconds=SELF_CHECK_STARTUP_DELAY_SECONDS)
        ),
        args=[get_db, close_db],
        id="startup_catchup",
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
