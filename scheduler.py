"""
Starts one background job that runs the nightly backup at whatever time is
configured in Settings (default 02:00). Re-reads the configured time each
day so a change in Settings takes effect the next night without restarting
the app.
"""
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import logic

_scheduler = None


def _do_nightly_backup(get_db, close_db):
    db = get_db()
    try:
        import backup
        backup.run_backup(db, triggered_by="nightly")
    finally:
        close_db(db)


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
    sched.start()
    _scheduler = sched
    return sched


def reschedule(time_str):
    """Called after Settings saves a new backup_time so it applies immediately."""
    if _scheduler is None:
        return
    hour, minute = _parse_hour_minute(time_str)
    _scheduler.reschedule_job(
        "nightly_backup",
        trigger=CronTrigger(hour=hour, minute=minute),
    )
