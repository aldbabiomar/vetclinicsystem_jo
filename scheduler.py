"""
Starts one background job that runs the nightly backup at whatever time is
configured in Settings (default 02:00). Re-reads the configured time each
day so a change in Settings takes effect the next night without restarting
the app.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import logic

_scheduler = None


def _do_nightly_backup(get_db, close_db):
    db = get_db()
    try:
        import backup
        backup.run_backup(db)
    finally:
        close_db(db)


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
    hour, _, minute = time_str.partition(":")

    sched = BackgroundScheduler(daemon=True)
    sched.add_job(
        _do_nightly_backup,
        trigger=CronTrigger(hour=int(hour or 2), minute=int(minute or 0)),
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
    hour, _, minute = (time_str or "02:00").partition(":")
    _scheduler.reschedule_job(
        "nightly_backup",
        trigger=CronTrigger(hour=int(hour or 2), minute=int(minute or 0)),
    )
